"""Comment/message export with FULL pagination.

The original spec's "25 most recent" claim is wrong - see CLAUDE.md. This module walks the
entire message history via QuipClient.paginate_messages and records the true total in state,
so the manifest can honestly report completeness instead of silently truncating.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .logging_setup import scrub_text
from .obsidian import now_iso
from .quip_client import QuipClient
from .utils import atomic_write_json, atomic_write_text, usec_to_iso

log = logging.getLogger("quip_vault_exporter.comments")


def export_comments(
    *,
    client: QuipClient,
    thread_id: str,
    title: str,
    comments_dir: Path,
    user_names: dict[str, str] | None = None,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Export all messages for a thread as JSON + readable Markdown.

    Returns {"count": int, "truncated": bool}. `truncated` stays False unless pagination
    was cut short by an error - a real signal for the manifest, not a guess.
    """
    user_names = user_names or {}
    secrets = secrets or []
    messages: list[dict[str, Any]] = []
    truncated = False
    try:
        for msg in client.paginate_messages(thread_id):
            messages.append(msg)
    except Exception as exc:  # partial is better than nothing; flag it honestly
        truncated = True
        log.warning("Comment pagination cut short for %s: %s", thread_id, exc)

    json_rows = [
        {
            "id": m.get("id"),
            "author": user_names.get(m.get("author_id", ""), m.get("author_id")),
            "created_at": usec_to_iso(m.get("created_usec")),
            "text": scrub_text(m.get("text", ""), secrets),
            "raw": m,
        }
        for m in messages
    ]
    atomic_write_json(comments_dir / f"{title}.comments.json", json_rows)
    atomic_write_text(comments_dir / f"{title}.comments.md", _render_markdown(title, json_rows))

    return {"count": len(messages), "truncated": truncated, "exported_at": now_iso()}


def _render_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"# Comments for {title}", ""]
    lines.append(f"> {len(rows)} message(s) exported via full API pagination.")
    lines.append("")
    for row in rows:  # newest-first from the API; readable as-is
        when = row.get("created_at") or "unknown time"
        who = row.get("author") or "unknown"
        lines.append(f"## {when} - {who}")
        lines.append("")
        lines.append(row.get("text", "").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
