import base64
import logging
import secrets
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, func, select

from app.config import ensure_dirs, load_dotenv_into_env, settings
from app.db import get_session, init_db
from app.models import Channel, OAuthStatus, Video, VideoStatus
from app.routers import (channels, media, music, playlists, profiles, queue,
                         settings as settings_router, topics, trends, videos,
                         youtube_admin)
from app.services import render_loop, scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv_into_env()
    ensure_dirs()
    init_db()
    render_loop.recover_orphaned_renders()        # re-queue renders orphaned by a restart
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="Owera Channels Manager", lifespan=lifespan)

# Dev convenience: the Vite dev server proxies /api, but allow direct CORS too.
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic Auth guard. Only active when MANAGER_APP_PASSWORD is set in .env."""
    # Liveness endpoint stays unauthenticated so external uptime monitors can reach it;
    # it exposes only aggregate counts, never channel names or tokens.
    if request.url.path == "/health":
        return await call_next(request)
    if not settings.app_password:
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
            _username, _, password = decoded.partition(":")
            if secrets.compare_digest(password, settings.app_password):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Owera Channels Manager"'},
    )

for r in (channels, playlists, profiles, topics, videos, queue, media, settings_router,
          youtube_admin, trends, music):
    app.include_router(r.router)


PROCESS_HEADROOM_DEGRADED_PCT = 85


def _read_process_headroom() -> dict | None:
    """Current process count vs macOS's kern.maxproc, without spawning render/publish
    subprocesses (only two cheap, read-only forks of our own: sysctl + ps). Returns
    None if the reading can't be taken — the failure mode we're watching for is
    exactly forking becoming unavailable, so a read failure must not itself crash
    /health; it just means this block is omitted rather than status flipping."""
    try:
        maxproc = int(subprocess.run(
            ["sysctl", "-n", "kern.maxproc"], capture_output=True, text=True,
            timeout=2, check=True,
        ).stdout.strip())
        count = len(subprocess.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True,
            timeout=2, check=True,
        ).stdout.splitlines())
        return {
            "count": count,
            "max": maxproc,
            "pct_used": round(count / maxproc * 100, 1),
        }
    except Exception:
        return None


def _health_snapshot(session: Session) -> dict:
    """Aggregate health for uptime monitors — no channel names or tokens. 'degraded'
    when a channel can't publish (oauth not connected and not intentionally paused),
    any video is in the failed state, or the OS is close to running out of process
    slots (the 2026-07-06 fork-exhaustion incident had no signal until it self-recovered)."""
    channels = session.exec(select(Channel)).all()
    needs_attention = sum(
        1 for c in channels
        if c.oauth_status != OAuthStatus.CONNECTED and not c.paused
    )
    failed = int(session.exec(
        select(func.count(Video.id)).where(Video.status == VideoStatus.FAILED)
    ).one())
    processes = _read_process_headroom()
    processes_low = processes is not None and processes["pct_used"] >= PROCESS_HEADROOM_DEGRADED_PCT
    return {
        "status": "degraded" if (needs_attention or failed or processes_low) else "ok",
        "channels_total": len(channels),
        "channels_connected": sum(1 for c in channels
                                  if c.oauth_status == OAuthStatus.CONNECTED),
        "channels_paused": sum(1 for c in channels if c.paused),
        "channels_needing_attention": needs_attention,
        "videos_failed": failed,
        "system": {"processes": processes},
    }


@app.get("/health")
def health(session: Session = Depends(get_session)):
    """Unauthenticated liveness + degradation summary for uptime monitors (aggregate-only)."""
    return _health_snapshot(session)


# Serve the built SPA (if present) with client-side-routing fallback.
_dist = Path(settings.frontend_dist)
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    # index.html must never be cached: its hashed asset references change on every
    # build, and a stale cached index.html points at a deleted bundle (blank screen).
    # The hashed /assets are immutable and stay cacheable.
    _NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = _dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html", headers=_NO_CACHE)
