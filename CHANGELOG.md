# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-06-01

First production-ready release. Read-only export of a Quip workspace into a local,
Obsidian-ready Markdown vault with an audit manifest.

### Added
- **CLI** (`init`, `inventory`, `export`, `verify`, `report`, `resume`, `serve`) built on Typer.
- **Local web UI** (`serve`) - a FastAPI + single-page app to paste a token, validate it,
  pick options, choose the output folder via a built-in server-side **folder picker**, and
  run the export with **live progress + ETA**, **pause/resume**, and **cancel** (cooperative;
  re-run with Incremental to continue). Token stays in memory (optionally remembered in
  `.env`), binds to localhost by default. Install with the `[web]` extra.
- **Progress + ETA** for every phase (scanning, reading documents, exporting) in both the
  CLI logs and the web UI, so large exports report how much is left.
- **Double-fetch eliminated at any scale**: thread bodies fetched during the metadata pass
  are written to a local disk cache and reused at export time, so every document is fetched
  from Quip exactly once per run (O(1) memory, works for 100k+ docs) - roughly halving the
  API calls / time on large accounts.
- Rate-limit auto-pauses are now **logged/surfaced** ("auto-pausing ~Ns until reset") so a
  long wait near the limit no longer looks frozen.
- **Read-only Quip client** with a fail-closed method+endpoint allowlist.
- **Adaptive rate limiting** honoring Quip's real limits: 50 req/min **and** 750 req/hour
  per token, plus a shared 600/min company budget - all enforced proactively. Backoff is
  driven by the `X-Ratelimit-Reset` epoch header (Quip returns HTTP 503, not 429, and sends
  no `Retry-After`).
- **Full comment pagination** via the `max_created_usec` cursor (25/100 is a page size, not
  a cap - boundary timestamps are never dropped).
- **Two-pass Obsidian link map** with deterministic, collision-safe filenames; unresolved
  links fall back to the original Quip URL.
- **Attachment/image download** with reference rewriting to Obsidian embeds.
- **Async PDF export** (start → poll → download) and **spreadsheet** export (XLSX + locally
  derived CSV + Markdown wrapper).
- **Admin / organization export** (users, folders, permissions) gated behind
  `--i-understand-admin-scope`, with `--no-users` / `--no-permissions` / `--redact-emails`
  minimization and an audit record.
- **Incremental export & resume** backed by SQLite, with crash-safe atomic writes and
  `in_progress → complete | partial` state transitions.
- **On-disk verification** (broken embeds/wikilinks, empty notes, PDF failures) and
  executive summary / cancellation-checklist manifests.
- **Token redaction** enforced as a logging filter + raw-body/URL scrubber.
- Dockerfile (multi-stage, non-root, pinned `requirements.lock`), docker-compose, and CI.

### Security
- `safe_join` containment check + object-id validation prevent path traversal / zip-slip.
- SSRF guard on signed-URL downloads (HTTPS to non-private hosts only); the bearer token is
  never sent to export/blob hosts.
- SQL identifier allowlist on state writes.

### Known limitations
- Quip is being retired by Salesforce; the API stops at the Read-Only phase - finish your
  export before your tenant's subscription lapses.
- The exact request/response field names for async PDF export and Admin API
  organization/permissions vary by tenant; validate against your live tenant (they are
  centralized in `quip_client.py` / `org.py`).
- `--incremental` detects body, title, and folder changes; comment-only / attachment-only
  changes require a full (non-incremental) run.
