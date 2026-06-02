"""Adaptive, header-driven rate limiting for large-account exports.

Quip's documented limits (per token): **50 requests/minute AND 750 requests/hour**, plus a
**600/minute company-wide** ceiling shared by everyone. For an account with thousands of
files the *hourly* budget is the real constraint - at a flat 50/min you exhaust 750 in ~15
minutes and then stall. So this limiter enforces BOTH a per-minute and a per-hour token
bucket, and additionally adapts to the server's own accounting.

Every response carries the live state (verbatim header names):
  X-Ratelimit-Limit / X-Ratelimit-Remaining / X-Ratelimit-Reset          (per user)
  X-Company-RateLimit-Limit / -Remaining / -Reset                        (per company)
`*-Reset` is a UTC epoch timestamp. Quip returns **HTTP 503** when throttled and does
**not** send Retry-After, so we back off until the relevant `*-Reset`.

`note_response()` feeds those headers back in: when remaining drops to the floor we pause
proactively until reset, so a long export glides just under the limit instead of repeatedly
slamming into 503s. The proactive token buckets are the floor; the headers are the
correction. Time and sleep are injectable for deterministic testing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger("quip_vault_exporter.ratelimit")

# Tolerance for token comparisons; without it, float drift busy-spins near tokens==1.0.
_EPS = 1e-6


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last: float


class RateLimiter:
    def __init__(
        self,
        per_minute: int = 50,
        per_hour: int = 750,
        *,
        company_per_minute: int = 600,
        remaining_floor: int = 2,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._per_minute = max(1, per_minute)
        self._per_hour = max(1, per_hour)
        self._company_per_minute = max(1, company_per_minute)
        self._floor = remaining_floor
        self._mono = monotonic
        self._wall = wall
        self._sleep = sleep
        self._lock = threading.Lock()
        self._buckets: dict[str, list[_Bucket]] = {}
        # The company minute-limit (600/min) is SHARED across every budget, so it gets its
        # own bucket that every acquire() also debits - proactive, not just reactive.
        now0 = self._mono()
        self._company = _Bucket(
            self._company_per_minute,
            self._company_per_minute / 60.0,
            self._company_per_minute,
            now0,
        )
        # Wall-clock epoch before which a budget must not issue requests (from headers/503).
        # The "__company__" key is consulted by every budget.
        self._pause_until: dict[str, float] = {}

    def _buckets_for(self, budget: str) -> list[_Bucket]:
        b = self._buckets.get(budget)
        if b is None:
            now = self._mono()
            b = [
                _Bucket(self._per_minute, self._per_minute / 60.0, self._per_minute, now),
                _Bucket(self._per_hour, self._per_hour / 3600.0, self._per_hour, now),
            ]
            self._buckets[budget] = b
        return b

    def acquire(self, budget: str = "user") -> None:
        """Block until a request slot is available on every budget dimension."""
        while True:
            with self._lock:
                # 1. Respect an active pause window - the budget's own AND the company's.
                pause = max(
                    self._pause_until.get(budget, 0.0),
                    self._pause_until.get("__company__", 0.0),
                )
                wait = pause - self._wall()
                if wait <= 0:
                    # 2. Proactive token buckets: per-minute, per-hour, AND shared company.
                    # EPS guards against float drift: a token asymptotically approaches 1.0
                    # but lands at 0.9999…, which would otherwise busy-spin forever.
                    now = self._mono()
                    dims = [*self._buckets_for(budget), self._company]
                    waits = []
                    for bucket in dims:
                        bucket.tokens = min(
                            bucket.capacity,
                            bucket.tokens + (now - bucket.last) * bucket.refill_per_sec,
                        )
                        bucket.last = now
                        if bucket.tokens < 1.0 - _EPS:
                            waits.append((1.0 - bucket.tokens) / bucket.refill_per_sec)
                    if not waits:
                        for bucket in dims:
                            bucket.tokens -= 1
                        return
                    wait = max(waits)
            self._sleep(min(wait, 1.0))

    def note_response(self, headers: Mapping[str, str], budget: str = "user") -> None:
        """Adapt to a successful response's rate-limit headers (proactive pause)."""
        for prefix, key, scope in (
            ("X-Ratelimit", budget, "rate limit"),
            ("X-Company-RateLimit", "__company__", "company rate limit"),
        ):
            remaining = _to_float(headers.get(f"{prefix}-Remaining"))
            reset = _to_float(headers.get(f"{prefix}-Reset"))
            if remaining is not None and remaining <= self._floor and reset is not None:
                with self._lock:
                    prev = self._pause_until.get(key, 0.0)
                    self._pause_until[key] = max(prev, reset)
                    newly = reset > prev
                wait = max(0.0, reset - self._wall())
                if newly and wait > 1.0:
                    log.info(
                        "Approaching %s (%.0f left); auto-pausing ~%.0fs until it resets.",
                        scope,
                        remaining,
                        wait,
                    )

    def note_throttled(self, headers: Mapping[str, str], budget: str = "user") -> float:
        """A 503 arrived: pause the right scope until reset; return the wait in seconds."""
        wait = 0.0
        with self._lock:
            for prefix, key in (("X-Ratelimit", budget), ("X-Company-RateLimit", "__company__")):
                reset = _to_float(headers.get(f"{prefix}-Reset"))
                if reset is not None:
                    self._pause_until[key] = max(self._pause_until.get(key, 0.0), reset)
                    wait = max(wait, reset - self._wall())
            # No reset header at all → caller falls back to exponential backoff.
            return max(0.0, wait)


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
