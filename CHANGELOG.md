# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-06-01

First production-ready release. Read-only export of a Quip workspace into a local,
Obsidian-ready Markdown vault with an audit manifest.

### Pipeline (resilient, download-then-process)
`export` runs four explicit phases so it leans on the (heavily rate-limited) Quip API as
little as possible and is robust for very large accounts:

1. **SCAN** (API): build and persist Quip's folder/sub-folder tree. Structure is preserved
   end to end (canonical parent, all parents recorded, cycles guarded, orphans -> `_unfiled/`).
2. **DOWNLOAD** (API): fetch every document once (body, comments, attachments, optional
   PDF/XLSX) into a persistent local raw store under `output_dir/_raw/`, marking per-document
   state. Fully **resumable**: a crashed/cancelled/interrupted run continues where it left off
   (already-downloaded documents are skipped, so API work is never repeated).
3. **LINK MAP** (local): resolve paths and Obsidian wikilinks.
4. **PROCESS** (local, no API): render the ordered vault from the raw store. Fast and
   **re-runnable** - rendering can be redone locally without re-consuming the API.

Validated at scale: 12,000 documents across 2,000 folders complete with a single fetch per
document and full folder-structure preservation. Memory is dominated by a small per-document
index (a few tens of MB at ~12k docs); document bodies stream through the disk raw store and
are never all held in RAM.

### Added
- **CLI** (`init`, `inventory`, `export`, `verify`, `report`, `resume`, `serve`) built on Typer.
- **Local web UI** (`serve`): a FastAPI + single-page app to paste a token, validate it, pick
  options, choose the output folder via a built-in server-side folder picker, and run the
  export with **live progress + ETA**, **pause/resume**, and **cancel** (cooperative). Token
  stays in memory (optionally remembered in `.env`); binds to localhost by default. Install
  with the `[web]` extra.
- **Live progress + ETA** per phase, in both the CLI logs (explicit `PHASE 1/4..4/4` banners)
  and the web UI.
- **Adaptive rate limiting** honoring Quip's real limits: 50 req/min **and** 750 req/hour per
  token, plus a shared 600/min company budget, enforced proactively. Backoff is driven by the
  `X-Ratelimit-Reset` epoch header (Quip returns HTTP 503, not 429, and sends no
  `Retry-After`); auto-pauses are logged ("auto-pausing ~Ns until reset").
- **Read-only Quip client** with a fail-closed method+endpoint allowlist.
- **Full comment pagination** via the `max_created_usec` cursor (25/100 is a page size, not a
  cap - boundary timestamps are never dropped).
- **Two-pass Obsidian link map** with deterministic, collision-safe filenames; unresolved
  links fall back to the original Quip URL.
- **Attachment/image download** with reference rewriting to Obsidian embeds.
- **Async PDF export** (start -> poll -> download) and **spreadsheet** export (XLSX + locally
  derived CSV + Markdown wrapper).
- **Admin / organization export** (users, folders, permissions) gated behind
  `--i-understand-admin-scope`, with `--no-users` / `--no-permissions` / `--redact-emails`
  minimization and an audit record.
- **On-disk verification** (broken embeds/wikilinks, empty notes, PDF failures) plus executive
  summary and cancellation-checklist manifests.
- **Token redaction** enforced as a logging filter + raw-body/URL scrubber.
- Dockerfile (multi-stage, non-root, pinned `requirements.lock`), docker-compose, and CI.

### Security
- `safe_join` containment check + object-id validation prevent path traversal / zip-slip.
- SSRF guard on signed-URL downloads (HTTPS to non-private hosts only); the bearer token is
  never sent to export/blob hosts, nor written to logs, the raw store, or any vault file.
- SQL identifier allowlist on state writes.

### Known limitations
- Quip is being retired by Salesforce; the API stops at the Read-Only phase - finish your
  export before your tenant's subscription lapses.
- The exact request/response field names for async PDF export and the Admin API
  (organization/permissions) vary by tenant; validate against your live tenant (they are
  centralized in `quip_client.py` / `org.py`).
- Re-running `export` (or `resume`) continues an interrupted run: documents already exported
  are skipped. It does not re-detect content that changed in Quip since the last run; use
  `--force` for a full re-download and re-render.
