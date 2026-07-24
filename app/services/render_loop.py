"""Render tick: queued -> rendering -> rendered -> (review|approved), per Video.

Profile resolution per video: video.render_profile -> topic.render_profile ->
channel.default_render_profile.
"""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Channel, RenderProfile, Topic, Video, VideoStatus, utcnow
from app.services import metadata, quota
from app.services.engines import STATE_COMPLETE, STATE_FAILED, get_engine, resolve_engine
from app.services.engines.worker import _has_visible_frames
from app.services.mpt_client import build_video_params
from app.services.topic_playlist import ensure_topic_playlist

logger = logging.getLogger("manager.render")


def recover_orphaned_renders() -> None:
    """Re-queue renders left in 'rendering' by a previous process. HyperFrames runs
    on in-process daemon threads that die with the process, so any such render still
    marked 'rendering' at startup is orphaned — nothing will ever advance it. MPT runs
    in its own service and its task survives a manager restart, so leave those to be
    re-polled by the render loop. Call once at startup."""
    with session_scope() as session:
        stuck = session.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all()
        n = 0
        for v in stuck:
            if v.engine == "mpt":
                continue  # external task survives the restart; the render loop re-polls it
            v.status = VideoStatus.QUEUED
            v.mpt_task_id = None
            v.render_progress = 0
            v.error = None
            session.add(v)
            quota.log(session, kind="render", status="error", video_id=v.id,
                      channel_id=v.channel_id, detail="recovered orphaned render — re-queued")
            n += 1
        if n:
            logger.info("recovered %d orphaned in-process render(s) at startup", n)


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


def _format_overrides(content_format: str) -> dict:
    """Highest-priority render params for long-form: force a landscape aspect and a
    longer script. Shorts keep the existing profile-driven behavior (no overrides)."""
    if content_format == "long":
        return {"video_aspect": "16:9", "paragraph_number": 8}
    return {}


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


def _finalize(session: Session, video: Video, channel: Channel, engine, task: dict) -> None:
    src = engine.final_path(video.mpt_task_id)
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

    # Last gate before APPROVED -> auto-publish: reject a blank render (covers every
    # engine, including ones without the worker-side pre-mux pixel check).
    if not _has_visible_frames(dest):
        video.status = VideoStatus.FAILED
        video.error = "post-render frames blank at finalize — not publishing"
        quota.log(session, kind="render", status="error", video_id=video.id,
                  channel_id=channel.id, detail=video.error)
        return

    video.script = task.get("script") or video.script
    if task.get("creation_config"):
        video.creation_config = json.dumps(task["creation_config"])

    thumb = dest_dir / "thumb.jpg"
    if _make_thumbnail(dest, thumb):
        video.thumb_path = str(thumb)

    if not video.metadata_generated:
        from app.services import video_gen
        topic = session.get(Topic, video.topic_id)
        fmt = "long" if topic and topic.content_format == "long" else "short"
        meta = metadata.generate(video.subject, video.script or "", fmt,
                                 language=video_gen.channel_language(session, video.channel_id))
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
        engine = get_engine(video.engine)
        try:
            task = engine.poll(video.mpt_task_id)
        except Exception:
            continue  # engine unreachable — retry next tick
        video.render_progress = int(task.get("progress") or video.render_progress)
        if task.get("state") == STATE_COMPLETE:
            _finalize(session, video, channel, engine, task)
        elif task.get("state") == STATE_FAILED:
            err = task.get("error") or f"{video.engine or 'mpt'} reported render failure"
            _TRANSIENT = ("overloaded_error", "rate_limit_error", "RateLimitError",
                          "overloaded", "529", "503")
            if any(sig in err for sig in _TRANSIENT) and video.retry_count < 2:
                # Re-render: go back to QUEUED so _submit_new picks it up again.
                # (APPROVED would skip rendering and hand a file-less video to the
                # publish loop.) Clear the engine handle + progress for a clean retry.
                video.status = VideoStatus.QUEUED
                video.retry_count += 1
                video.mpt_task_id = None
                video.render_progress = 0
                video.error = None
                quota.log(session, kind="render", status="error", video_id=video.id,
                          channel_id=video.channel_id,
                          detail=f"transient error (retry {video.retry_count}/2): {err[:200]}")
            else:
                video.status = VideoStatus.FAILED
                video.error = err
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
        fmt = "long" if topic and topic.content_format == "long" else "short"
        params = build_video_params(
            video.subject,
            _profile_params(session, channel.default_render_profile_id),
            _profile_params(session, topic.render_profile_id if topic else None),
            _profile_params(session, video.render_profile_id),
            json.loads(video.overrides_json) if video.overrides_json else None,
            _format_overrides(fmt),
        )
        params["content_format"] = fmt
        params["topic_id"] = video.topic_id   # lets the composition theme match the thumbnail
        engine_name = resolve_engine(session, video, topic, channel)
        engine = get_engine(engine_name)
        try:
            task_id = engine.submit(video, params)
        except Exception as e:
            quota.log(session, kind="render", status="error", video_id=video.id,
                      channel_id=channel.id, detail=f"submit failed: {e}")
            continue
        video.engine = engine_name
        video.mpt_task_id = task_id
        video.status = VideoStatus.RENDERING
        video.render_progress = 0
        video.error = None
        video.last_attempt_at = utcnow()
        quota.log(session, kind="render", status="started", video_id=video.id, channel_id=channel.id)
        in_flight += 1


def _auto_produce(session: Session) -> None:
    """Promote DRAFT -> QUEUED to fill today's free render capacity.

    Nothing else in the app makes this transition (only an explicit produce call),
    which is how the 07-18..07-23 stall happened: a full bench of drafts satisfied
    the board-capacity gate while the render loop starved on an empty queue. The
    render/publish loops already own every later transition, so closing this one
    gap makes the pipeline self-sustaining.

    Per non-paused channel: headroom = daily_render_budget - rendered_today -
    (queued + rendering). Drafts from weight-0 or inactive topics are never touched
    (weight 0 = operator-parked). If the APPROVED buffer holds no long-form, one
    long draft is queued first so the publish loop's reserved daily long slot
    (quota.published_long_today) can always be filled; remaining slots go to shorts
    weight-first, then longs.
    """
    for ch in session.exec(select(Channel).where(Channel.paused == False)).all():  # noqa: E712
        active = session.exec(
            select(func.count(Video.id)).where(
                Video.channel_id == ch.id,
                Video.status.in_((VideoStatus.QUEUED, VideoStatus.RENDERING)))
        ).one()
        headroom = ch.daily_render_budget - quota.rendered_today(session, ch.id) - active
        if headroom <= 0:
            continue
        rows = session.exec(
            select(Video, Topic).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == ch.id, Video.status == VideoStatus.DRAFT,
                   Topic.active == True, Topic.weight > 0)  # noqa: E712
            .order_by(Topic.weight.desc(), Video.position, Video.id)
        ).all()
        if not rows:
            continue
        longs = [v for v, t in rows if t.content_format == "long"]
        shorts = [v for v, t in rows if t.content_format != "long"]
        picks = []
        if longs:
            approved_longs = session.exec(
                select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
                .where(Video.channel_id == ch.id,
                       Video.status == VideoStatus.APPROVED,
                       Topic.content_format == "long")
            ).one()
            if approved_longs == 0:
                picks.append(longs.pop(0))
        picks.extend(shorts[: headroom - len(picks)])
        picks.extend(longs[: headroom - len(picks)])
        for v in picks:
            v.status = VideoStatus.QUEUED
            session.add(v)
            quota.log(session, kind="produce", status="success", video_id=v.id,
                      channel_id=ch.id,
                      detail="auto-produced: draft queued to fill free render capacity")
        if picks:
            logger.info("auto-produced %d draft(s) for channel %s", len(picks), ch.slug)


def tick() -> None:
    with session_scope() as session:
        if app_settings(session).scheduler_paused:
            return
        _advance_in_flight(session)
        _auto_produce(session)
        _submit_new(session)
