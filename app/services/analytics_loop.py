"""Daily per-video YouTube Analytics snapshots — the measurement foundation.

Records one VideoMetric row per published video per UTC day (lifetime-to-date
metrics) so the growth agent can learn which topics / formats / titles perform.
Each video costs ~2 quota units (two analytics queries); the loop honors the
per-channel quota cap and the global scheduler pause, and snapshots at most once
per video per UTC day (idempotent).

Analytics data lags 24–72h, so brand-new videos return empties — we skip videos
younger than a maturity window to avoid spending quota on guaranteed-empty queries.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Channel, OAuthStatus, Video, VideoMetric, VideoStatus
from app.services import notify, quota, youtube
from app.services.quota import _day_start

logger = logging.getLogger("manager.analytics")

# Videos younger than this have no analytics data yet (API latency); skip them.
_MIN_MATURITY_HOURS = 24
# Quota reserved per video: core query + (for viewed videos) traffic-source queries.
_QUOTA_PER_VIDEO = 2 * youtube.QUOTA_ANALYTICS_QUERY


def _snapshot_due(session: Session, video_id: int) -> bool:
    """True if there's no VideoMetric for this video since UTC midnight."""
    latest = session.exec(
        select(VideoMetric).where(VideoMetric.video_id == video_id)
        .order_by(VideoMetric.captured_at.desc())
    ).first()
    if latest is None:
        return True
    cap = latest.captured_at
    if cap.tzinfo is None:                        # SQLite returns naive datetimes
        cap = cap.replace(tzinfo=timezone.utc)
    return cap < _day_start()


def _mature(video: Video, now: datetime) -> bool:
    pub = video.published_at
    if pub is None:
        return False
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return (now - pub) >= timedelta(hours=_MIN_MATURITY_HOURS)


def record_video_snapshot(session: Session, analytics, channel: Channel,
                          video: Video, now: datetime) -> VideoMetric | None:
    """Fetch lifetime-to-date analytics for one video and store a snapshot.
    Returns None if the query is unreachable (logged, not raised). Propagates
    QuotaExceeded so the caller can stop hitting the API for this channel."""
    pub = video.published_at or video.created_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    start_date = pub.date().isoformat()
    end_date = now.date().isoformat()
    try:
        data = youtube.fetch_video_analytics(
            analytics, channel.yt_channel_id, video.yt_video_id, start_date, end_date)
    except youtube.QuotaExceeded:
        raise
    except Exception as e:
        # A failed call (e.g. the Analytics API not enabled in the Cloud project, or
        # the channel not yet reconsented) bills no quota — only successes do.
        logger.info("analytics snapshot skipped for video %s: %s", video.id, e)
        quota.log(session, kind="analytics", status="error", channel_id=channel.id,
                  video_id=video.id, detail=str(e), quota_cost=0)
        return None
    # Traffic-source attribution: only worth a query once the video has views.
    traffic_json = None
    if data["views"] > 0:
        try:
            traffic = youtube.fetch_traffic_sources(
                analytics, channel.yt_channel_id, video.yt_video_id, start_date, end_date)
            if traffic.get("sources"):
                import json as _json
                traffic_json = _json.dumps(traffic)
        except Exception as e:
            logger.info("traffic sources skipped for video %s: %s", video.id, e)

    m = VideoMetric(
        video_id=video.id,
        channel_id=channel.id,
        views=data["views"],
        impressions=data["impressions"],
        ctr=data["ctr"],
        avg_view_pct=data["avg_view_pct"],
        average_view_duration=data.get("average_view_duration", 0.0),
        watch_time_minutes=data["watch_time_minutes"],
        likes=data["likes"],
        comments=data["comments"],
        subscribers_gained=data["subscribers_gained"],
        traffic_json=traffic_json,
    )
    session.add(m)
    quota.log(session, kind="analytics", status="success", channel_id=channel.id,
              video_id=video.id, quota_cost=_QUOTA_PER_VIDEO)
    return m


def _dead_token_error(slug: str) -> str | None:
    """The NeedsConnect message if the narrow-scope (upload) token is dead,
    None when it still loads — i.e. an analytics failure is scope-only or
    transient and must not kill the channel."""
    try:
        youtube.get_service(slug)
        return None
    except youtube.NeedsConnect as e:
        return str(e)
    except Exception:
        return None


def _snapshot_channel(session: Session, channel: Channel, now: datetime,
                      force: bool = False) -> int:
    """Snapshot every due, mature published video for one channel; returns the count
    recorded. Builds the analytics client once; skips the whole channel if it isn't
    reconsented yet. `force` bypasses the once-per-day gate (for manual refresh)."""
    try:
        analytics = youtube.get_analytics_service(channel.slug)
    except Exception as e:
        # Distinguish a genuinely dead token from one merely missing the
        # analytics scope: the narrow-scope Data-API creds still load for the
        # latter, and only the former may flip (and alert) the channel.
        dead = _dead_token_error(channel.slug)
        if dead is not None:
            notify.mark_dead_committed(session, channel, dead)
            return 0
        logger.info("analytics unavailable for %s (reconnect for analytics?): %s",
                    channel.slug, e)
        return 0

    videos = session.exec(
        select(Video).where(
            Video.channel_id == channel.id,
            Video.status == VideoStatus.PUBLISHED,
            Video.yt_video_id.is_not(None),
        )
        # Newest first: when the quota cap cuts the pass short, starve the old,
        # slow-moving tail — never the fresh cohort the growth loop steers by.
        .order_by(Video.published_at.desc())
    ).all()
    cap = settings.youtube_daily_quota_cap
    recorded = 0
    for video in videos:
        if not force and not _snapshot_due(session, video.id):
            continue
        if not _mature(video, now):
            continue
        if quota.quota_spent_today(session, channel.id) + _QUOTA_PER_VIDEO > cap:
            logger.info("analytics: quota cap reached for %s, stopping", channel.slug)
            break
        try:
            ok = record_video_snapshot(session, analytics, channel, video, now)
        except youtube.QuotaExceeded as e:
            logger.info("analytics: quota exceeded for %s: %s", channel.slug, e)
            break
        session.commit()           # persist each snapshot so quota accounting is live
        if ok:
            recorded += 1
        elif recorded == 0:
            # The first attempt hard-failed and nothing has succeeded — the channel
            # isn't analytics-ready (API disabled in the Cloud project, or not yet
            # reconsented for the scope). Stop hammering the rest; retry next tick.
            logger.info("analytics: first call failed for %s — channel not ready, "
                        "skipping the rest this tick", channel.slug)
            break
    return recorded


def tick() -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        if app_settings(session).scheduler_paused:
            return
        channels = session.exec(
            select(Channel).where(Channel.oauth_status == OAuthStatus.CONNECTED)
        ).all()
        for channel in channels:
            if not channel.yt_channel_id:
                continue
            _snapshot_channel(session, channel, now)
