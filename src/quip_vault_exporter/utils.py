"""Crash-safe filesystem helpers and small shared utilities.

Atomic writes are mandatory (see CLAUDE.md): write to a temp file in the same directory,
fsync, then os.replace() into place. A crash mid-write therefore never leaves a truncated
file that `verify` would accept.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC
from pathlib import Path
from typing import Any

# Quip object ids are alphanumeric (sometimes with -/_). Reject anything that could act as a
# path component (e.g. "..", "/") before it reaches the filesystem.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_id(value: str) -> bool:
    return bool(_SAFE_ID.match(value or ""))


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def measure_write_speed(directory: Path, sample_mb: int = 4) -> float:
    """Write a sample file to `directory` and return throughput in MB/s (best-effort).

    Used to warn when the output dir is a slow network share (a common bottleneck), since the
    vault is written there. Returns 0.0 on failure (treated as 'unknown').
    """
    import time as _t

    ensure_dir(directory)
    data = b"\0" * (sample_mb * 1024 * 1024)
    tmp = directory / ".qve_write_test.tmp"
    try:
        start = _t.monotonic()
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        elapsed = _t.monotonic() - start
    except OSError:
        return 0.0
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return (sample_mb / elapsed) if elapsed > 0 else 0.0


class PathTraversalError(Exception):
    """Raised when a derived path would escape the output root."""


def safe_join(root: Path, *parts: str) -> Path:
    """Join under `root` and assert the result stays inside it (anti zip-slip).

    A backstop against malicious/garbage Quip ids or folder titles reaching the filesystem:
    even if upstream validation is bypassed, a write can never escape `output_dir`.
    """
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathTraversalError(f"Path {candidate} escapes output root {root_resolved}")
    return candidate


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to `target` (temp -> fsync -> os.replace)."""
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def atomic_write_text(target: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(target, text.encode(encoding))


def atomic_write_json(target: Path, obj: Any) -> None:
    atomic_write_text(target, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def usec_to_iso(usec: int | float | None) -> str | None:
    """Convert a Quip UNIX-microsecond timestamp to an ISO-8601 UTC string."""
    if usec is None:
        return None
    from datetime import datetime

    return (
        datetime.fromtimestamp(float(usec) / 1_000_000, tz=UTC).isoformat().replace("+00:00", "Z")
    )
