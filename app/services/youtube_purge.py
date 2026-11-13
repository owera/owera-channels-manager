"""P3: delete a channel's YouTube API data within 30 days of losing access.

YouTube API Services Developer Policies §III.E.4 and the Google API Services
User Data Policy: when a user revokes access (or the grant is otherwise gone),
API data stored for them must be deleted within 30 days.

Clock: ``Channel.access_lost_at`` (naive UTC), stamped by
- ``notify.mark_operator_disconnect`` — disconnect in the app (reason "disconnected");
- ``notify.mark_dead`` — a refresh that failed with ``invalid_grant`` (revoked /
  expired at Google; reason "invalid_grant").
A reconnect (``notify.mark_connected``) clears it and cancels the purge.

The scheduler runs ``tick`` every 6 h. A channel is purged once its clock is
``PURGE_AFTER`` (29 days) old, so with the tick interval the data is gone
within 30 days. Idempotent: ``youtube_data_purged_at`` >= ``access_lost_at``
means already purged; re-running deletes nothing more.

Deleted (YouTube API data only):
- ``ChannelMetric`` rows (subscriber/view/video counts from channels.list);
- ``VideoMetric`` rows (YouTube Analytics, including ``traffic_json`` search terms);
- the stored OAuth credential (encrypted row and any legacy token files);
- ``Channel.yt_channel_title``, and ``Channel.name`` when it is still the
  YouTube title (reset to the slug).

Kept (not API data the user can revoke, or not YouTube data at all): the
workspace, the channel row and its settings, topics, scripts, renders and
``Video`` rows (our own production records, including the ``yt_video_id`` of
videos CM uploaded), ``Playlist`` rows CM created, and ``yt_channel_id``
(the identity binding that stops a reconnect to the wrong channel).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlmodel import Session, select

from app.models import Channel, ChannelMetric, OAuthCredential, OAuthStatus, VideoMetric, utcnow

logger = logging.getLogger("manager.youtube_purge")

RETENTION_DAYS = 30
PURGE_AFTER = timedelta(days=RETENTION_DAYS - 1)   # + ≤6h tick ⇒ within 30 days
TICK_HOURS = 6


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def due_channels(session: Session, now: datetime | None = None) -> list[Channel]:
    now = _naive(now or utcnow())
    cutoff = now - PURGE_AFTER
    rows = session.exec(
        select(Channel).where(
            Channel.access_lost_at != None,  # noqa: E711
            Channel.access_lost_at <= cutoff,
            Channel.oauth_status != OAuthStatus.CONNECTED,
        )
    ).all()
    return [c for c in rows
            if c.youtube_data_purged_at is None
            or _naive(c.youtube_data_purged_at) < _naive(c.access_lost_at)]


def purge_channel(session: Session, channel: Channel, now: datetime | None = None) -> dict:
    """Delete one channel's YouTube API data and stamp youtube_data_purged_at.
    Commits. Returns row counts for the log line (no names, no tokens)."""
    from app.services import youtube

    now = _naive(now or utcnow())
    cid = channel.id
    n_cm = session.exec(delete(ChannelMetric).where(ChannelMetric.channel_id == cid)).rowcount
    n_vm = session.exec(delete(VideoMetric).where(VideoMetric.channel_id == cid)).rowcount
    cred = session.get(OAuthCredential, cid)
    if cred is not None:
        session.delete(cred)
    try:
        youtube.disconnect(channel.slug)          # legacy token.json / .bak / tmp
    except OSError as e:
        logger.warning("purge: token files for channel id=%s not removed: %s", cid, type(e).__name__)
    if channel.yt_channel_title and channel.name == channel.yt_channel_title:
        channel.name = channel.slug
    channel.yt_channel_title = None
    channel.youtube_data_purged_at = now
    channel.updated_at = now
    session.add(channel)
    session.commit()
    counts = {"channel_metrics": int(n_cm or 0), "video_metrics": int(n_vm or 0),
              "credential": cred is not None}
    logger.info("purged YouTube data for channel id=%s workspace=%s (lost %s, reason=%s): %s",
                cid, channel.workspace_id, channel.access_lost_at, channel.access_lost_reason, counts)
    return counts


def tick(now: datetime | None = None) -> int:
    """Scheduler entry point. Returns how many channels were purged."""
    from app.db import session_scope

    n = 0
    with session_scope() as session:
        for ch in due_channels(session, now):
            try:
                purge_channel(session, ch, now)
                n += 1
            except Exception:
                session.rollback()
                logger.exception("purge failed for channel id=%s", ch.id)
    return n
