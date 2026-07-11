"""Publish tick: approved -> publishing -> published, per Video.

The playlist is the video's topic's playlist. Per-channel daily budget, quota cap,
and drip spacing apply.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from pathlib import Path

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Channel, OAuthStatus, Playlist, Topic, Video, VideoStatus, utcnow
from app.services import metadata, quota, thumbnail, video_gen, youtube
from app.services.youtube import (NeedsConnect, QuotaExceeded, UploadStalled,
                                  QUOTA_COMMENT_INSERT, QUOTA_PLAYLISTITEM_INSERT,
                                  QUOTA_THUMBNAIL_SET, QUOTA_UPLOAD)

# Localized author first-comment (engagement seed + series pointer). Keyed by
# BCP-47 prefix; en is the fallback.
_FIRST_COMMENT = {
    "pt": "O que você mudaria nesse setup? Leio todos os comentários.",
    "en": "What would you change here? I read every comment.",
}
_FIRST_COMMENT_PLAYLIST = {
    "pt": "▶ Série completa:",
    "en": "▶ Full series:",
}


def _first_comment_text(language_code: str | None, playlist_yt_id: str | None) -> str:
    lang = (language_code or "en").split("-")[0].lower()
    text = _FIRST_COMMENT.get(lang, _FIRST_COMMENT["en"])
    if playlist_yt_id:
        tail = _FIRST_COMMENT_PLAYLIST.get(lang, _FIRST_COMMENT_PLAYLIST["en"])
        text += f"\n{tail} https://www.youtube.com/playlist?list={playlist_yt_id}"
    return text


def _drip_ok(session: Session, channel: Channel, drip_minutes: int) -> bool:
    last = quota.last_publish_at(session, channel.id)
    if not last:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= timedelta(minutes=drip_minutes)


def _next_approved(session: Session, channel_id: int) -> Video | None:
    # Daily mix: until a long-form has published this quota day, give the slot to
    # the oldest approved long-form. Plain FIFO never reaches one behind a deep
    # short backlog (4-shorts-+-1-long directive; longs convert subscribers).
    if not quota.published_long_today(session, channel_id):
        video = session.exec(
            select(Video)
            .join(Topic, Topic.id == Video.topic_id)
            .where(
                Video.channel_id == channel_id,
                Video.status == VideoStatus.APPROVED,
                Topic.content_format == "long",
            )
            .order_by(Video.approved_at, Video.id)
        ).first()
        if video:
            return video
    return session.exec(
        select(Video).where(
            Video.channel_id == channel_id, Video.status == VideoStatus.APPROVED
        ).order_by(Video.approved_at, Video.id)
    ).first()


def _recover_stuck_publishing(session: Session) -> None:
    """Reset videos stranded in 'publishing' (upload interrupted by a crash, restart,
    or network drop) back to 'approved' so they re-publish. The publish loop only
    advances 'approved' videos, so without this a stuck upload would sit forever.

    Skips uploads still inside the timeout window, so a genuinely in-flight upload is
    never reset out from under itself.

    Recovery is capped: an upload that keeps stalling (e.g. a resumable upload that hangs
    with no HTTP timeout) is re-queued at most ``publish_max_retries`` times, then marked
    ``failed`` instead of re-queued. Otherwise it re-queues → re-publishes → stalls forever,
    and — because recovery flips it back to APPROVED (in_flight → 0) before the channel loop
    runs — it defeats the in-flight guard and blocks every other publish on that channel."""
    now = datetime.now(timezone.utc)
    timeout = settings.publish_timeout_seconds
    cap = settings.publish_max_retries
    for v in session.exec(select(Video).where(Video.status == VideoStatus.PUBLISHING)).all():
        started = v.last_attempt_at or v.updated_at
        if started and started.tzinfo is None:        # SQLite returns naive datetimes
            started = started.replace(tzinfo=timezone.utc)
        if started and (now - started).total_seconds() < timeout:
            continue
        v.retry_count += 1
        if v.retry_count >= cap:
            # Given up: park it as failed so it stops blocking the channel's publish drip.
            v.status = VideoStatus.FAILED
            v.render_progress = 0
            v.error = (f"upload repeatedly stalled: stuck 'publishing' past {timeout}s on "
                       f"{v.retry_count} attempts — gave up re-queuing to unblock the channel")
            session.add(v)
            quota.log(session, kind="publish", status="error", video_id=v.id,
                      channel_id=v.channel_id,
                      detail=f"gave up on stuck publish after {v.retry_count} attempts — marked failed")
            continue
        v.status = VideoStatus.APPROVED
        v.render_progress = 0
        v.error = None
        session.add(v)
        quota.log(session, kind="publish", status="error", video_id=v.id,
                  channel_id=v.channel_id,
                  detail=f"recovered stuck publish (>{timeout}s) — re-queued (attempt {v.retry_count}/{cap})")
    session.commit()


def _set_custom_thumbnail(session: Session, service, channel: Channel,
                          video: Video, video_id: str) -> None:
    """Generate + upload a custom thumbnail. Best-effort: never raises, never fails a
    publish. Custom thumbnails require a phone-verified channel — an unverified channel
    returns 403, which is logged like any other thumbnail error and otherwise ignored."""
    if not video.video_path:
        return
    out_png = Path(video.video_path).parent / "thumb_custom.png"
    try:
        import json as _json
        _params = _json.loads(video.overrides_json or "{}")
        png = thumbnail.make_thumbnail_png(
            video.subject, video.title, out_png,
            topic_id=video.topic_id or 0,
            content_format=_params.get("content_format", "short"))
        if not png:
            quota.log(session, kind="thumbnail", status="error", video_id=video.id,
                      channel_id=channel.id, detail="thumbnail generation failed")
            return
        youtube.set_thumbnail(service, video_id, str(png))
        video.thumb_path = str(png)
        quota.log(session, kind="thumbnail", status="success", video_id=video.id,
                  channel_id=channel.id, quota_cost=QUOTA_THUMBNAIL_SET)
    except Exception as e:
        quota.log(session, kind="thumbnail", status="error", video_id=video.id,
                  channel_id=channel.id, detail=str(e)[:300])


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
        from app.services import notify
        video.status = VideoStatus.APPROVED
        quota.log(session, kind="publish", status="error", video_id=video.id,
                  channel_id=channel.id, detail=f"needs reconnect: {e}")
        notify.mark_dead_committed(session, channel, str(e))
        return

    # Ensure the topic playlist BEFORE upload so the description can link it.
    from app.services.topic_playlist import ensure_topic_playlist
    topic = session.get(Topic, video.topic_id)
    if topic and not topic.playlist_id:
        try:
            ensure_topic_playlist(session, topic, channel)
            session.refresh(topic)
        except Exception:
            pass  # best-effort: publish must not fail over a playlist
    pl = session.get(Playlist, topic.playlist_id) if topic and topic.playlist_id else None

    # Subscriber machinery: language-tagged upload + CTA/links description block.
    language_code = video_gen.channel_language_code(session, channel.id)
    video.description = metadata.finalize_description(
        video.description or "", language_code, channel.yt_channel_id,
        pl.yt_playlist_id if pl else None)
    session.add(video)
    session.commit()

    tags = json.loads(video.tags_json) if video.tags_json else []
    privacy = video.privacy or channel.default_privacy
    try:
        video_id = youtube.upload_video(
            service, video.video_path, video.title or video.subject,
            video.description or "", tags, privacy, progress_cb=_progress,
            language_code=language_code,
        )
    except QuotaExceeded as e:
        video.status = VideoStatus.APPROVED
        channel.cooldown_until = quota.cooldown_until_for(e.reason)
        session.add(channel)
        # Keep the "quota exceeded:" prefix — quota.daily_limit_hit() matches on it.
        quota.log(session, kind="publish", status="error", video_id=video.id,
                  channel_id=channel.id,
                  detail=f"quota exceeded: [{e.reason}] cooldown until "
                         f"{channel.cooldown_until.isoformat()}; {e}")
        raise
    except UploadStalled as e:
        # Transient socket stall (now caught fast by the HTTP timeout instead of hanging
        # the whole window). Retry up to the cap, then give up so it stops occupying the drip.
        video.retry_count += 1
        cap = settings.publish_max_retries
        if video.retry_count < cap:
            video.status = VideoStatus.APPROVED
            detail = f"upload stalled (retry {video.retry_count}/{cap}): {e}"
        else:
            video.status = VideoStatus.FAILED
            video.error = f"upload stalled and gave up after {video.retry_count} attempts: {e}"
            detail = video.error
        quota.log(session, kind="publish", status="error", video_id=video.id,
                  channel_id=channel.id, detail=detail)
        return
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

    # Custom thumbnail (biggest CTR lever) — best-effort, never fails the publish.
    _set_custom_thumbnail(session, service, channel, video, video_id)

    # Author first comment (engagement seed + series pointer) — best-effort.
    try:
        youtube.insert_comment(service, video_id,
                               _first_comment_text(language_code,
                                                   pl.yt_playlist_id if pl else None))
        quota.log(session, kind="comment", status="success", video_id=video.id,
                  channel_id=channel.id, quota_cost=QUOTA_COMMENT_INSERT)
    except Exception as e:
        quota.log(session, kind="comment", status="error", video_id=video.id,
                  channel_id=channel.id, detail=str(e)[:300])

    # Add to the topic's playlist (ensured before upload; recover if it vanished).
    if pl:
        try:
            youtube.add_to_playlist(service, pl.yt_playlist_id, video_id)
            video.added_to_playlist = True
            quota.log(session, kind="playlist_add", status="success", video_id=video.id,
                      channel_id=channel.id, quota_cost=QUOTA_PLAYLISTITEM_INSERT)
        except Exception as e:
            quota.log(session, kind="playlist_add", status="error", video_id=video.id,
                      channel_id=channel.id, detail=str(e))
            # If the playlist was deleted on YouTube, the local mapping is stale and
            # will 404 on every future publish for this topic. Drop the dead mapping so
            # the safety-net branch above auto-recreates a fresh playlist next publish.
            if topic is not None and topic.playlist_id and youtube.is_playlist_missing(e):
                topic.playlist_id = None
                session.add(topic)


def tick() -> None:
    with session_scope() as session:
        cfg = app_settings(session)
        if cfg.scheduler_paused:
            return
        _recover_stuck_publishing(session)            # re-queue any orphaned uploads
        for channel in session.exec(select(Channel).where(Channel.paused == False)).all():  # noqa: E712
            if channel.oauth_status != OAuthStatus.CONNECTED:
                continue
            if channel.cooldown_until:
                cu = channel.cooldown_until
                if cu.tzinfo is None:                 # SQLite returns naive datetimes
                    cu = cu.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < cu:
                    continue  # in cooldown after a YouTube daily cap — wait for reset
            if quota.daily_limit_hit(session, channel.id):
                continue  # fallback: same-day cap logged but cooldown not set
            if quota.published_today(session, channel.id) >= channel.daily_publish_budget:
                continue
            if (quota.quota_spent_today(session, channel.id) + QUOTA_UPLOAD
                    + QUOTA_THUMBNAIL_SET + QUOTA_COMMENT_INSERT
                    > settings.youtube_daily_quota_cap):
                continue
            if not _drip_ok(session, channel, cfg.publish_drip_minutes):
                continue
            # Skip if an upload is already in-flight for this channel.
            # APScheduler runs ticks in parallel threads; without this guard,
            # hung uploads (no HTTP timeout on resumable chunks) pile up across
            # ticks and every slot times out repeatedly instead of advancing.
            in_flight = session.exec(
                select(func.count(Video.id)).where(
                    Video.channel_id == channel.id,
                    Video.status == VideoStatus.PUBLISHING,
                )
            ).one()
            if in_flight:
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
