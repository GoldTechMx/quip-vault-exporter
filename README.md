<div align="center">

# 📦 Quip Vault Exporter

### Turn an entire Quip workspace into a local, portable, Obsidian-ready Markdown vault.

**Read-only. Resumable. Built for accounts with thousands of documents.**

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![Mode](https://img.shields.io/badge/Quip%20access-read--only-success)
![UI](https://img.shields.io/badge/interface-CLI%20%2B%20Web%20UI-8a2be2)

By **[GoldTech MX](https://goldtech.mx)** · [github.com/GoldTechMx](https://github.com/GoldTechMx)

</div>

---

A read-only tool (**CLI and a simple local web UI**) that exports a Quip workspace into a
local Markdown vault you can open directly in Obsidian: documents, comments, attachments,
spreadsheets, optional PDFs, admin/organization metadata, and an audit-ready manifest. It is
a defensible backup for teams leaving Quip, with adaptive rate-limiting, live progress + ETA,
pause/resume/cancel, and resumable re-runs.

> ### ⚠️ Quip is being retired - this is a time-boxed migration aid
> Salesforce has announced Quip end-of-life (announced 2026-03-04; no renewals after
> 2027-03-01; then Read-Only, then Blocked, then Deletion). **The Quip API stops working once
> your tenant reaches the Read-Only phase.** Finish your export before your subscription
> lapses.

---

## ✨ What you get

- 🗂️ **Full workspace export**: every accessible folder and document, recursively, with cycle
  detection and de-duplication.
- 📝 **Obsidian-ready Markdown**: YAML frontmatter, `[[wikilinks]]` between docs, and
  `![[embeds]]` for images/attachments. Open the output folder as a vault. No conversion step.
- 💬 **Complete comment history** (not just the latest 25 - it paginates through everything).
- 📎 **Attachments, spreadsheets (XLSX + CSV), and optional PDFs**.
- 🧾 **Audit manifest**: inventory, errors, verification report, executive summary, and a
  cancellation checklist - every exported file traces back to its Quip source ID.
- 🔁 **Resumable**: a crashed, cancelled, or interrupted run continues where it left off
  (already-exported documents are skipped; use `--force` for a full re-download).
- 🛡️ **Safe by design**: strictly read-only, tokens never logged, exports contained to your
  output folder.

---

## 🚀 Quick start

### Option A - Web UI (easiest, just paste your token)

```bash
pip install "quip-vault-exporter[web]"     # Python 3.11+
quip-vault-exporter serve                  # opens http://127.0.0.1:8000  (localhost only)
```

Open <http://127.0.0.1:8000>, paste your token (from `https://<tenant>.quip.com/dev/token`),
click **Validate token**, choose an output folder, and hit **Export**. You get a live
progress bar with ETA, **Pause / Resume / Cancel**, and a server-side folder picker. Tick
**Remember in .env** and it auto-connects next time.

### Option B - Command line

```bash
git clone https://github.com/GoldTechMx/quip-vault-exporter.git
cd quip-vault-exporter
pip install -e .
cp .env.example .env            # set QUIP_ACCESS_TOKEN (and QUIP_BASE_URL if not platform.quip.com)
quip-vault-exporter init        # writes config.yml
quip-vault-exporter export      # runs inventory + export + writes the vault
quip-vault-exporter verify
```

> 💡 You do **not** need to run `inventory` before `export` - `export` does it internally.
> Running both separately just repeats the (slow) scan.

---

## 📊 What to expect at scale (worked example)

Here is a real-world example based on a large workspace:

> **~350 root folders · ~2,010 nested folders · ~11,638 documents.**

A first full export of an account this size takes **roughly a day or more**. That is not the
tool being slow: **Quip caps API usage at ~750 requests/hour per token** (a hard, server-side
limit). Every document needs at least one request, so the math dominates everything else.

> 🖥️ **Your hardware does not change this.** An i9 with 128 GB RAM and a 10 GbE NAS finishes
> at the same speed as a laptop, because the wall is Quip's rate limit, not your CPU, RAM, or
> network. Memory use stays flat regardless of account size (documents are streamed through a
> local disk cache, never held in RAM all at once).

### The four phases, and when files appear

| Phase | What it does | What you see | Vault written yet? | Rough time (~11,600 docs) |
|------:|--------------|--------------|:------------------:|---------------------------|
| **1. Scan** | Builds Quip's folder tree | `Scanning folders... N folders, M documents found` | not yet (structure saved) | ~3 h (~2,400 folder reads) |
| **2. Download** | Fetches every document once to a local raw store (**resumable**) | `Downloading... 6040/11638 (52%) · ETA 7h` | not yet (raw saved under `_raw/`) | ~16 h, +~16 h if Comments on (one read each) |
| **3. Link map** | Resolves paths and wikilinks | brief | not yet | seconds (local, no API) |
| **4. Process** | Renders the ordered vault from the raw store | `Writing vault N/11638` + **files appear** | ✅ **vault written here** | minutes (local, no API) |

> ### ⏳ Why you do not see the Markdown for many hours
> The Markdown/HTML vault is written in **Phase 4 (Process)**. During Phase 2 the staged data
> accumulates under `_raw/` (and any attachments under `_assets/`), so the output folder is not
> empty - but the readable `.md` notes appear in Phase 4. This is expected: **do not assume it
> is stuck.** The log shows the current phase (`PHASE 2/4 - DOWNLOAD: ...`) and a live count.

> ### 🔁 It is resumable and re-runnable
> Downloads are saved per document, so a crashed, cancelled, or interrupted run **continues
> where it left off** when you run it again (already-downloaded documents are skipped - the
> rate-limited API work is never repeated). And because rendering is local, the vault can be
> re-built from the raw store without re-downloading.

### Rough totals for ~11,600 documents

| Configuration | Approx. total time |
|---|---|
| Documents only (Comments + Attachments **off**) | ~19 h (scan + download + local render) |
| With **Comments** (one more read per doc) | ~+16 h |
| With Attachments / PDFs | additional, depends on file count |

Subsequent runs are far faster: completed documents are skipped.

> 💾 **Tip for very large accounts:** export to a **local disk first**, then copy the finished
> vault to your NAS/SharePoint. Writing tens of thousands of small files directly to a network
> share is slower.

---

## 🧭 How it works

```
   QUIP ──API──▶ [1] Scan folders ──▶ [2] Download every doc once  ──▶  _raw/ (resumable)
                                                                          │
   Obsidian ◀──writes── [4] Process (render vault, local) ◀── [3] Build link map
```

- Phases 1-2 are the API-bound, rate-limited part (the long wait), and are **resumable**.
- Phases 3-4 are fully local: each document is fetched from Quip **once**, then rendered from
  the local raw store. Memory is a small per-document index (a few tens of MB at ~12k docs);
  bodies stream through disk, never all in RAM. Validated at 12,000 documents.

---

## 🛡️ Rate limits are handled automatically

The client respects Quip's documented limits (**50/min and 750/hour per token; 600/min
company-wide**) and **auto-pauses and resumes** when it nears a limit or hits an HTTP 503,
backing off until the `X-Ratelimit-Reset` time. These waits are logged
(`auto-pausing ~Ns until reset`), so a long wait near the limit never looks frozen. Nothing
for you to configure.

## ⏸️ Pause, resume, cancel

In the web UI you can **Pause/Resume** or **Cancel** a running export at any time. Cancel is
safe: completed documents are kept, and simply **re-running Export** continues where
it left off (no re-downloading what is already done).

---

## 🗂️ Output layout

```
exports/quip-vault/
├─ Clients/Client A/Document Title.md            # frontmatter + wikilinks
├─ Clients/Client A/Document Title.html
├─ Clients/Client A/Document Title.raw.json      # token-scrubbed
├─ Clients/Client A/_comments/Document Title.comments.{json,md}
├─ _assets/<thread_id>/image.png                 # embedded as ![[...]]
├─ _unfiled/Orphan Doc.md                        # documents with no folder
├─ _manifest/{inventory,folders,errors}.{json,csv}
├─ _manifest/{verification_report,export_summary,cancellation_checklist}.md
└─ _quip_export_state.sqlite                      # resume state (per-document)
```

## 🪪 Obsidian

There is **no separate command** for Obsidian: the normal export **is** the Obsidian vault.
The `.md` files carry YAML frontmatter, `[[wikilinks]]`, and `![[embeds]]`. When the export
finishes, just **open the output folder as a vault** in Obsidian. Internal Quip links that
point at documents outside the export fall back to the original Quip URL, so nothing is
silently lost.

## 🔐 Token & security

- Get a personal token from `https://<tenant>.quip.com/dev/token`.
- The token lives **only** in `.env` (or the web server's memory), never in `config.yml`, the
  state DB, logs, or any exported file.
- Strictly **read-only**: the tool never creates, modifies, moves, or deletes anything in Quip.
- See [`SECURITY.md`](SECURITY.md) for the full security model and operator responsibilities
  (the export is sensitive at rest: keep it on encrypted media, especially with admin/PII export).

```env
QUIP_ACCESS_TOKEN=...
QUIP_BASE_URL=https://platform.quip.com   # enterprise tenants may differ
```

## 🐳 Docker

```bash
# Web UI
docker compose --profile web up web --build      # then open http://127.0.0.1:8000

# One-shot CLI export (batch)
docker compose run --rm exporter export
```

Production image: multi-stage, **non-root**, pinned `requirements.lock`. Pushing a `vX.Y.Z`
tag builds and publishes the wheel and image to GHCR (see `.github/workflows/release.yml`).

## 👤 Admin / organization export

With `QUIP_ADMIN_MODE=true` + a separate `QUIP_ADMIN_TOKEN`, the export can include org-level
metadata (users, folders, permissions). Because it reads the **whole tenant**, it is gated
behind `--i-understand-admin-scope`, and supports `--no-users` / `--no-permissions` /
`--redact-emails` for data minimization. Requires Admin API enablement (via Salesforce/Quip
support); exact field shapes vary by tenant.

## ✅ Verify & manifests

`verify` walks the finished vault and reports broken embeds/wikilinks, empty notes, comment
truncations, and PDF failures into `_manifest/verification_report.md`. `report` writes an
executive `export_summary.md`. A `cancellation_checklist.md` is generated so you can safely
sign off before canceling Quip.

## 🧯 Troubleshooting

| Symptom | Fix |
|---|---|
| `QUIP_ACCESS_TOKEN is not set` | Copy `.env.example` to `.env` and set the token. |
| No Markdown for hours | Expected: `_raw/` fills during Phase 2 (Download); the `.md` vault is rendered in Phase 4 (Process). Check the log for the current `PHASE`. |
| `HTTP 503 Over Rate Limit` | Normal under load - the client auto-pauses and resumes. |
| Wrong tenant host | Set `QUIP_BASE_URL` (enterprise tenants are not all on `platform.quip.com`). |
| Slow writes to a NAS | Export to a local disk, then copy the vault to the share. |

---

## 📄 License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Copyright © 2026 GoldTech MX.
Unaffiliated with Quip / Salesforce.

<div align="center">

---

Proudly Powered By **[GoldTech MX](https://goldtech.mx)**

[goldtech.mx](https://goldtech.mx) · [github.com/GoldTechMx](https://github.com/GoldTechMx)

</div>
