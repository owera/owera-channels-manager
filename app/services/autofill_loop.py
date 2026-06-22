"""Auto-refill: keep each topic's idea queue topped up.

When the "Auto-refill topic queues" setting is on and a topic's pending
(draft + queued) video count drops below the threshold, generate a fresh batch
of video ideas for it (format-aware, never repeating existing subjects). The new
ideas land as drafts — same as the manual "generate ideas" — so you still decide
what to produce.
"""

import logging

from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Topic, Video, VideoStatus
from app.services import quota, video_gen

logger = logging.getLogger("manager.autofill")

_PENDING = (VideoStatus.DRAFT, VideoStatus.QUEUED)


def _pending_count(session: Session, topic_id: int) -> int:
    return session.exec(
        select(func.count(Video.id)).where(
            Video.topic_id == topic_id, Video.status.in_(_PENDING)
        )
    ).one()


def _refill_topic(session: Session, topic: Topic, batch: int) -> int:
    existing = session.exec(select(Video.subject).where(Video.topic_id == topic.id)).all()
    try:
        ideas = video_gen.generate_ideas(
            topic.name, topic.theme_prompt, list(existing), batch, topic.content_format)
    except Exception as e:
        logger.info("autofill skipped for topic '%s': %s", topic.name, e)
        return 0
    if not ideas:
        return 0
    mx = session.exec(
        select(func.max(Video.position)).where(Video.channel_id == topic.channel_id)
    ).one() or 0
    for i, subject in enumerate(ideas):
        session.add(Video(channel_id=topic.channel_id, topic_id=topic.id, subject=subject,
                          status=VideoStatus.DRAFT, position=mx + 1 + i))
    quota.log(session, kind="generate", status="success", channel_id=topic.channel_id,
              detail=f"auto-refilled {len(ideas)} ideas for '{topic.name}'")
    return len(ideas)


def tick() -> None:
    with session_scope() as session:
        cfg = app_settings(session)
        if not cfg.topic_autogen_enabled:
            return
        threshold = max(1, cfg.topic_autogen_min_pending)
        target = max(threshold, cfg.topic_autogen_target)  # ceiling never below the trigger
        topics = session.exec(select(Topic).where(Topic.active == True)).all()  # noqa: E712
        for topic in topics:
            weight = topic.weight if topic.weight is not None else 1
            if weight <= 0:
                continue  # soft-paused by the growth agent (weight 0): no new ideas
            # Winners (weight > 1) keep a proportionally deeper bench; cap the
            # multiplier so a stray large weight can't blow up the idea count / LLM cost.
            mult = min(weight, 4)
            pending = _pending_count(session, topic.id)
            if pending >= threshold * mult:
                continue  # bench still deep enough — refill in bursts, not every tick
            # Top up to the ceiling instead of blasting a fixed batch: this is what
            # stops the IDEAS column overshooting and piling up day over day.
            need = min(target * mult - pending, settings.autofill_batch * mult)
            if need <= 0:
                continue
            n = _refill_topic(session, topic, need)
            if n:
                logger.info("auto-refilled %d ideas for topic '%s' (weight %d, ceiling %d)",
                            n, topic.name, weight, target * mult)
