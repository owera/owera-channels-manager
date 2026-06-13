"""Background scheduler running the render and publish ticks.

Uses a threaded BackgroundScheduler because all tick work is synchronous/blocking
(httpx, google-api, ffmpeg, file IO) and shouldn't run on the FastAPI event loop.
Jobs are non-overlapping (max_instances=1, coalesce=True).
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.services import publish_loop, render_loop

logger = logging.getLogger("manager.scheduler")

_scheduler: BackgroundScheduler | None = None


def _safe(fn, name):
    def wrapper():
        try:
            fn()
        except Exception as e:  # never let a tick kill the scheduler thread
            logger.exception(f"{name} tick failed: {e}")
    return wrapper


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _safe(render_loop.tick, "render"),
        "interval", seconds=settings.render_tick_seconds,
        id="render", max_instances=1, coalesce=True,
    )
    _scheduler.add_job(
        _safe(publish_loop.tick, "publish"),
        "interval", seconds=settings.publish_tick_seconds,
        id="publish", max_instances=1, coalesce=True,
    )
    _scheduler.start()
    logger.info("scheduler started (render %ss / publish %ss)",
                settings.render_tick_seconds, settings.publish_tick_seconds)
    return None


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
