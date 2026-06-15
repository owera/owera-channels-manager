import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import ensure_dirs, load_dotenv_into_env, settings
from app.db import init_db
from app.routers import (channels, media, playlists, profiles, queue,
                         settings as settings_router, topics, videos,
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

for r in (channels, playlists, profiles, topics, videos, queue, media, settings_router,
          youtube_admin):
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
