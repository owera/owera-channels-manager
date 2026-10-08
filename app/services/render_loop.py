"""Render tick: queued -> rendering -> rendered -> (review|approved), per Video.

Profile resolution per video: video.render_profile -> topic.render_profile ->
channel.default_render_profile.
"""

import json
import random
import time
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
from app.services.subject_guard import HOLD_PREFIX, subject_guard_reason
from app.services.topic_playlist import ensure_topic_playlist

logger = logging.getLogger("manager.render")

# Engine-reported errors that did not produce an artifact. Same class as the
# loop wall-clock timeout: the work never finished, so a bounded re-queue is
# cheaper than a permanent FAILED (observed 2026-08-12..14: 16× "render timed
# out" + litellm 600s / Anthropic disconnect overnight, all retry_count=0).
# 2026-08-31: edge-tts NoAudioReceived on ch1 v1213 (t5 long) went FAILED at
# retry_count=0 overnight → 0L+5S publish mix. TTS flakes produce no artifact
# and rendered_today only counts success, so a bounded re-queue is free.
# 2026-09-03: BlockingIOError Errno 35 (EAGAIN) on 14 concurrent midnight
# renders went FAILED at retry_count=0. Pipe/FD contention, no artifact.
_TRANSIENT = (
    "overloaded_error", "rate_limit_error", "RateLimitError",
    "overloaded", "529", "503",
    "litellm.Timeout", "Connection timed out",
    "InternalServerError", "Server disconnected",
    "grok.Timeout",
    "NoAudioReceived",
    "BlockingIOError",
)

# Post-Timeout cool-down before _submit_new re-picks a QUEUED row. Avoids
# immediately restacking another 600s grok -p on a saturated CLI (observed
# ~13–14 grok.Timeout / 24h). In-process map — single manager process.
_GROK_TIMEOUT_BACKOFF_LO_S = 30
_GROK_TIMEOUT_BACKOFF_HI_S = 60
_grok_timeout_not_before: dict[int, float] = {}


def _retry_or_fail(session: Session, video: Video, err: str, *, transient: bool) -> None:
    """Re-queue a failed-to-finish render, or mark FAILED once the budget is spent.

    QUEUED (not APPROVED): _submit_new picks it up again. APPROVED would skip
    rendering and hand a file-less video to the publish loop. The handle and
    progress are cleared so the retry is a clean start.

    grok.Timeout gets an extra 30–60s cool-down before re-submit (see
    ``_grok_timeout_not_before``); other transients re-queue immediately.
    """
    if transient and video.retry_count < 2:
        video.status = VideoStatus.QUEUED
        video.retry_count += 1
        video.mpt_task_id = None
        video.render_progress = 0
        video.error = None
        if video.id is not None and "grok.Timeout" in (err or ""):
            delay = random.randint(_GROK_TIMEOUT_BACKOFF_LO_S, _GROK_TIMEOUT_BACKOFF_HI_S)
            _grok_timeout_not_before[video.id] = time.time() + delay
            logger.info(
                "grok.Timeout backoff %ds before retry for video %s (attempt %d/2)",
                delay, video.id, video.retry_count,
            )
        quota.log(session, kind="render", status="error", video_id=video.id,
                  channel_id=video.channel_id,
                  detail=f"transient error (retry {video.retry_count}/2): {err[:200]}")
    else:
        if video.id is not None:
            _grok_timeout_not_before.pop(video.id, None)
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


def _vm_pass_missing(video: Video) -> str | None:
    """review_guard.vm_pass_required_reason (topic_flags vm_pass_required)."""
    from app.services import review_guard
    return review_guard.vm_pass_required_reason(video)


def _cc_dict(raw) -> dict:
    """creation_config column (JSON str / dict / None) → dict (never raises)."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _prepare_provided_script(session: Session, video: Video, channel: Channel,
                             fmt: str, brand: str | None) -> str | None:
    """Pre-render gate + prep for a provided script. Returns the VO text to
    speak verbatim, "" when the video has no provided script (generate as
    usual), or None when the video was parked FAILED (misaligned hook — never
    silently regenerated, no render slot spent)."""
    from app.services import craft
    if not (craft.is_provided_script(video.creation_config)
            and (video.script or "").strip()):
        return ""
    blocked = craft.provided_script_hook_reason(
        video.script, title=video.title, subject=video.subject)
    if blocked:
        video.status = VideoStatus.FAILED
        video.error = blocked
        video.craft_review = craft.CRAFT_REVIEW_FAIL
        quota.log(session, kind="render", status="error", video_id=video.id,
                  channel_id=channel.id,
                  detail=f"provided script blocked before render (no slot used): {blocked}")
        return None
    series_src = video.title if craft.spoken_title_ok(video.title) else video.subject
    prepared, edits = craft.prepare_provided_script(
        video.script, series_src, brand=brand, content_format=fmt)
    video.script = prepared
    cc = _cc_dict(video.creation_config)
    cc["script_source"] = craft.SCRIPT_SOURCE_PROVIDED
    cc["script_edits"] = edits
    video.creation_config = json.dumps(cc)
    return prepared


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

    # Provided scripts (creation_config.script_source=provided) are the spoken
    # text of record: the engine only echoes them back, so an empty/None (or
    # re-derived) task script must never clobber the provided + endcard VO.
    from app.services import craft as _craft
    prior_cc = _cc_dict(video.creation_config)
    provided = (_craft.is_provided_script(prior_cc)
                and bool((video.script or "").strip()))
    if not provided:
        video.script = task.get("script") or video.script
    if task.get("creation_config"):
        cc = task["creation_config"]
        if provided and isinstance(cc, dict):
            cc = {**cc, "script_source": _craft.SCRIPT_SOURCE_PROVIDED,
                  "script_edits": prior_cc.get("script_edits") or []}
        video.creation_config = json.dumps(cc)
    # A new render never inherits the VM's Gate B PASS of the previous artifact
    # (engines without a task creation_config keep the old blob, vm_pass included).
    _cc_now = _cc_dict(video.creation_config)
    if "vm_pass" in _cc_now:
        _cc_now.pop("vm_pass")
        video.creation_config = json.dumps(_cc_now)

    # A completed re-render is the only thing that clears a PATCH /craft
    # stale-render marker (2026-10-06): this mp4 was rendered from the saved text.
    video.creation_config = _craft.clear_stale_render(video.creation_config)
    # Measurement-fail cap (CTO 06/10 2c): count consecutive renders whose
    # gate had an unverifiable (kind="measurement") check; at the cap the gate
    # FAILs below (→ review, VM review via vm-pass), never a silent PASS.
    _t = session.get(Topic, video.topic_id)
    _fmt = "long" if _t and _t.content_format == "long" else "short"
    video.measurement_streak = _craft.next_measurement_streak(
        video.measurement_streak, video.creation_config, _fmt)
    video.creation_config = _craft.with_measurement_cap(
        video.creation_config, video.measurement_streak,
        video_path=video.video_path, content_format=_fmt)

    # An operator-provided thumbnail (thumb_provided.*) is never replaced by the
    # 1s still — skip the still entirely and keep the marker on the new blob.
    from app.services import provided_thumb
    if provided_thumb.is_provided(video):
        video.creation_config = provided_thumb.carry_marker(video, video.creation_config)
    else:
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
    from app.services import craft
    topic = session.get(Topic, video.topic_id)
    fmt = "long" if topic and topic.content_format == "long" else "short"
    # Full publish craft gate (title / A+B+C / script / VO / mute) so skip-gate
    # auto-approve writes an explicit craft_review=pass (or parks in review).
    from app.services import episode_guard
    blocked = craft.publish_craft_block_reason(
        title=video.title, script=video.script,
        creation_config=video.creation_config, content_format=fmt,
        video_path=video.video_path,
    ) or episode_guard.duplicate_episode_reason(session, video)
    if blocked:
        video.status = VideoStatus.REVIEW
        video.craft_review = craft.CRAFT_REVIEW_FAIL
        video.error = blocked
        video.approved_at = None
    elif _effective_skip_gate(video, channel) and _vm_pass_missing(video):
        # vm_pass_required topic (default 47): skip-gate never auto-approves it;
        # it waits in review for the VM's Gate B PASS (#1449/#1450).
        video.status = VideoStatus.REVIEW
        video.craft_review = craft.CRAFT_REVIEW_PENDING
        video.error = _vm_pass_missing(video)
        video.approved_at = None
    elif _effective_skip_gate(video, channel):
        video.status = VideoStatus.APPROVED
        video.craft_review = craft.CRAFT_REVIEW_PASS
        video.approved_at = utcnow()
    else:
        video.status = VideoStatus.REVIEW
        # Pending operator approve — not yet an explicit pass.
        video.craft_review = craft.CRAFT_REVIEW_PENDING
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
                   Video.held.is_not(True),     # held longs are not banked runway
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
                   Video.held.is_not(True),     # held longs are not banked runway
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
    now = time.time()
    for video in candidates:
        if in_flight >= cfg.render_concurrency:
            break
        # compose-script (no render) owns this row's script right now: submitting
        # would generate a second script and race the compose write.
        from app.services.script_compose import is_composing
        if is_composing(video.id):
            continue
        not_before = _grok_timeout_not_before.get(video.id) if video.id is not None else None
        if not_before is not None and now < not_before:
            continue  # still inside grok.Timeout cool-down
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
        fmt = "long" if topic and topic.content_format == "long" else "short"
        from app.services.craft import brand_of
        brand = brand_of(channel.slug, channel.name, channel_id=channel.id)
        # Provided script: hook-gate before any side effect (playlist / slot).
        provided_script = _prepare_provided_script(session, video, channel, fmt, brand)
        if provided_script is None:
            continue
        # A video is starting production → make sure its topic playlist exists.
        ensure_topic_playlist(session, topic, channel)
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
        params["topic_name"] = topic.name if topic else ""  # endcard series chip (P1 e)
        params["brand"] = brand
        if provided_script:
            # Spoken verbatim: HyperFrames worker skips _generate_script on
            # provided_script; MPT uses its native video_script field.
            params["provided_script"] = provided_script
            params["video_script"] = provided_script
        params["channel_id"] = channel.id
        params["channel_slug"] = channel.slug
        # Designer split-card never replaces an operator-provided thumb (#39).
        from app.services import provided_thumb as _pt
        params["thumb_source"] = "provided" if _pt.is_provided(video) else ""
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
        if video.id is not None:
            _grok_timeout_not_before.pop(video.id, None)
        quota.log(session, kind="render", status="started", video_id=video.id, channel_id=channel.id)
        in_flight += 1


def _hold_invalid_subject(session: Session, video: Video, ch: Channel) -> bool:
    """True (and keep it DRAFT) when the subject fails the pre-produce guard.

    Records the reason on video.error and one `produce` error JobRun — only when
    the reason changes, so a held draft doesn't write a row every 15s tick."""
    reason = subject_guard_reason(video.subject)
    if not reason:
        return False
    if video.error != reason:
        video.error = reason
        session.add(video)
        quota.log(session, kind="produce", status="error", video_id=video.id,
                  channel_id=ch.id,
                  detail=f"auto-produce held draft (not queued): {reason}")
        logger.info("auto-produce held draft %s on channel %s: %s",
                    video.id, ch.slug, reason)
    return True



def _take_weighted_shorts(short_pairs: list[tuple[Video, Topic]], n: int) -> list[Video]:
    """Take up to ``n`` short drafts proportional to ``topic.weight``.

    Autofill already gives winners a deeper bench (``weight`` multiplies the
    pending ceiling). Historically ``_auto_produce`` *also* ordered drafts by
    ``Topic.weight.desc()`` and took a prefix — so a weight-2 topic with any
    drafts monopolized every short render slot (Owera Software 2026-10-05:
    live mix 2:1:1:1 but 14/15 recent publics were Agent memory).

    Playbook semantics: weight steers *share* ("winner refills more"), not a
    hard priority queue. Smooth weighted round-robin keeps within-topic
    ``position``/``id`` order while giving weight=2 ~2× the slots of weight=1
    when drafts exist on both.
    """
    from collections import defaultdict, deque

    if n <= 0 or not short_pairs:
        return []
    bags: dict[int, deque[Video]] = defaultdict(deque)
    weights: dict[int, int] = {}
    for v, t in short_pairs:
        bags[t.id].append(v)
        weights[t.id] = max(1, int(t.weight or 1))
    topic_ids = sorted(bags.keys())
    credit = {tid: 0 for tid in topic_ids}
    total_w = sum(weights[tid] for tid in topic_ids)
    picks: list[Video] = []
    while len(picks) < n:
        eligible = [tid for tid in topic_ids if bags[tid]]
        if not eligible:
            break
        for tid in eligible:
            credit[tid] += weights[tid]
        tid = max(eligible, key=lambda i: (credit[i], -i))
        credit[tid] -= total_w
        picks.append(bags[tid].popleft())
    return picks


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
    - Remaining headroom → shorts by topic-weight share (smooth WRR), then longs.
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
            # position/id only — topic share is applied in _take_weighted_shorts
            # (weight.desc() prefix caused winner monopoly; see helper docstring).
            .order_by(Video.position, Video.id)
        ).all()
        # Subject guard (2026-09-28 RR refill): a draft whose subject lost its
        # leading number/stake ("B em…", "camadas na GPU…") or carries a currency
        # value stays DRAFT — it never takes a render slot; the next valid draft
        # does. The reason is recorded once on video.error + a produce JobRun.
        rows = [(v, t) for v, t in rows if not _hold_invalid_subject(session, v, ch)]
        if not rows:
            continue
        long_pairs = [(v, t) for v, t in rows if t.content_format == "long"]
        short_pairs = [(v, t) for v, t in rows if t.content_format != "long"]
        longs = [v for v, _ in long_pairs]
        picks: list[Video] = []
        approved_longs = 0
        in_flight_longs = 0
        if longs:
            approved_longs = session.exec(
                select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
                .where(Video.channel_id == ch.id,
                       Video.status == VideoStatus.APPROVED,
                       Video.held.is_not(True),  # held longs are not banked runway
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
        taken = _take_weighted_shorts(short_pairs, short_slots)
        taken_ids = {id(v) for v in taken}
        picks.extend(taken)
        short_pairs = [(v, t) for v, t in short_pairs if id(v) not in taken_ids]
        if need_long_reserve and longs:
            picks.append(longs.pop(0))
        more = _take_weighted_shorts(short_pairs, headroom - len(picks))
        more_ids = {id(v) for v in more}
        picks.extend(more)
        short_pairs = [(v, t) for v, t in short_pairs if id(v) not in more_ids]
        picks.extend(longs[: headroom - len(picks)])
        for v in picks:
            v.status = VideoStatus.QUEUED
            if (v.error or "").startswith(HOLD_PREFIX):
                v.error = None          # subject was fixed since it was held
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
