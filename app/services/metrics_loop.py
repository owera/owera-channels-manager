"""Daily snapshot of each connected channel's public YouTube statistics.

Records one ChannelMetric row per channel per UTC day so the UI can show
subscriber / view / video trends. Cheap (channels.list = 1 quota unit).
"""

import logging
from datetime import timezone

from sqlmodel import Session, select

from app.db import session_scope
from app.models import Channel, ChannelMetric, OAuthStatus
from app.services import notify, quota, youtube
from app.services.quota import _day_start

logger = logging.getLogger("manager.metrics")


def _snapshot_due(session: Session, channel_id: int) -> bool:
    """True if there's no snapshot for this channel since UTC midnight."""
    latest = session.exec(
        select(ChannelMetric).where(ChannelMetric.channel_id == channel_id)
        .order_by(ChannelMetric.captured_at.desc())
    ).first()
    if latest is None:
        return True
    cap = latest.captured_at
    if cap.tzinfo is None:                        # SQLite returns naive datetimes
        cap = cap.replace(tzinfo=timezone.utc)
    return cap < _day_start()


def record_snapshot(session: Session, channel: Channel) -> ChannelMetric | None:
    """Fetch live statistics and store a snapshot. Returns None if unreachable."""
    try:
        service = youtube.get_service(channel.slug)
        data = youtube.fetch_channel(service)
    except youtube.NeedsConnect as e:
        # A dead token on a channel with nothing queued never reaches the publish
        # loop — this daily probe is the alert path during a publishing lull.
        notify.mark_dead_committed(session, channel, str(e))
        return None
    except Exception as e:
        logger.info("metrics snapshot skipped for %s: %s", channel.slug, e)
        return None
    stats = data.get("statistics") or {}
    m = ChannelMetric(
        channel_id=channel.id,
        subscriber_count=stats.get("subscriber_count", 0),
        view_count=stats.get("view_count", 0),
        video_count=stats.get("video_count", 0),
    )
    session.add(m)
    quota.log(session, kind="metrics", status="success", channel_id=channel.id, quota_cost=1)
    return m


def tick() -> None:
    with session_scope() as session:
        channels = session.exec(
            select(Channel).where(Channel.oauth_status == OAuthStatus.CONNECTED)
        ).all()
        for channel in channels:
            if _snapshot_due(session, channel.id):
                record_snapshot(session, channel)
