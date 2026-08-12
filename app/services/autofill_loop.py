"""Auto-refill: keep each topic's idea queue topped up.

When the "Auto-refill topic queues" setting is on and a topic's pending
(draft + queued) video count drops below the threshold, generate a fresh batch
of video ideas for it (format-aware, never repeating existing subjects). The new
ideas land as drafts — same as the manual "generate ideas" — so you still decide
what to produce.

Board-cap guard: before refilling any topic, the channel's total DRAFT+QUEUED
count is checked against `daily_render_budget × board_horizon_days`. Once the
channel has that many days of work sitting in the idea bench, autofill pauses for
that channel until ideas are consumed — preventing multi-day backlog pile-up.

Long-draft reserve: when a channel has zero long pending (draft+queued) but still
has an active long topic (weight > 0), keep one board slot for that long so
high-weight shorts cannot permanently starve the 1L+4S daily mix (observed ch2
2026-08-12: draft 0 long while t4/t27 filled the horizon).
"""

import logging

from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import Channel, Topic, Video, VideoStatus
from app.services import quota, video_gen

logger = logging.getLogger("manager.autofill")

_PENDING = (VideoStatus.DRAFT, VideoStatus.QUEUED)


def _pending_count(session: Session, topic_id: int) -> int:
    return session.exec(
        select(func.count(Video.id)).where(
            Video.topic_id == topic_id, Video.status.in_(_PENDING)
        )
    ).one()


def _channel_pending_count(session: Session, channel_id: int) -> int:
    return session.exec(
        select(func.count(Video.id)).where(
            Video.channel_id == channel_id, Video.status.in_(_PENDING)
        )
    ).one()


def _long_pending_count(session: Session, channel_id: int) -> int:
    """DRAFT+QUEUED rows on long-format topics for this channel."""
    return session.exec(
        select(func.count(Video.id)).where(
            Video.channel_id == channel_id,
            Video.status.in_(_PENDING),
            Video.topic_id.in_(
                select(Topic.id).where(
                    Topic.channel_id == channel_id,
                    Topic.content_format == "long",
                )
            ),
        )
    ).one()


def _refill_topic(session: Session, topic: Topic, batch: int) -> int:
    existing = session.exec(select(Video.subject).where(Video.topic_id == topic.id)).all()
    try:
        ideas = video_gen.generate_ideas(
            topic.name, topic.theme_prompt, list(existing), batch, topic.content_format,
            language=video_gen.channel_language(session, topic.channel_id))
    except Exception as e:
        logger.info("autofill skipped for topic '%s': %s", topic.name, e)
        return 0
    if not ideas:
        return 0
    ideas = ideas[:batch]  # board-space math assumes len <= batch; the LLM can return more
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

        channels = {ch.id: ch for ch in session.exec(select(Channel)).all()}
        topics = list(session.exec(select(Topic).where(Topic.active == True)).all())  # noqa: E712
        # High-weight topics must claim board space first: the horizon cap is shared
        # per channel, so id-order iteration let low-id anchor topics drain it before
        # the weighted winners were ever reached (07-26: t2/t5 filled both boards
        # while the w3 short topics sat at 0 pending).
        topics.sort(key=lambda t: (-(t.weight if t.weight is not None else 1), t.id))

        # Active long topics per channel (weight > 0) — drives the long-draft reserve.
        has_live_long: dict[int, bool] = {}
        for t in topics:
            w = t.weight if t.weight is not None else 1
            if t.content_format == "long" and w > 0:
                has_live_long[t.channel_id] = True

        # Running totals per channel so multiple topics in one tick can't collectively
        # overshoot the board cap (updated after each successful refill).
        channel_pending: dict[int, int] = {}
        long_pending: dict[int, int] = {}

        for topic in topics:
            weight = topic.weight if topic.weight is not None else 1
            if weight <= 0:
                continue  # soft-paused by the growth agent (weight 0): no new ideas

            cid = topic.channel_id
            is_long = topic.content_format == "long"

            # Lazy-load the channel pending total for this channel.
            if cid not in channel_pending:
                channel_pending[cid] = _channel_pending_count(session, cid)
            if cid not in long_pending:
                long_pending[cid] = _long_pending_count(session, cid)

            # Channel-level board cap: don't let the idea bench exceed the horizon.
            # When the long bench is empty, reserve 1 slot so shorts cannot fill
            # every seat and starve tomorrow's 1L publish (ch2 2026-08-12).
            ch = channels.get(cid)
            board_cap = -1
            if ch and cfg.board_horizon_days > 0:
                board_cap = ch.daily_render_budget * cfg.board_horizon_days
                need_long_slot = long_pending[cid] == 0 and has_live_long.get(cid, False)

                if is_long and need_long_slot and channel_pending[cid] >= board_cap:
                    # Board already full of shorts: allow exactly one long overshoot
                    # so auto_produce still has a long draft to queue overnight.
                    board_space = 1
                elif not is_long and need_long_slot:
                    effective_cap = max(0, board_cap - 1)
                    if channel_pending[cid] >= effective_cap:
                        logger.debug(
                            "channel '%s' board holding long reserve (%d/%d short-fill) "
                            "— skipping short autofill",
                            ch.name, channel_pending[cid], board_cap,
                        )
                        continue
                    board_space = effective_cap - channel_pending[cid]
                elif channel_pending[cid] >= board_cap:
                    logger.debug(
                        "channel '%s' board at capacity (%d/%d pending) — skipping autofill",
                        ch.name, channel_pending[cid], board_cap,
                    )
                    continue
                else:
                    board_space = board_cap - channel_pending[cid]
            else:
                board_space = settings.autofill_batch * 4  # no cap configured

            # Winners (weight > 1) keep a proportionally deeper bench; cap the
            # multiplier so a stray large weight can't blow up the idea count / LLM cost.
            mult = min(weight, 4)
            pending = _pending_count(session, topic.id)
            if pending >= threshold * mult:
                continue  # bench still deep enough — refill in bursts, not every tick
            # Top up to the ceiling instead of blasting a fixed batch, and also respect
            # the remaining channel board space so we never overshoot the horizon
            # (except the deliberate +1 long seed when the long bench is empty).
            need = min(target * mult - pending, settings.autofill_batch * mult, board_space)
            if need <= 0:
                continue
            n = _refill_topic(session, topic, need)
            if n:
                channel_pending[cid] += n
                if is_long:
                    long_pending[cid] += n
                logger.info(
                    "auto-refilled %d ideas for topic '%s' (weight %d, ceiling %d, "
                    "channel board %d/%d)",
                    n, topic.name, weight, target * mult,
                    channel_pending[cid],
                    board_cap if ch and cfg.board_horizon_days > 0 else -1,
                )
