"""Simple local web UI for Quip Vault Exporter.

A FastAPI app + a single embedded HTML page so a non-CLI user can: paste a Quip token,
validate it, pick options, and run inventory/export/verify with live progress - without
touching the terminal. Token handling matches the CLI's guarantees:
  * the token lives in the server process memory only (never logged - the root logger's
    RedactionFilter scrubs it), and is optionally persisted to `.env` if "remember" is set;
  * everything stays read-only.

Run it with `quip-vault-exporter serve` (binds 127.0.0.1 by default).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from . import __version__
from .cache import RawStore
from .config import Config, ConfigError, ExportToggles, Settings, validate_output_dir
from .control import Cancelled, Control
from .inventory import Inventory
from .logging_setup import RedactionFilter, scrub_text
from .pipeline import run_export
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

log = logging.getLogger("quip_vault_exporter.web")

# --- in-memory server state (single local user) -----------------------------------------
_redaction = RedactionFilter([])
_session: dict[str, Any] = {"token": None, "base_url": "https://platform.quip.com", "user": None}
_jobs: dict[str, Job] = {}
_active: Job | None = None
_lock = threading.Lock()


@dataclass
class Job:
    id: str
    command: str
    status: str = "running"  # running | done | failed | cancelled
    logs: list[str] = field(default_factory=list)
    log_dropped: int = 0  # cumulative lines trimmed off the front (so `since` stays absolute)
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    # live progress
    phase: str = "starting"
    done: int = 0
    detail: str = ""
    started: float = 0.0  # monotonic
    phase_started: float = 0.0  # monotonic; resets each phase for per-phase ETA
    control: Control = field(default_factory=Control)

    def progress(self, phase: str, done: int, total: int | None, detail: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.phase_started = time.monotonic()
        self.done = done
        self.total = total or 0
        self.detail = detail


class _JobLogHandler(logging.Handler):
    """Routes app log records into the active job's buffer (already token-scrubbed)."""

    def emit(self, record: logging.LogRecord) -> None:
        job = _active
        if job is None:
            return
        try:
            line = self.format(record)
        except Exception:
            return
        job.logs.append(line)
        overflow = len(job.logs) - 1000  # keep the tail bounded
        if overflow > 0:
            del job.logs[:overflow]
            job.log_dropped += overflow  # remember how many we dropped, so `since` stays absolute


def _install_logging() -> None:
    handler = _JobLogHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_redaction)
    root = logging.getLogger()
    root.addFilter(_redaction)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _write_env(token: str, base_url: str) -> None:
    """Persist token+base_url to .env (the sanctioned, gitignored token location)."""
    env = Path(".env")
    lines: list[str] = []
    if env.exists():
        lines = [
            ln
            for ln in env.read_text(encoding="utf-8").splitlines()
            if not ln.startswith(("QUIP_ACCESS_TOKEN=", "QUIP_BASE_URL="))
        ]
    lines.append(f"QUIP_ACCESS_TOKEN={token}")
    lines.append(f"QUIP_BASE_URL={base_url}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _browse(raw: str | None) -> dict[str, Any]:
    """List the immediate subfolders of `raw` on the SERVER filesystem (for the picker).

    Localhost-only by design. Returns no file contents - only directory names - and degrades
    gracefully on permission/IO errors so the picker can never crash the server.
    """
    base = Path(raw).expanduser() if raw else Path.home()
    try:
        base = base.resolve()
        if not base.is_dir():
            base = Path.home().resolve()
    except OSError:
        base = Path.home().resolve()
    dirs: list[dict[str, str]] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except (PermissionError, OSError):
        pass
    parent = str(base.parent) if base.parent != base else None
    return {
        "path": str(base),
        "parent": parent,
        "writable": os.access(base, os.W_OK),
        "dirs": dirs[:1000],
    }


def _check_path(raw: str) -> dict[str, Any]:
    """Resolve a typed output path and report whether it's usable (for live UI feedback)."""
    if not raw.strip():
        return {"ok": False, "abs": "", "msg": "enter a folder path"}
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return {"ok": False, "abs": raw, "msg": "invalid path"}
    exists = p.exists()
    # Writability of the target if it exists, else of the nearest existing ancestor.
    probe = p if exists else next((a for a in p.parents if a.exists()), p)
    writable = os.access(probe, os.W_OK)
    if not writable:
        msg = "not writable"
    elif exists:
        msg = "exists - writable"
    else:
        msg = "will be created"
    return {"ok": writable, "abs": str(p), "exists": exists, "writable": writable, "msg": msg}


def _load_env_token() -> None:
    """At startup, pick up a token already present in .env / the environment so the UI can
    auto-connect without re-pasting it. Validation happens lazily on the first status call."""
    s = Settings()  # reads .env + QUIP_* env vars
    token = s.access_token.get_secret_value()
    if token:
        _session.update({"token": token, "base_url": s.base_url, "user": None})
        _redaction.set_secrets([token])


def _settings() -> Settings:
    token = _session["token"]
    if not token:
        raise QuipError("No token set. Validate a token first.")
    return Settings(access_token=SecretStr(token), base_url=_session["base_url"], admin_mode=False)


def _build_config(opts: dict[str, Any]) -> Config:
    cfg = Config()
    if opts.get("output_dir"):
        cfg.output_dir = validate_output_dir(opts["output_dir"])
    cfg.export = ExportToggles(
        markdown=opts.get("markdown", True),
        html=opts.get("html", True),
        json_raw=opts.get("json_raw", True),
        comments=opts.get("comments", True),
        attachments=opts.get("attachments", True),
        pdf=opts.get("pdf", False),
        spreadsheets=opts.get("spreadsheets", True),
    )
    return cfg


def _run_job(job: Job, settings: Settings, cfg: Config) -> None:
    global _active
    secrets = settings.secret_values()
    _redaction.set_secrets(secrets)
    job.started = job.phase_started = time.monotonic()
    try:
        limiter = RateLimiter(
            cfg.limits.requests_per_minute,
            cfg.limits.requests_per_hour,
            company_per_minute=cfg.limits.company_requests_per_minute,
        )
        client = QuipClient(
            settings.base_url, settings.require_access_token(), rate_limiter=limiter
        )
        state = StateDB.open(ensure_dir(Path(cfg.output_dir)))
        mdir = ensure_dir(Path(cfg.output_dir) / cfg.obsidian.manifest_folder_name)
        with client, state:
            if job.command == "export":
                raw = RawStore(Path(cfg.output_dir) / "_raw")
                run = run_export(
                    config=cfg,
                    client=client,
                    state=state,
                    secrets=secrets,
                    raw=raw,
                    force=False,
                    progress=job.progress,
                    control=job.control,
                )
                write_error_manifest(state, mdir)
                write_summary(state, cfg, mdir, admin_used=False)
                write_cancellation_checklist(mdir)
                job.result = {**run.summary(), "output_dir": str(cfg.output_dir)}
            elif job.command == "inventory":
                inv = Inventory(cfg, client)
                inv.scan(progress=job.progress, control=job.control)
                inv.persist(state)
                inv.write_manifest(mdir)
                job.total = len(inv.threads)
                job.result = {"threads": len(inv.threads), "folders": len(inv.folders)}
            elif job.command == "verify":
                job.result = run_verify(state, cfg, mdir)
        job.status = "done"
        log.info("%s complete.", job.command)
    except Cancelled:
        job.status = "cancelled"
        log.info("%s cancelled by user. Re-run Export to continue where it left off.", job.command)
    except Exception as exc:  # never crash the server on a job failure
        job.error = scrub_text(str(exc), secrets)
        job.status = "failed"
        log.error("%s failed: %s", job.command, job.error)
    finally:
        with _lock:
            _active = None


def create_app() -> Any:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    _install_logging()
    _load_env_token()  # auto-connect from .env if a token is already there
    app = FastAPI(title="Quip Vault Exporter", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> Any:
        # no-store so browsers never serve a stale UI after the app is updated/restarted.
        html = INDEX_HTML.replace("__QVE_VERSION__", __version__)
        return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/browse")
    def browse(path: str | None = None) -> JSONResponse:
        return JSONResponse(_browse(path))

    @app.get("/api/check-path")
    def check_path(path: str = "") -> JSONResponse:
        return JSONResponse(_check_path(path))

    @app.get("/api/status")
    def status() -> JSONResponse:
        # If a token was loaded from .env but not yet validated, validate it once (fast-fail)
        # to fetch the user name. A bad/unreachable token is dropped so the UI prompts.
        if _session["token"] and _session["user"] is None:
            try:
                with QuipClient(
                    _session["base_url"],
                    _session["token"],
                    rate_limiter=RateLimiter(),
                    timeout=15.0,
                    max_attempts=1,
                ) as c:
                    _session["user"] = c.current_user().get("name", "Quip user")
            except QuipError as exc:
                # The root logger's RedactionFilter scrubs the token from this message.
                log.warning("Token from .env could not be validated: %s", exc)
                _session["token"] = None
        return JSONResponse(
            {
                "connected": bool(_session["token"]),
                "user": _session["user"],
                "base_url": _session["base_url"],
            }
        )

    @app.post("/api/token")
    def set_token(body: dict[str, Any]) -> JSONResponse:
        token = (body.get("token") or "").strip()
        base_url = (body.get("base_url") or "https://platform.quip.com").strip().rstrip("/")
        if not token:
            raise HTTPException(status_code=400, detail="Token is required.")
        # Validation must fail fast: a single attempt with a short timeout, so a wrong
        # token/URL surfaces in seconds instead of retrying with exponential backoff.
        try:
            with QuipClient(
                base_url, token, rate_limiter=RateLimiter(), timeout=15.0, max_attempts=1
            ) as client:
                user = client.current_user()
        except QuipError as exc:
            return JSONResponse(
                {"ok": False, "error": scrub_text(str(exc), [token])}, status_code=400
            )
        _session.update(
            {"token": token, "base_url": base_url, "user": user.get("name", "Quip user")}
        )
        _redaction.set_secrets([token])
        if body.get("remember"):
            _write_env(token, base_url)
        return JSONResponse(
            {"ok": True, "user": _session["user"], "remembered": bool(body.get("remember"))}
        )

    @app.post("/api/run")
    def run(body: dict[str, Any]) -> JSONResponse:
        global _active
        command = body.get("command")
        if command not in ("inventory", "export", "verify"):
            raise HTTPException(status_code=400, detail="Unknown command.")
        if not _session["token"]:
            raise HTTPException(status_code=400, detail="Validate a token first.")
        # Validate settings + output path BEFORE registering the job, so a bad path fails
        # immediately with a clear 400 (and never orphans `_active`, which would wedge /api/run).
        try:
            settings = _settings()
            cfg = _build_config(body.get("options", {}))
        except (ConfigError, QuipError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with _lock:
            if _active is not None and _active.status == "running":
                raise HTTPException(status_code=409, detail="A job is already running.")
            job = Job(id=uuid.uuid4().hex[:12], command=command)
            _jobs[job.id] = job
            _active = job
        threading.Thread(target=_run_job, args=(job, settings, cfg), daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @app.post("/api/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: str) -> JSONResponse:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        if action == "pause":
            job.control.pause()
        elif action == "resume":
            job.control.resume()
        elif action == "cancel":
            job.control.cancel()
        else:
            raise HTTPException(status_code=400, detail="Unknown action.")
        return JSONResponse({"ok": True, "paused": job.control.is_paused})

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, since: int = 0) -> JSONResponse:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        now = time.monotonic()
        elapsed = now - job.started if job.started else 0.0
        # Per-phase ETA: extrapolate from how long the current phase has taken so far.
        eta = None
        if job.total and job.done > 0 and job.status == "running":
            phase_elapsed = now - job.phase_started
            per_item = phase_elapsed / job.done
            eta = max(0.0, (job.total - job.done) * per_item)
        return JSONResponse(
            {
                "status": job.status,
                "command": job.command,
                "phase": job.phase,
                "paused": job.control.is_paused,
                "done": job.done,
                "total": job.total,
                "detail": job.detail,
                "elapsed": round(elapsed, 1),
                "eta": None if eta is None else round(eta, 1),
                "result": job.result,
                "error": job.error,
                # `since` is an absolute line index; map it past any trimmed-off lines. If the
                # client fell >1000 lines behind, it gets the whole retained tail (never frozen).
                "logs": job.logs[max(0, since - job.log_dropped) :],
                "log_count": job.log_dropped + len(job.logs),
            }
        )

    @app.get("/favicon.ico")
    def favicon() -> Any:
        from fastapi.responses import Response

        return Response(status_code=204)

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quip Vault Exporter</title>
<style>
  :root{--bg:#0f1115;--panel:#181b22;--ink:#e6e8ec;--mut:#9aa3b2;--ok:#3fb950;--warn:#d29922;
        --err:#f85149;--acc:#388bfd;--line:#272b34}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:12px}
  header h1{font-size:18px;margin:0}
  header .v{color:var(--mut);font-size:12px}
  main{max-width:980px;margin:0 auto;padding:24px;display:grid;gap:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}
  .card h2{margin:0 0 12px;font-size:14px;letter-spacing:.02em;text-transform:uppercase;color:var(--mut)}
  label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
  input[type=text],input[type=password]{width:100%;padding:9px 11px;background:#0c0e13;border:1px solid var(--line);
    border-radius:7px;color:var(--ink);font:inherit}
  .row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:200px}
  .toggles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:8px}
  .chk{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
  button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:10px 16px;font:inherit;
    font-weight:600;cursor:pointer}
  button.ghost{background:#21262d;color:var(--ink);border:1px solid var(--line)}
  button:disabled{opacity:.45;cursor:not-allowed}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
  .pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}
  .dot.ok{background:var(--ok)}.dot.run{background:var(--warn)}.dot.err{background:var(--err)}
  .bar{height:8px;background:#0c0e13;border-radius:6px;overflow:hidden;margin:10px 0}
  .bar>i{display:block;height:100%;background:var(--acc);width:0}
  pre#log{background:#0a0c10;border:1px solid var(--line);border-radius:8px;padding:12px;height:280px;
    overflow:auto;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;color:#c9d1d9;white-space:pre-wrap;margin:0}
  .muted{color:var(--mut);font-size:12px}
  .picker{border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px;background:#0c0e13}
  .pkbar{display:flex;justify-content:space-between;gap:10px;font-family:ui-monospace,Consolas,monospace}
  .pkdirs{max-height:210px;overflow:auto;margin:8px 0;display:flex;flex-direction:column;gap:2px}
  .pkdir{padding:6px 8px;border-radius:6px;cursor:pointer;color:var(--ink);font-size:13px}
  .pkdir:hover{background:#1b2230}
  .pkfoot{display:flex;gap:8px}.pkfoot input{flex:1}
  .banner{padding:10px 12px;border-radius:8px;font-size:13px;margin-top:10px;display:none}
  .banner.show{display:block}
  .banner.ok{background:rgba(63,185,80,.12);border:1px solid var(--ok)}
  .banner.err{background:rgba(248,81,73,.12);border:1px solid var(--err)}
  .banner.info{background:rgba(56,139,253,.12);border:1px solid var(--acc)}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.35);
    border-top-color:#fff;border-radius:50%;animation:sp .6s linear infinite;vertical-align:-2px;margin-right:7px}
  @keyframes sp{to{transform:rotate(360deg)}}
  footer{text-align:center;color:var(--mut);font-size:12px;padding:22px 0 30px}
  footer a{color:var(--acc);text-decoration:none;font-weight:600}
  footer a:hover{text-decoration:underline}
</style>
</head>
<body>
<header><h1>Quip Vault Exporter</h1><span class="v">v__QVE_VERSION__</span>
  <span class="pill" style="margin-left:auto"><span id="dot" class="dot"></span><span id="who">not connected</span></span>
</header>
<main>
  <section class="card">
    <h2>1 · Connect</h2>
    <div class="row">
      <div><label>Quip access token</label><input id="token" type="password" placeholder="paste your token from https://&lt;tenant&gt;.quip.com/dev/token"></div>
      <div><label>Base URL</label><input id="base" type="text" value="https://platform.quip.com"></div>
    </div>
    <label class="chk" style="margin-top:10px"><input id="remember" type="checkbox"> Remember in .env (so the CLI can reuse it)</label>
    <div class="actions"><button id="connect">Validate token</button></div>
    <div id="connBanner" class="banner"></div>
  </section>

  <section class="card">
    <h2>2 · Options</h2>
    <div class="row"><div><label>Output folder (vault)</label>
      <div style="display:flex;gap:8px">
        <input id="out" type="text" value="./exports/quip-vault">
        <button id="browse" class="ghost" type="button" style="white-space:nowrap">Browse…</button>
      </div>
      <div id="outinfo" class="muted" style="margin-top:6px">Paste a full path, or use Browse. Files are written here in the Export phase.</div>
    </div></div>
    <div id="picker" class="picker" hidden>
      <div class="pkbar"><span id="pkpath" class="muted"></span><span id="pkwrite" class="muted"></span></div>
      <div id="pkdirs" class="pkdirs"></div>
      <div class="pkfoot">
        <input id="pknew" type="text" placeholder="optional: create new subfolder here">
        <button id="pkuse" type="button">Use this folder</button>
        <button id="pkcancel" class="ghost" type="button">Cancel</button>
      </div>
    </div>
    <div class="toggles">
      <label class="chk"><input type="checkbox" class="opt" data-k="markdown" checked> Markdown</label>
      <label class="chk"><input type="checkbox" class="opt" data-k="html" checked> HTML</label>
      <label class="chk"><input type="checkbox" class="opt" data-k="json_raw" checked> Raw JSON</label>
      <label class="chk"><input type="checkbox" class="opt" data-k="comments" checked> Comments</label>
      <label class="chk"><input type="checkbox" class="opt" data-k="attachments" checked> Attachments</label>
      <label class="chk"><input type="checkbox" class="opt" data-k="spreadsheets" checked> Spreadsheets</label>
      <label class="chk"><input type="checkbox" class="opt" data-k="pdf"> PDF (slow)</label>
    </div>
  </section>

  <section class="card">
    <h2>3 · Run</h2>
    <div class="actions">
      <button id="btnInv" class="ghost" disabled>Inventory</button>
      <button id="btnExp" disabled>Export</button>
      <button id="btnVer" class="ghost" disabled>Verify</button>
      <button id="btnPause" class="ghost" type="button" hidden>Pause</button>
      <button id="btnCancel" class="ghost" type="button" hidden style="border-color:var(--err);color:var(--err)">Cancel</button>
      <span class="pill" style="margin-left:auto"><span id="jdot" class="dot"></span><span id="jstat" class="muted">idle</span></span>
    </div>
    <div class="bar"><i id="bar"></i></div>
    <div id="summary" class="muted"></div>
    <pre id="log"></pre>
  </section>
</main>
<footer>Proudly Powered By <a href="https://goldtech.mx" target="_blank" rel="noopener">GoldTech MX</a></footer>
<script>
const $=s=>document.querySelector(s);
let connected=false, poll=null, shown=0;
function setConn(ok,name){connected=ok;$('#dot').className='dot'+(ok?' ok':'');$('#who').textContent=ok?('connected · '+name):'not connected';
  ['#btnInv','#btnExp','#btnVer'].forEach(s=>$(s).disabled=!ok);}
function banner(el,kind,msg){el.className='banner show '+kind;el.textContent=msg;}
function options(){const o={output_dir:$('#out').value};document.querySelectorAll('.opt').forEach(c=>o[c.dataset.k]=c.checked);return o;}

async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},
  body:body?JSON.stringify(body):undefined});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||j.error||('HTTP '+r.status));return j;}

$('#connect').onclick=async()=>{const b=$('#connBanner');const btn=$('#connect');
  const old=btn.textContent;btn.disabled=true;btn.innerHTML='<span class="spin"></span>Validating…';
  banner(b,'info','Validating token with Quip…');
  try{
    const j=await api('/api/token',{token:$('#token').value,base_url:$('#base').value,remember:$('#remember').checked});
    if(!j.ok){banner(b,'err',j.error||'Invalid token');setConn(false);return;}
    setConn(true,j.user);banner(b,'ok','Connected as '+j.user+(j.remembered?' · saved to .env':''));
  }catch(e){banner(b,'err',e.message);setConn(false);}
  finally{btn.disabled=false;btn.textContent=old;}};

let curJob=null;
function showControls(on){$('#btnPause').hidden=!on;$('#btnCancel').hidden=!on;if(on){$('#btnPause').textContent='Pause';$('#btnCancel').disabled=false;}}
function run(cmd){return async()=>{if(!connected)return;shown=0;$('#log').textContent='';$('#summary').textContent='';
  $('#bar').style.width='0';$('#jdot').className='dot run';$('#jstat').textContent=cmd+' starting…';
  ['#btnInv','#btnExp','#btnVer'].forEach(s=>$(s).disabled=true);
  try{const {job_id}=await api('/api/run',{command:cmd,options:options()});
    curJob=job_id;showControls(true);poll=setInterval(()=>track(job_id),700);}
  catch(e){$('#jstat').textContent=e.message;$('#jdot').className='dot err';
    ['#btnInv','#btnExp','#btnVer'].forEach(s=>$(s).disabled=!connected);}};}
$('#btnPause').onclick=async()=>{if(!curJob)return;const resuming=$('#btnPause').textContent==='Resume';
  try{await api('/api/jobs/'+curJob+'/'+(resuming?'resume':'pause'),{});$('#btnPause').textContent=resuming?'Pause':'Resume';}catch(e){}};
$('#btnCancel').onclick=async()=>{if(!curJob)return;$('#btnCancel').disabled=true;try{await api('/api/jobs/'+curJob+'/cancel',{});}catch(e){}};

function fmt(s){if(s==null)return '?';s=Math.round(s);if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+(s%60)+'s';return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';}
const PH={starting:'Starting',scan:'Scanning folders',download:'Downloading from Quip',linkmap:'Building links',process:'Writing vault'};
async function track(id){try{const j=await api('/api/jobs/'+id+'?since='+shown);
  if(j.logs&&j.logs.length){$('#log').textContent+=j.logs.join('\\n')+'\\n';shown=j.log_count;$('#log').scrollTop=$('#log').scrollHeight;}
  const pct=j.total?Math.min(100,Math.round(100*j.done/j.total)):0;$('#bar').style.width=pct+'%';
  let line=(PH[j.phase]||j.phase||j.command);
  if(j.total)line+=' · '+j.done+'/'+j.total+' ('+pct+'%)';else if(j.done)line+=' · '+j.done;
  line+=' · elapsed '+fmt(j.elapsed);
  if(j.eta!=null)line+=' · ETA '+fmt(j.eta);
  if(j.detail)line+=' · '+j.detail;
  if(j.status==='running'){$('#btnPause').textContent=j.paused?'Resume':'Pause';$('#jdot').className=j.paused?'dot':'dot run';
    $('#jstat').textContent=(j.paused?'⏸ paused · ':'')+line;}
  if(j.status!=='running'){clearInterval(poll);showControls(false);curJob=null;['#btnInv','#btnExp','#btnVer'].forEach(s=>$(s).disabled=!connected);
    if(j.status==='done'){$('#jdot').className='dot ok';$('#jstat').textContent=j.command+' done · '+fmt(j.elapsed);$('#bar').style.width='100%';
      $('#summary').textContent=JSON.stringify(j.result);}
    else if(j.status==='cancelled'){$('#jdot').className='dot';$('#jstat').textContent=j.command+' cancelled';
      $('#summary').textContent='Cancelled. Re-run Export to continue where it left off (already-downloaded docs are skipped).';}
    else{$('#jdot').className='dot err';$('#jstat').textContent=j.command+' failed';$('#summary').textContent=j.error||'';}}
}catch(e){clearInterval(poll);$('#jstat').textContent=e.message;$('#jdot').className='dot err';}}

// --- server-side folder picker ---
let pkPath='';
async function nav(p){try{const d=await api('/api/browse?path='+encodeURIComponent(p||''));renderPicker(d);}catch(e){}}
function renderPicker(d){pkPath=d.path;$('#pkpath').textContent=d.path;
  $('#pkwrite').textContent=d.writable?'writable':'not writable';$('#pkwrite').style.color=d.writable?'var(--ok)':'var(--err)';
  const box=$('#pkdirs');box.innerHTML='';
  if(d.parent){const up=document.createElement('div');up.className='pkdir';up.textContent='⬆  ..';up.onclick=()=>nav(d.parent);box.appendChild(up);}
  d.dirs.forEach(x=>{const el=document.createElement('div');el.className='pkdir';el.textContent='📁  '+x.name;el.onclick=()=>nav(x.path);box.appendChild(el);});
  if(!d.dirs.length){const e=document.createElement('div');e.className='muted';e.style.padding='6px 8px';e.textContent='(no subfolders here)';box.appendChild(e);}}
// live feedback for the typed/pasted output path
let outTimer=null;
async function checkOut(){const v=$('#out').value;if(!v.trim()){$('#outinfo').textContent='';return;}
  try{const d=await api('/api/check-path?path='+encodeURIComponent(v));
    $('#outinfo').innerHTML=(d.ok?'✓ ':'✗ ')+d.abs+' · '+d.msg;
    $('#outinfo').style.color=d.ok?'var(--ok)':'var(--err)';}catch(e){}}
$('#out').addEventListener('input',()=>{clearTimeout(outTimer);outTimer=setTimeout(checkOut,400);});
checkOut();
$('#browse').onclick=()=>{$('#picker').hidden=false;nav($('#out').value);};
$('#pkcancel').onclick=()=>$('#picker').hidden=true;
$('#pkuse').onclick=()=>{let p=pkPath;const nw=$('#pknew').value.trim();if(nw)p=p.replace(/[\\\\/]+$/,'')+'/'+nw;
  $('#out').value=p;$('#pknew').value='';$('#picker').hidden=true;checkOut();};

$('#btnInv').onclick=run('inventory');$('#btnExp').onclick=run('export');$('#btnVer').onclick=run('verify');
api('/api/status').then(j=>{if(j.connected){$('#base').value=j.base_url;setConn(true,j.user);
  banner($('#connBanner'),'ok','Connected as '+j.user+' (loaded from .env)');}}).catch(()=>{});
</script>
</body></html>"""
