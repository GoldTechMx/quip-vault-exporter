"""Persistent raw store for the resumable download-then-process pipeline.

The DOWNLOAD phase writes each document's raw payload here (body, comments, asset index);
the PROCESS phase reads it back to render the vault WITHOUT touching the Quip API. Living on
disk under `output_dir/_raw/` (not in RAM) means:

  * memory stays O(1) for any account size (100k+ documents);
  * a crashed/cancelled run RESUMES - already-downloaded documents are not re-fetched;
  * rendering can be re-run locally (e.g. after a fix) without re-consuming the rate-limited
    API.

Keyed by thread_id (collision-free); the human-facing folder/sub-folder tree is preserved in
the FINAL vault that the process phase writes. The payload contains workspace content but no
access token and no signed URLs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import atomic_write_bytes, atomic_write_json, ensure_dir


class RawStore:
    def __init__(self, root: Path | str) -> None:
        self.dir = ensure_dir(Path(root))

    def _p(self, thread_id: str, suffix: str) -> Path:
        return self.dir / f"{thread_id}.{suffix}"

    def path_for(self, thread_id: str, suffix: str) -> Path:
        return self._p(thread_id, suffix)

    def has(self, thread_id: str, suffix: str) -> bool:
        return self._p(thread_id, suffix).exists()

    def put_bytes(self, thread_id: str, suffix: str, data: bytes) -> None:
        atomic_write_bytes(self._p(thread_id, suffix), data)

    def get_bytes(self, thread_id: str, suffix: str) -> bytes | None:
        p = self._p(thread_id, suffix)
        if not p.exists():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def _read(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- thread body ----------------------------------------------------------------------
    def has_thread(self, thread_id: str) -> bool:
        return self._p(thread_id, "json").exists()

    def put_thread(self, thread_id: str, resp: dict[str, Any]) -> None:
        atomic_write_json(self._p(thread_id, "json"), resp)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        data = self._read(self._p(thread_id, "json"))
        return data if isinstance(data, dict) else None

    # -- comments -------------------------------------------------------------------------
    def put_comments(self, thread_id: str, rows: list[dict[str, Any]]) -> None:
        atomic_write_json(self._p(thread_id, "comments.json"), rows)

    def get_comments(self, thread_id: str) -> list[dict[str, Any]]:
        data = self._read(self._p(thread_id, "comments.json"))
        return data if isinstance(data, list) else []

    # -- asset index (blob_id -> {name, vault_path, sha256}) ------------------------------
    def put_assets_index(self, thread_id: str, index: dict[str, Any]) -> None:
        atomic_write_json(self._p(thread_id, "assets.json"), index)

    def get_assets_index(self, thread_id: str) -> dict[str, Any]:
        data = self._read(self._p(thread_id, "assets.json"))
        return data if isinstance(data, dict) else {}

    def cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)
