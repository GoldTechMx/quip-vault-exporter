"""Manifest, verification, and executive reporting.

Phase 1 implements the inventory/error/summary manifests and a basic verification pass.
Deeper verification (broken asset links, broken wikilinks, empty markdown, PDF failures)
arrives with the matching features in Phase 2/3.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Config
from .obsidian import now_iso
from .state import StateDB
from .utils import atomic_write_json, atomic_write_text

# Obsidian embed (![[...]]) and wikilink ([[...]]) targets.
_EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _link_target(inner: str) -> str:
    """Strip an Obsidian link's alias (`|`) and heading (`#`) to the bare target."""
    return inner.split("|", 1)[0].split("#", 1)[0].strip()


def scan_vault_links(config: Config, state: StateDB) -> dict[str, Any]:
    """Walk the exported vault and resolve every embed/wikilink against the filesystem."""
    out = Path(config.output_dir)
    manifest_name = config.obsidian.manifest_folder_name
    title_index = {rec["title"].lower(): rec["vault_path"] for rec in state.linkmap().values()}
    known_paths = {rec["vault_path"].lower() for rec in state.linkmap().values()}

    broken_embeds: list[str] = []
    broken_links: list[str] = []
    empty_docs: list[str] = []
    scanned = 0

    for md in out.rglob("*.md"):
        if manifest_name in md.parts:
            continue
        scanned += 1
        text = md.read_text(encoding="utf-8", errors="replace")
        rel = md.relative_to(out).as_posix()

        if _is_empty_body(text):
            empty_docs.append(rel)

        for inner in _EMBED_RE.findall(text):
            target = _link_target(inner)
            if not (out / target).exists():
                broken_embeds.append(f"{rel} -> {target}")

        for inner in _WIKILINK_RE.findall(text):
            target = _link_target(inner)
            if not _resolves(target, out, title_index, known_paths):
                broken_links.append(f"{rel} -> {target}")

    return {
        "scanned_markdown_files": scanned,
        "broken_asset_embeds": broken_embeds,
        "broken_wikilinks": broken_links,
        "empty_markdown_files": empty_docs,
    }


def _resolves(target: str, out: Path, title_index: dict[str, str], known: set[str]) -> bool:
    if _FILE_EXT_RE.search(target):  # embed-style file link inside [[ ]]
        return (out / target).exists()
    if "/" in target:  # path-aliased note link -> file at <target>.md
        return target.lower() in known or (out / f"{target}.md").exists()
    return target.lower() in title_index  # bare [[Title]] -> resolve via title index


def _is_empty_body(text: str) -> bool:
    """True if the note has no content beyond frontmatter and its leading H1."""
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :]
    body = re.sub(r"^\s*#\s+.*$", "", body, count=1, flags=re.MULTILINE)
    return not body.strip()


def write_error_manifest(state: StateDB, manifest_dir: Path) -> None:
    errors = state.all_errors()
    atomic_write_json(manifest_dir / "errors.json", errors)
    import csv
    import io

    buf = io.StringIO()
    fields = [
        "item_id",
        "item_title",
        "item_type",
        "operation",
        "error_type",
        "error_message",
        "timestamp",
        "retry_count",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in errors:
        writer.writerow(row)
    atomic_write_text(manifest_dir / "errors.csv", buf.getvalue())


def verify(state: StateDB, config: Config, manifest_dir: Path) -> dict[str, Any]:
    threads = state.all_threads()
    total = len(threads)
    complete = sum(1 for t in threads if t["export_status"] == "complete")
    failed = sum(1 for t in threads if t["export_status"] == "failed")
    in_progress = sum(1 for t in threads if t["export_status"] == "in_progress")
    truncated_comments = sum(1 for t in threads if t.get("comments_truncated"))

    empty_md = sum(1 for t in threads if t["export_status"] == "complete" and not t.get("checksum"))
    pdf_failed = sum(1 for t in threads if t.get("pdf_status") in ("failed", "timeout"))

    vault = scan_vault_links(config, state)
    broken_embeds = vault["broken_asset_embeds"]
    broken_links = vault["broken_wikilinks"]
    empty_docs = vault["empty_markdown_files"]

    report = {
        "generated_at": now_iso(),
        "total_inventory": total,
        "exported_complete": complete,
        "failed": failed,
        "in_progress_incomplete": in_progress,
        "comment_truncations": truncated_comments,
        "documents_missing_checksum": empty_md,
        "scanned_markdown_files": vault["scanned_markdown_files"],
        "broken_asset_embeds": len(broken_embeds),
        "broken_wikilinks": len(broken_links),
        "empty_markdown_files": len(empty_docs),
        "pdf_failures": pdf_failed,
    }
    lines = [
        "# Verification report",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Total inventory items: {total}",
        f"- Exported (complete): {complete}",
        f"- Failed: {failed}",
        f"- Left in_progress (incomplete — rerun `resume`): {in_progress}",
        f"- Threads with truncated comments: {truncated_comments}",
        f"- Complete docs missing a checksum: {empty_md}",
        "",
        "## On-disk vault scan",
        f"- Markdown files scanned: {vault['scanned_markdown_files']}",
        f"- Broken asset embeds: {len(broken_embeds)}",
        f"- Broken wikilinks: {len(broken_links)}",
        f"- Empty Markdown files: {len(empty_docs)}",
        f"- PDF exports failed/timed-out: {pdf_failed}",
    ]
    lines += _sample_section("Broken asset embeds", broken_embeds)
    lines += _sample_section("Broken wikilinks", broken_links)
    lines += _sample_section("Empty Markdown files", empty_docs)
    atomic_write_text(manifest_dir / "verification_report.md", "\n".join(lines) + "\n")
    return report


def _sample_section(title: str, items: list[str], limit: int = 20) -> list[str]:
    if not items:
        return []
    out = ["", f"### {title} ({len(items)})"]
    out += [f"- `{item}`" for item in items[:limit]]
    if len(items) > limit:
        out.append(f"- … and {len(items) - limit} more")
    return out


def write_summary(state: StateDB, config: Config, manifest_dir: Path, admin_used: bool) -> None:
    threads = state.all_threads()
    complete = sum(1 for t in threads if t["export_status"] == "complete")
    failed = sum(1 for t in threads if t["export_status"] == "failed")
    comments_total = sum(t.get("comment_count") or 0 for t in threads)
    assets_total = sum(t.get("asset_count") or 0 for t in threads)
    pdf_complete = sum(1 for t in threads if t.get("pdf_status") == "complete")
    pdf_failed = sum(1 for t in threads if t.get("pdf_status") in ("failed", "timeout"))
    lines = [
        "# Quip Export Summary",
        "",
        f"- Workspace: {config.workspace_name}",
        f"- Export date: {now_iso()}",
        f"- Total documents: {len(threads)}",
        f"- Exported: {complete}",
        f"- Failed: {failed}",
        f"- Total comments/messages exported: {comments_total}",
        f"- Total assets downloaded: {assets_total}",
        f"- PDFs exported: {pdf_complete} (failed/timed-out: {pdf_failed})",
        f"- Output: {config.output_dir}",
        f"- Admin API used: {admin_used}",
        "",
        "## Limitations",
        "- Comments are paginated in full (25/100 is a page size, not a cap).",
        "- Spreadsheet PDF rendering caps at ~40,000 cells and excludes charts.",
        "- Admin org/permission fields must be validated against a live admin tenant.",
        "- Quip is being retired by Salesforce; the API stops at the Read-Only phase.",
    ]
    atomic_write_text(manifest_dir / "export_summary.md", "\n".join(lines) + "\n")


def write_cancellation_checklist(manifest_dir: Path) -> None:
    content = """# Quip Cancellation Checklist

Before canceling Quip, confirm:

- [ ] Inventory generated.
- [ ] All accessible folders scanned.
- [ ] All critical documents exported.
- [ ] All important PDFs generated if required.
- [ ] Markdown files open correctly in Obsidian.
- [ ] Images and attachments resolve.
- [ ] Comments/messages exported (full pagination) where the API allowed.
- [ ] Raw JSON backup retained.
- [ ] Export copied to OneDrive/SharePoint/NAS.
- [ ] Export backed up offsite.
- [ ] Stakeholders approved archive.
- [ ] Final export completed after the last Quip changes.
- [ ] Done BEFORE the tenant enters the Read-Only phase (API stops working there).
"""
    atomic_write_text(manifest_dir / "cancellation_checklist.md", content)
