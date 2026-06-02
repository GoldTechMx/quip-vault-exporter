# Limitations & known caveats

These are the honest boundaries of what the tool can guarantee. They trace directly to the
validation report (`../../VALIDATION_REPORT.md`).

## Platform / lifecycle
- **Quip is being retired.** EOL announced 2026-03-04; no renewals after 2027-03-01; then
  Read-Only → Blocked → Deletion. **The API stops at the Read-Only phase** — export before
  your tenant gets there.

## Comments
- Comments ARE exported in full via pagination (`count=100` + `max_created_usec` cursor).
  The widely-repeated "25 most recent" figure is Quip's default **page size**, not a cap.
- Per-document comment visibility still depends on your token's access.
- The manifest records the true message count and flags any thread where pagination was cut
  short by an error.

## Rate limits
- Per-token ceilings: **50 requests/minute AND 750 requests/hour**; **600/minute**
  company-wide (admin tokens ~100/min + 1500/hr). The **hourly** budget is the binding
  constraint for large accounts.
- Throttling is **HTTP 503 "Over Rate Limit"** (not 429). Quip sends **no Retry-After** —
  the client backs off using `X-Ratelimit-Reset` (a UTC epoch) and pauses proactively when
  `X-Ratelimit-Remaining` is low, so big exports glide under the limits.

## Spreadsheets (Phase 3)
- Exportable as XLSX/PDF/HTML/JSON via the API. **CSV is not a native export** — it is
  derived locally from XLSX (openpyxl).
- PDF rendering caps at ~40,000 cells and excludes charts.

## Admin / organization (Phase 4)
- Requires Admin API enablement and a separate admin token.
- **The exact field-level output of the Admin API for organization/permissions is
  unverified from public docs** and must be validated against a live admin tenant before
  relying on `organization.json` / `permissions.csv`. If unavailable, the tool writes
  `_manifest/admin_api_unavailable.md`.

## HTML → Markdown fidelity
- Checklists, nested tables, and code blocks are handled by a custom converter, but
  extremely complex Quip layouts may lose structure. Pandoc is an optional higher-fidelity
  backend (install separately; GPL, used as an external binary only).

## Base URL
- Enterprise/Salesforce-provisioned tenants may not be on `platform.quip.com`. Always set
  `QUIP_BASE_URL` to your instance.
