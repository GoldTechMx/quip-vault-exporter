"""Pass 2 of wikilink resolution + YAML frontmatter assembly.

Resolves the {{QVE_LINK:<thread_id>|<text>}} tokens emitted by converter.py against the
finished link map:
  * target in the export set  -> path/aliased wikilink [[vault/path|text]]
    (path-aliased so disambiguated files stay addressable; falls back to [[Title]] if
    path_aliased_links is off);
  * target NOT in the export set -> original Quip URL, so the link is never silently lost.
"""

from __future__ import annotations

from datetime import UTC, datetime

import yaml

from .config import Config
from .converter import LINK_TOKEN_RE
from .utils import usec_to_iso

COMMENTS_LIMIT_NOTE = (
    "Comments are exported in full via API pagination (25/100 is a page size, not a cap)."
)


def resolve_tokens(markdown: str, linkmap: dict[str, dict[str, str]], config: Config) -> str:
    use_wikilinks = config.obsidian.wikilinks
    path_aliased = config.obsidian.path_aliased_links

    def repl(match: object) -> str:
        thread_id = match.group("id")  # type: ignore[attr-defined]
        text = match.group("text") or thread_id  # type: ignore[attr-defined]
        target = linkmap.get(thread_id)
        if target is None:
            return f"[{text}](https://quip.com/{thread_id})"
        if not use_wikilinks:
            return f"[{text}]({target['vault_path']})"
        if path_aliased:
            base = target["vault_path"].removesuffix(".md")
            return f"[[{base}|{text}]]"
        return f"[[{target['title']}|{text}]]"

    return LINK_TOKEN_RE.sub(repl, markdown)


def build_frontmatter(
    *,
    thread_id: str,
    title: str,
    thread_type: str,
    created_usec: int | None,
    updated_usec: int | None,
    original_url: str | None,
    folder_path: str,
    parent_folder_ids: list[str],
    exported_at: str,
    comments_exported: bool,
    raw_title: str,
) -> str:
    aliases = sorted({raw_title, thread_id} - {""})
    data = {
        "source": "quip",
        "thread_id": thread_id,
        "title": title,
        "type": thread_type,
        "aliases": aliases,
        "created_at": usec_to_iso(created_usec),
        "updated_at": usec_to_iso(updated_usec),
        "original_url": original_url,
        "exported_at": exported_at,
        "folder_path": folder_path,
        "parent_folder_ids": parent_folder_ids,
        "comments_exported": comments_exported,
        "comments_note": COMMENTS_LIMIT_NOTE,
    }
    dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n"


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
