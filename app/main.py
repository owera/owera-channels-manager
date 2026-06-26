import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import ensure_dirs, load_dotenv_into_env, settings
from app.db import init_db
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
