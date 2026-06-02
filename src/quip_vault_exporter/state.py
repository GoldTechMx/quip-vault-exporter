"""SQLite-backed export state for incremental runs, resume, and the audit manifest.

Skip key is `updated_usec` (NOT a vague timestamp). `checksum` (sha256 of raw JSON) is for
`verify`/integrity only, never for skip decisions. Change signals that DON'T bump the thread
timestamp (new comments, asset-set changes, folder moves, title changes) are tracked in
separate columns.

State transitions are explicit for crash safety:
  pending -> in_progress -> complete   (or -> failed)
On resume, any row left in `in_progress` is treated as incomplete and redone.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .utils import ensure_dir

STATE_FILENAME = "_quip_export_state.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id            TEXT PRIMARY KEY,
    title                TEXT,
    type                 TEXT,
    canonical_folder_id  TEXT,
    folder_path          TEXT,
    updated_usec         INTEGER,
    last_seen_updated_usec INTEGER,
    last_exported_at     TEXT,
    checksum             TEXT,
    comment_cursor_usec  INTEGER,
    comment_count        INTEGER,
    comments_truncated   INTEGER DEFAULT 0,
    asset_set_hash       TEXT,
    asset_count          INTEGER DEFAULT 0,
    pdf_status           TEXT,
    export_status        TEXT DEFAULT 'pending',
    error_count          INTEGER DEFAULT 0,
    last_error           TEXT
);

CREATE TABLE IF NOT EXISTS folders (
    folder_id   TEXT PRIMARY KEY,
    title       TEXT,
    parent_ids  TEXT,
    path        TEXT
);

-- Pass-1 link map: thread_id -> resolved title + final vault path.
CREATE TABLE IF NOT EXISTS linkmap (
    thread_id   TEXT PRIMARY KEY,
    title       TEXT,
    vault_path  TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT,
    item_title  TEXT,
    item_type   TEXT,
    operation   TEXT,
    error_type  TEXT,
    error_message TEXT,
    timestamp   TEXT,
    retry_count INTEGER
);
"""

# Per-document lifecycle:
#   pending -> downloading -> downloaded -> processing -> complete   (or failed | partial)
# DOWNLOAD (API) advances pending->downloaded; PROCESS (local) advances downloaded->complete.
VALID_STATUSES = {
    "pending",
    "downloading",
    "downloaded",
    "processing",
    "in_progress",
    "complete",
    "failed",
    "partial",
}

# Column allowlist for upsert_thread: makes SQL-identifier safety explicit rather than
# trusting every caller to pass only literal column names.
_THREAD_COLUMNS = {
    "thread_id",
    "title",
    "type",
    "canonical_folder_id",
    "folder_path",
    "updated_usec",
    "last_seen_updated_usec",
    "last_exported_at",
    "checksum",
    "comment_cursor_usec",
    "comment_count",
    "comments_truncated",
    "asset_set_hash",
    "asset_count",
    "pdf_status",
    "export_status",
    "error_count",
    "last_error",
}


class StateDB:
    def __init__(self, db_path: Path) -> None:
        ensure_dir(db_path.parent)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL is safe under WAL and avoids an fsync on every commit; the pipeline writes
        # several state rows per document, so at 12k+ docs this removes ~80k fsyncs.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def open(cls, output_dir: Path) -> StateDB:
        return cls(Path(output_dir) / STATE_FILENAME)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- threads --------------------------------------------------------------------------
    def upsert_thread(self, **fields: Any) -> None:
        if "thread_id" not in fields:
            raise ValueError("upsert_thread requires thread_id")
        unknown = set(fields) - _THREAD_COLUMNS
        if unknown:
            raise ValueError(f"upsert_thread got unknown column(s): {sorted(unknown)}")
        cols = list(fields)
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "thread_id")
        sql = (
            f"INSERT INTO threads ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(thread_id) DO UPDATE SET {updates}"
        )
        with closing(self._conn.cursor()) as cur:
            cur.execute(sql, fields)
        self._conn.commit()

    def set_status(self, thread_id: str, status: str, error: str | None = None) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        with closing(self._conn.cursor()) as cur:
            if status == "failed":
                cur.execute(
                    "UPDATE threads SET export_status=?, last_error=?, "
                    "error_count=error_count+1 WHERE thread_id=?",
                    (status, error, thread_id),
                )
            else:
                cur.execute(
                    "UPDATE threads SET export_status=? WHERE thread_id=?",
                    (status, thread_id),
                )
        self._conn.commit()

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def threads_by_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM threads WHERE export_status IN ({marks})", statuses
        ).fetchall()
        return [dict(r) for r in rows]

    def all_threads(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM threads").fetchall()]

    def all_threads_map(self) -> dict[str, dict[str, Any]]:
        """All thread rows keyed by thread_id (one scan, for cheap bulk rehydration)."""
        return {r["thread_id"]: dict(r) for r in self._conn.execute("SELECT * FROM threads")}

    # -- link map ------------------------------------------------------------------------
    def set_linkmap(self, thread_id: str, title: str, vault_path: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO linkmap (thread_id, title, vault_path) VALUES (?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET title=excluded.title, "
                "vault_path=excluded.vault_path",
                (thread_id, title, vault_path),
            )
        self._conn.commit()

    def linkmap(self) -> dict[str, dict[str, str]]:
        rows = self._conn.execute("SELECT thread_id, title, vault_path FROM linkmap").fetchall()
        return {r["thread_id"]: {"title": r["title"], "vault_path": r["vault_path"]} for r in rows}

    # -- folders -------------------------------------------------------------------------
    def upsert_folder(self, folder_id: str, title: str, parent_ids: str, path: str) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO folders (folder_id, title, parent_ids, path) VALUES (?,?,?,?) "
                "ON CONFLICT(folder_id) DO UPDATE SET title=excluded.title, "
                "parent_ids=excluded.parent_ids, path=excluded.path",
                (folder_id, title, parent_ids, path),
            )
        self._conn.commit()

    # -- errors --------------------------------------------------------------------------
    def record_error(
        self,
        *,
        item_id: str,
        item_title: str,
        item_type: str,
        operation: str,
        error_type: str,
        error_message: str,
        timestamp: str,
        retry_count: int,
    ) -> None:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO errors (item_id, item_title, item_type, operation, "
                "error_type, error_message, timestamp, retry_count) VALUES (?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    item_title,
                    item_type,
                    operation,
                    error_type,
                    error_message,
                    timestamp,
                    retry_count,
                ),
            )
        self._conn.commit()

    def all_errors(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM errors").fetchall()]
