"""WS3 early-access onboarding checklist (workspace-scoped)."""

from __future__ import annotations

from sqlmodel import Session, func, select

from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus, Workspace


def _step(step_id: str, label: str, done: bool) -> dict:
    return {"id": step_id, "label": label, "done": done}


def evaluate(session: Session, ws: Workspace) -> dict:
    channels = list(session.exec(
        select(Channel).where(Channel.workspace_id == ws.id)
    ).all())
    ch_ids = [c.id for c in channels if c.id is not None]
    connected = any(c.oauth_status == OAuthStatus.CONNECTED for c in channels)
    topic_n = 0
    idea_n = 0
    live_n = 0
    if ch_ids:
        topic_n = session.exec(
            select(func.count(Topic.id)).where(Topic.channel_id.in_(ch_ids))
        ).one()
        idea_n = session.exec(
            select(func.count(Video.id)).where(
                Video.channel_id.in_(ch_ids),
                Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED]),
            )
        ).one()
        live_n = session.exec(
            select(func.count(Video.id)).where(
                Video.channel_id.in_(ch_ids),
                Video.status == VideoStatus.PUBLISHED,
            )
        ).one()
    budgets_ok = bool(channels) and all(
        (c.daily_publish_budget or 0) > 0 and (c.daily_render_budget or 0) > 0
        for c in channels
    )
    steps = [
        _step("channel", "Create a channel", len(channels) > 0),
        _step("youtube", "Connect YouTube (OAuth)", connected),
        _step("topic", "Add a content topic", topic_n > 0),
        _step("budgets", "Set daily render + publish budgets", budgets_ok),
        _step("ideas", "Generate video ideas", idea_n > 0),
        _step("live", "Publish first video (optional milestone)", live_n > 0),
    ]
    required_done = all(s["done"] for s in steps[:5])
    complete = required_done
    if ws.onboarding_complete != complete:
        ws.onboarding_complete = complete
        session.add(ws)
        session.commit()
    return {
        "workspace_id": ws.id,
        "workspace_slug": ws.slug,
        "onboarding_complete": complete,
        "steps": steps,
    }
