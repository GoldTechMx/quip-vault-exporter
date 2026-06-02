"""Structured logging with token redaction.

"Never log tokens" is enforced as a MECHANISM, not a guideline:
  * a logging.Filter scrubs known token values and auth params from every record;
  * `scrub_text` / `scrub_url` strip secrets from raw bodies and signed URLs before they
    are persisted to .raw.json or asset paths.
A unit test (tests/test_redaction.py) asserts a token never survives into a log record.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from rich.logging import RichHandler

REDACTED = "***REDACTED***"

# Auth-bearing query params and header fragments that must never be persisted/logged.
_PARAM_PATTERNS = [
    re.compile(r"(?i)(access_token|admin_token|token|signature|sig|x-amz-credential)=[^&\s\"']+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
]


# Real Quip tokens are long random strings. Skip redacting very short "secrets" so a
# pathologically short value can't garble unrelated text (every "x" -> REDACTED).
_MIN_SECRET_LEN = 8


def scrub_text(text: str, secrets: list[str]) -> str:
    """Remove configured token values and auth params from arbitrary text."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            text = text.replace(secret, REDACTED)
    for pat in _PARAM_PATTERNS:
        text = pat.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return text


def scrub_url(url: str, secrets: list[str]) -> str:
    """Drop the query string from signed blob/PDF URLs before persisting them.

    Signed Quip/S3-style URLs carry time-limited auth in the query string. We keep the
    path (useful for provenance) but discard credentials entirely.
    """
    if not url:
        return url
    parts = urlsplit(url)
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return scrub_text(cleaned, secrets)


class RedactionFilter(logging.Filter):
    """Scrubs secrets from every log record's message and args."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def set_secrets(self, secrets: list[str]) -> None:
        """Update the redaction set at runtime (e.g. when the web UI receives a token)."""
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub_text(str(record.msg), self._secrets)
            args = record.args
            if isinstance(args, Mapping):
                # %(name)s-style args: scrub string values, keep the rest (preserves %d/%f).
                record.args = {
                    k: (scrub_text(v, self._secrets) if isinstance(v, str) else v)
                    for k, v in args.items()
                }
            elif args:
                record.args = tuple(
                    scrub_text(a, self._secrets) if isinstance(a, str) else a for a in args
                )
        except Exception:  # never let logging crash the export
            record.msg = REDACTED
            record.args = ()
        return True


def configure_logging(secrets: list[str], level: int = logging.INFO) -> logging.Logger:
    """Install the redaction filter on the root logger and return the app logger."""
    redaction = RedactionFilter(secrets)

    handler = RichHandler(rich_tracebacks=True, show_path=False)
    handler.addFilter(redaction)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.addFilter(redaction)  # belt and suspenders
    root.setLevel(level)

    # HTTPX can log full request URLs (with signed query strings) at DEBUG — keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return logging.getLogger("quip_vault_exporter")
