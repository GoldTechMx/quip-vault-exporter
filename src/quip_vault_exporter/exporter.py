"""Export orchestration (Pass 2: render).

Per thread, with crash-safe state transitions (pending -> in_progress -> complete|failed):
  * write raw JSON (token-scrubbed), HTML, and Markdown (frontmatter + resolved wikilinks);
  * optionally export comments via full pagination;
  * record errors without ever stopping the whole run.

Incremental skip uses `updated_usec`. `dry_run` writes nothing — not even state.
Asset download, PDF, and spreadsheets are later phases; their hooks are stubbed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assets import AssetRef, asset_set_hash, download_assets, rewrite_markdown_assets
from .comments import export_comments
from .config import Config
from .control import Control
from .converter import html_to_markdown
from .inventory import Inventory
from .linkmap import LinkMap
from .logging_setup import scrub_text
from .obsidian import build_frontmatter, now_iso, resolve_tokens
from .pdf import PdfExporter
from .quip_client import QuipClient
from .spreadsheets import export_spreadsheet
from .state import StateDB
from .utils import atomic_write_text, is_safe_id, safe_join, sha256_text

log = logging.getLogger("quip_vault_exporter.exporter")


@dataclass
class ExportStats:
    exported: int = 0
    skipped: int = 0
    failed: int = 0
    comments_threads: int = 0
    assets: int = 0
    spreadsheets: int = 0
    pdfs: int = 0
    pdf_failures: int = 0


class Exporter:
    def __init__(
        self,
        config: Config,
        client: QuipClient,
        state: StateDB,
        linkmap: LinkMap,
        secrets: list[str],
        thread_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._state = state
        self._linkmap = linkmap
        self._secrets = secrets
        # Full thread responses captured during hydrate; popped here so each is fetched once.
        self._cache = thread_cache if thread_cache is not None else {}
        self._pdf = PdfExporter(
            client,
            poll_interval=config.limits.pdf_poll_seconds,
            timeout=config.limits.pdf_timeout_seconds,
        )
        self.stats = ExportStats()

    def export_all(
        self,
        inventory: Inventory,
        *,
        incremental: bool = False,
        force: bool = False,
        progress: Callable[[str, int, int | None, str], None] | None = None,
        control: Control | None = None,
    ) -> ExportStats:
        out = Path(self._config.output_dir)
        total = len(inventory.threads)
        for i, entry in enumerate(inventory.threads.values(), 1):
            if control is not None:
                control.checkpoint()  # pause/cancel between documents (Cancelled propagates)
            if (
                incremental
                and not force
                and not self._state.needs_export(
                    entry.thread_id,
                    entry.updated_usec,
                    current_title=entry.title,
                    current_folder_path=entry.folder_path,
                )
            ):
                self.stats.skipped += 1
            else:
                try:
                    self._export_one(entry, out)
                    self.stats.exported += 1
                except Exception as exc:  # never stop the run on one failure
                    self.stats.failed += 1
                    self._record_failure(entry, exc)
            if i % 10 == 0 or i == total:
                log.info(
                    "Exporting... %d/%d (exported=%d skipped=%d failed=%d)",
                    i,
                    total,
                    self.stats.exported,
                    self.stats.skipped,
                    self.stats.failed,
                )
                if progress is not None:
                    progress("export", i, total, f"{self.stats.failed} failed")
        return self.stats

    def _export_one(self, entry: Any, out_root: Path) -> None:
        tid = entry.thread_id
        if not is_safe_id(tid):
            raise ValueError(f"Unsafe thread_id from API: {tid!r}")
        dry = self._config.safety.dry_run
        if not dry:
            self._state.set_status(tid, "in_progress")

        # Reuse the response captured during hydrate (pop frees memory as we go); only
        # re-fetch if it wasn't cached (large-account fallback or resume).
        thread_resp = self._cache.pop(tid, None) or self._client.get_thread(tid)
        thread = thread_resp.get("thread", {})
        html = thread_resp.get("html", "")
        record = self._linkmap.get(tid) or {"stem": "Untitled", "vault_path": ""}
        stem = record["stem"]
        # safe_join is the backstop: folder_path derives from sanitized titles, but a
        # malicious/garbage API value can never escape the output root.
        folder_dir = safe_join(out_root, entry.folder_path)
        title = thread.get("title") or entry.title or "Untitled"

        if dry:
            log.info("[dry-run] would export %s -> %s/%s.md", tid, entry.folder_path, stem)
            return

        cfg = self._config.export
        doc_vault_path = record.get("vault_path") or f"{entry.folder_path}/{stem}.md"

        # Raw JSON (token-scrubbed) -- provenance + verify checksum source.
        if cfg.json_raw and self._config.safety.preserve_raw_api_responses:
            scrubbed = scrub_text(_safe_json(thread_resp), self._secrets)
            atomic_write_text(folder_dir / f"{stem}.raw.json", scrubbed)

        # Attachments / images: download blobs, then rewrite Markdown references to embeds.
        asset_map: dict[str, AssetRef] = {}
        if cfg.attachments:
            asset_map = download_assets(
                client=self._client,
                thread_id=tid,
                html=html,
                config=self._config,
                secrets=self._secrets,
            )

        # HTML.
        if cfg.html:
            atomic_write_text(folder_dir / f"{stem}.html", html or "")

        is_spreadsheet = (thread.get("type") or entry.type) == "spreadsheet"

        # Markdown (frontmatter + resolved wikilinks + asset embeds), or — for spreadsheets
        # — an XLSX/CSV export plus a Markdown wrapper note.
        markdown_body = ""
        if is_spreadsheet and cfg.spreadsheets:
            sheet_result = export_spreadsheet(
                client=self._client,
                thread_id=tid,
                title=title,
                stem=stem,
                folder_dir=folder_dir,
                config=self._config,
            )
            if sheet_result.status == "complete":
                self.stats.spreadsheets += 1
                markdown_body = sheet_result.markdown  # checksum the wrapper note
            else:
                log.warning("Spreadsheet export failed for %s: %s", tid, sheet_result.error)
        elif cfg.markdown:
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

        # PDF (async; long-running). Failures are recorded but never abort the document.
        pdf_status = "skipped"
        if cfg.pdf:
            pdf_result = self._pdf.export(tid, folder_dir / f"{stem}.pdf")
            pdf_status = pdf_result.status
            if pdf_result.status == "complete":
                self.stats.pdfs += 1
            else:
                self.stats.pdf_failures += 1
                self._record_pdf_failure(entry, pdf_result)

        # Comments (full pagination).
        comment_info: dict[str, Any] = {}
        if cfg.comments:
            comments_dir = folder_dir / self._config.obsidian.comments_folder_name
            comment_info = export_comments(
                client=self._client,
                thread_id=tid,
                title=stem,
                comments_dir=comments_dir,
                secrets=self._secrets,
            )
            self.stats.comments_threads += 1

        self.stats.assets += len(asset_map)

        # Mark complete only after every artifact is durably written.
        self._state.upsert_thread(
            thread_id=tid,
            title=title,
            type=thread.get("type", entry.type),
            folder_path=entry.folder_path,
            last_seen_updated_usec=thread.get("updated_usec"),
            last_exported_at=now_iso(),
            checksum=sha256_text(markdown_body) if markdown_body else None,
            comment_count=comment_info.get("count"),
            comments_truncated=1 if comment_info.get("truncated") else 0,
            asset_set_hash=asset_set_hash(asset_map) if asset_map else None,
            asset_count=len(asset_map),
            pdf_status=pdf_status,
        )
        # A timed-out/failed PDF leaves the doc 'partial' so `resume` retries it; everything
        # else is durable, so the document itself is otherwise complete.
        final_status = "partial" if pdf_status in ("failed", "timeout") else "complete"
        self._state.set_status(tid, final_status)

    def _record_pdf_failure(self, entry: Any, result: Any) -> None:
        msg = scrub_text(result.error or result.status, self._secrets)
        log.warning("PDF export %s for %s: %s", result.status, entry.thread_id, msg)
        self._state.record_error(
            item_id=entry.thread_id,
            item_title=entry.title,
            item_type=entry.type,
            operation="pdf_export",
            error_type=result.status,
            error_message=msg,
            timestamp=now_iso(),
            retry_count=0,
        )

    def _record_failure(self, entry: Any, exc: Exception) -> None:
        if self._config.safety.dry_run:
            return
        msg = scrub_text(str(exc), self._secrets)
        log.error("Export failed for %s: %s", entry.thread_id, msg)
        self._state.set_status(entry.thread_id, "failed", error=msg)
        self._state.record_error(
            item_id=entry.thread_id,
            item_title=entry.title,
            item_type=entry.type,
            operation="export",
            error_type=type(exc).__name__,
            error_message=msg,
            timestamp=now_iso(),
            retry_count=0,
        )


def _safe_json(obj: Any) -> str:
    import json

    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
