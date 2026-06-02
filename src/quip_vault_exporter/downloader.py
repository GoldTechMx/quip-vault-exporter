"""DOWNLOAD phase (the only API-bound, rate-limited part of an export).

For each document discovered by the scan (whose folder/sub-folder path is already known and
preserved), fetch everything from Quip ONCE and persist it to the local raw store, marking
per-document state as it goes:

    pending -> downloading -> downloaded   (or failed)

This is fully RESUMABLE: a crashed or cancelled run skips documents already `downloaded` or
`complete`, so the rate-limited API work is never repeated. No vault files are written here -
rendering happens later in the local PROCESS phase, which reads the raw store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .assets import asset_set_hash, download_assets, index_from_map
from .cache import RawStore
from .comments import fetch_comments
from .config import Config
from .control import Cancelled, Control
from .inventory import Inventory
from .logging_setup import scrub_text
from .obsidian import now_iso
from .pdf import PdfExporter
from .quip_client import QuipClient
from .state import StateDB
from .utils import is_safe_id

log = logging.getLogger("quip_vault_exporter.downloader")


@dataclass
class DownloadStats:
    downloaded: int = 0
    skipped: int = 0  # already complete (incremental)
    reused: int = 0  # already downloaded (resume)
    failed: int = 0
    comments_threads: int = 0
    assets: int = 0
    pdfs: int = 0
    pdf_failures: int = 0
    spreadsheets: int = 0


class Downloader:
    def __init__(
        self,
        config: Config,
        client: QuipClient,
        state: StateDB,
        raw: RawStore,
        secrets: list[str],
    ) -> None:
        self._config = config
        self._client = client
        self._state = state
        self._raw = raw
        self._secrets = secrets
        self._pdf = PdfExporter(
            client,
            poll_interval=config.limits.pdf_poll_seconds,
            timeout=config.limits.pdf_timeout_seconds,
        )
        self.stats = DownloadStats()

    def download_all(
        self,
        inventory: Inventory,
        *,
        force: bool = False,
        progress: Any | None = None,
        control: Control | None = None,
    ) -> DownloadStats:
        threads = sorted(inventory.threads.values(), key=lambda e: e.thread_id)
        total = len(threads)
        log.info(
            "PHASE 2/4 - DOWNLOAD: fetching %d documents from Quip (API, rate-limited). "
            "Resumable - already-downloaded documents are skipped. No vault files yet.",
            total,
        )
        if progress is not None:
            progress("download", 0, total, "downloading from Quip")
        for i, entry in enumerate(threads, 1):
            if control is not None:
                control.checkpoint()
            tid = entry.thread_id
            if not is_safe_id(tid):
                log.warning("Skipping unsafe thread_id from API: %r", tid)
                self.stats.failed += 1
                continue
            row = self._state.get_thread(tid)
            status = row["export_status"] if row else "pending"
            if not force and status == "complete":
                self.stats.skipped += 1
            elif not force and status == "downloaded" and self._raw.has_thread(tid):
                self.stats.reused += 1  # already on disk; PROCESS phase will render it
            else:
                try:
                    self._download_one(entry)
                    self.stats.downloaded += 1
                except Cancelled:
                    raise
                except Exception as exc:
                    self.stats.failed += 1
                    self._record_failure(entry, exc)
            if i % 10 == 0 or i == total:
                log.info(
                    "Downloading... %d/%d (new=%d reused=%d skipped=%d failed=%d)",
                    i,
                    total,
                    self.stats.downloaded,
                    self.stats.reused,
                    self.stats.skipped,
                    self.stats.failed,
                )
                if progress is not None:
                    progress("download", i, total, f"{self.stats.failed} failed")
        return self.stats

    def _download_one(self, entry: Any) -> None:
        tid = entry.thread_id
        cfg = self._config.export
        self._state.set_status(tid, "downloading")

        resp = self._client.get_thread(tid)
        self._raw.put_thread(tid, resp)
        thread = resp.get("thread", {})
        html = resp.get("html", "")
        # Update the in-memory entry so the link map (next phase) has titles/timestamps.
        entry.title = thread.get("title") or entry.title or "Untitled"
        entry.type = thread.get("type", entry.type)
        entry.updated_usec = thread.get("updated_usec")
        entry.created_usec = thread.get("created_usec")
        entry.author_id = thread.get("author_id")
        entry.link = thread.get("link")

        comment_count = 0
        truncated = False
        if cfg.comments:
            fc = fetch_comments(client=self._client, thread_id=tid, secrets=self._secrets)
            self._raw.put_comments(tid, fc["rows"])
            comment_count = fc["count"]
            truncated = fc["truncated"]
            self.stats.comments_threads += 1

        asset_count = 0
        asset_hash: str | None = None
        if cfg.attachments:
            amap = download_assets(
                client=self._client,
                thread_id=tid,
                html=html,
                config=self._config,
                secrets=self._secrets,
            )
            self._raw.put_assets_index(tid, index_from_map(amap))
            asset_count = len(amap)
            asset_hash = asset_set_hash(amap) if amap else None
            self.stats.assets += asset_count

        pdf_status = "skipped"
        if cfg.pdf:
            result = self._pdf.export(tid, self._raw.path_for(tid, "pdf"))
            pdf_status = result.status
            if result.status == "complete":
                self.stats.pdfs += 1
            else:
                self.stats.pdf_failures += 1
                log.warning("PDF %s for %s: %s", result.status, tid, result.error)

        if cfg.spreadsheets and entry.type == "spreadsheet":
            try:
                self._raw.put_bytes(tid, "xlsx", self._client.export_xlsx(tid))
                self.stats.spreadsheets += 1
            except Exception as exc:
                log.warning("XLSX download failed for %s: %s", tid, exc)

        self._state.upsert_thread(
            thread_id=tid,
            title=entry.title,
            type=entry.type,
            folder_path=entry.folder_path,
            canonical_folder_id=entry.canonical_folder_id,
            updated_usec=entry.updated_usec,
            last_seen_updated_usec=entry.updated_usec,
            comment_count=comment_count,
            comments_truncated=1 if truncated else 0,
            asset_count=asset_count,
            asset_set_hash=asset_hash,
            pdf_status=pdf_status,
        )
        self._state.set_status(tid, "downloaded")

    def _record_failure(self, entry: Any, exc: Exception) -> None:
        msg = scrub_text(str(exc), self._secrets)
        log.error("Download failed for %s: %s", entry.thread_id, msg)
        self._state.set_status(entry.thread_id, "failed", error=msg)
        self._state.record_error(
            item_id=entry.thread_id,
            item_title=entry.title,
            item_type=entry.type,
            operation="download",
            error_type=type(exc).__name__,
            error_message=msg,
            timestamp=now_iso(),
            retry_count=0,
        )
