"""Comment/message handling, split for the download-then-process pipeline.

`fetch_comments` (DOWNLOAD phase, API): walks the FULL message history via
QuipClient.paginate_messages (the "25 most recent" claim is wrong - see CLAUDE.md) and
returns token-scrubbed rows to store in the raw store.

`render_comments_markdown` (PROCESS phase, local): turns stored rows into readable Markdown.
No API.
"""

from __future__ import annotations

import logging
from typing import Any

from .logging_setup import scrub_text
from .quip_client import QuipClient
from .utils import usec_to_iso

log = logging.getLogger("quip_vault_exporter.comments")


def fetch_comments(
    *,
    client: QuipClient,
    thread_id: str,
    user_names: dict[str, str] | None = None,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch all messages for a thread (full pagination). Returns rows + truncation flag.

    `truncated` stays False unless pagination was cut short by an error - a real signal for
    the manifest, not a guess.
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

    rows = [
        {
            "id": m.get("id"),
            "author": user_names.get(m.get("author_id", ""), m.get("author_id")),
            "created_at": usec_to_iso(m.get("created_usec")),
            "text": scrub_text(m.get("text", ""), secrets),
            "raw": m,
        }
        for m in messages
    ]
    return {"rows": rows, "count": len(messages), "truncated": truncated}


def render_comments_markdown(title: str, rows: list[dict[str, Any]]) -> str:
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
