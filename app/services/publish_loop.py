"""Publish tick: approved -> publishing -> published, per Video.

The playlist is the video's topic's playlist. Per-channel daily budget, quota cap,
and drip spacing apply.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Channel, OAuthStatus, Playlist, Topic, Video, VideoStatus, utcnow
from app.services import quota, youtube
from app.services.youtube import (NeedsConnect, QuotaExceeded, QUOTA_PLAYLISTITEM_INSERT,
                                  QUOTA_UPLOAD)


def _drip_ok(session: Session, channel: Channel, drip_minutes: int) -> bool:
    last = quota.last_publish_at(session, channel.id)
    if not last:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= timedelta(minutes=drip_minutes)


def _next_approved(session: Session, channel_id: int) -> Video | None:
    return session.exec(
        select(Video).where(
            Video.channel_id == channel_id, Video.status == VideoStatus.APPROVED
        ).order_by(Video.approved_at, Video.id)
    ).first()


def _publish_one(session: Session, channel: Channel, video: Video) -> None:
    video.status = VideoStatus.PUBLISHING
    video.render_progress = 0          # reuse as upload progress while publishing
    video.last_attempt_at = utcnow()
    session.add(video)
    session.commit()

    # Persist upload progress so the board card can show it (throttled to ~5% steps).
    _last = {"p": -10}

    def _progress(p: int):
        if p - _last["p"] >= 5 or p >= 100:
            _last["p"] = p
            video.render_progress = p
            session.add(video)
            session.commit()

    try:
        service = youtube.get_service(channel.slug)
    except NeedsConnect as e:
        channel.oauth_status = OAuthStatus.EXPIRED
        channel.oauth_error = str(e)
        video.status = VideoStatus.APPROVED
        quota.log(session, kind="publish", status="error", video_id=video.id,
                  channel_id=channel.id, detail=f"needs reconnect: {e}")
        return

    tags = json.loads(video.tags_json) if video.tags_json else []
    privacy = video.privacy or channel.default_privacy
    try:
        video_id = youtube.upload_video(
            service, video.video_path, video.title or video.subject,
            video.description or "", tags, privacy, progress_cb=_progress,
        )
    except QuotaExceeded as e:
        video.status = VideoStatus.APPROVED
        quota.log(session, kind="publish", status="error", video_id=video.id,
                  channel_id=channel.id, detail=f"quota exceeded: {e}")
        raise
    except Exception as e:
        video.status = VideoStatus.FAILED
        video.error = f"upload failed: {e}"
        video.retry_count += 1
        quota.log(session, kind="publish", status="error", video_id=video.id,
                  channel_id=channel.id, detail=video.error)
        return

    video.yt_video_id = video_id
    video.published_at = utcnow()
    video.status = VideoStatus.PUBLISHED
    quota.log(session, kind="publish", status="success", video_id=video.id,
              channel_id=channel.id, quota_cost=QUOTA_UPLOAD,
              detail=f"https://youtube.com/watch?v={video_id}")

    # Add to the topic's playlist (auto-create if it's somehow still missing).
    from app.services.topic_playlist import ensure_topic_playlist
    topic = session.get(Topic, video.topic_id)
    if topic and not topic.playlist_id:
        ensure_topic_playlist(session, topic, channel)
        session.refresh(topic)
    pl = session.get(Playlist, topic.playlist_id) if topic and topic.playlist_id else None
    if pl:
        try:
            youtube.add_to_playlist(service, pl.yt_playlist_id, video_id)
            video.added_to_playlist = True
            quota.log(session, kind="playlist_add", status="success", video_id=video.id,
                      channel_id=channel.id, quota_cost=QUOTA_PLAYLISTITEM_INSERT)
        except Exception as e:
            quota.log(session, kind="playlist_add", status="error", video_id=video.id,
                      channel_id=channel.id, detail=str(e))


def tick() -> None:
    with session_scope() as session:
        cfg = app_settings(session)
        if cfg.scheduler_paused:
            return
        for channel in session.exec(select(Channel).where(Channel.paused == False)).all():  # noqa: E712
            if channel.oauth_status != OAuthStatus.CONNECTED:
                continue
            if quota.daily_limit_hit(session, channel.id):
                continue  # quota / upload-limit already hit today — wait for reset
            if quota.published_today(session, channel.id) >= channel.daily_publish_budget:
                continue
            if quota.quota_spent_today(session, channel.id) + QUOTA_UPLOAD > settings.youtube_daily_quota_cap:
                continue
            if not _drip_ok(session, channel, cfg.publish_drip_minutes):
                continue
            video = _next_approved(session, channel.id)
            if not video:
                continue
            try:
                _publish_one(session, channel, video)
                session.commit()
            except QuotaExceeded:
                session.commit()
                continue
            except Exception:
                session.rollback()
