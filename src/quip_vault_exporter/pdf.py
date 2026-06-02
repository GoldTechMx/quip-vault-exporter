"""Asynchronous PDF export (Phase 3).

Quip's PDF export is asynchronous: start a render job (returns a request_id), poll until it
reports success/failure, then download the resulting PDF URL. A large document can take up
to ~10 minutes. The poller here is deliberately endpoint-agnostic — it consumes whatever
status dict `QuipClient.poll_pdf_export` returns and tolerates the common field-name
variants — so it is fully unit-tested without a live tenant.

Safety properties for large-account runs:
  * start/poll go through the client's shared "export" rate budget (counts against the same
    per-minute / per-hour ceilings as everything else);
  * a per-job wall-clock timeout prevents a stuck job from blocking the whole export;
  * a failed/timed-out PDF is recorded and skipped — it never aborts the document;
  * the signed PDF URL is downloaded WITHOUT the auth token (see client.download_url).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import atomic_write_bytes

log = logging.getLogger("quip_vault_exporter.pdf")

_SUCCESS = {"success", "complete", "completed", "done", "ready"}
_FAILED = {"failed", "failure", "error"}


@dataclass
class PdfResult:
    thread_id: str
    status: str  # "complete" | "failed" | "timeout"
    path: str | None = None
    error: str | None = None


class PdfExporter:
    def __init__(
        self,
        client: Any,
        *,
        poll_interval: float = 15.0,
        timeout: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep
        self._mono = monotonic

    def export(self, thread_id: str, dest: Path) -> PdfResult:
        try:
            request_id = self._client.start_pdf_export([thread_id])
        except Exception as exc:
            return PdfResult(thread_id, "failed", error=f"start failed: {exc}")

        deadline = self._mono() + self._timeout
        while self._mono() < deadline:
            try:
                status = self._client.poll_pdf_export(request_id)
            except Exception as exc:
                return PdfResult(thread_id, "failed", error=f"poll failed: {exc}")

            state = _normalize_status(status)
            if state in _SUCCESS:
                url = _extract_pdf_url(status, thread_id)
                if not url:
                    return PdfResult(thread_id, "failed", error="no pdf url in response")
                try:
                    data = self._client.download_url(url)
                except Exception as exc:
                    return PdfResult(thread_id, "failed", error=f"download failed: {exc}")
                atomic_write_bytes(dest, data)
                return PdfResult(thread_id, "complete", path=str(dest))
            if state in _FAILED:
                return PdfResult(thread_id, "failed", error=str(status)[:200])
            # still pending -> wait and poll again
            self._sleep(self._poll_interval)

        return PdfResult(thread_id, "timeout", error=f"not ready within {self._timeout}s")


def _normalize_status(status: dict[str, Any]) -> str:
    raw = status.get("status") or status.get("state") or ""
    return str(raw).strip().lower()


def _extract_pdf_url(status: dict[str, Any], thread_id: str) -> str | None:
    """Tolerant extraction of the PDF URL across documented response shapes."""
    for key in ("pdf_url", "url", "export_url", "download_url"):
        if status.get(key):
            return str(status[key])
    threads = status.get("threads")
    if isinstance(threads, dict):
        entry = threads.get(thread_id)
        if isinstance(entry, dict):
            for key in ("pdf_url", "url", "export_url"):
                if entry.get(key):
                    return str(entry[key])
        elif isinstance(entry, str):
            return entry
    return None
