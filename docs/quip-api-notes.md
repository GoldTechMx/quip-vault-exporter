# Quip API notes (validated)

Working notes on the endpoints this tool relies on. Verify anything marked **unverified**
against your live tenant before depending on it.

## Auth
- Bearer token in `Authorization: Bearer <token>`.
- Personal/automation tokens from `https://<tenant>/dev/token`. There is no "read-only"
  token scope, so read-only behavior is enforced *client-side* (method+endpoint allowlist).

## Endpoints used (Automation API v1)
| Purpose | Endpoint | Notes |
|---|---|---|
| Current user | `GET /1/users/current` | Discovery entry point: private/shared/starred folder ids |
| Users | `GET /1/users/{ids}` | Resolve author/owner ids to names |
| Folder | `GET /1/folders/{id}` | `folder`, `member_ids`, `children[]` (thread_id or folder_id) |
| Thread | `GET /1/threads/{id}` | `thread` metadata + rendered `html` |
| Messages | `GET /1/messages/{threadId}?count=&max_created_usec=` | **Paginates** — see below |
| Blob | `GET /1/blob/{threadId}/{blobId}` | Attachment/image bytes (Phase 2) |
| Export start | `POST /1/threads/export/async` | Async PDF/XLSX job → `request_id` (Phase 3) |
| Export poll | `GET /1/threads/export/async?request_id=` | Status + download URL when ready |
| Spreadsheet XLSX | `GET /1/threads/{id}/export/xlsx` | XLSX bytes; CSV derived locally |

## Rate limits (critical for large accounts)
Verbatim headers on **every** response:
- `X-Ratelimit-Limit` / `X-Ratelimit-Remaining` / `X-Ratelimit-Reset` (per user)
- `X-Company-RateLimit-Limit` / `-Remaining` / `-Reset` (per company)

`*-Reset` is a **UTC epoch timestamp**. Documented ceilings per token: **50 requests/minute
AND 750 requests/hour**; **600/minute company-wide**. The *hourly* budget is the real
constraint at scale — at a flat 50/min you exhaust 750 in ~15 minutes and stall.

Throttling returns **HTTP 503 "Over Rate Limit"** (NOT 429) and Quip sends **no
Retry-After** — back off using `X-Ratelimit-Reset`. The client also reads
`X-Ratelimit-Remaining` on every response and pauses proactively before hitting 503, so a
multi-thousand-file export glides just under the limits instead of repeatedly stalling.

## Messages pagination (critical)
- `count` accepts up to **100** per call (default 25).
- `max_created_usec` is a backward cursor (UNIX microseconds). Set it to
  `oldest_created_usec_in_prior_page - 1` to page backward without re-fetching the boundary.
- Loop until a short/empty page. **There is no 25-message cap.**

## Export (Phase 3)
- PDF export is **asynchronous**: `POST /1/threads/export/async` → `request_id`; poll
  `GET /1/threads/export/async?request_id=` until success/failure; download the signed URL.
  A large doc can take up to ~10 minutes.
- The signed download URL points at a different host — the bearer token is **never** sent
  there (`download_url` uses a separate unauthenticated client).
- The start POST is a server-side render job — it does **not** mutate user content. It is
  the only non-GET call the read-only client permits (`allow_export`).
- **Verify the exact request-body / response-field names against your tenant's API
  reference.** They are centralized in `quip_client.py`; the poller in `pdf.py` is
  endpoint-agnostic and unit-tested.
- Spreadsheet PDF caps at ~40,000 cells and excludes charts. CSV is **not** a native
  export — derived locally from XLSX via openpyxl.

## Admin API (Phase 4)
- Requires enablement by Salesforce/Quip support and a separate admin token.
- Higher ceilings (~100/min, 1500/hr; bulk export ~36,000 docs/hr).
- **Unverified:** exact org/users/permissions field shapes — validate live.

## Reference implementation
- `github.com/quip/quip-api` (+ `baqup`) is official but stale, has no resume, and has
  known 503 failures (issue #44). Used as reference only; traversal + retry/backoff are
  reimplemented here.
