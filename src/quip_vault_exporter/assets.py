"""Attachment / image download and reference rewriting (Phase 2).

Quip embeds blobs in rendered HTML as `/blob/{thread_id}/{blob_id}`. This module:
  * discovers those references,
  * downloads each blob once (idempotent, atomic write) to
    `_assets/{thread_id}/{name}` deriving a filename from Content-Disposition or
    Content-Type,
  * rewrites Markdown image links `![alt](/blob/T/B)` to Obsidian embeds
    `![[_assets/T/name]]` (or relative standard Markdown when wikilinks are disabled),
  * computes a stable `asset_set_hash` so incremental runs can detect asset changes that
    don't bump the thread's `updated_usec`.

Per-asset failures are isolated: a blob that won't download is logged and skipped, never
aborting the document.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import Config
from .logging_setup import scrub_text
from .quip_client import QuipClient
from .sanitize import sanitize_filename
from .utils import atomic_write_bytes, ensure_dir, is_safe_id, safe_join, sha256_bytes

log = logging.getLogger("quip_vault_exporter.assets")

# Quip embeds blobs as /blob/<thread_id>/<blob_id> inside rendered HTML and, after
# conversion, inside Markdown image links.
BLOB_REF = re.compile(r"/blob/(?P<thread>[A-Za-z0-9]+)/(?P<blob>[A-Za-z0-9]+)")

# Markdown image link whose target contains a blob reference: ![alt](.../blob/T/B...)
_MD_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]*?/blob/(?P<thread>[A-Za-z0-9]+)/(?P<blob>[A-Za-z0-9]+)[^)]*)\)"
)


@dataclass
class AssetRef:
    blob_id: str
    name: str
    vault_path: str  # vault-root-relative POSIX path, e.g. _assets/THREAD/image.png
    sha256: str


def discover_blob_ids(html: str) -> list[tuple[str, str]]:
    """Return unique (thread_id, blob_id) pairs referenced in a thread's HTML."""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for m in BLOB_REF.finditer(html or ""):
        pair = (m.group("thread"), m.group("blob"))
        if pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    return ordered


def _derive_name(blob_id: str, filename: str | None, content_type: str | None) -> str:
    if filename:
        return sanitize_filename(filename)
    ext = ""
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    return f"{blob_id}{ext}"


def download_assets(
    *,
    client: QuipClient,
    thread_id: str,
    html: str,
    config: Config,
    secrets: list[str],
) -> dict[str, AssetRef]:
    """Download every blob referenced in `html`. Returns {blob_id: AssetRef}."""
    if not is_safe_id(thread_id):
        log.warning("Skipping assets for unsafe thread_id %r", thread_id)
        return {}
    assets_root = config.obsidian.assets_folder_name
    out_dir = ensure_dir(_assets_dir(config, thread_id))
    result: dict[str, AssetRef] = {}
    used_names: dict[str, str] = {}

    for ref_thread, blob_id in discover_blob_ids(html):
        if blob_id in result or not is_safe_id(blob_id):
            continue
        try:
            content, content_type, filename = client.get_blob_meta(ref_thread, blob_id)
        except Exception as exc:
            log.warning("Asset %s/%s failed: %s", thread_id, blob_id, scrub_text(str(exc), secrets))
            continue
        name = _derive_name(blob_id, filename, content_type)
        # Disambiguate within the thread folder if two blobs share a derived name.
        if name in used_names and used_names[name] != blob_id:
            stem, dot, ext = name.partition(".")
            name = f"{stem}-{blob_id[:8]}{dot}{ext}"
        used_names[name] = blob_id

        atomic_write_bytes(safe_join(out_dir, name), content)
        vault_path = str(PurePosixPath(assets_root) / thread_id / name)
        result[blob_id] = AssetRef(blob_id, name, vault_path, sha256_bytes(content))

    return result


def rewrite_markdown_assets(
    markdown: str,
    asset_map: dict[str, AssetRef],
    *,
    doc_vault_path: str,
    config: Config,
) -> str:
    """Rewrite Markdown blob image links to Obsidian embeds (or relative Markdown)."""
    use_wikilinks = config.obsidian.wikilinks

    def repl(match: re.Match[str]) -> str:
        alt = match.group("alt")
        blob_id = match.group("blob")
        ref = asset_map.get(blob_id)
        if ref is None:
            return match.group(0)  # download failed; leave the original link untouched
        if use_wikilinks:
            return f"![[{ref.vault_path}]]"
        rel = _relative_path(doc_vault_path, ref.vault_path)
        return f"![{alt}]({rel})"

    return _MD_IMAGE.sub(repl, markdown)


def asset_set_hash(asset_map: dict[str, AssetRef]) -> str:
    """Stable hash of the asset set, so incremental runs detect asset-only changes."""
    joined = "\n".join(f"{bid}:{ref.sha256}" for bid, ref in sorted(asset_map.items()))
    return sha256_bytes(joined.encode("utf-8"))


def _assets_dir(config: Config, thread_id: str) -> Path:
    return safe_join(Path(config.output_dir), config.obsidian.assets_folder_name, thread_id)


def _relative_path(from_doc: str, to_asset: str) -> str:
    """POSIX relative path from a document to an asset, both vault-root-relative."""
    doc_parent = PurePosixPath(from_doc).parent
    target = PurePosixPath(to_asset)
    # Walk up from the document's folder to the common root, then down to the asset.
    up = [".."] * len(doc_parent.parts)
    return str(PurePosixPath(*up, *target.parts)) if up else str(target)
