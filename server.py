"""Local development backend for the Forum frontend.

Wraps the `forum audit` CLI in a FastAPI app so you can submit a repo URL
from the browser, watch the CLI's progress stream live via SSE, and have
the resulting artifacts automatically dropped into `docs/data/<slug>/` and
registered in `docs/data/manifest.json`.

This is a local-dev convenience — no auth, no rate-limit, single job at a
time. Run with: `uvicorn server:app --reload`. Visit http://localhost:8000/.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"
MANIFEST = DATA / "manifest.json"
SVG_TO_JSON = DOCS / "tools" / "svg_to_graph_json.py"

# Bearer token gating /api/*. Set FORUM_API_TOKEN in the server env and share
# it (plus the public tunnel URL) with your trusted users. Local-only when
# unset — keeps the no-auth dev flow working.
API_TOKEN = os.environ.get("FORUM_API_TOKEN")

# In-process job state — single user, single laptop, so a dict is enough.
_JOBS: dict[str, "Job"] = {}
_active_job_id: str | None = None
_lock = asyncio.Lock()


EVENT_PREFIX = "__FORUM_EVENT__"  # mirrors forum.events.EVENT_PREFIX


@dataclass
class Job:
    id: str
    slug: str
    repo_url: str
    language: str | None
    top_n: int
    status: str = "queued"      # queued | running | completed | failed
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Items are (event_type, payload). event_type is "log" (plain string) or
    # "event" (JSON-encoded structured event from forum.events).
    buffer: list[tuple[str, str]] = field(default_factory=list)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    def emit(self, event_type: str, payload: str) -> None:
        self.buffer.append((event_type, payload))
        for q in list(self._subscribers):
            q.put_nowait((event_type, payload))

    def emit_log(self, line: str) -> None:
        self.emit("log", line)

    def emit_event(self, payload: str) -> None:
        self.emit("event", payload)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q


class AuditRequest(BaseModel):
    repo_url: str = Field(..., description="git-cloneable URL")
    slug: str | None = None
    language: str | None = Field(None, pattern="^(python|c|auto)$")
    top_n: int = Field(5, ge=1, le=20)


app = FastAPI(title="Forum local backend")

# CORS — frontend on github.io needs to call this server cross-origin. We
# accept any origin because the bearer-token check is the actual gate;
# without the token, every request is rejected regardless of origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(request: Request, token: str | None = Query(default=None)) -> None:
    """Bearer-token gate for /api/* routes.

    Accepts either an `Authorization: Bearer <token>` header (preferred) or
    a `?token=` query parameter (needed for EventSource, which can't set
    headers).  Disabled entirely when FORUM_API_TOKEN is unset — same-origin
    local-dev keeps working with no friction.
    """
    if API_TOKEN is None:
        return
    auth = request.headers.get("authorization", "")
    presented = None
    if auth.lower().startswith("bearer "):
        presented = auth.split(None, 1)[1].strip()
    elif token:
        presented = token
    if presented != API_TOKEN:
        raise HTTPException(401, "Missing or invalid bearer token.")


@app.post("/api/audits", dependencies=[Depends(require_token)])
async def start_audit(req: AuditRequest) -> dict:
    global _active_job_id

    async with _lock:
        if _active_job_id and _JOBS[_active_job_id].status in ("queued", "running"):
            raise HTTPException(409, "An audit is already running. Wait for it to finish.")

        slug = (req.slug or _derive_slug(req.repo_url)).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", slug):
            raise HTTPException(400, "Slug must be lowercase alphanum + hyphens, ≤41 chars.")
        if (DATA / slug).exists():
            raise HTTPException(409, f"docs/data/{slug}/ already exists — pick a new slug.")

        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id, slug=slug, repo_url=req.repo_url,
            language=req.language if req.language and req.language != "auto" else None,
            top_n=req.top_n,
        )
        _JOBS[job_id] = job
        _active_job_id = job_id
        asyncio.create_task(_run_job(job))
        return {"job_id": job_id, "slug": slug}


@app.get("/api/audits/{job_id}", dependencies=[Depends(require_token)])
async def get_audit(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return {
        "job_id": job.id,
        "slug": job.slug,
        "status": job.status,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


@app.get("/api/audits/{job_id}/stream", dependencies=[Depends(require_token)])
async def stream_audit(job_id: str) -> StreamingResponse:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    async def gen() -> AsyncIterator[bytes]:
        # Replay backlog so late subscribers don't miss anything.
        for event_type, payload in job.buffer:
            yield _sse(event_type, payload)
        q = job.subscribe()
        while True:
            if job.status in ("completed", "failed") and q.empty():
                done_payload = json.dumps({
                    "status": job.status, "slug": job.slug, "error": job.error,
                })
                yield _sse("done", done_payload)
                return
            try:
                event_type, payload = await asyncio.wait_for(q.get(), timeout=15.0)
                yield _sse(event_type, payload)
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/manifest", dependencies=[Depends(require_token)])
async def get_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


# Slug shape validation — must match the slug rules used by submit_audit so
# someone can't pass ".." or absolute paths through and delete arbitrary disk.
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@app.delete("/api/audits/{slug}", dependencies=[Depends(require_token)])
async def delete_audit(slug: str) -> dict:
    """Remove docs/data/<slug>/ + drop the entry from manifest.json.

    Refuses to delete if a job for this slug is currently running, to keep
    the runner from writing into a dir we just removed.
    """
    if not _SAFE_SLUG.match(slug):
        raise HTTPException(status_code=400, detail=f"bad slug: {slug!r}")

    # Block if a live job is mid-write into this slug's dir.
    for job in _JOBS.values():
        if job.slug == slug and job.status in ("queued", "running"):
            raise HTTPException(
                status_code=409,
                detail=f"audit job {job.id} is still {job.status} on slug {slug!r}",
            )

    target = DATA / slug
    # Final safety: target must live UNDER DATA (no symlink escape).
    try:
        target.resolve().relative_to(DATA.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="slug resolves outside data dir")

    removed_dir = False
    if target.exists() and target.is_dir():
        shutil.rmtree(target)
        removed_dir = True

    # Drop from manifest. Idempotent — safe to call on already-deleted slug.
    manifest = json.loads(MANIFEST.read_text())
    before = len(manifest.get("audits", []))
    manifest["audits"] = [a for a in manifest.get("audits", []) if a.get("slug") != slug]
    removed_entry = (len(manifest["audits"]) != before)
    # If the deleted slug was the default, fall back to whatever's left.
    if manifest.get("default") == slug:
        manifest["default"] = manifest["audits"][0]["slug"] if manifest["audits"] else None
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")

    return {
        "slug": slug,
        "removed_dir": removed_dir,
        "removed_manifest_entry": removed_entry,
        "remaining": [a["slug"] for a in manifest["audits"]],
    }


# Static mount last so /api/* routes win.
app.mount("/", StaticFiles(directory=str(DOCS), html=True), name="docs")


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

async def _run_job(job: Job) -> None:
    global _active_job_id
    try:
        job.status = "running"
        job.emit_log(f"[forum-server] cloning {job.repo_url}…")

        with tempfile.TemporaryDirectory(prefix=f"forum-{job.slug}-") as workdir:
            workpath = Path(workdir)
            repo_dir = workpath / "repo"
            cache_dir = workpath / "cache"

            await _exec_streaming(
                ["git", "clone", "--depth=1", job.repo_url, str(repo_dir)],
                job,
            )

            cmd = [
                sys.executable, "-m", "forum.cli", "audit",
                str(repo_dir),
                "--cache", str(cache_dir),
                "--top-n", str(job.top_n),
            ]
            if job.language:
                cmd += ["--language", job.language]

            job.emit_log(f"[forum-server] running: {' '.join(cmd[3:])}")
            rc = await _exec_streaming(
                cmd, job,
                # FORUM_EVENTS=1 turns on per-token streaming in the cache layer.
                env_extra={"NO_COLOR": "1", "FORCE_COLOR": "0", "FORUM_EVENTS": "1"},
            )
            if rc != 0:
                raise RuntimeError(f"`forum audit` exited with code {rc}")

            # CLI writes to cache_dir/<hash>/. Grab whichever dir it produced.
            audit_dirs = [p for p in cache_dir.iterdir() if p.is_dir()]
            if not audit_dirs:
                raise RuntimeError("CLI completed but no audit dir was written.")
            audit_out = max(audit_dirs, key=lambda p: p.stat().st_mtime)

            job.emit_log(f"[forum-server] copying artifacts → docs/data/{job.slug}/")
            await _publish_audit(audit_out, job)

        job.status = "completed"
        job.finished_at = time.time()
        job.emit_log("[forum-server] done.")
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.finished_at = time.time()
        job.emit_log(f"[forum-server] FAILED: {exc}")
    finally:
        async with _lock:
            if _active_job_id == job.id:
                _active_job_id = None


async def _exec_streaming(cmd: list[str], job: Job,
                          env_extra: dict[str, str] | None = None) -> int:
    env = {**os.environ, **(env_extra or {})}
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        try:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:  # noqa: BLE001
            continue
        # Structured events from the CLI come through stdout with this prefix
        # so we don't need a side channel. Everything else is rendered as log.
        if line.startswith(EVENT_PREFIX):
            job.emit_event(line[len(EVENT_PREFIX):].lstrip())
        else:
            job.emit_log(line)
    return await proc.wait()


async def _publish_audit(audit_out: Path, job: Job) -> None:
    """Copy artifacts into docs/data/<slug>/, derive graph.json, update manifest."""
    target = DATA / job.slug
    target.mkdir(parents=True, exist_ok=True)
    for name in ("evidence.json", "prioritized.json", "verdicts.json", "report.md", "graph.svg"):
        src = audit_out / name
        if src.exists():
            shutil.copy2(src, target / name)

    if (target / "graph.svg").exists():
        rc = subprocess.run([sys.executable, str(SVG_TO_JSON)], capture_output=True)
        if rc.returncode != 0:
            job.emit(f"[forum-server] svg→json warning: {rc.stderr.decode().strip()[:200]}")

    evidence = json.loads((target / "evidence.json").read_text())
    commit_sha = (evidence.get("git_summary", {}) or {}).get("commit_sha") or evidence.get("commit_sha") or ""
    language = (evidence.get("language") or evidence.get("git_summary", {}).get("language") or "python")
    num_modules = (evidence.get("graph_summary") or {}).get("num_modules", "?")

    manifest = json.loads(MANIFEST.read_text())
    manifest["audits"] = [a for a in manifest["audits"] if a["slug"] != job.slug]
    manifest["audits"].append({
        "slug": job.slug,
        "label": job.slug,
        "version": "live",
        "language": language,
        "source": _strip_git_url(job.repo_url),
        "commit": commit_sha[:8] if commit_sha else "",
        "note": f"Live-audited from {_strip_git_url(job.repo_url)} — {num_modules} modules.",
    })
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")


def _derive_slug(repo_url: str) -> str:
    # github.com/foo/bar(.git) → bar
    last = repo_url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", last).lower()


def _strip_git_url(url: str) -> str:
    return re.sub(r"^(https?://|git@)", "", url).replace(":", "/").rstrip("/").removesuffix(".git")


def _sse(event: str, data: str) -> bytes:
    # Multi-line data must each be prefixed with `data: `.
    lines = "".join(f"data: {ln}\n" for ln in data.splitlines() or [""])
    return f"event: {event}\n{lines}\n".encode("utf-8")
