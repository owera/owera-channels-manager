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

# Engine-reported errors that did not produce an artifact. Same class as the
# loop wall-clock timeout: the work never finished, so a bounded re-queue is
# cheaper than a permanent FAILED (observed 2026-08-12..14: 16× "render timed
# out" + litellm 600s / Anthropic disconnect overnight, all retry_count=0).
# 2026-08-31: edge-tts NoAudioReceived on ch1 v1213 (t5 long) went FAILED at
# retry_count=0 overnight → 0L+5S publish mix. TTS flakes produce no artifact
# and rendered_today only counts success, so a bounded re-queue is free.
_TRANSIENT = (
    "overloaded_error", "rate_limit_error", "RateLimitError",
    "overloaded", "529", "503",
    "litellm.Timeout", "Connection timed out",
    "InternalServerError", "Server disconnected",
    "grok.Timeout",
    "NoAudioReceived",
)


def _retry_or_fail(session: Session, video: Video, err: str, *, transient: bool) -> None:
    """Re-queue a failed-to-finish render, or mark FAILED once the budget is spent.

    QUEUED (not APPROVED): _submit_new picks it up again. APPROVED would skip
    rendering and hand a file-less video to the publish loop. The handle and
    progress are cleared so the retry is a clean start.
    """
    if transient and video.retry_count < 2:
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
        try:
            meta = metadata.generate(video.subject, video.script or "", fmt,
                                     language=video_gen.channel_language(session, video.channel_id))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _retry_or_fail(session, video, err,
                           transient=any(sig in err for sig in _TRANSIENT))
            return
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


def _past_timeout(video: Video) -> bool:
    if not video.last_attempt_at:
        return False
    started = video.last_attempt_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() > settings.render_timeout_seconds


def _advance_in_flight(session: Session) -> None:
    for video in session.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all():
        channel = session.get(Channel, video.channel_id)
        timed_out = _past_timeout(video)
        if not video.mpt_task_id:
            # No handle to poll. A wall-clock miss is a hang; otherwise wait.
            if timed_out:
                _retry_or_fail(session, video, "render timed out", transient=True)
            continue
        engine = get_engine(video.engine)
        try:
            task = engine.poll(video.mpt_task_id)
        except Exception:
            # Engine unreachable: if the wall clock already expired, treat it
            # as a hang (the previous skip-poll path never observed COMPLETE
            # either). Otherwise leave the row for the next tick.
            if timed_out:
                _retry_or_fail(session, video, "render timed out", transient=True)
            continue
        video.render_progress = int(task.get("progress") or video.render_progress)
        if task.get("state") == STATE_COMPLETE:
            _finalize(session, video, channel, engine, task)
        elif task.get("state") == STATE_FAILED:
            err = task.get("error") or f"{video.engine or 'mpt'} reported render failure"
            _retry_or_fail(session, video, err,
                           transient=any(sig in err for sig in _TRANSIENT))
        elif timed_out:
            # Still PROCESSING past the cap — no artifact, bounded re-queue.
            # A just-finished COMPLETE/FAILED is handled above (poll first),
            # so a 40-min mux that already wrote status.json is not retried.
            _retry_or_fail(session, video, "render timed out", transient=True)


def _split_queued_by_format(session: Session, queued: list[Video]) -> tuple[list[Video], list[Video]]:
    """Partition QUEUED rows into (longs, shorts) preserving input order."""
    longs: list[Video] = []
    shorts: list[Video] = []
    for v in queued:
        topic = session.get(Topic, v.topic_id)
        if topic and topic.content_format == "long":
            longs.append(v)
        else:
            shorts.append(v)
    return longs, shorts


def _queued_candidates(session: Session) -> list[Video]:
    """QUEUED videos in submit order.

    Per channel, order tracks the 1-long + 4-shorts daily mix:

    - **No approved long** → surface queued longs first (then shorts by
      position/id). `_auto_produce` already queues a long when the approved
      long buffer is empty, but a pure FIFO submit order lets earlier short
      ids burn the daily render budget before that long starts — so the
      publish loop's reserved long slot finds nothing that day (observed
      ch2 2026-08-07: 11 approved shorts / 0 longs while a long sat QUEUED
      behind higher-id shorts that already filled rendered_today=5).

    - **Approved long already banked** → prefer shorts first (then remaining
      longs). Dual of the case above: a pure-long FIFO queue rebuilds an
      all-long approved pool even when short drafts exist, and the publish
      mix can only paper over it when shorts are already approved (observed
      ch1 2026-08-08: approved 2L+1S, QUEUED = 5× t5 longs, drafts = 5× t6
      shorts; without this, overnight would render 5 more longs).
    """
    ordered: list[Video] = []
    channels = session.exec(
        select(Channel).where(Channel.paused == False).order_by(Channel.id)  # noqa: E712
    ).all()
    for channel in channels:
        queued = session.exec(
            select(Video).where(
                Video.status == VideoStatus.QUEUED,
                Video.channel_id == channel.id,
            ).order_by(Video.position, Video.id)
        ).all()
        if not queued:
            continue
        approved_longs = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == channel.id,
                   Video.status == VideoStatus.APPROVED,
                   Topic.content_format == "long")
        ).one()
        longs, shorts = _split_queued_by_format(session, queued)
        if approved_longs == 0:
            ordered.extend(longs + shorts)
        else:
            ordered.extend(shorts + longs)
    return ordered


def _rebalance_queued_mix(session: Session) -> None:
    """Keep the queued mix able to feed 1 long + 4 shorts.

    Two duals — `_auto_produce` headroom counts every QUEUED row, so a full
    queue of the wrong format starves the other side forever:

    - **Approved long buffer healthy** + queue full of longs + short drafts
      exist → demote excess queued longs (keep one reserve) so shorts can fill.
      Observed ch1 2026-08-08: 5× t5 longs queued, short drafts waiting.
    - **No approved long** + no long in flight + queue full of shorts + a
      promotable long draft exists + **headroom == 0** (midnight, budget
      reset, queue still full) → demote one queued short so `_auto_produce`
      can pick the long. Does NOT fire when headroom < 0 (render budget
      already spent, typical 14:00 after the day's long publishes) — that
      drained the whole short queue one tick at a time on ch2 2026-08-22.

    Lifecycle: QUEUED → DRAFT (undo of produce); re-render still starts at QUEUED.
    """
    for ch in session.exec(select(Channel).where(Channel.paused == False)).all():  # noqa: E712
        approved_longs = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == ch.id,
                   Video.status == VideoStatus.APPROVED,
                   Topic.content_format == "long")
        ).one()
        queued = session.exec(
            select(Video).where(
                Video.channel_id == ch.id,
                Video.status == VideoStatus.QUEUED,
            ).order_by(Video.position, Video.id)
        ).all()
        longs, shorts = _split_queued_by_format(session, queued)

        if approved_longs >= 1:
            short_drafts = session.exec(
                select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
                .where(Video.channel_id == ch.id,
                       Video.status == VideoStatus.DRAFT,
                       Topic.active == True,  # noqa: E712
                       Topic.weight > 0,
                       Topic.content_format != "long")
            ).one()
            if short_drafts < 1:
                continue
            # Keep one queued long as next-day buffer; demote the rest (newest first
            # among longs so earlier-position subjects stay closer to production).
            excess = list(reversed(longs[1:]))  # drop index 0 reserve; demote from end
            if not excess:
                continue
            for v in excess:
                v.status = VideoStatus.DRAFT
                v.error = None
                session.add(v)
                quota.log(
                    session, kind="produce", status="success", video_id=v.id,
                    channel_id=ch.id,
                    detail="rebalance: demoted excess queued long → draft so shorts can fill mix",
                )
            logger.info(
                "rebalance: demoted %d excess queued long(s) on channel %s (approved longs=%s, short drafts=%s)",
                len(excess), ch.slug, approved_longs, short_drafts,
            )
            continue

        # Dual: approved_longs == 0. If a long is already queued/rendering, submit
        # will prefer it. If headroom > 0, auto_produce already picks a long first.
        # The stuck shape is a *full short queue* (headroom ≤ 0) with a long draft
        # waiting — demote one short so the next produce tick can queue the long.
        in_flight_longs = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == ch.id,
                   Video.status.in_((VideoStatus.QUEUED, VideoStatus.RENDERING)),
                   Topic.content_format == "long")
        ).one()
        if in_flight_longs > 0 or not shorts:
            continue
        long_drafts = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == ch.id,
                   Video.status == VideoStatus.DRAFT,
                   Topic.active == True,  # noqa: E712
                   Topic.weight > 0,
                   Topic.content_format == "long")
        ).one()
        if long_drafts < 1:
            continue
        rendering = session.exec(
            select(func.count(Video.id)).where(
                Video.channel_id == ch.id,
                Video.status == VideoStatus.RENDERING,
            )
        ).one()
        headroom = ch.daily_render_budget - quota.rendered_today(session, ch.id) - len(queued) - rendering
        # Demoting one short only helps auto_produce when it creates a slot
        # *this tick* (headroom 0 → 1). headroom > 0: auto_produce can already
        # pick the long. headroom < 0: the render budget is already spent (the
        # 14:00 post-publish shape: approved_longs just hit 0, rendered_today
        # == budget, shorts sitting until midnight). Demoting then drains the
        # whole short queue one tick at a time and auto_produce cannot fill
        # until the UTC-day reset. Observed ch2 2026-08-22 14:00: five demotes
        # (1037, 1036, 1035, 1034, 1029) while rendered_today=5. Midnight
        # recovered 1L+4S, but the afternoon queue was emptied for nothing.
        if headroom != 0:
            continue
        v = shorts[-1]  # newest (queued is position, id)
        v.status = VideoStatus.DRAFT
        v.error = None
        session.add(v)
        quota.log(
            session, kind="produce", status="success", video_id=v.id,
            channel_id=ch.id,
            detail="rebalance: demoted queued short → draft so a long can fill the 1L mix",
        )
        logger.info(
            "rebalance: demoted queued short %s on channel %s (approved longs=0, long drafts=%s)",
            v.id, ch.slug, long_drafts,
        )


def _submit_new(session: Session) -> None:
    cfg = app_settings(session)
    in_flight = quota.in_flight_renders(session)
    if in_flight >= cfg.render_concurrency:
        return
    candidates = _queued_candidates(session)
    for video in candidates:
        if in_flight >= cfg.render_concurrency:
            break
        channel = session.get(Channel, video.channel_id)
        if not channel or channel.paused:
            continue
        # In-flight renders count against the budget too: gating on completed
        # renders alone overshoots to budget+concurrency-1, because a new render
        # starts after each success while the rest are still in flight (2026-07-26
        # burst: 8 rendered against a budget of 5 at concurrency 4, both channels).
        if (quota.rendered_today(session, channel.id)
                + quota.in_flight_renders(session, channel.id)
                >= channel.daily_render_budget):
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
    (weight 0 = operator-parked).

    Long-form buffer policy (pairs with `_rebalance_queued_mix` "keep 1 queued
    long as next-day reserve"):
    - **No long in pipeline** (approved + queued + rendering == 0) → queue one
      long first so the publish loop's reserved daily long slot can fill.
    - **Exactly one approved long and none in flight**, with headroom ≥ 2 →
      leave one slot for a queued long reserve *after* shorts take the rest.
      Without this, a pure-short fill under a banked long leaves the queue with
      0 longs; after that long publishes, approved_longs hits 0 until the next
      overnight cycle (observed ch1 2026-08-11 noon: approved 0L+3S).
    - Remaining headroom → shorts weight-first, then longs.
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
        picks: list[Video] = []
        approved_longs = 0
        in_flight_longs = 0
        if longs:
            approved_longs = session.exec(
                select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
                .where(Video.channel_id == ch.id,
                       Video.status == VideoStatus.APPROVED,
                       Topic.content_format == "long")
            ).one()
            in_flight_longs = session.exec(
                select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
                .where(Video.channel_id == ch.id,
                       Video.status.in_((VideoStatus.QUEUED, VideoStatus.RENDERING)),
                       Topic.content_format == "long")
            ).one()
            # Urgent: nothing long in the whole pipeline.
            if approved_longs == 0 and in_flight_longs == 0:
                picks.append(longs.pop(0))
        # Next-day reserve: one approved long banked, none already queued/rendering,
        # and enough headroom that shorts still get slots (budget=1 keeps preferring
        # the short — see verify_render "no second long queued while one is banked").
        need_long_reserve = (
            bool(longs)
            and approved_longs == 1
            and in_flight_longs == 0
            and headroom - len(picks) >= 2
        )
        # Leave one slot for the long reserve when needed; consume shorts so
        # the fill-remainder pass cannot re-pick the same draft objects.
        short_slots = max(0, headroom - len(picks) - (1 if need_long_reserve else 0))
        picks.extend(shorts[:short_slots])
        shorts = shorts[short_slots:]
        if need_long_reserve and longs:
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
        _rebalance_queued_mix(session)
        _auto_produce(session)
        _submit_new(session)
