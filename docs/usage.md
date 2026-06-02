# Usage

## Install
```bash
pip install -e ".[dev]"   # Python 3.11+
```

## First run
```bash
cp .env.example .env       # set QUIP_ACCESS_TOKEN (and QUIP_BASE_URL if not platform.quip.com)
quip-vault-exporter init   # writes config.yml from the example
```

## Commands
| Command | What it does |
|---|---|
| `init` | Create `config.yml` and `.env` from the bundled examples. |
| `inventory` | Scan folders/threads; write `_manifest/inventory.{json,csv}`, `folders.json`, `export_plan.md`. |
| `export` | Run the four-phase pipeline: scan, download (resumable), build the link map, render the vault. |
| `verify` | Compare state vs export; write `_manifest/verification_report.md`. |
| `report` | (Re)write `_manifest/export_summary.md`. |
| `resume` | Continue an interrupted export (skips already-downloaded/completed documents). |

## Useful flags
```bash
quip-vault-exporter export --dry-run   # write nothing; scan only and preview the plan
quip-vault-exporter export --force     # re-download and re-render everything (ignore prior state)
quip-vault-exporter export -c path/to/config.yml
```

## Admin / organization export (Phase 4)
With `QUIP_ADMIN_MODE=true` + `QUIP_ADMIN_TOKEN` set, the export can additionally pull
org-level metadata. Because this reads the **whole tenant** (a bulk PII extract), it is
gated behind an explicit flag and the admin token is kept strictly separate (never falls
back to the personal token):
```bash
quip-vault-exporter export --i-understand-admin-scope
```
Outputs `_manifest/{organization.json,users.json,users.csv,folders.csv,permissions.csv}`.
If the Admin API isn't enabled, it writes `_manifest/admin_api_unavailable.md` instead of
failing. Data minimization:
```bash
quip-vault-exporter export --i-understand-admin-scope --no-users          # skip user PII
quip-vault-exporter export --i-understand-admin-scope --no-permissions    # skip permission PII
quip-vault-exporter export --i-understand-admin-scope --redact-emails     # mask emails
```
The admin run (who/when + which PII categories) is recorded in `organization.json`.

## Output layout
```
exports/quip-vault/
├─ Clients/Client A/Document Title.md          # frontmatter + wikilinks
├─ Clients/Client A/Document Title.html
├─ Clients/Client A/Document Title.raw.json    # token-scrubbed
├─ Clients/Client A/_comments/Document Title.comments.{json,md}
├─ _unfiled/Orphan Doc.md                       # threads with no folder
├─ _manifest/{inventory,folders,errors}.{json,csv}
├─ _manifest/{verification_report,export_summary,cancellation_checklist}.md
├─ _manifest/{organization.json,users.csv,permissions.csv}   # admin mode only
├─ _raw/                                        # staged downloads (resume cache)
└─ _quip_export_state.sqlite                    # resume state (per-document)
```

## Resume & re-run
Every download is staged per document under `_raw/` and recorded in
`_quip_export_state.sqlite`. A crashed, cancelled, or interrupted run continues where it left
off: re-run `export` (or `resume`) and already-downloaded/completed documents are skipped, so
the rate-limited API work is never repeated. Because rendering is local, the vault can be
re-built from `_raw/` without re-downloading. Use `--force` to ignore prior state and
re-download + re-render everything.
