"""Render tick: queued -> rendering -> rendered -> (review|approved), per Video.

Profile resolution per video: video.render_profile -> topic.render_profile ->
channel.default_render_profile.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Channel, RenderProfile, Topic, Video, VideoStatus, utcnow
from app.services import metadata, quota
from app.services.mpt_client import STATE_COMPLETE, STATE_FAILED, build_video_params, mpt
from app.services.topic_playlist import ensure_topic_playlist


def _profile_params(session: Session, profile_id) -> dict:
    if not profile_id:
        return {}
    p = session.get(RenderProfile, profile_id)
    if not p:
        return {}
    try:
        return json.loads(p.params_json or "{}")
    except json.JSONDecodeError:
        return {}


def _effective_skip_gate(video: Video, channel: Channel) -> bool:
    return channel.default_skip_gate if video.skip_gate is None else video.skip_gate


def _make_thumbnail(video_path: Path, out_path: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(out_path)],
            check=True, timeout=30,
        )
        return out_path.exists()
    except Exception:
        return False


def _finalize(session: Session, video: Video, channel: Channel, task: dict) -> None:
    src = mpt.local_final_path(video.mpt_task_id)
    if not src.exists():
        video.status = VideoStatus.FAILED
        video.error = f"render reported complete but {src} is missing"
        quota.log(session, kind="render", status="error", video_id=video.id,
                  channel_id=channel.id, detail=video.error)
        return

    dest_dir = Path(settings.storage_dir) / "videos" / str(video.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "video.mp4"
    shutil.copy(src, dest)
    video.video_path = str(dest)
    video.script = task.get("script") or video.script

    thumb = dest_dir / "thumb.jpg"
    if _make_thumbnail(dest, thumb):
        video.thumb_path = str(thumb)

    if not video.metadata_generated:
        meta = metadata.generate(video.subject, video.script or "")
        video.title = video.title or meta["title"]
        video.description = video.description or meta["description"]
        video.tags_json = video.tags_json or json.dumps(meta["tags"])
        video.metadata_generated = True

    video.render_progress = 100
    if _effective_skip_gate(video, channel):
        video.status = VideoStatus.APPROVED
        video.approved_at = utcnow()
    else:
        video.status = VideoStatus.REVIEW
    quota.log(session, kind="render", status="success", video_id=video.id, channel_id=channel.id)


def _advance_in_flight(session: Session) -> None:
    for video in session.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all():
        channel = session.get(Channel, video.channel_id)
        if video.last_attempt_at:
            started = video.last_attempt_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() > settings.render_timeout_seconds:
                video.status = VideoStatus.FAILED
                video.error = "render timed out"
                quota.log(session, kind="render", status="error", video_id=video.id,
                          channel_id=video.channel_id, detail=video.error)
                continue
        if not video.mpt_task_id:
            continue
        try:
            task = mpt.poll(video.mpt_task_id)
        except Exception:
            continue  # MPT unreachable — retry next tick
        video.render_progress = int(task.get("progress") or video.render_progress)
        if task.get("state") == STATE_COMPLETE:
            _finalize(session, video, channel, task)
        elif task.get("state") == STATE_FAILED:
            video.status = VideoStatus.FAILED
            video.error = "MPT reported render failure"
            quota.log(session, kind="render", status="error", video_id=video.id,
                      channel_id=video.channel_id, detail=video.error)


def _submit_new(session: Session) -> None:
    cfg = app_settings(session)
    in_flight = quota.in_flight_renders(session)
    if in_flight >= cfg.render_concurrency:
        return
    candidates = session.exec(
        select(Video).where(Video.status == VideoStatus.QUEUED)
        .order_by(Video.channel_id, Video.position, Video.id)
    ).all()
    for video in candidates:
        if in_flight >= cfg.render_concurrency:
            break
        channel = session.get(Channel, video.channel_id)
        if not channel or channel.paused:
            continue
        if quota.rendered_today(session, channel.id) >= channel.daily_render_budget:
            continue
        topic = session.get(Topic, video.topic_id)
        # A video is starting production → make sure its topic playlist exists.
        ensure_topic_playlist(session, topic, channel)
        params = build_video_params(
            video.subject,
            _profile_params(session, channel.default_render_profile_id),
            _profile_params(session, topic.render_profile_id if topic else None),
            _profile_params(session, video.render_profile_id),
            json.loads(video.overrides_json) if video.overrides_json else None,
        )
        try:
            task_id = mpt.submit(params)
        except Exception as e:
            quota.log(session, kind="render", status="error", video_id=video.id,
                      channel_id=channel.id, detail=f"submit failed: {e}")
            continue
        video.mpt_task_id = task_id
        video.status = VideoStatus.RENDERING
        video.render_progress = 0
        video.error = None
        video.last_attempt_at = utcnow()
        quota.log(session, kind="render", status="started", video_id=video.id, channel_id=channel.id)
        in_flight += 1


def tick() -> None:
    with session_scope() as session:
        if app_settings(session).scheduler_paused:
            return
        _advance_in_flight(session)
        _submit_new(session)
