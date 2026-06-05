"""Configuration: secrets from the environment, structured options from config.yml.

Tokens come ONLY from environment variables (Settings). config.yml holds non-secret
options (Config). The two are kept apart on purpose: tokens must never land in a YAML file
or the SQLite state DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets and connection info, loaded from the environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="QUIP_",
        extra="ignore",
    )

    access_token: SecretStr = Field(default=SecretStr(""))
    base_url: str = "https://platform.quip.com"
    admin_mode: bool = False
    admin_token: SecretStr = Field(default=SecretStr(""))

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def require_access_token(self) -> str:
        token = self.access_token.get_secret_value()
        if not token:
            raise ConfigError("QUIP_ACCESS_TOKEN is not set. Copy .env.example to .env.")
        return token

    def require_admin_token(self) -> str:
        token = self.admin_token.get_secret_value()
        if not self.admin_mode or not token:
            raise ConfigError(
                "Admin mode requires QUIP_ADMIN_MODE=true and QUIP_ADMIN_TOKEN to be set."
            )
        return token

    def secret_values(self) -> list[str]:
        """Token strings to redact from logs/artifacts (see logging_setup.py)."""
        return [
            v
            for v in (
                self.access_token.get_secret_value(),
                self.admin_token.get_secret_value(),
            )
            if v
        ]


class ExportToggles(BaseModel):
    markdown: bool = True
    html: bool = True
    pdf: bool = False
    json_raw: bool = True
    comments: bool = True
    attachments: bool = True
    spreadsheets: bool = True


class FolderOptions(BaseModel):
    include_starred_root: bool = True
    include_shared_folders: bool = True
    include_archived: bool = False
    root_folder_ids: list[str] = Field(default_factory=list)


class NamingOptions(BaseModel):
    sanitize_filenames: bool = True
    prefix_with_modified_date: bool = False
    handle_duplicates: str = "append_thread_id"
    max_filename_length: int = 180


class ObsidianOptions(BaseModel):
    wikilinks: bool = True
    path_aliased_links: bool = True
    yaml_frontmatter: bool = True
    assets_folder_name: str = "_assets"
    comments_folder_name: str = "_comments"
    manifest_folder_name: str = "_manifest"
    unfiled_folder_name: str = "_unfiled"


class LimitOptions(BaseModel):
    # Quip's documented per-token ceilings: 50/min AND 750/hour; 600/min company-wide.
    # The hourly budget is the real constraint for large accounts.
    requests_per_minute: int = 50
    requests_per_hour: int = 750
    company_requests_per_minute: int = 600
    retry_count: int = 5
    retry_backoff_seconds: float = 3.0
    # Async PDF export pacing (a large doc can take up to ~10 minutes).
    pdf_poll_seconds: float = 15.0
    pdf_timeout_seconds: float = 600.0

    @field_validator("requests_per_minute")
    @classmethod
    def _cap_rpm(cls, v: int) -> int:
        if v < 1:
            raise ValueError("requests_per_minute must be >= 1")
        return min(v, 50)

    @field_validator("requests_per_hour")
    @classmethod
    def _cap_rph(cls, v: int) -> int:
        if v < 1:
            raise ValueError("requests_per_hour must be >= 1")
        return min(v, 750)


class SafetyOptions(BaseModel):
    dry_run: bool = False
    overwrite_existing: bool = False
    preserve_raw_api_responses: bool = True


class ComplianceOptions(BaseModel):
    redact_emails: bool = False
    export_users: bool = True
    export_permissions: bool = True


class Config(BaseModel):
    """Non-secret options, loaded from config.yml."""

    workspace_name: str = "Company Quip Archive"
    output_dir: Path = Path("./exports/quip-vault")
    obsidian_mode: bool = True
    export: ExportToggles = Field(default_factory=ExportToggles)
    folders: FolderOptions = Field(default_factory=FolderOptions)
    naming: NamingOptions = Field(default_factory=NamingOptions)
    obsidian: ObsidianOptions = Field(default_factory=ObsidianOptions)
    limits: LimitOptions = Field(default_factory=LimitOptions)
    safety: SafetyOptions = Field(default_factory=SafetyOptions)
    compliance: ComplianceOptions = Field(default_factory=ComplianceOptions)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}. Run `init` to create one.")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"Config file {p} must be a YAML mapping.")
        if "QUIP_ACCESS_TOKEN" in data or "access_token" in data:
            raise ConfigError("Tokens must not appear in config.yml. Put them in .env instead.")
        return cls.model_validate(data)


class ConfigError(Exception):
    """Raised for invalid or missing configuration."""


def validate_output_dir(output_dir: Path | str, *, posix: bool | None = None) -> Path:
    """Reject a Windows path used on a POSIX host (the classic Docker foot-gun).

    On Linux a back-slash is an ordinary filename character, so a path like
    `\\\\host\\share\\quip_export` (a Windows UNC path) does NOT mean a network share - it
    becomes a single literal directory of that name inside the container, written to the
    ephemeral writable layer and lost when the container is removed. A whole multi-hour
    export silently lands there instead of on the mounted volume. Catch it up-front with an
    actionable message instead of after the run finishes.

    `posix` overrides platform detection (for tests); defaults to the real host.
    """
    is_posix = (os.sep == "/") if posix is None else posix
    if is_posix and "\\" in str(output_dir):
        raise ConfigError(
            f"output_dir {str(output_dir)!r} looks like a Windows path, but this process is "
            "running on POSIX (most likely inside a Linux Docker container). On Linux a "
            "back-slash is a normal filename character, so the whole export would be written "
            "to a literal folder of that name inside the container (and lost when the "
            "container is removed), not to your share.\n"
            "Fix: point output_dir at a path INSIDE the container under the mounted volume, "
            "e.g. /app/exports/quip-vault (then copy ./exports to your share); or run the tool "
            "natively on Windows, where a UNC path like \\\\host\\share works directly."
        )
    return Path(output_dir)
