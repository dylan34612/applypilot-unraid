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
from applypilot.database import init_db, get_stats

WEBUI_DIR = Path(__file__).parent
MAX_PDF_BYTES = 20 * 1024 * 1024

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
    })


@app.get("/api/logs")
def logs(since: int = 0) -> JSONResponse:
    lines = [{"seq": s, "line": l} for s, l in runner.buf if s > since]
    return JSONResponse({"lines": lines, "seq": runner.seq})


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

VALID_STAGES = {"all", "discover", "enrich", "score", "tailor", "cover", "pdf"}


class TaskRequest(BaseModel):
    action: str
    stages: list[str] | None = None
    workers: int = 1
    min_score: int | None = None
    dry_run: bool = False


@app.post("/api/task")
def start_task(req: TaskRequest) -> JSONResponse:
    workers = max(1, min(req.workers, 8))

    if req.action == "run":
        stages = [s for s in (req.stages or ["all"]) if s in VALID_STAGES] or ["all"]
        args = ["applypilot", "run", *stages]
        if workers > 1:
            args += ["--workers", str(workers)]
        if req.min_score is not None:
            args += ["--min-score", str(max(1, min(req.min_score, 10)))]
        task = f"run {' '.join(stages)}"

    elif req.action == "apply":
        args = ["applypilot", "apply", "--headless"]
        if req.dry_run:
            args.append("--dry-run")
        if workers > 1:
            args += ["--workers", str(workers)]
        if req.min_score is not None:
            args += ["--min-score", str(max(1, min(req.min_score, 10)))]
        task = "apply (dry-run)" if req.dry_run else "apply"

    elif req.action == "doctor":
        args = ["applypilot", "doctor"]
        task = "doctor"

    else:
        raise HTTPException(400, f"Unknown action: {req.action}")

    runner.start(task, args)
    return JSONResponse({"started": task})


@app.post("/api/stop")
def stop_task() -> JSONResponse:
    return JSONResponse({"stopped": runner.stop()})


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
