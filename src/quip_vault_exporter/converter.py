"""HTML -> Markdown conversion tuned for Quip.

markdownify gets several Quip-specific things wrong, so we subclass it and pre-process the
soup:
  * Checklists are `data-*`-attributed list items, not GFM task lists -> emit `- [ ]`/`- [x]`.
  * In-document links to other Quip threads are rewritten to STABLE LINK TOKENS of the form
    {{QVE_LINK:<thread_id>|<text>}} before conversion. Pass 2 (obsidian.py) resolves each
    token to a wikilink or a URL fallback once the full link map exists.
  * Code blocks preserve language hints and newlines.

Pandoc remains an optional high-fidelity backend (not wired here; see docs/limitations.md).
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

# Matches a Quip thread id inside an href, e.g. https://tenant.quip.com/abcd1234EFGH
_QUIP_HREF = re.compile(r"https?://[^/]*quip\.com/(?P<id>[A-Za-z0-9]{8,})")

# Token marker deliberately avoids underscores/markdown-special chars so markdownify won't
# escape it (e.g. QVE_LINK would become QVE\_LINK and never match).
LINK_TOKEN_RE = re.compile(r"\{\{QVELINK:(?P<id>[^|}]+)\|(?P<text>[^}]*)\}\}")


class QuipMarkdownConverter(MarkdownConverter):
    """markdownify converter with Quip checklist + code handling."""

    def convert_li(self, el: Tag, text: str, parent_tags: Any = None) -> str:
        checked = _checklist_state(el)
        if checked is not None:
            box = "[x]" if checked else "[ ]"
            return f"- {box} {text.strip()}\n"
        result: str = super().convert_li(el, text, parent_tags)
        return result

    def convert_pre(self, el: Tag, text: str, parent_tags: Any = None) -> str:
        raw_lang = el.get("data-language") or el.get("class") or ""
        if isinstance(raw_lang, list):
            lang = next((c for c in raw_lang if c.startswith("lang") or c.isalpha()), "")
        else:
            lang = raw_lang
        code = el.get_text()
        fence = "```"
        return f"\n{fence}{lang}\n{code.rstrip()}\n{fence}\n\n"


def _checklist_state(el: Tag) -> bool | None:
    """Return True/False if `el` is a Quip checklist item, else None."""
    attrs = " ".join(f"{k}={v}" for k, v in el.attrs.items()).lower()
    raw_class: Any = el.get("class") or []
    class_list = [raw_class] if isinstance(raw_class, str) else list(raw_class)
    classes = " ".join(class_list).lower()
    blob = f"{attrs} {classes}"
    if "checklist" not in blob and "checkbox" not in blob and "data-section-style" not in blob:
        return None
    # Check the NEGATIVE tokens first: "unchecked" contains "checked" and "incomplete"
    # contains "complete", so testing positives first would misclassify them.
    if any(tok in blob for tok in ("unchecked", "incomplete", "todo", "false")):
        return False
    return any(tok in blob for tok in ("checked", "complete", "done", "true"))


def _rewrite_quip_links(soup: BeautifulSoup) -> None:
    """Replace <a> tags pointing at Quip threads with resolvable link tokens."""
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        match = _QUIP_HREF.search(href)
        if not match:
            continue
        text = a.get_text() or match.group("id")
        a.replace_with(f"{{{{QVELINK:{match.group('id')}|{text}}}}}")


def html_to_markdown(html: str) -> str:
    """Convert a Quip thread's rendered HTML to Markdown with link tokens intact."""
    soup = BeautifulSoup(html or "", "html.parser")
    _rewrite_quip_links(soup)
    converter = QuipMarkdownConverter(
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )
    md = converter.convert_soup(soup)
    # Collapse the runs of blank lines markdownify tends to leave behind.
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
