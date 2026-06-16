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
from app.services import quota, youtube
from app.services.quota import _day_start

logger = logging.getLogger("manager.analytics")

# Videos younger than this have no analytics data yet (API latency); skip them.
_MIN_MATURITY_HOURS = 24
# Quota reserved per video: two analytics queries (core + impressions/CTR).
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
        logger.info("analytics snapshot skipped for video %s: %s", video.id, e)
        quota.log(session, kind="analytics", status="error", channel_id=channel.id,
                  video_id=video.id, detail=str(e), quota_cost=_QUOTA_PER_VIDEO)
        return None
    m = VideoMetric(
        video_id=video.id,
        channel_id=channel.id,
        views=data["views"],
        impressions=data["impressions"],
        ctr=data["ctr"],
        avg_view_pct=data["avg_view_pct"],
        watch_time_minutes=data["watch_time_minutes"],
        likes=data["likes"],
        comments=data["comments"],
        subscribers_gained=data["subscribers_gained"],
    )
    session.add(m)
    quota.log(session, kind="analytics", status="success", channel_id=channel.id,
              video_id=video.id, quota_cost=_QUOTA_PER_VIDEO)
    return m


def _snapshot_channel(session: Session, channel: Channel, now: datetime,
                      force: bool = False) -> int:
    """Snapshot every due, mature published video for one channel; returns the count
    recorded. Builds the analytics client once; skips the whole channel if it isn't
    reconsented yet. `force` bypasses the once-per-day gate (for manual refresh)."""
    try:
        analytics = youtube.get_analytics_service(channel.slug)
    except Exception as e:
        # Not reconsented for the analytics scope yet (or token unrefreshable).
        logger.info("analytics unavailable for %s (reconnect for analytics?): %s",
                    channel.slug, e)
        return 0

    videos = session.exec(
        select(Video).where(
            Video.channel_id == channel.id,
            Video.status == VideoStatus.PUBLISHED,
            Video.yt_video_id.is_not(None),
        )
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
            if record_video_snapshot(session, analytics, channel, video, now):
                recorded += 1
        except youtube.QuotaExceeded as e:
            logger.info("analytics: quota exceeded for %s: %s", channel.slug, e)
            break
        session.commit()           # persist each snapshot so quota accounting is live
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
