"""Typer CLI: init · inventory · export · verify · report · resume.

Command ordering contract: `export` ensures a fresh inventory first; `verify`/`report` read
state and fail gracefully if there is no export; `resume` re-runs failed/in_progress rows.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import EOL_NOTICE, __version__
from .config import Config, ConfigError, Settings
from .exporter import Exporter
from .inventory import Inventory
from .linkmap import LinkMap
from .logging_setup import configure_logging
from .org import export_organization
from .quip_client import QuipClient, QuipError
from .ratelimit import RateLimiter
from .reports import (
    verify as run_verify,
)
from .reports import (
    write_cancellation_checklist,
    write_error_manifest,
    write_summary,
)
from .state import StateDB
from .utils import ensure_dir

app = typer.Typer(
    add_completion=False,
    help="Read-only exporter: Quip workspace -> Obsidian-ready Markdown vault.",
)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yml")]


def _banner() -> None:
    console.print(f"[bold]Quip Vault Exporter[/bold] v{__version__}")
    console.print(f"[yellow]{EOL_NOTICE}[/yellow]\n")


def _bootstrap(config_path: Path) -> tuple[Config, Settings, StateDB, QuipClient, list[str]]:
    settings = Settings()
    config = Config.load(config_path)
    secrets = settings.secret_values()
    configure_logging(secrets, level=logging.INFO)
    token = settings.require_access_token()
    out = ensure_dir(Path(config.output_dir))
    state = StateDB.open(out)
    limiter = RateLimiter(
        config.limits.requests_per_minute,
        config.limits.requests_per_hour,
        company_per_minute=config.limits.company_requests_per_minute,
    )
    client = QuipClient(settings.base_url, token, rate_limiter=limiter)
    return config, settings, state, client, secrets


def _manifest_dir(config: Config) -> Path:
    return ensure_dir(Path(config.output_dir) / config.obsidian.manifest_folder_name)


def _admin_export(settings: Settings, cfg: Config, inv: Inventory, scope_ok: bool) -> None:
    """Run the admin/org export, gated behind explicit scope confirmation."""
    if not scope_ok:
        console.print(
            "[yellow]Admin mode is on but --i-understand-admin-scope was not passed.[/yellow] "
            "Admin/org export (whole-tenant PII) skipped."
        )
        return
    try:
        admin_token = settings.require_admin_token()
    except ConfigError as exc:
        console.print(f"[red]Admin export skipped:[/red] {exc}")
        return

    # Separate admin client with the admin rate budget (~100/min, 1500/hr).
    admin_limiter = RateLimiter(
        100, 1500, company_per_minute=cfg.limits.company_requests_per_minute
    )
    console.print(
        "[yellow]Admin/org export: reads the WHOLE tenant (users, folders, permissions).[/yellow]"
    )
    with QuipClient(settings.base_url, admin_token, rate_limiter=admin_limiter) as admin_client:
        try:
            operator = str(admin_client.current_user().get("name", "admin-token-holder"))
        except Exception:
            operator = "admin-token-holder"
        result = export_organization(
            admin_client=admin_client,
            inventory=inv,
            manifest_dir=_manifest_dir(cfg),
            config=cfg,
            operator=operator,
        )
    if result.status == "complete":
        console.print(
            f"[green]Admin export:[/green] users={result.users} folders={result.folders} "
            f"permission_rows={result.permission_rows} pii={result.pii_categories}"
        )
    else:
        console.print(f"[red]Admin export unavailable:[/red] {result.error}")


@app.command()
def init(
    config: ConfigOpt = Path("config.yml"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files")] = False,
) -> None:
    """Create config.yml and .env from the bundled examples."""
    _banner()
    here = Path(__file__).resolve()
    pkg_root = here.parents[2]  # src/quip_vault_exporter/cli.py -> project root
    for example, target in [("config.example.yml", config), (".env.example", Path(".env"))]:
        src = pkg_root / example
        if target.exists() and not force:
            console.print(f"[dim]exists, skipping[/dim] {target}")
            continue
        if src.exists():
            shutil.copyfile(src, target)
            console.print(f"[green]created[/green] {target}")
        else:
            console.print(f"[red]missing template[/red] {src}")
    console.print("\nNext: edit .env (set QUIP_ACCESS_TOKEN), then run `inventory`.")


@app.command()
def inventory(config: ConfigOpt = Path("config.yml")) -> None:
    """Scan folders/threads and write the inventory manifest."""
    _banner()
    cfg, _settings, state, client, _secrets = _bootstrap(config)
    with client, state:
        inv = Inventory(cfg, client)
        console.print("Scanning folders...")
        inv.scan()
        inv.hydrate_threads()
        inv.persist(state)
        inv.write_manifest(_manifest_dir(cfg))
    console.print(
        f"Found [bold]{len(inv.threads)}[/bold] threads in [bold]{len(inv.folders)}[/bold] folders."
    )


@app.command()
def export(
    config: ConfigOpt = Path("config.yml"),
    incremental: Annotated[bool, typer.Option("--incremental")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    admin_scope_ok: Annotated[
        bool,
        typer.Option(
            "--i-understand-admin-scope",
            help="Required to run admin/org export (reads the WHOLE tenant).",
        ),
    ] = False,
    no_users: Annotated[bool, typer.Option("--no-users", help="Skip user PII export")] = False,
    no_permissions: Annotated[
        bool, typer.Option("--no-permissions", help="Skip permission PII export")
    ] = False,
    redact_emails: Annotated[
        bool, typer.Option("--redact-emails", help="Redact emails in org export")
    ] = False,
) -> None:
    """Export all accessible threads to the vault (runs inventory first)."""
    _banner()
    cfg, settings, state, client, secrets = _bootstrap(config)
    if dry_run:
        cfg.safety.dry_run = True
    if no_users:
        cfg.compliance.export_users = False
    if no_permissions:
        cfg.compliance.export_permissions = False
    if redact_emails:
        cfg.compliance.redact_emails = True
    with client, state:
        inv = Inventory(cfg, client)
        console.print("Scanning folders...")
        inv.scan()
        inv.hydrate_threads()
        inv.persist(state)
        inv.write_manifest(_manifest_dir(cfg))

        console.print("Building link map (pass 1)...")
        linkmap = LinkMap(cfg)
        linkmap.build(inv, state)

        console.print(f"Exporting {len(inv.threads)} threads...")
        exporter = Exporter(cfg, client, state, linkmap, secrets, thread_cache=inv.thread_cache)
        stats = exporter.export_all(inv, incremental=incremental, force=force)

        if settings.admin_mode and not dry_run:
            _admin_export(settings, cfg, inv, admin_scope_ok)

        write_error_manifest(state, _manifest_dir(cfg))
        write_summary(state, cfg, _manifest_dir(cfg), admin_used=settings.admin_mode)
        write_cancellation_checklist(_manifest_dir(cfg))

    console.print(
        f"\n[green]Done.[/green] exported={stats.exported} skipped={stats.skipped} "
        f"failed={stats.failed} comments_threads={stats.comments_threads} "
        f"assets={stats.assets} spreadsheets={stats.spreadsheets} "
        f"pdfs={stats.pdfs} pdf_failures={stats.pdf_failures}"
    )
    console.print(f"Report: {Path(cfg.output_dir) / cfg.obsidian.manifest_folder_name}")


@app.command()
def resume(config: ConfigOpt = Path("config.yml")) -> None:
    """Re-run threads left failed or in_progress (incomplete) from a prior run."""
    _banner()
    cfg, _settings, state, client, secrets = _bootstrap(config)
    with client, state:
        pending = state.threads_by_status(("failed", "in_progress", "partial", "pending"))
        if not pending:
            console.print("Nothing to resume.")
            return
        console.print(f"Resuming {len(pending)} thread(s)...")
        inv = Inventory(cfg, client)
        inv.scan()
        inv.hydrate_threads()
        linkmap = LinkMap(cfg)
        linkmap.build(inv, state)
        ids = {row["thread_id"] for row in pending}
        inv.threads = {tid: t for tid, t in inv.threads.items() if tid in ids}
        exporter = Exporter(cfg, client, state, linkmap, secrets)
        stats = exporter.export_all(inv, incremental=False, force=True)
        write_error_manifest(state, _manifest_dir(cfg))
    console.print(f"[green]Resume done.[/green] exported={stats.exported} failed={stats.failed}")


@app.command()
def verify(config: ConfigOpt = Path("config.yml")) -> None:
    """Verify the export against state and write the verification report."""
    _banner()
    cfg, _settings, state, client, _secrets = _bootstrap(config)
    with client, state:
        report = run_verify(state, cfg, _manifest_dir(cfg))
    console.print(report)


@app.command()
def report(config: ConfigOpt = Path("config.yml")) -> None:
    """(Re)write the executive export summary from current state."""
    _banner()
    cfg, settings, state, client, _secrets = _bootstrap(config)
    with client, state:
        write_summary(state, cfg, _manifest_dir(cfg), admin_used=settings.admin_mode)
    console.print(f"Summary written to {_manifest_dir(cfg) / 'export_summary.md'}")


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port")] = 8000,
) -> None:
    """Launch the local web UI (paste token, click run). Defaults to localhost only."""
    _banner()
    try:
        from .web import serve as _serve
    except ModuleNotFoundError as exc:
        console.print(
            "[red]The web UI needs extra packages.[/red] Install them with:\n"
            "  pip install 'quip-vault-exporter[web]'"
        )
        raise typer.Exit(code=2) from exc
    console.print(f"Web UI: [bold]http://{host}:{port}[/bold]  (Ctrl-C to stop)")
    _serve(host=host, port=port)


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        sys.exit(2)
    except QuipError as exc:
        # Network/auth/API failures are expected operational errors — show them cleanly
        # (already token-scrubbed) instead of a raw traceback.
        console.print(f"[red]Quip API error:[/red] {exc}")
        console.print(
            "[dim]Check QUIP_ACCESS_TOKEN, QUIP_BASE_URL, and network connectivity.[/dim]"
        )
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow] Re-run `resume` to continue.")
        sys.exit(130)


if __name__ == "__main__":
    main()
