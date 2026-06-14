"""Topics = channel content themes (managed in channel content settings).
Each topic owns a playlist; videos are generated from its theme."""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from app.db import get_session
from app.models import (Channel, Playlist, Topic, Video, VideoStatus, utcnow)
from app.schemas import GenerateBody, TopicCreate, TopicUpdate
from app.services import quota, video_gen, youtube

router = APIRouter(prefix="/api/topics", tags=["topics"])


def _topic_out(session: Session, t: Topic) -> dict:
    counts = dict(session.exec(
        select(Video.status, func.count(Video.id))
        .where(Video.topic_id == t.id).group_by(Video.status)
    ).all())
    pl = session.get(Playlist, t.playlist_id) if t.playlist_id else None
    return {
        **t.model_dump(),
        "video_counts": counts,
        "video_total": sum(counts.values()),
        "playlist_title": pl.title if pl else None,
        "playlist_yt_id": pl.yt_playlist_id if pl else None,
    }


@router.get("")
def list_topics(channel_id: int, session: Session = Depends(get_session)):
    topics = session.exec(
        select(Topic).where(Topic.channel_id == channel_id).order_by(Topic.position, Topic.id)
    ).all()
    return [_topic_out(session, t) for t in topics]


@router.post("", status_code=201)
def create_topic(body: TopicCreate, session: Session = Depends(get_session)):
    ch = session.get(Channel, body.channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    mx = session.exec(select(func.max(Topic.position)).where(Topic.channel_id == ch.id)).one()
    topic = Topic(
        channel_id=ch.id, name=body.name.strip(), theme_prompt=body.theme_prompt,
        content_format="long" if body.content_format == "long" else "short",
        render_profile_id=body.render_profile_id, position=(mx or 0) + 1,
    )
    # Optionally link an existing playlist; otherwise the topic's playlist is
    # created automatically the first time one of its videos enters production.
    if body.playlist_id:
        topic.playlist_id = body.playlist_id
    session.add(topic)
    session.commit()
    session.refresh(topic)
    return _topic_out(session, topic)


@router.get("/{topic_id}")
def get_topic(topic_id: int, session: Session = Depends(get_session)):
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "topic not found")
    return _topic_out(session, t)


@router.patch("/{topic_id}")
def update_topic(topic_id: int, body: TopicUpdate, session: Session = Depends(get_session)):
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "topic not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    t.updated_at = utcnow()
    session.add(t)
    session.commit()
    session.refresh(t)
    return _topic_out(session, t)


@router.delete("/{topic_id}", status_code=204)
def delete_topic(topic_id: int, session: Session = Depends(get_session)):
    t = session.get(Topic, topic_id)
    if not t:
        return
    # Refuse if it still has videos, to avoid orphaning the queue.
    n = session.exec(select(func.count(Video.id)).where(Video.topic_id == topic_id)).one()
    if n:
        raise HTTPException(409, f"topic has {n} videos — delete or move them first")
    session.delete(t)
    session.commit()


@router.post("/{topic_id}/generate")
def generate_videos(topic_id: int, body: GenerateBody, session: Session = Depends(get_session)):
    """Generate video ideas from the topic theme; they land as draft videos."""
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "topic not found")
    existing = session.exec(select(Video.subject).where(Video.topic_id == topic_id)).all()
    try:
        ideas = video_gen.generate_ideas(t.name, t.theme_prompt, list(existing),
                                         body.count, t.content_format)
    except Exception as e:
        raise HTTPException(502, f"idea generation failed: {e}")
    mx = session.exec(select(func.max(Video.position)).where(Video.channel_id == t.channel_id)).one() or 0
    for i, subj in enumerate(ideas):
        session.add(Video(channel_id=t.channel_id, topic_id=t.id, subject=subj,
                          status=VideoStatus.DRAFT, position=mx + 1 + i))
    quota.log(session, kind="generate", status="success", channel_id=t.channel_id,
              detail=f"generated {len(ideas)} ideas for '{t.name}'")
    session.commit()
    return {"generated": len(ideas)}
