"""WS3 early-access onboarding checklist (workspace-scoped)."""

from __future__ import annotations

from sqlmodel import Session, func, select

from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus, Workspace
from app.services import activation


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
    fd_at, _src = activation.first_draft_at(session, ws)
    first_pub = None
    first_approved = None
    if ch_ids:
        first_pub = session.exec(select(func.min(Video.published_at)).where(
            Video.channel_id.in_(ch_ids), Video.status == VideoStatus.PUBLISHED)).one()
        first_approved = session.exec(select(func.min(Video.approved_at)).where(
            Video.channel_id.in_(ch_ids), Video.approved_at.is_not(None))).one()
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
        # S1: first rendered draft in review. A milestone, not part of
        # onboarding_complete (the first five steps), so that flag is unchanged.
        _step("first_draft", "First draft rendered for review", fd_at is not None),
        _step("live", "Publish first video (optional milestone)", live_n > 0),
    ]
    required_done = all(s["done"] for s in steps[:5])
    for st in steps:
        st["at"] = None
    by_id = {st["id"]: st for st in steps}
    by_id["first_draft"]["at"] = activation.iso_utc(fd_at)
    by_id["live"]["at"] = activation.iso_utc(first_pub)
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
        "is_test": bool(ws.is_test),
        "timestamps": {
            "created_at": activation.iso_utc(ws.created_at),
            "paid_at": activation.iso_utc(ws.paid_at),
            "first_draft_at": activation.iso_utc(fd_at),
            "first_approved_at": activation.iso_utc(first_approved),
            "first_published_at": activation.iso_utc(first_pub),
        },
        "render_failed": activation.render_failed_signal(session, ws),
    }
