"""Pass 1 of the two-pass wikilink resolution.

Builds the complete `thread_id -> {title, vault_path}` map for the WHOLE export set before
any document is rendered. Pass 2 (obsidian.py) resolves link tokens against this finished
map. The map must include every exportable thread - even ones skipped on an incremental run
- or inbound wikilinks to a skipped doc would break.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .inventory import Inventory
from .sanitize import disambiguate, sanitize_filename
from .state import StateDB


class LinkMap:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._by_thread: dict[str, dict[str, str]] = {}

    def build(self, inventory: Inventory, state: StateDB) -> dict[str, dict[str, str]]:
        naming = self._config.naming
        used_stems: dict[str, str] = {}  # (folder_path/stem) -> thread_id

        # Iterate in a STABLE order (by thread_id) so collision disambiguation is
        # deterministic across runs: the same document always wins the bare name, so
        # inbound wikilinks never break and files are never orphaned between runs.
        ordered = sorted(inventory.threads.values(), key=lambda e: e.thread_id)
        for entry in ordered:
            stem = sanitize_filename(entry.title or "Untitled", naming.max_filename_length)
            key = f"{entry.folder_path}/{stem}".lower()
            if key in used_stems and used_stems[key] != entry.thread_id:
                stem = disambiguate(stem, entry.thread_id, naming.handle_duplicates)
                key = f"{entry.folder_path}/{stem}".lower()
            used_stems[key] = entry.thread_id

            vault_path = str(Path(entry.folder_path) / f"{stem}.md").replace("\\", "/")
            record = {"title": entry.title or "Untitled", "vault_path": vault_path, "stem": stem}
            self._by_thread[entry.thread_id] = record
            state.set_linkmap(entry.thread_id, record["title"], vault_path)

        return self._by_thread

    def get(self, thread_id: str) -> dict[str, str] | None:
        return self._by_thread.get(thread_id)

    @property
    def mapping(self) -> dict[str, dict[str, str]]:
        return self._by_thread
