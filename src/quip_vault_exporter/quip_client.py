"""Read-only Quip API client — the single network choke point.

Design guarantees (see CLAUDE.md):
  * READ-ONLY, fail-closed: only GET requests to an allowlisted set of path prefixes are
    permitted. Export render jobs (transient, non-mutating POSTs) go through an explicit,
    separately-named path so "read-only" can never be violated by accident.
  * 503-aware: Quip signals throttling with HTTP 503 "Over Rate Limit" (NOT 429) and sends
    NO Retry-After. Backoff is driven by the X-(Company-)RateLimit-Reset epoch headers via
    the limiter, with exponential + jitter as the fallback.
  * Pagination is first-class: `paginate_messages` walks the full comment history with the
    `max_created_usec` cursor — 25/100 is a page size, never a cap.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx

log = logging.getLogger("quip_vault_exporter.client")

# Only these GET path prefixes are reachable. Anything else fails closed.
_READ_ALLOWLIST = (
    "/1/users/current",
    "/1/users/",
    "/1/folders/",
    "/1/threads/",
    "/1/messages/",
    "/1/blob/",
)

# Export render jobs: non-mutating server-side POSTs, allowlisted explicitly and used only
# by the (Phase 3) pdf/spreadsheets modules via `start_pdf_export` / `poll_pdf_export`.
# Kept as narrow as possible so a stray POST can never slip through the read-only gate.
_EXPORT_ALLOWLIST = ("/1/threads/export",)

_THROTTLE_STATUS = 503
_MAX_ATTEMPTS = 6


class QuipError(Exception):
    """Network or API error surfaced to callers (already token-scrubbed by logging)."""


class ReadOnlyViolation(QuipError):
    """Raised if code attempts a non-allowlisted or mutating request. Fails closed."""


class QuipClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        rate_limiter: Any | None = None,
        timeout: float = 60.0,
        max_attempts: int = _MAX_ATTEMPTS,
        sleep: Any = time.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._limiter = rate_limiter
        self._max_attempts = max_attempts
        self._timeout = timeout
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        # Lazily-created, reused, UNAUTHENTICATED client for signed export/blob URLs (a
        # different host — the bearer token must never be sent there).
        self._anon: httpx.Client | None = None

    def _anon_client(self) -> httpx.Client:
        if self._anon is None:
            self._anon = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self._anon

    # -- lifecycle ------------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()
        if self._anon is not None:
            self._anon.close()

    def __enter__(self) -> QuipClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- core request with read-only enforcement + 503 backoff ----------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        budget: str = "user",
        allow_export: bool = False,
    ) -> httpx.Response:
        method = method.upper()
        if method != "GET":
            if not (allow_export and path.startswith(_EXPORT_ALLOWLIST)):
                raise ReadOnlyViolation(
                    f"Refusing non-GET request {method} {path} — tool is read-only."
                )
        elif not path.startswith(_READ_ALLOWLIST):
            raise ReadOnlyViolation(f"Path {path} is not on the read allowlist.")

        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            if self._limiter is not None:
                self._limiter.acquire(budget)
            try:
                resp = self._client.request(method, path, params=params, data=data)
            except httpx.TransportError as exc:  # connection/timeout
                last_exc = exc
                self._sleep_backoff(attempt, None, budget)
                continue

            # Feed live rate-limit accounting back to the limiter on EVERY response so a
            # long export paces itself just under the per-minute / per-hour ceilings.
            if self._limiter is not None:
                self._limiter.note_response(resp.headers, budget)

            if resp.status_code == _THROTTLE_STATUS:  # 503 Over Rate Limit (Quip's 429)
                last_exc = QuipError("Throttled (HTTP 503 Over Rate Limit)")
                self._sleep_backoff(attempt, resp, budget)
                continue
            if resp.status_code >= 500:
                last_exc = QuipError(f"Server error {resp.status_code} on {path}")
                self._sleep_backoff(attempt, None, budget)
                continue
            if resp.status_code >= 400:
                # 4xx (auth/not-found/forbidden) is not retryable.
                raise QuipError(f"HTTP {resp.status_code} on {path}: {resp.text[:200]}")
            return resp

        raise QuipError(f"Exhausted retries on {path}: {last_exc}")

    def _sleep_backoff(self, attempt: int, resp: httpx.Response | None, budget: str) -> None:
        # On 503, back off until the server's *-Reset (Quip sends no Retry-After). The
        # limiter both computes the wait and records the pause window for future calls.
        delay = 0.0
        if resp is not None and self._limiter is not None:
            delay = self._limiter.note_throttled(resp.headers, budget)
        if delay <= 0:
            delay = min(60.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
        log.warning("Rate limited; auto-pausing %.1fs then retrying (attempt %d).", delay, attempt)
        self._sleep(delay)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    def _get_dict(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self._get_json(path, params)
        if not isinstance(data, dict):
            raise QuipError(f"Expected a JSON object from {path}, got {type(data).__name__}")
        return data

    # -- read endpoints (Phase 1) ---------------------------------------------------------
    def current_user(self) -> dict[str, Any]:
        return self._get_dict("/1/users/current")

    def get_users(self, ids: list[str]) -> dict[str, Any]:
        return self._get_dict(f"/1/users/{','.join(ids)}")

    def get_folder(self, folder_id: str) -> dict[str, Any]:
        return self._get_dict(f"/1/folders/{folder_id}")

    def get_folders(self, ids: list[str]) -> dict[str, Any]:
        return self._get_dict(f"/1/folders/{','.join(ids)}")

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        """Full thread object — includes rendered HTML under the `html` key."""
        return self._get_dict(f"/1/threads/{thread_id}")

    # -- admin / organization (Phase 4) ---------------------------------------------------
    def company_members(self) -> dict[str, Any]:
        """All company members (admin token), merged across cursor pages.

        GET /1/users/company-members returns a map user_id -> user object plus a `has_more`
        flag and a cursor. Field/cursor names vary by tenant — handled tolerantly. Uses the
        admin rate budget (~100/min, 1500/hr).
        """
        members: dict[str, Any] = {}
        cursor: str | None = None
        for _ in range(10_000):  # hard stop against a server that never sets has_more
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            page = self._get_dict("/1/users/company-members", params=params or None)
            chunk = page.get("members")
            if isinstance(chunk, dict):
                members.update(chunk)
            else:  # some tenants return the map at the top level
                members.update(
                    {
                        k: v
                        for k, v in page.items()
                        if k not in ("has_more", "cursor", "next_cursor")
                    }
                )
            if not page.get("has_more"):
                break
            cursor = page.get("cursor") or page.get("next_cursor")
            if not cursor:
                break
        return members

    def get_blob(self, thread_id: str, blob_id: str) -> bytes:
        resp = self._request("GET", f"/1/blob/{thread_id}/{blob_id}")
        return resp.content

    def get_blob_meta(self, thread_id: str, blob_id: str) -> tuple[bytes, str | None, str | None]:
        """Download a blob, returning (content, content_type, original_filename)."""
        resp = self._request("GET", f"/1/blob/{thread_id}/{blob_id}")
        content_type = resp.headers.get("Content-Type")
        filename = _filename_from_disposition(resp.headers.get("Content-Disposition", ""))
        return resp.content, content_type, filename

    # -- async export (Phase 3) -----------------------------------------------------------
    # NOTE: Quip's async export is documented as POST /1/threads/export/async -> request_id,
    # polled via GET /1/threads/export/async?request_id=...  The exact request-body and
    # response-field names below are centralized here; validate them against your tenant's
    # current API reference. The poller in pdf.py is endpoint-agnostic and fully tested.
    def start_pdf_export(self, thread_ids: list[str]) -> str:
        """Start an async PDF export job; return its request_id."""
        resp = self._request(
            "POST",
            "/1/threads/export/async",
            data={"thread_ids": ",".join(thread_ids), "format": "pdf"},
            budget="export",
            allow_export=True,
        )
        body = resp.json()
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not request_id:
            raise QuipError(f"Export start returned no request_id: {body!r}")
        return str(request_id)

    def poll_pdf_export(self, request_id: str) -> dict[str, Any]:
        """Poll an async export job. Returns the raw status dict (status + per-thread urls)."""
        return self._get_dict("/1/threads/export/async", params={"request_id": request_id})

    def export_xlsx(self, thread_id: str) -> bytes:
        """Export a spreadsheet thread to XLSX bytes."""
        resp = self._request("GET", f"/1/threads/{thread_id}/export/xlsx")
        return resp.content

    def download_url(self, url: str) -> bytes:
        """Download a signed export URL.

        Uses a SEPARATE, reused client with NO Authorization header (signed URLs point at a
        different host; the bearer token must never be sent there) and validates the URL is
        HTTPS to a non-private host first (anti-SSRF — the URL comes from the export poll
        response, which the tool does not fully control).
        """
        if not _is_safe_export_url(url):
            raise QuipError(f"Refusing to download unsafe export URL: {url[:60]}")
        resp = self._anon_client().get(url)
        resp.raise_for_status()
        return resp.content

    # -- comments / messages: FULL pagination, not capped at 25 ---------------------------
    def paginate_messages(self, thread_id: str, page_size: int = 100) -> Iterator[dict[str, Any]]:
        """Yield ALL messages for a thread, newest-first, walking backward.

        25 is only Quip's default page size; `count` accepts up to 100 and
        `max_created_usec` is the backward cursor. We page until a short/empty page.
        """
        cursor: int | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"count": page_size}
            if cursor is not None:
                params["max_created_usec"] = cursor
            page = self._get_json(f"/1/messages/{thread_id}", params=params)
            if not page:
                return
            new_in_page = 0
            oldest: int | None = None
            for msg in page:
                mid = msg.get("id", "")
                created = msg.get("created_usec")
                if oldest is None or (created is not None and created < oldest):
                    oldest = created
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                new_in_page += 1
                yield msg
            # Stop when the page wasn't full or the cursor can't advance.
            if len(page) < page_size or oldest is None or new_in_page == 0:
                return
            # Inclusive cursor: re-fetch the boundary usec (deduped via `seen`) so messages
            # sharing the boundary timestamp across a page boundary are never dropped.
            cursor = oldest


def _is_safe_export_url(url: str) -> bool:
    """Allow only HTTPS URLs to non-private hosts (anti-SSRF for poll-response URLs)."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)  # only when host is a literal IP
    except ValueError:
        return True  # a DNS name to a public host — accepted
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


_DISPOSITION_FILENAME = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _filename_from_disposition(disposition: str) -> str | None:
    match = _DISPOSITION_FILENAME.search(disposition or "")
    if not match:
        return None
    from urllib.parse import unquote

    return unquote(match.group(1)).strip() or None
