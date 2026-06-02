"""Cooperative pause/cancel control for long-running jobs.

A `Control` is shared between the web job thread and the inventory/exporter loops. They call
`checkpoint()` between work items: it blocks while paused and raises `Cancelled` if the user
cancelled. Cancellation is cooperative and safe — the current item finishes, state is already
persisted atomically per document, so a cancelled export can be continued later with an
incremental run (completed documents are skipped).
"""

from __future__ import annotations

import threading
import time


class Cancelled(Exception):
    """Raised at a checkpoint when the job was cancelled by the user."""


class Control:
    def __init__(self, *, poll_seconds: float = 0.15) -> None:
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._poll = poll_seconds

    def pause(self) -> None:
        if not self._cancel.is_set():
            self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()  # unblock any thread parked in checkpoint()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set() and not self._cancel.is_set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def checkpoint(self) -> None:
        """Block while paused; raise Cancelled if cancelled. Call between work items."""
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(self._poll)
        if self._cancel.is_set():
            raise Cancelled
