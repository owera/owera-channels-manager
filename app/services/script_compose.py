"""Compose a video's narration script without rendering (P0 2026-09-29, item iii).

Before: the script was only written inside the render job (worker.run_job →
_generate_script), so refilling the runway burned render budget just to get
scripts, and a re-render overwrote a script that had been saved. PATCH …/craft
refuses drafts. This module runs the same script prompt through the storyboard
compose seam (``worker._llm_compose``: effort pinned to
``settings.grok_compose_reasoning_effort``, default "medium"; the existing
``grok_timeout_seconds``) and stores the result as the video's *provided*
script, which the render loop already speaks verbatim (PR #37).

Invariants:
- never touches status, daily_render_budget, render_progress, mpt_task_id;
- JobRun kind=compose_script only (rendered_today counts kind=render success);
- the grok call runs with no DB transaction open;
- a video being composed is skipped by render_loop._submit_new (``is_composing``)
  so the render cannot race the compose and generate its own script;
- the result is discarded (logged) if the video left draft/queued or got a
  script in the meantime (unless force).
"""

from __future__ import annotations

import json
import logging
import threading

from sqlmodel import Session

from app.config import settings
from app.models import Channel, Topic, Video, VideoStatus, utcnow
from app.services import quota

logger = logging.getLogger("manager.compose")

JOB_KIND = "compose_script"
COMPOSE_FROM = (VideoStatus.DRAFT, VideoStatus.QUEUED)

_lock = threading.Lock()
_composing: set[int] = set()


def is_composing(video_id: int | None) -> bool:
    if video_id is None:
        return False
    with _lock:
        return video_id in _composing


def claim(video_id: int) -> bool:
    with _lock:
        if video_id in _composing:
            return False
        _composing.add(video_id)
        return True


def release(video_id: int) -> None:
    with _lock:
        _composing.discard(video_id)


def block_reason(v: Video, *, force: bool = False) -> str | None:
    """Why this video cannot be composed now (→ 409), or None."""
    if v.status == VideoStatus.RENDERING or v.mpt_task_id:
        return f"render in progress (status '{v.status}') — compose refused"
    if v.status not in COMPOSE_FROM:
        return (f"cannot compose script from status '{v.status}' "
                f"(need draft/queued; after render use PATCH …/craft)")
    if (v.script or "").strip() and not force:
        return ("video already has a saved script — refusing to overwrite "
                "(pass ?force=true to replace it)")
    return None


def build_params(session: Session, v: Video) -> dict:
    """Same param resolution as render_loop._submit_new (profiles → overrides →
    format), so the composed script matches what the render would generate."""
    from app.services.craft import brand_of
    from app.services.mpt_client import build_video_params
    from app.services.render_loop import _format_overrides, _profile_params
    channel = session.get(Channel, v.channel_id)
    topic = session.get(Topic, v.topic_id)
    fmt = "long" if topic and topic.content_format == "long" else "short"
    params = build_video_params(
        v.subject,
        _profile_params(session, channel.default_render_profile_id if channel else None),
        _profile_params(session, topic.render_profile_id if topic else None),
        _profile_params(session, v.render_profile_id),
        json.loads(v.overrides_json) if v.overrides_json else None,
        _format_overrides(fmt),
    )
    params["content_format"] = fmt
    params["topic_id"] = v.topic_id
    if channel is not None:
        params["brand"] = brand_of(channel.slug, channel.name, channel_id=channel.id)
        params["channel_id"] = channel.id
        params["channel_slug"] = channel.slug
    return params


def generate(subject: str, params: dict) -> str:
    """The render's script prompt, through the compose seam (effort pin)."""
    from app.services.engines import worker
    return worker._generate_script(subject, params, llm=worker._llm_compose)


def compose_into(session: Session, video_id: int, *, force: bool = False) -> dict:
    """Compose + persist. Caller must hold ``claim(video_id)``.

    Returns {"ok": True, "words": n, "hook_warning": str|None} or
    {"error": str, "http": int}. Commits between phases so no transaction is
    open during the grok call."""
    v = session.get(Video, video_id)
    if v is None:
        return {"error": "video not found", "http": 404}
    blocked = block_reason(v, force=force)
    if blocked:
        return {"error": blocked, "http": 409}
    params = build_params(session, v)
    subject, channel_id = v.subject, v.channel_id
    effort = settings.grok_compose_reasoning_effort or "default"
    quota.log(session, kind=JOB_KIND, status="started", video_id=video_id,
              channel_id=channel_id,
              detail=f"compose script (no render): status={v.status} effort={effort}")
    session.commit()

    try:
        text = (generate(subject, params) or "").strip()
    except Exception as e:  # grok.Timeout / nonzero / missing binary
        err = f"{type(e).__name__}: {e}"[:500]
        quota.log(session, kind=JOB_KIND, status="error", video_id=video_id,
                  channel_id=channel_id, detail=f"compose failed: {err}")
        session.commit()
        logger.warning("compose-script failed for video %s: %s", video_id, err)
        return {"error": f"compose failed: {err}", "http": 502}
    if not text:
        quota.log(session, kind=JOB_KIND, status="error", video_id=video_id,
                  channel_id=channel_id, detail="compose returned an empty script")
        session.commit()
        return {"error": "compose returned an empty script", "http": 502}

    session.expire_all()
    v = session.get(Video, video_id)
    blocked = block_reason(v, force=force) if v is not None else "video deleted"
    if blocked:
        quota.log(session, kind=JOB_KIND, status="error", video_id=video_id,
                  channel_id=channel_id,
                  detail=f"compose result discarded ({len(text.split())} words): {blocked}")
        session.commit()
        return {"error": f"compose result discarded: {blocked}", "http": 409}

    from app.routers.videos import _apply_provided_script
    _apply_provided_script(v, text)
    cc = json.loads(v.creation_config) if v.creation_config else {}
    cc["script_composed"] = {"by": "grok", "effort": effort,
                             "at": utcnow().isoformat(), "no_render": True}
    v.creation_config = json.dumps(cc)
    v.updated_at = utcnow()
    hook = v.error if (v.error or "").startswith("provided script") else None
    session.add(v)
    quota.log(session, kind=JOB_KIND, status="success", video_id=video_id,
              channel_id=channel_id,
              detail=f"script composed ({len(text.split())} words, effort={effort}); "
                     f"saved as provided script, no render slot used"
                     + (f"; {hook}" if hook else ""))
    session.commit()
    return {"ok": True, "words": len(text.split()), "hook_warning": hook}


def start_background(bind, video_id: int, *, force: bool = False) -> threading.Thread:
    """Run compose_into on a daemon thread with its own session on ``bind``.
    Caller already holds ``claim(video_id)``; released here."""
    def _run() -> None:
        try:
            with Session(bind) as s:
                compose_into(s, video_id, force=force)
        except Exception:
            logger.exception("compose-script thread crashed for video %s", video_id)
        finally:
            release(video_id)

    t = threading.Thread(target=_run, name=f"compose-script-{video_id}", daemon=True)
    t.start()
    return t
