"""Filename sanitization and collision handling.

Designed together with the wikilink policy (see CLAUDE.md). Quip titles are not globally
unique and may contain Windows-illegal characters, trailing dots/spaces, or reserved
device names. Sanitize BEFORE collision detection, because sanitization can collapse two
distinct titles into the same string.
"""

from __future__ import annotations

import re
import unicodedata

# Illegal on Windows (and a good cross-platform baseline): <>:"/\|?* and control chars.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

# Windows reserved device names (case-insensitive, with or without extension).
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(title: str, max_length: int = 180) -> str:
    """Turn an arbitrary Quip title into a safe filename stem (no extension).

    Always returns a non-empty string.
    """
    name = unicodedata.normalize("NFC", title or "").strip()
    name = _ILLEGAL.sub("_", name)
    name = _WHITESPACE.sub(" ", name).strip()
    # Windows forbids trailing dots and spaces.
    name = name.rstrip(" .")
    if not name:
        name = "untitled"
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name


def short_id(thread_id: str, length: int = 8) -> str:
    """A short, filesystem-safe slice of a thread id for disambiguation."""
    safe = re.sub(r"[^A-Za-z0-9]", "", thread_id)
    return safe[:length] or "id"


def disambiguate(stem: str, thread_id: str, strategy: str = "append_thread_id") -> str:
    """Make a colliding stem unique. Currently only `append_thread_id` is supported."""
    if strategy == "append_thread_id":
        return f"{stem}-{short_id(thread_id)}"
    raise ValueError(f"Unknown handle_duplicates strategy: {strategy!r}")
