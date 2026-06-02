"""Organization / admin metadata export (Phase 4).

Runs only when admin mode is enabled AND the operator passes --i-understand-admin-scope.
Uses an admin-scoped client (QUIP_ADMIN_TOKEN, strictly separate from QUIP_ACCESS_TOKEN -
never falls back) with the admin rate budget (~100/min, 1500/hr).

Outputs:
  _manifest/organization.json   org metadata + the admin-run audit record
  _manifest/users.{json,csv}    company members (PII - opt-out with --no-users)
  _manifest/folders.csv         folder tree (from the already-traversed inventory)
  _manifest/permissions.csv     folder -> member mapping (PII - opt-out with --no-permissions)

COMPLIANCE: users/permissions are personal data (GDPR/CCPA). Export is opt-in; emails can be
redacted (--redact-emails); the manifest records which PII categories were written.

IMPORTANT (unverified from public docs): the exact field shapes of the Admin API for
organization/permissions vary by tenant. Extraction here is tolerant; validate the output
against your live admin tenant before relying on it. If the Admin API is unavailable, we
write _manifest/admin_api_unavailable.md instead of failing the run.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .obsidian import now_iso
from .utils import atomic_write_json, atomic_write_text

log = logging.getLogger("quip_vault_exporter.org")


@dataclass
class OrgResult:
    status: str  # "complete" | "unavailable"
    pii_categories: list[str] = field(default_factory=list)
    users: int = 0
    folders: int = 0
    permission_rows: int = 0
    error: str | None = None


def write_admin_unavailable(manifest_dir: Path, reason: str) -> None:
    atomic_write_text(
        manifest_dir / "admin_api_unavailable.md",
        f"# Admin API unavailable\n\n{reason}\n\n"
        "Org-level export (users, folders, permissions) was skipped. This usually means the "
        "Admin API is not enabled on this Quip instance, or the admin token lacks scope. "
        "Request Admin API enablement via Salesforce/Quip support.\n",
    )


def _redact_email(email: str, redact: bool) -> str:
    if not redact or "@" not in email:
        return email
    name, _, domain = email.partition("@")
    head = name[0] if name else "*"
    return f"{head}***@{domain}"


def _emails(user: dict[str, Any], redact: bool) -> list[str]:
    raw = user.get("emails") or ([user["email"]] if user.get("email") else [])
    return [_redact_email(str(e), redact) for e in raw]


def export_organization(
    *,
    admin_client: Any,
    inventory: Any,
    manifest_dir: Path,
    config: Config,
    operator: str,
) -> OrgResult:
    """Export org metadata. Falls back to admin_api_unavailable.md on Admin API failure."""
    compliance = config.compliance
    pii: list[str] = []

    # Users (company members) - the Admin-API-gated part.
    members: dict[str, Any] = {}
    if compliance.export_users:
        try:
            members = admin_client.company_members()
        except Exception as exc:
            reason = f"Admin API call `company-members` failed: {exc}"
            log.warning(reason)
            write_admin_unavailable(manifest_dir, reason)
            return OrgResult("unavailable", error=reason)

    name_by_id: dict[str, str] = {}
    if members:
        pii.append("users")
        user_rows = []
        redacted_members: dict[str, Any] = {}
        any_plaintext_email = False
        for uid, user in members.items():
            name = user.get("name", "")
            name_by_id[uid] = name
            emails = _emails(user, compliance.redact_emails)
            if emails and not compliance.redact_emails:
                any_plaintext_email = True
            user_rows.append({"id": uid, "name": name, "emails": "; ".join(emails)})
            # users.json honors --redact-emails too (the raw API objects carry plaintext).
            redacted_members[uid] = {**user, "emails": emails} if compliance.redact_emails else user
        atomic_write_json(manifest_dir / "users.json", redacted_members)
        _write_csv(manifest_dir / "users.csv", ["id", "name", "emails"], user_rows)
        if any_plaintext_email:
            pii.append("emails")

    # Folders (from the already-traversed inventory; no extra API calls).
    folder_rows = [
        {
            "folder_id": f.folder_id,
            "title": f.title,
            "path": f.path,
            "parent_ids": ";".join(f.parent_ids),
            "member_count": len(f.member_ids),
        }
        for f in inventory.folders.values()
    ]
    _write_csv(
        manifest_dir / "folders.csv",
        ["folder_id", "title", "path", "parent_ids", "member_count"],
        folder_rows,
    )

    # Permissions: folder -> member mapping derived from folder member_ids. These rows
    # contain user identifiers (PII), so --no-users suppresses them too.
    permission_rows = 0
    if compliance.export_permissions and compliance.export_users:
        pii.append("permissions")
        rows = []
        for f in inventory.folders.values():
            for uid in f.member_ids:
                rows.append(
                    {
                        "folder_id": f.folder_id,
                        "folder_path": f.path,
                        "user_id": uid,
                        "user_name": name_by_id.get(uid, ""),
                        "access": "member",
                    }
                )
        _write_csv(
            manifest_dir / "permissions.csv",
            ["folder_id", "folder_path", "user_id", "user_name", "access"],
            rows,
        )
        permission_rows = len(rows)

    # Organization metadata + admin-run audit record.
    org = {
        "exported_at": now_iso(),
        "admin_run_by": operator,
        "admin_mode": True,
        "counts": {
            "users": len(members),
            "folders": len(folder_rows),
            "permission_rows": permission_rows,
        },
        "pii_categories_exported": pii,
        "notes": (
            "Permissions are derived from folder member_ids surfaced during traversal. "
            "Validate Admin API field shapes against your live tenant."
        ),
    }
    atomic_write_json(manifest_dir / "organization.json", org)

    return OrgResult(
        "complete",
        pii_categories=pii,
        users=len(members),
        folders=len(folder_rows),
        permission_rows=permission_rows,
    )


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    atomic_write_text(path, buf.getvalue())
