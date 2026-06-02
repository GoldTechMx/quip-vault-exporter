"""Folder traversal + inventory.

Handles the cases the original spec glossed over:
  * a thread can live in MULTIPLE folders or NONE -> deterministic canonical parent, all
    parents recorded; orphans routed to the `_unfiled/` folder;
  * the same thread surfaces via several roots/shared folders -> de-dup per thread_id;
  * folder graphs can contain cycles -> visited-set guard.

Produces the in-memory inventory that `linkmap` and `exporter` consume, and writes the
manifest inventory artifacts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import ThreadCache
from .config import Config
from .control import Control
from .quip_client import QuipClient
from .sanitize import sanitize_filename
from .state import StateDB
from .utils import atomic_write_json, atomic_write_text

log = logging.getLogger("quip_vault_exporter.inventory")


@dataclass
class ThreadEntry:
    thread_id: str
    title: str
    type: str
    parent_folder_ids: list[str] = field(default_factory=list)
    canonical_folder_id: str | None = None
    folder_path: str = ""
    updated_usec: int | None = None
    created_usec: int | None = None
    author_id: str | None = None
    link: str | None = None


@dataclass
class FolderEntry:
    folder_id: str
    title: str
    parent_ids: list[str] = field(default_factory=list)
    path: str = ""
    member_ids: list[str] = field(default_factory=list)  # for permission reporting (Phase 4)


# progress(phase, done, total_or_None, detail)
Progress = Callable[[str, int, int | None, str], None]


class Inventory:
    def __init__(self, config: Config, client: QuipClient) -> None:
        self._config = config
        self._client = client
        self.threads: dict[str, ThreadEntry] = {}
        self.folders: dict[str, FolderEntry] = {}
        self._folders_scanned = 0

    # -- discovery ------------------------------------------------------------------------
    def discover_root_folder_ids(self) -> list[str]:
        """Entry points: explicit config roots, else the user's private/shared/starred."""
        roots = list(self._config.folders.root_folder_ids)
        if roots:
            return roots
        user = self._client.current_user()
        for key in ("private_folder_id", "desktop_folder_id", "archive_folder_id"):
            fid = user.get(key)
            if fid:
                roots.append(fid)
        if self._config.folders.include_starred_root and user.get("starred_folder_id"):
            roots.append(user["starred_folder_id"])
        if self._config.folders.include_shared_folders:
            roots.extend(user.get("shared_folder_ids", []) or [])
        # De-dup while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for r in roots:
            if r not in seen:
                seen.add(r)
                ordered.append(r)
        return ordered

    def scan(self, progress: Progress | None = None, control: Control | None = None) -> None:
        roots = self.discover_root_folder_ids()
        log.info("Discovered %d root folder(s); walking the folder tree...", len(roots))
        self._folders_scanned = 0
        if progress is not None:
            progress("scan", 0, None, "walking folder tree")  # show the phase immediately
        visited: set[str] = set()
        for root in roots:
            self._walk(root, "", (), visited, progress=progress, control=control)
        log.info(
            "Folder scan done: %d folders, %d documents discovered.",
            len(self.folders),
            len(self.threads),
        )
        self._canonicalize_paths()

    def _walk(
        self,
        folder_id: str,
        parent_path: str,
        ancestry: tuple[str, ...],
        visited: set[str],
        progress: Progress | None = None,
        control: Control | None = None,
    ) -> None:
        if folder_id in ancestry:
            log.warning("Folder cycle detected at %s - skipping back-edge", folder_id)
            return
        if control is not None:
            control.checkpoint()  # blocks if paused; raises Cancelled if cancelled
        try:
            data = self._client.get_folder(folder_id)
        except Exception as exc:  # one bad folder must not stop the scan
            log.warning("Failed to read folder %s: %s", folder_id, exc)
            return

        self._folders_scanned += 1
        if self._folders_scanned % 10 == 0:
            log.info(
                "Scanning... %d folders walked, %d documents found so far",
                self._folders_scanned,
                len(self.threads),
            )
            if progress is not None:
                progress(
                    "scan", self._folders_scanned, None, f"{len(self.threads)} documents found"
                )

        meta = data.get("folder", {})
        title = meta.get("title", folder_id)
        if not self._config.folders.include_archived and meta.get("archived"):
            return

        if parent_path or title:
            path = f"{parent_path}/{sanitize_filename(title)}".strip("/")
        else:
            path = ""
        fe = self.folders.get(folder_id)
        if fe is None:
            fe = FolderEntry(folder_id=folder_id, title=title, path=path)
            self.folders[folder_id] = fe
        if ancestry and ancestry[-1] not in fe.parent_ids:
            fe.parent_ids.append(ancestry[-1])
        members = data.get("member_ids") or []
        if members:
            fe.member_ids = list(members)

        # Avoid re-descending the same folder, but still record cross-membership above.
        already = folder_id in visited
        visited.add(folder_id)

        for child in data.get("children", []) or []:
            if "thread_id" in child:
                self._record_thread(child["thread_id"], folder_id, path)
            elif "folder_id" in child and not already:
                self._walk(
                    child["folder_id"], path, ancestry + (folder_id,), visited, progress, control
                )

    def _record_thread(self, thread_id: str, folder_id: str, folder_path: str) -> None:
        entry = self.threads.get(thread_id)
        if entry is None:
            entry = ThreadEntry(thread_id=thread_id, title="", type="document")
            self.threads[thread_id] = entry
        if folder_id not in entry.parent_folder_ids:
            entry.parent_folder_ids.append(folder_id)

    def hydrate_threads(
        self,
        progress: Progress | None = None,
        control: Control | None = None,
        cache: ThreadCache | None = None,
    ) -> None:
        """Fetch per-thread metadata (title/type/timestamps) for the inventory + link map.

        When a `cache` is given, each full response is written to a local disk cache so the
        export phase reuses it (single fetch per document, O(1) memory, any account size).
        Emits progress every 10 documents.
        """
        total = len(self.threads)
        log.info("Fetching metadata for %d documents...", total)
        if progress is not None:
            progress("metadata", 0, total, "reading documents")  # show the phase immediately
        for i, (tid, entry) in enumerate(self.threads.items(), 1):
            if control is not None:
                control.checkpoint()
            try:
                resp = self._client.get_thread(tid)
            except Exception as exc:
                log.warning("Failed to hydrate thread %s: %s", tid, exc)
                continue
            if cache is not None:
                cache.put(tid, resp)
            thread = resp.get("thread", {})
            entry.title = thread.get("title") or entry.title or "Untitled"
            entry.type = thread.get("type", entry.type)
            entry.updated_usec = thread.get("updated_usec")
            entry.created_usec = thread.get("created_usec")
            entry.author_id = thread.get("author_id")
            entry.link = thread.get("link")
            if i % 10 == 0 or i == total:
                log.info("Metadata: %d/%d documents", i, total)
                if progress is not None:
                    progress("metadata", i, total, "")

    # -- canonicalization -----------------------------------------------------------------
    def _canonicalize_paths(self) -> None:
        unfiled = self._config.obsidian.unfiled_folder_name
        for entry in self.threads.values():
            if not entry.parent_folder_ids:
                entry.canonical_folder_id = None
                entry.folder_path = unfiled
                continue
            # Deterministic: first parent by stable sort of folder id.
            canonical = sorted(entry.parent_folder_ids)[0]
            entry.canonical_folder_id = canonical
            folder = self.folders.get(canonical, FolderEntry(canonical, ""))
            entry.folder_path = folder.path or unfiled

    # -- persistence ----------------------------------------------------------------------
    def persist(self, state: StateDB) -> None:
        for fid, fe in self.folders.items():
            state.upsert_folder(fid, fe.title, ",".join(fe.parent_ids), fe.path)
        for tid, te in self.threads.items():
            state.upsert_thread(
                thread_id=tid,
                title=te.title,
                type=te.type,
                canonical_folder_id=te.canonical_folder_id,
                folder_path=te.folder_path,
                updated_usec=te.updated_usec,
            )

    def write_manifest(self, manifest_dir: Path) -> None:
        rows = [
            {
                "thread_id": t.thread_id,
                "title": t.title,
                "type": t.type,
                "folder_path": t.folder_path,
                "created_usec": t.created_usec,
                "updated_usec": t.updated_usec,
                "author_id": t.author_id,
                "link": t.link,
                "parent_folder_ids": t.parent_folder_ids,
            }
            for t in self.threads.values()
        ]
        atomic_write_json(manifest_dir / "inventory.json", rows)
        atomic_write_json(
            manifest_dir / "folders.json",
            [
                {
                    "folder_id": f.folder_id,
                    "title": f.title,
                    "path": f.path,
                    "parent_ids": f.parent_ids,
                }
                for f in self.folders.values()
            ],
        )
        self._write_csv(manifest_dir / "inventory.csv", rows)
        plan = (
            f"# Export plan\n\n"
            f"- Folders discovered: {len(self.folders)}\n"
            f"- Threads discovered: {len(self.threads)}\n"
            f"- Orphan (unfiled) threads: "
            f"{sum(1 for t in self.threads.values() if not t.parent_folder_ids)}\n"
        )
        atomic_write_text(manifest_dir / "export_plan.md", plan)

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        import csv
        import io

        buf = io.StringIO()
        fields = [
            "thread_id",
            "title",
            "type",
            "folder_path",
            "created_usec",
            "updated_usec",
            "author_id",
            "link",
        ]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        atomic_write_text(path, buf.getvalue())
