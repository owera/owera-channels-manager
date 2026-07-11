"""Per-channel daily counters: render/publish budgets and YouTube quota accounting."""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.models import JobRun, Topic, Video, VideoStatus


def _next_pt_midnight_utc(now: datetime) -> datetime:
    """Next midnight America/Los_Angeles, as tz-aware UTC. The YouTube Data API
    project quota resets at Pacific midnight (NOT UTC midnight), so resuming at
    UTC midnight would retry ~7-8h early and burn another guaranteed failure."""
    try:
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
        now_pt = now.astimezone(pt)
        next_mid = (now_pt + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return next_mid.astimezone(timezone.utc)
    except Exception:
        # tz database unavailable (slim container) — fall back to 08:00 UTC, which
        # is >= Pacific midnight year-round (08:00 in PST, 1h past reset in PDT).
        candidate = now.replace(hour=8, minute=0, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)


def next_quota_reset() -> datetime:
    """When the YouTube Data API project quota next resets (Pacific midnight, UTC)."""
    return _next_pt_midnight_utc(datetime.now(timezone.utc))


def cooldown_until_for(reason: str) -> datetime:
    """When a channel that just hit a YouTube daily cap may be retried (tz-aware UTC).

    - uploadLimitExceeded: the per-channel upload cap is a ROLLING 24h window with no
      fixed reset and no Retry-After header, so wait 24h from now.
    - quotaExceeded / dailyLimitExceeded: the API project quota resets at Pacific
      midnight — wait until then."""
    now = datetime.now(timezone.utc)
    if (reason or "").lower() == "uploadlimitexceeded":
        return now + timedelta(hours=24)
    return _next_pt_midnight_utc(now)


def _day_start() -> datetime:
    """Start of the local accounting day (UTC midnight). Used for the render budget,
    a local throttle that consumes no YouTube quota."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _quota_day_start() -> datetime:
    """Start of YouTube's current quota day = most recent Pacific midnight (UTC).
    Publishing and quota accounting use this so the app's 'today' lines up with when
    YouTube actually replenishes quota (it resets at Pacific midnight, not UTC)."""
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        now_pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
        return now_pt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    except Exception:
        c = now.replace(hour=8, minute=0, second=0, microsecond=0)  # >= Pacific midnight year-round
        return c if c <= now else c - timedelta(days=1)


def _count(session: Session, channel_id: int, kind: str, since: datetime) -> int:
    return session.exec(
        select(func.count(JobRun.id)).where(
            JobRun.channel_id == channel_id, JobRun.kind == kind,
            JobRun.status == "success", JobRun.created_at >= since,
        )
    ).one()


def published_today(session: Session, channel_id: int) -> int:
    return _count(session, channel_id, "publish", _quota_day_start())


def rendered_today(session: Session, channel_id: int) -> int:
    return _count(session, channel_id, "render", _day_start())


def published_long_today(session: Session, channel_id: int) -> int:
    """Long-form videos published this quota day. The publish loop reserves the
    day's first free slot for a long-form until this is nonzero."""
    return session.exec(
        select(func.count(Video.id))
        .join(Topic, Topic.id == Video.topic_id)
        .where(
            Video.channel_id == channel_id,
            Video.status == VideoStatus.PUBLISHED,
            Video.published_at >= _quota_day_start(),
            Topic.content_format == "long",
        )
    ).one()


def quota_spent_today(session: Session, channel_id: int) -> int:
    total = session.exec(
        select(func.coalesce(func.sum(JobRun.quota_cost), 0)).where(
            JobRun.channel_id == channel_id, JobRun.created_at >= _quota_day_start(),
        )
    ).one()
    return int(total or 0)


def last_publish_at(session: Session, channel_id: int):
    return session.exec(
        select(func.max(JobRun.created_at)).where(
            JobRun.channel_id == channel_id, JobRun.kind == "publish",
            JobRun.status == "success",
        )
    ).one()


def daily_limit_hit(session: Session, channel_id: int) -> bool:
    """True if this channel already hit a YouTube daily cap during the current quota
    day (quota or the per-channel upload limit). Both are logged with a 'quota
    exceeded:' detail prefix. Used to stop publishing for the channel until YouTube's
    quota resets (Pacific midnight) — otherwise the publish loop would retry every
    tick and hammer the API."""
    n = session.exec(
        select(func.count(JobRun.id)).where(
            JobRun.channel_id == channel_id, JobRun.kind == "publish",
            JobRun.status == "error", JobRun.created_at >= _quota_day_start(),
            JobRun.detail.like("quota exceeded:%"),
        )
    ).one()
    return n > 0


def in_flight_renders(session: Session) -> int:
    return session.exec(
        select(func.count(Video.id)).where(Video.status == VideoStatus.RENDERING)
    ).one()


def log(session: Session, *, kind: str, status: str, video_id=None, channel_id=None,
        detail: str = None, quota_cost: int = 0) -> None:
    session.add(JobRun(
        kind=kind, status=status, video_id=video_id, channel_id=channel_id,
        detail=(detail or "")[:1000], quota_cost=quota_cost,
    ))
