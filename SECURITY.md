# Security Policy

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories
("Report a vulnerability") rather than a public issue. We aim to acknowledge within 72
hours. Do not include real tokens, PII, or exported customer data in reports.

## Security model

This tool is **read-only** and handles sensitive material (Quip access tokens, exported
workspace content, and - in admin mode - personal data). The design enforces:

- **Read-only, fail-closed networking.** `quip_client.py` only issues GET requests to an
  allowlisted set of path prefixes; the single non-GET path (async export jobs, which do
  not mutate user content) is explicitly allowlisted. Anything else raises.
- **Token redaction as a mechanism, not a rule.** A root-logger filter scrubs configured
  token values and auth params from every log record; raw API bodies and signed URLs are
  scrubbed before being persisted. Client DEBUG logging is disabled. Tokens live only in
  the environment (`.env`), never in `config.yml` or the SQLite state DB.
- **No token egress to third-party hosts.** Signed export/blob URLs are downloaded with a
  separate, unauthenticated HTTP client, and only over HTTPS to non-private hosts (SSRF
  guard blocks `localhost`, loopback, link-local, and private IP ranges).
- **Path-traversal containment.** Object ids are validated and every write is contained
  under `output_dir` via a resolved-path check (anti zip-slip).
- **Admin scope is gated.** Org/admin export requires a separate `QUIP_ADMIN_TOKEN` (never
  falls back to the personal token) and the explicit `--i-understand-admin-scope` flag.

## Operator responsibilities (the export is sensitive at rest)

- Treat the export tree as confidential: it contains workspace content, and - in admin mode
  - user and permission data (PII). Store it on encrypted-at-rest media; consider
  `--redact-emails` / `--no-users` / `--no-permissions` for data minimization.
- Keep `.env` out of version control (the provided `.gitignore` does this) and rotate any
  token that may have been exposed.
- The export root and SQLite state are written with your default umask. On multi-user hosts,
  restrict permissions (e.g. `chmod 700` the export directory) yourself.
