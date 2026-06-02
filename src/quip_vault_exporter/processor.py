"""PROCESS phase (fully local, no API).

Reads each already-downloaded document from the raw store and renders the final, ordered
Obsidian vault - Markdown (frontmatter + resolved wikilinks + asset embeds), HTML, raw JSON,
comments, spreadsheet wrappers, and PDFs - exactly mirroring Quip's folder/sub-folder tree.

Because it never touches the API, this phase is fast and fully re-runnable: if rendering
changes or fails, re-run it against the local raw store without re-consuming the rate-limited
API. State advances `downloaded -> complete`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assets import map_from_index, rewrite_markdown_assets
from .cache import RawStore
from .comments import render_comments_markdown
from .config import Config
from .control import Control
from .converter import html_to_markdown
from .inventory import Inventory
from .linkmap import LinkMap
from .logging_setup import scrub_text
from .obsidian import build_frontmatter, now_iso, resolve_tokens
from .spreadsheets import render_spreadsheet
from .state import StateDB
from .utils import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    safe_join,
    sha256_text,
)

log = logging.getLogger("quip_vault_exporter.processor")


@dataclass
class ProcessStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    missing_raw: int = 0
    spreadsheets: int = 0
    pdfs: int = 0


class Processor:
    def __init__(
        self,
        config: Config,
        state: StateDB,
        linkmap: LinkMap,
        raw: RawStore,
        secrets: list[str],
    ) -> None:
        self._config = config
        self._state = state
        self._linkmap = linkmap
        self._raw = raw
        self._secrets = secrets
        self.stats = ProcessStats()

    def process_all(
        self,
        inventory: Inventory,
        *,
        force: bool = False,
        progress: Any | None = None,
        control: Control | None = None,
    ) -> ProcessStats:
        out = Path(self._config.output_dir)
        threads = list(inventory.threads.values())
        total = len(threads)
        log.info("PHASE 4/4 - PROCESS: rendering %d documents to the vault (local, no API).", total)
        if progress is not None:
            progress("process", 0, total, "rendering vault")
        for i, entry in enumerate(threads, 1):
            if control is not None:
                control.checkpoint()
            row = self._state.get_thread(entry.thread_id)
            status = row["export_status"] if row else "pending"
            if status == "complete" and not force:
                self.stats.skipped += 1
            elif status in ("downloaded", "complete"):
                try:
                    self._process_one(entry, out)
                    self.stats.processed += 1
                except Exception as exc:  # never stop the run on one document
                    self.stats.failed += 1
                    self._record_failure(entry, exc)
            else:
                # never downloaded (failed/pending) -> can't render locally
                self.stats.missing_raw += 1
            if i % 25 == 0 or i == total:
                log.info(
                    "Rendering... %d/%d (done=%d failed=%d)",
                    i,
                    total,
                    self.stats.processed,
                    self.stats.failed,
                )
                if progress is not None:
                    progress("process", i, total, f"{self.stats.failed} failed")
        return self.stats

    def _process_one(self, entry: Any, out_root: Path) -> None:
        tid = entry.thread_id
        resp = self._raw.get_thread(tid)
        if resp is None:
            self.stats.missing_raw += 1
            raise ValueError("raw payload missing (re-download needed)")
        cfg = self._config.export
        thread = resp.get("thread", {})
        html = resp.get("html", "")
        record = self._linkmap.get(tid) or {"stem": "Untitled", "vault_path": ""}
        stem = record["stem"]
        folder_dir = safe_join(out_root, entry.folder_path)
        title = thread.get("title") or entry.title or "Untitled"
        doc_vault_path = record.get("vault_path") or f"{entry.folder_path}/{stem}.md"

        if cfg.json_raw and self._config.safety.preserve_raw_api_responses:
            atomic_write_text(
                folder_dir / f"{stem}.raw.json",
                scrub_text(_safe_json(resp), self._secrets),
            )
        if cfg.html:
            atomic_write_text(folder_dir / f"{stem}.html", html or "")

        is_spreadsheet = (thread.get("type") or entry.type) == "spreadsheet"
        markdown_body = ""
        if is_spreadsheet and cfg.spreadsheets and self._raw.has(tid, "xlsx"):
            xlsx = self._raw.get_bytes(tid, "xlsx") or b""
            result = render_spreadsheet(
                xlsx=xlsx,
                thread_id=tid,
                title=title,
                stem=stem,
                folder_dir=folder_dir,
                config=self._config,
            )
            markdown_body = result.markdown
            self.stats.spreadsheets += 1
        elif cfg.markdown:
            asset_map = map_from_index(self._raw.get_assets_index(tid))
            body = resolve_tokens(html_to_markdown(html), self._linkmap.mapping, self._config)
            if asset_map:
                body = rewrite_markdown_assets(
                    body, asset_map, doc_vault_path=doc_vault_path, config=self._config
                )
            front = (
                build_frontmatter(
                    thread_id=tid,
                    title=title,
                    thread_type=thread.get("type", entry.type),
                    created_usec=thread.get("created_usec"),
                    updated_usec=thread.get("updated_usec"),
                    original_url=thread.get("link"),
                    folder_path=entry.folder_path,
                    parent_folder_ids=entry.parent_folder_ids,
                    exported_at=now_iso(),
                    comments_exported=cfg.comments,
                    raw_title=title,
                )
                if self._config.obsidian.yaml_frontmatter
                else ""
            )
            markdown_body = f"{front}\n# {title}\n\n{body}"
            atomic_write_text(folder_dir / f"{stem}.md", markdown_body)

        # PDF downloaded earlier -> copy into the vault under its final name.
        if cfg.pdf and self._raw.has(tid, "pdf"):
            data = self._raw.get_bytes(tid, "pdf")
            if data:
                atomic_write_bytes(folder_dir / f"{stem}.pdf", data)
                self.stats.pdfs += 1

        # Comments rendered from the downloaded rows (no API).
        if cfg.comments:
            rows = self._raw.get_comments(tid)
            cdir = folder_dir / self._config.obsidian.comments_folder_name
            atomic_write_json(cdir / f"{stem}.comments.json", rows)
            atomic_write_text(cdir / f"{stem}.comments.md", render_comments_markdown(title, rows))

        self._state.upsert_thread(
            thread_id=tid,
            title=title,
            checksum=sha256_text(markdown_body) if markdown_body else None,
        )
        self._state.set_status(tid, "complete")

    def _record_failure(self, entry: Any, exc: Exception) -> None:
        msg = scrub_text(str(exc), self._secrets)
        log.error("Render failed for %s: %s", entry.thread_id, msg)
        self._state.set_status(entry.thread_id, "failed", error=msg)
        self._state.record_error(
            item_id=entry.thread_id,
            item_title=entry.title,
            item_type=entry.type,
            operation="process",
            error_type=type(exc).__name__,
            error_message=msg,
            timestamp=now_iso(),
            retry_count=0,
        )


def _safe_json(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
