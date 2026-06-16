"""Background scheduler running the render and publish ticks.

Uses a threaded BackgroundScheduler because all tick work is synchronous/blocking
(httpx, google-api, ffmpeg, file IO) and shouldn't run on the FastAPI event loop.
Jobs are non-overlapping (max_instances=1, coalesce=True).
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.services import (
    analytics_loop,
    autofill_loop,
    metrics_loop,
    publish_loop,
    render_loop,
)

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
    # Channel metrics: a light daily snapshot per channel. Runs every few hours
    # (the tick itself records at most once/UTC-day/channel) with an initial run
    # shortly after startup so trend data starts accumulating right away.
    _scheduler.add_job(
        _safe(metrics_loop.tick, "metrics"),
        "interval", hours=settings.metrics_tick_hours,
        id="metrics", max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    # Per-video analytics: a daily snapshot per published video so the growth loop
    # can learn what performs. The tick records at most once/UTC-day/video and is a
    # no-op for channels not yet reconsented for the analytics scope; first run is
    # delayed a minute so startup recovery/metrics settle first.
    _scheduler.add_job(
        _safe(analytics_loop.tick, "analytics"),
        "interval", hours=settings.analytics_tick_hours,
        id="analytics", max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    # Auto-refill low topic idea queues. The tick no-ops unless the setting is on,
    # so it's safe to always register; runs soon after start, then on its interval.
    _scheduler.add_job(
        _safe(autofill_loop.tick, "autofill"),
        "interval", minutes=settings.autofill_tick_minutes,
        id="autofill", max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
    )
    _scheduler.start()
    logger.info("scheduler started (render %ss / publish %ss / metrics %sh / "
                "analytics %sh / autofill %smin)",
                settings.render_tick_seconds, settings.publish_tick_seconds,
                settings.metrics_tick_hours, settings.analytics_tick_hours,
                settings.autofill_tick_minutes)
    return None


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
