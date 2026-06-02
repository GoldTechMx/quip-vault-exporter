"""Disk-backed, single-fetch cache of full thread responses.

Shared between the metadata pass (which `put`s each fetched thread) and the export pass
(which `pop`s it back, then deletes it). This means every document is fetched from Quip
exactly ONCE per export run, regardless of account size:

  * stored in a LOCAL temp dir (not RAM, not the possibly-remote/slow output dir), so memory
    stays O(1) even for 100k+ documents;
  * best-effort: if a cache file is missing/corrupt, the exporter simply re-fetches that one
    document, so the cache can never cause data loss.

The cached payload is the raw thread response (document HTML + metadata); it contains no
access token and no signed URLs, and the temp dir is removed when the run finishes.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any


class ThreadCache:
    def __init__(self, root: Path | str | None = None) -> None:
        self.dir = Path(root) if root else Path(tempfile.mkdtemp(prefix="qve-cache-"))
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, thread_id: str, resp: dict[str, Any]) -> None:
        # Best-effort; on any write failure the exporter simply re-fetches that document.
        with contextlib.suppress(OSError):
            (self.dir / f"{thread_id}.json").write_text(json.dumps(resp), encoding="utf-8")

    def pop(self, thread_id: str) -> dict[str, Any] | None:
        f = self.dir / f"{thread_id}.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        f.unlink(missing_ok=True)
        return data if isinstance(data, dict) else None

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
