# Quip Vault Exporter

A **read-only** tool (CLI **and** local web UI) that exports a Quip workspace into a local,
portable, Obsidian-ready Markdown vault — with HTML, raw JSON, comments, attachments,
spreadsheets, optional PDFs, admin/organization metadata, and an audit-ready manifest. Built
as a defensible backup for teams leaving Quip, with adaptive rate-limiting, incremental
re-export, resume, and live progress/ETA.

<p align="center">
  By <a href="https://goldtech.mx"><b>GoldTech MX</b></a> ·
  <a href="https://github.com/GoldTechMx">github.com/GoldTechMx</a> ·
  Apache-2.0
</p>

> ## ⚠️ Important: Quip is being retired
> Salesforce has announced Quip end-of-life (EOL announced **2026-03-04**; no renewals
> after **2027-03-01**; then **Read-Only → Blocked → Deletion**). **The Quip API stops
> working once your tenant reaches the Read-Only phase.** This tool is a *time-boxed
> migration aid* — finish your export before your subscription lapses.

> ## Important Limitation
> Quip API access varies by account, permissions, and whether the Admin API is enabled.
> Some organization-wide exports require Quip/Salesforce admin configuration. Comment
> export pages through the **full** message history (the old "25 most recent" figure is a
> page size, not a cap), but per-document visibility still depends on your token's access.

## What it does
- Recursively traverses folders (starred, configured roots, admin-visible) with cycle
  detection and de-duplication.
- Exports each thread as Markdown (YAML frontmatter + Obsidian wikilinks), HTML, and raw
  JSON, written **atomically** (crash-safe).
- Exports the **complete** comment history per thread via API pagination.
- Tracks state in SQLite for **incremental** re-export and **resume**.
- Produces inventory, error, verification, and executive-summary manifests.

## What it cannot do
- It cannot export content your token can't access, or run after your tenant hits
  Read-Only.
- It does not modify, create, move, or delete anything in Quip (read-only by design).
- Admin/org exports require Admin API enablement; some field shapes vary by tenant and
  should be validated against your live tenant (see `docs/quip-api-notes.md`).

## Setup
```bash
git clone https://github.com/GoldTechMx/quip-vault-exporter.git
cd quip-vault-exporter
pip install -e ".[web]"        # Python 3.11+  (use ".[dev]" for development)
cp .env.example .env           # then set QUIP_ACCESS_TOKEN
quip-vault-exporter init       # writes config.yml
```

### Token configuration
Tokens live **only** in `.env` (never in `config.yml` or the state DB):
```env
QUIP_ACCESS_TOKEN=...
QUIP_BASE_URL=https://platform.quip.com   # enterprise tenants may differ
```

## Web UI (easiest — just paste your token)
A simple local web UI lets you paste your Quip token, validate it, pick options, and run
the export with a live progress bar — no terminal needed.

```bash
pip install -e ".[web]"        # or:  pip install 'quip-vault-exporter[web]'
quip-vault-exporter serve      # opens on http://127.0.0.1:8000  (localhost only)
```

Then open <http://127.0.0.1:8000>, paste your token (from
`https://<tenant>.quip.com/dev/token`), click **Validate token**, choose the **output
folder** with the built-in **Browse…** picker (navigates the server's filesystem and can
create a new subfolder), and run **Inventory / Export / Verify** with a live progress bar.
You get **live progress + ETA** per phase, and can **Pause/Resume** or **Cancel** a running
export at any time (cancel is safe — re-run Export with **Incremental** to continue where it
left off). The token stays in the server's memory and is never logged; tick **Remember in
.env** to persist it for CLI reuse. Binds to `127.0.0.1` by default (`--host 0.0.0.0` to
expose it — only behind your own auth/network).

### Rate limits are handled automatically
The client respects Quip's limits (50/min **and** 750/hour per token; 600/min company-wide)
and **auto-pauses and resumes** when it nears a limit or hits an HTTP 503 — backing off until
the `X-Ratelimit-Reset` time. These waits are logged ("auto-pausing ~Ns until reset") so the
UI shows them instead of looking frozen. No action needed on your part.

## Basic export (CLI)
```bash
quip-vault-exporter inventory
quip-vault-exporter export
quip-vault-exporter verify
```

## Obsidian export
With `obsidian.wikilinks: true` (default), internal Quip links resolve to path-aliased
wikilinks `[[folder/path|Title]]` via a two-pass link map. Point Obsidian at
`output_dir` and open it as a vault.

## Docker
```bash
docker compose run --rm exporter inventory
docker compose run --rm exporter export
docker compose run --rm exporter verify
```

## Production deployment
The image is multi-stage, runs as a **non-root** user, and installs from a pinned
`requirements.lock` for reproducible builds.

```bash
# Build (slim, markdownify-only). Use --target full for the Pandoc-enabled image.
docker build -t quip-vault-exporter:latest --target base .

# Run a one-shot export (batch tool — each run performs one command and exits).
docker run --rm \
  --env-file .env \
  -v "$(pwd)/exports:/app/exports" \
  -v "$(pwd)/config.yml:/app/config.yml:ro" \
  quip-vault-exporter:latest export --config /app/config.yml
```

- **Secrets**: provide `QUIP_ACCESS_TOKEN` (and optional `QUIP_ADMIN_TOKEN`) via
  `--env-file .env`; never bake tokens into the image (`.dockerignore` excludes `.env`,
  `config.yml`, and all export output/PII).
- **Persistence**: the vault and SQLite state live on the mounted `./exports` volume, so
  `--incremental` and `resume` work across container runs.
- **Releases**: pushing a `vX.Y.Z` tag builds and publishes the wheel and the image to GHCR
  (see `.github/workflows/release.yml`). See `SECURITY.md` for the security model and
  operator responsibilities, and `CHANGELOG.md` for release notes.

## Admin API notes
Admin mode (`QUIP_ADMIN_MODE=true` + `QUIP_ADMIN_TOKEN`) reads the **whole tenant** and
requires Admin API enablement (via Salesforce/Quip support) plus the explicit
`--i-understand-admin-scope` flag. The admin token is kept strictly separate and never
falls back to the personal token.

## Comment export
Comments are exported in full by paginating with the `max_created_usec` cursor. The
manifest records the true message count and flags any thread where pagination was cut short.

## Safe cancellation checklist
After every export the tool writes `_manifest/cancellation_checklist.md`. Complete it —
including verifying the vault opens in Obsidian and copying the export offsite — **before**
canceling Quip.

## Troubleshooting
- **`Configuration error: QUIP_ACCESS_TOKEN is not set`** — copy `.env.example` to `.env`.
- **HTTP 503 "Over Rate Limit"** — expected under load; the client backs off automatically
  using server headers. Lower `limits.requests_per_minute` if it persists.
- **Wrong base URL** — enterprise tenants aren't all on `platform.quip.com`; set
  `QUIP_BASE_URL`.

## License
Apache-2.0. See `LICENSE` and `NOTICE`. Copyright © 2026 GoldTech MX. Unaffiliated with
Quip/Salesforce.

---

<p align="center">
  Proudly Powered By <a href="https://goldtech.mx"><b>GoldTech MX</b></a><br>
  <a href="https://goldtech.mx">goldtech.mx</a> · <a href="https://github.com/GoldTechMx">github.com/GoldTechMx</a>
</p>
