import base64
import logging
import os
import re
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


def routed_path(request: Request) -> str:
    """The path the router will actually dispatch on.

    NEVER authorize on `request.url.path` here: Starlette rebuilds that URL as
    f"{scheme}://{host_header}{path}" and re-parses it, so a request carrying
    `Host: h/health?` reads back as path "/health" while the router still serves
    the real target — i.e. any auth exemption matched against it is bypassable by
    an attacker-chosen Host header. `scope["path"]` is the routed value (root_path
    stripped exactly as starlette.routing does), so an exemption matched here can
    only ever admit the endpoint it names.
    """
    path = request.scope["path"]
    root = request.scope.get("root_path", "")
    if root and path.startswith(root):
        if path == root:
            return ""
        if path[len(root)] == "/":
            return path[len(root):]
    return path


# Google's consent redirect lands in whatever browser finished the consent, which
# need not hold a Basic Auth session (fresh browser, or credentials not replayed
# after the cross-origin hop) — a 401 prompt there kills the reconnect at the finish
# line. Exempting it is safe because the callback authenticates itself: it acts only
# when `state` matches a pending flow (unguessable, single-use, 30-min TTL) and a hit
# with no match renders the failure page without touching tokens or status. Minting
# those flows (POST .../oauth/start) stays behind auth. The id pattern is deliberately
# narrow, since everything it admits is served without credentials: [0-9] not \d (\d
# also matches Unicode digits the int-typed route never serves), and bounded in length
# because an id past SQLite's int64 range makes session.get raise, turning an anonymous
# request into a 500 with a ~19KB traceback in the log.
_OAUTH_CALLBACK_PATH = re.compile(r"/api/channels/[0-9]{1,18}/oauth/callback")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic Auth guard. Only active when MANAGER_APP_PASSWORD is set in .env."""
    path = routed_path(request)
    # Liveness endpoint stays unauthenticated so external uptime monitors can reach it;
    # it exposes only aggregate counts, never channel names or tokens.
    if path == "/health":
        return await call_next(request)
    if request.method == "GET" and _OAUTH_CALLBACK_PATH.fullmatch(path):
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


def _contained_file(root: Path, rel: str) -> Path | None:
    """`root / rel` if it is a readable file that stays inside `root`, else None.

    The SPA fallback below hands user-controlled path text to the filesystem, so
    containment is checked by RESOLVING first and comparing — never by scanning the
    input for "..", which would miss two of the three escape primitives:
      * `..` segments — uvicorn percent-decodes the request target before routing,
        so `/%2e%2e/x` (and `%2e%2e%2f`) arrive as real parent-directory hops;
      * an ABSOLUTE `rel` — `Path("dist") / "/etc/hosts"` is `/etc/hosts`, because
        pathlib drops the left operand, so `//etc/hosts` escapes with no ".." at all;
      * a symlink inside dist pointing out of it — resolve() collapses those too, so
        it is rejected rather than followed.
    Containment is by PATH, so it cannot see a hardlink inside dist to an outside
    file; that needs local write access to dist, which already means owning the SPA.
    `root` must already be resolved, or the comparison fails on every path reached
    through a symlinked ancestor (on macOS /tmp is itself a link to /private/tmp).
    """
    if not rel:
        return None          # fast path for GET /; the is_file() below also refuses it
    try:
        candidate = (root / rel).resolve()
        if not candidate.is_relative_to(root):
            return None
        if not candidate.is_file():
            return None
        # is_file() only stats; FileResponse opens. An unreadable file would raise
        # from inside the response, i.e. outside this guard.
        return candidate if os.access(candidate, os.R_OK) else None
    except (OSError, ValueError, RuntimeError):
        # A NUL byte, an over-long segment, or a symlink loop must fall through to
        # index.html, not raise: every one of these is anonymous-reachable when no
        # password is set, and an unhandled error hands the caller a 500 plus a
        # traceback-sized write into the log for each request.
        return None


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
        # Resolved per request, never pinned at import: if dist is a symlink that an
        # atomic-swap deploy retargets, a pinned root keeps serving the OLD build's
        # index.html while the new hashed assets fall through into it — exactly the
        # blank screen _NO_CACHE exists to prevent, and unfixable by any header.
        root = _dist.resolve()
        asset = _contained_file(root, full_path)
        if asset is not None:
            # Serve the real file, but no index.html spelling may be cached (a
            # case-insensitive volume accepts several, and dist can nest its own).
            no_cache = asset.name.lower() == "index.html"
            return FileResponse(asset, headers=_NO_CACHE if no_cache else None)
        return FileResponse(root / "index.html", headers=_NO_CACHE)
