"""ApplyPilot WebUI — browser control panel for the pipeline.

Runs inside the applypilot container and shells out to the `applypilot`
CLI for pipeline actions, so behavior is identical to running commands
in a console. Configuration files are edited directly (replacing the
interactive `applypilot init` wizard).

No authentication — intended for LAN-only use behind Unraid.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from applypilot.config import (
    APP_DIR,
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH,
    ensure_dirs,
    load_env,
)
from applypilot.database import init_db, get_stats, get_connection

WEBUI_DIR = Path(__file__).parent
MAX_PDF_BYTES = 20 * 1024 * 1024
# Statuses a human can resolve by hand in the live browser
MANUAL_STATUSES = ("captcha", "login_issue", "manual")
# Persistent Chrome profile for manual solving — logins carry across the batch
MANUAL_PROFILE_DIR = APP_DIR / "manual-chrome"
NOVNC_DISPLAY = os.environ.get("DISPLAY", ":99")

load_env()
ensure_dirs()
init_db()

app = FastAPI(title="ApplyPilot WebUI")


# ---------------------------------------------------------------------------
# Task runner: one pipeline subprocess at a time, output kept in a ring buffer
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.task: str | None = None
        self.buf: deque[tuple[int, str]] = deque(maxlen=4000)
        self.seq = 0

    def append(self, line: str) -> None:
        self.seq += 1
        self.buf.append((self.seq, line))

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, task: str, args: list[str]) -> None:
        with self.lock:
            if self.running():
                raise HTTPException(409, f"A task is already running: {self.task}")
            env = os.environ.copy()
            # Keep rich/typer output plain so it reads well in a <pre> pane
            env.update(PYTHONUNBUFFERED="1", NO_COLOR="1", FORCE_COLOR="0",
                       TERM="dumb", COLUMNS="160")
            self.append(f"$ {' '.join(args)}")
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                preexec_fn=os.setsid,  # own process group -> stop kills children too
            )
            self.task = task
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.append(line.rstrip("\n"))
        rc = proc.wait()
        self.append(f"[{self.task} exited with code {rc}]")

    def stop(self) -> bool:
        if not self.running():
            return False
        assert self.proc is not None
        self.append("[stop requested — sending SIGTERM to task]")
        os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        return True


runner = Runner()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEBUI_DIR / "index.html")


@app.get("/dashboard")
def dashboard() -> FileResponse:
    """Regenerate ApplyPilot's HTML results dashboard and serve it."""
    from applypilot.view import generate_dashboard
    path = generate_dashboard()
    return FileResponse(path, media_type="text/html")


# ---------------------------------------------------------------------------
# State + logs
# ---------------------------------------------------------------------------

@app.get("/api/state")
def state() -> JSONResponse:
    try:
        stats = get_stats()
    except Exception as exc:  # DB empty/locked mid-run — degrade, don't 500
        stats = {"error": str(exc)}
    return JSONResponse({
        "running": runner.running(),
        "task": runner.task if runner.running() else None,
        "seq": runner.seq,
        "stats": stats,
        "files": {
            "profile": PROFILE_PATH.exists(),
            "searches": SEARCH_CONFIG_PATH.exists(),
            "resume": RESUME_PATH.exists(),
            "resume_pdf": RESUME_PDF_PATH.exists(),
        },
        "llm_configured": any(os.environ.get(k) for k in
                              ("LLM_URL", "GEMINI_API_KEY", "OPENAI_API_KEY")),
        "novnc_port": int(os.environ.get("NOVNC_PORT", "8485")),
    })


@app.get("/api/logs")
def logs(since: int = 0) -> JSONResponse:
    lines = [{"seq": s, "line": l} for s, l in runner.buf if s > since]
    return JSONResponse({"lines": lines, "seq": runner.seq})


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

VALID_STAGES = {"all", "discover", "enrich", "score", "tailor", "cover", "pdf"}
VALID_VALIDATION = {"strict", "normal", "lenient"}
# Model names are passed straight to the CLI, so keep them to a safe charset
MODEL_RE = re.compile(r"[A-Za-z0-9._:\-/]{1,64}")


class TaskRequest(BaseModel):
    action: str
    stages: list[str] | None = None
    workers: int = 1
    min_score: int | None = None
    dry_run: bool = False
    watch: bool = False  # run headed on the virtual display (viewable via noVNC)
    # run-only
    stream: bool = False
    validation: str = "normal"
    # apply-only
    limit: int | None = None
    model: str | None = None
    continuous: bool = False
    reset_failed: bool = False


def _clamp_score(v: int) -> str:
    return str(max(1, min(v, 10)))


@app.post("/api/task")
def start_task(req: TaskRequest) -> JSONResponse:
    workers = max(1, min(req.workers, 8))

    if req.action == "run":
        stages = [s for s in (req.stages or ["all"]) if s in VALID_STAGES] or ["all"]
        args = ["applypilot", "run", *stages]
        if workers > 1:
            args += ["--workers", str(workers)]
        if req.min_score is not None:
            args += ["--min-score", _clamp_score(req.min_score)]
        if req.stream:
            args.append("--stream")
        if req.dry_run:
            args.append("--dry-run")
        if req.validation in VALID_VALIDATION and req.validation != "normal":
            args += ["--validation", req.validation]
        task = f"run {' '.join(stages)}" + (" (dry-run)" if req.dry_run else "")

    elif req.action == "apply":
        args = ["applypilot", "apply"]
        # Headed (no --headless) renders on the virtual display for noVNC
        if not req.watch:
            args.append("--headless")
        if req.dry_run:
            args.append("--dry-run")
        if req.reset_failed:
            args.append("--reset-failed")
        if req.continuous:
            args.append("--continuous")
        if workers > 1:
            args += ["--workers", str(workers)]
        if req.min_score is not None:
            args += ["--min-score", _clamp_score(req.min_score)]
        if req.limit is not None and req.limit > 0:
            args += ["--limit", str(min(req.limit, 999))]
        if req.model and MODEL_RE.fullmatch(req.model):
            args += ["--model", req.model]
        task = "apply" + (" watch" if req.watch else "") \
            + (" reset-failed" if req.reset_failed else "") \
            + (" (dry-run)" if req.dry_run else "")

    elif req.action == "doctor":
        args = ["applypilot", "doctor"]
        task = "doctor"

    elif req.action == "status":
        args = ["applypilot", "status"]
        task = "status"

    else:
        raise HTTPException(400, f"Unknown action: {req.action}")

    runner.start(task, args)
    return JSONResponse({"started": task})


@app.post("/api/stop")
def stop_task() -> JSONResponse:
    return JSONResponse({"stopped": runner.stop()})


# ---------------------------------------------------------------------------
# CAPTCHA / manual-review queue
#
# Jobs the agent couldn't finish (CAPTCHA, forced login, manual ATS) are left
# in the DB with these statuses and their materials already generated. This
# queue lets you grind them by hand in a live browser, then mark them applied.
# ---------------------------------------------------------------------------

@app.get("/api/manual-queue")
def manual_queue() -> JSONResponse:
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in MANUAL_STATUSES)
        rows = conn.execute(
            f"""SELECT url, title, site, application_url, apply_status,
                       apply_error, fit_score, tailored_resume_path, cover_letter_path
                FROM jobs WHERE apply_status IN ({placeholders})
                ORDER BY fit_score DESC""",
            MANUAL_STATUSES,
        ).fetchall()
    finally:
        conn.close()

    jobs = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {
            "url": r[0], "title": r[1], "site": r[2], "application_url": r[3],
            "apply_status": r[4], "apply_error": r[5], "fit_score": r[6],
            "tailored_resume_path": r[7], "cover_letter_path": r[8],
        }
        jobs.append(d)
    return JSONResponse({"jobs": jobs, "display": NOVNC_DISPLAY})


class MarkRequest(BaseModel):
    url: str
    status: str = "applied"  # 'applied' or 'failed'


@app.post("/api/mark")
def mark(req: MarkRequest) -> JSONResponse:
    if req.status not in ("applied", "failed"):
        raise HTTPException(400, "status must be 'applied' or 'failed'")
    from applypilot.apply.launcher import mark_job
    mark_job(req.url, req.status, "manually resolved via WebUI")
    return JSONResponse({"marked": req.url, "status": req.status})


class ManualBrowserRequest(BaseModel):
    urls: list[str]


@app.post("/api/manual-browser")
def manual_browser(req: ManualBrowserRequest) -> JSONResponse:
    """Open the given URLs as tabs in a persistent, human-driven Chrome on the
    virtual display (viewable via noVNC). Reuses one profile so a login done
    for the first job carries to the rest of the batch."""
    urls = [u for u in req.urls if u.startswith(("http://", "https://"))][:25]
    if not urls:
        raise HTTPException(400, "No valid http(s) URLs")

    chrome = os.environ.get("CHROME_PATH", "chromium")
    MANUAL_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DISPLAY"] = NOVNC_DISPLAY
    cmd = [chrome, f"--user-data-dir={MANUAL_PROFILE_DIR}",
           "--no-first-run", "--no-default-browser-check",
           "--start-maximized", *urls]
    try:
        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        raise HTTPException(500, f"Browser not found: {chrome}")
    return JSONResponse({"opened": len(urls), "display": NOVNC_DISPLAY})


# ---------------------------------------------------------------------------
# Docker control — pull image updates and restart the stack from the WebUI.
#
# Requires the Docker socket mounted and STACK_DIR set to the HOST path of the
# compose project. A container can't cleanly recreate itself (the process dies
# mid-operation), so restarts are delegated to a short-lived helper container
# that survives our recreation.
# ---------------------------------------------------------------------------

DOCKER = "/usr/bin/docker"
STACK_DIR_HOST = os.environ.get("STACK_DIR", "")          # host path, for helper -v
STACK_MOUNT = Path("/stack")                              # same dir, inside us
IMAGE_REPO = os.environ.get("IMAGE_REPO", "")
HELPER_IMAGE = os.environ.get("HELPER_IMAGE", "docker:cli")


def _docker_ok() -> bool:
    if not Path("/var/run/docker.sock").exists() or not Path(DOCKER).exists():
        return False
    try:
        return subprocess.run([DOCKER, "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def _set_env_tag(tag: str) -> None:
    """Write IMAGE_TAG=<tag> into the stack .env so the recreate uses it."""
    envf = STACK_MOUNT / ".env"
    lines = envf.read_text(encoding="utf-8").splitlines() if envf.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith("IMAGE_TAG=") or ln.strip().startswith("IMAGE_TAG ="):
            out.append(f"IMAGE_TAG={tag}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"IMAGE_TAG={tag}")
    envf.write_text("\n".join(out) + "\n", encoding="utf-8")


@app.get("/api/docker/status")
def docker_status() -> JSONResponse:
    ok = _docker_ok()
    stack_ready = bool(STACK_DIR_HOST) and (STACK_MOUNT / "docker-compose.yml").exists()
    return JSONResponse({
        "socket": Path("/var/run/docker.sock").exists(),
        "cli": Path(DOCKER).exists(),
        "ready": ok,
        "stack_ready": stack_ready,
        "repo": IMAGE_REPO,
        "current_tag": os.environ.get("IMAGE_TAG", "latest"),
        "reason": "" if (ok and stack_ready) else (
            "Docker socket not reachable" if not ok else
            "STACK_DIR not set / compose file not mounted at /stack"),
    })


@app.get("/api/docker/tags")
def docker_tags() -> JSONResponse:
    """Best-effort list of available tags from GHCR (public package)."""
    if not IMAGE_REPO or "ghcr.io/" not in IMAGE_REPO:
        return JSONResponse({"tags": []})
    name = IMAGE_REPO.split("ghcr.io/", 1)[1]
    try:
        import httpx
        tok = httpx.get(f"https://ghcr.io/token?scope=repository:{name}:pull",
                        timeout=10).json().get("token", "")
        r = httpx.get(f"https://ghcr.io/v2/{name}/tags/list",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        tags = r.json().get("tags", []) if r.status_code == 200 else []
        # Hide immutable per-commit build tags (sha-xxxx and bare hex SHAs)
        def _is_sha(t: str) -> bool:
            return t.startswith("sha-") or (
                len(t) >= 12 and all(c in "0123456789abcdef" for c in t))
        tags = [t for t in tags if not _is_sha(t)]
        return JSONResponse({"tags": sorted(tags)})
    except Exception as exc:
        return JSONResponse({"tags": [], "error": str(exc)})


class UpdateRequest(BaseModel):
    tag: str | None = None


@app.post("/api/docker/update")
def docker_update(req: UpdateRequest) -> JSONResponse:
    """Switch tag (optional) and recreate the stack via a detached helper
    container that outlives this one's recreation."""
    if not _docker_ok():
        raise HTTPException(400, "Docker socket not available")
    if not STACK_DIR_HOST or not (STACK_MOUNT / "docker-compose.yml").exists():
        raise HTTPException(400, "STACK_DIR not set or compose file not mounted at /stack")

    if req.tag:
        if not all(c.isalnum() or c in "._-" for c in req.tag) or len(req.tag) > 128:
            raise HTTPException(422, "Invalid tag")
        _set_env_tag(req.tag)

    # Helper mounts the HOST stack path (bind mounts are host-relative) and the
    # socket, waits for our HTTP response to flush, then pulls + recreates.
    helper = [
        DOCKER, "run", "--rm", "-d",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{STACK_DIR_HOST}:/stack",
        "-w", "/stack",
        HELPER_IMAGE, "sh", "-c",
        "sleep 3; docker compose pull && docker compose up -d",
    ]
    try:
        cid = subprocess.run(helper, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Timed out launching update helper")
    if cid.returncode != 0:
        raise HTTPException(500, f"Helper failed to start: {cid.stderr.strip()[:300]}")
    return JSONResponse({
        "updating": True,
        "tag": req.tag or os.environ.get("IMAGE_TAG", "latest"),
        "helper": cid.stdout.strip()[:12],
        "note": "Stack is recreating — this WebUI will disconnect and come back in ~15-30s.",
    })


# ---------------------------------------------------------------------------
# Config file editing (replaces the interactive init wizard)
# ---------------------------------------------------------------------------

FILES = {
    "profile": (PROFILE_PATH, WEBUI_DIR / "profile.example.json"),
    "searches": (SEARCH_CONFIG_PATH, WEBUI_DIR / "searches.example.yaml"),
    "resume": (RESUME_PATH, None),
}


@app.get("/api/file/{name}")
def read_file(name: str) -> JSONResponse:
    if name not in FILES:
        raise HTTPException(404, "Unknown file")
    path, template = FILES[name]
    if path.exists():
        return JSONResponse({"content": path.read_text(encoding="utf-8"),
                             "exists": True})
    content = template.read_text(encoding="utf-8") if template and template.exists() else ""
    return JSONResponse({"content": content, "exists": False})


class FileBody(BaseModel):
    content: str


@app.put("/api/file/{name}")
def write_file(name: str, body: FileBody) -> JSONResponse:
    if name not in FILES:
        raise HTTPException(404, "Unknown file")
    if name == "profile":
        try:
            json.loads(body.content)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, f"profile.json is not valid JSON: {exc}")
    if name == "searches":
        try:
            import yaml
            yaml.safe_load(body.content)
        except ImportError:
            pass
        except Exception as exc:
            raise HTTPException(422, f"searches.yaml is not valid YAML: {exc}")
    path, _ = FILES[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return JSONResponse({"saved": str(path)})


@app.post("/api/resume-pdf")
async def upload_resume_pdf(file: UploadFile = File(...)) -> JSONResponse:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(422, "Expected a .pdf file")
    data = await file.read()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF larger than 20MB")
    RESUME_PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESUME_PDF_PATH.write_bytes(data)
    return JSONResponse({"saved": str(RESUME_PDF_PATH), "bytes": len(data)})
