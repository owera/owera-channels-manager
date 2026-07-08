"""Trend signals: the growth agent researches trending topics (via WebSearch) and
records them here, scored for 'smart adoption'. Persisting them lets adoption dedupe
across days and learn from how prior adoptions performed. Adopting a trend spins up a
topic, seeds ideas, and auto-produces a few so it starts rendering immediately."""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from app.db import get_session
from app.models import (Channel, Topic, TrendSignal, TrendStatus, Video, VideoStatus,
                        utcnow)
from app.schemas import TrendAdoptBody, TrendCreate, TrendUpdate
from app.services import quota, video_gen

router = APIRouter(prefix="/api/trends", tags=["trends"])

_ACTIVE = (TrendStatus.RESEARCHED, TrendStatus.WATCHING, TrendStatus.ADOPTED)


def _norm(term: str) -> str:
    return " ".join((term or "").strip().lower().split())


@router.get("")
def list_trends(status: str | None = None, channel_id: int | None = None,
                sort: str = "score", session: Session = Depends(get_session)):
    """Trends, newest-scored first. Filter by status/channel; sort by score|updated."""
    q = select(TrendSignal)
    if status:
        q = q.where(TrendSignal.status == status)
    if channel_id is not None:
        q = q.where(TrendSignal.channel_id == channel_id)
    order = TrendSignal.updated_at.desc() if sort == "updated" else TrendSignal.score.desc()
    return session.exec(q.order_by(order)).all()


@router.post("", status_code=201)
def upsert_trend(body: TrendCreate, session: Session = Depends(get_session)):
    """Record a researched trend. Upserts by normalized term so re-seeing a trend
    refreshes it (momentum/score/source) instead of creating a duplicate."""
    norm = _norm(body.term)
    if not norm:
        raise HTTPException(400, "term is required")
    if body.channel_id is not None and not session.get(Channel, body.channel_id):
        raise HTTPException(404, "channel not found")
    existing = session.exec(
        select(TrendSignal).where(TrendSignal.term_norm == norm)).first()
    data = body.model_dump(exclude_unset=True)
    if existing:
        # Don't clobber an adoption decision with a fresh sighting.
        for k, v in data.items():
            if k == "term":
                continue
            if k == "status" and existing.status == TrendStatus.ADOPTED:
                continue
            setattr(existing, k, v)
        existing.updated_at = utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    t = TrendSignal(
        term=body.term.strip(), term_norm=norm,
        status=body.status or TrendStatus.RESEARCHED,
        **{k: v for k, v in data.items() if k not in ("term", "status")},
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


@router.get("/{trend_id}")
def get_trend(trend_id: int, session: Session = Depends(get_session)):
    t = session.get(TrendSignal, trend_id)
    if not t:
        raise HTTPException(404, "trend not found")
    return t


@router.patch("/{trend_id}")
def update_trend(trend_id: int, body: TrendUpdate, session: Session = Depends(get_session)):
    t = session.get(TrendSignal, trend_id)
    if not t:
        raise HTTPException(404, "trend not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    t.updated_at = utcnow()
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


@router.post("/{trend_id}/adopt")
def adopt_trend(trend_id: int, body: TrendAdoptBody | None = None,
                session: Session = Depends(get_session)):
    """Smart-adopt: create a topic from the trend, seed ideas, and auto-produce a few
    (queued to render). Links the trend → topic so its performance can be tracked."""
    body = body or TrendAdoptBody()
    t = session.get(TrendSignal, trend_id)
    if not t:
        raise HTTPException(404, "trend not found")
    if t.status == TrendStatus.ADOPTED and t.adopted_topic_id:
        raise HTTPException(409, f"trend already adopted (topic {t.adopted_topic_id})")

    channel_id = body.channel_id or t.channel_id
    if channel_id is None:
        raise HTTPException(400, "no target channel — set the trend's channel_id or pass one")
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")

    fmt = body.content_format or t.content_format or "short"
    fmt = "long" if fmt == "long" else "short"
    theme = body.theme_prompt or t.description or f"Trending topic: {t.term}"

    # 1. Topic (same construction as topics.create_topic).
    mx_pos = session.exec(
        select(func.max(Topic.position)).where(Topic.channel_id == ch.id)).one()
    topic = Topic(channel_id=ch.id, name=t.term.strip(), theme_prompt=theme,
                  content_format=fmt, position=(mx_pos or 0) + 1)
    session.add(topic)
    session.commit()
    session.refresh(topic)

    # 2. Seed ideas; auto-produce the first `produce_count` (QUEUED), rest as DRAFT.
    try:
        ideas = video_gen.generate_ideas(topic.name, topic.theme_prompt, [],
                                         max(1, body.idea_count), fmt,
                                         language=video_gen.channel_language(session, ch.id))
    except Exception as e:
        raise HTTPException(502, f"idea generation failed: {e}")
    mx_v = session.exec(
        select(func.max(Video.position)).where(Video.channel_id == ch.id)).one() or 0
    produced = 0
    for i, subject in enumerate(ideas):
        status = VideoStatus.QUEUED if i < max(0, body.produce_count) else VideoStatus.DRAFT
        session.add(Video(channel_id=ch.id, topic_id=topic.id, subject=subject,
                          status=status, position=mx_v + 1 + i))
        produced += status == VideoStatus.QUEUED

    # 3. Link + close out the trend.
    t.adopted_topic_id = topic.id
    t.status = TrendStatus.ADOPTED
    t.channel_id = ch.id
    t.content_format = fmt
    t.updated_at = utcnow()
    session.add(t)
    quota.log(session, kind="trend_adopt", status="success", channel_id=ch.id,
              detail=f"adopted '{t.term}' → topic {topic.id} ({len(ideas)} ideas, "
                     f"{produced} producing)")
    session.commit()
    session.refresh(t)
    return {"trend": t, "topic_id": topic.id, "ideas": len(ideas), "producing": produced}
