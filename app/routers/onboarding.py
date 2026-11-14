"""WS3 onboarding checklist API + S2 guided onboarding wizard.

Wizard routes (``/api/onboarding/wizard*``) are scoped to the caller's workspace
(``current_workspace``) and every channel id goes through
``workspace.require_channel`` (S4: a foreign id is a 404). Each step reuses the
existing route function instead of a parallel code path:

  channel      → channels.create_channel
  topics       → topics.create_topic (+ Channel.niche)
  first-draft  → topics.generate_videos (ideas, LLM guard scope "ideas") and
                 videos.produce (draft → queued); autofill stays a global flag
                 and is never flipped.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.models import Topic, Video, VideoStatus, utcnow
from app.services import llm_guard, onboarding_wizard as wiz, topic_presets
from app.services import onboarding as onboarding_svc
from app.workspace import Workspace, current_workspace, require_channel

logger = logging.getLogger("manager.onboarding")

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.get("/status")
def onboarding_status(
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    return onboarding_svc.evaluate(session, ws)


# ----------------------------------------------------------------- S2 wizard

def _need(session: Session, ws: Workspace, step: str) -> None:
    miss = wiz.missing_before(wiz.step_done(session, ws), step)
    if miss:
        raise HTTPException(409, {"code": "step_out_of_order", "step": step,
                                  "missing": miss,
                                  "message": f"finish step '{miss}' first"})


def _own_wizard_channel(session: Session, ws: Workspace, channel_id: int):
    ch = require_channel(session, ws, channel_id)   # S4: foreign/missing → 404
    wch = wiz.wizard_channel(session, ws)
    if wch is None or wch.id != ch.id:
        raise HTTPException(409, {"code": "not_wizard_channel",
                                  "message": "the wizard sets up the workspace's first channel"})
    return ch


@router.get("/wizard")
def wizard_state(session: Session = Depends(get_session),
                 ws: Workspace = Depends(current_workspace)):
    return wiz.state(session, ws)


class WizardChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/wizard/channel")
def wizard_channel(body: WizardChannelBody, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace)):
    from app.routers import channels as channels_router
    from app.schemas import ChannelCreate
    existing = wiz.wizard_channel(session, ws)
    if existing is not None:
        if ws.wizard_started_at is None:
            raise HTTPException(409, {"code": "workspace_has_channels",
                                      "message": "this workspace already has channels"})
        return wiz.state(session, ws)   # idempotent re-submit
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "channel name is required")
    slug = channels_router._slugify(name) or "channel"
    channels_router.create_channel(
        ChannelCreate(name=name, slug=slug, **wiz.default_budgets()), session=session, ws=ws)
    ws.wizard_started_at = utcnow()
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return wiz.state(session, ws)


class WizardTopic(BaseModel):
    name: str
    theme_prompt: str | None = None


class WizardTopicsBody(BaseModel):
    channel_id: int
    niche: str
    preset_id: str | None = None
    topics: list[WizardTopic]


@router.post("/wizard/topics")
def wizard_topics(body: WizardTopicsBody, session: Session = Depends(get_session),
                  ws: Workspace = Depends(current_workspace)):
    from app.routers import topics as topics_router
    from app.schemas import TopicCreate
    ch = _own_wizard_channel(session, ws, body.channel_id)
    _need(session, ws, "topics")
    niche = (body.niche or "").strip()
    if not niche or len(niche) > topic_presets.MAX_NICHE_CHARS:
        raise HTTPException(400, f"niche is required (max {topic_presets.MAX_NICHE_CHARS} chars)")
    n = topic_presets.WIZARD_TOPIC_COUNT
    clean: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for t in body.topics:
        name = (t.name or "").strip()
        if not name:
            continue
        if len(name) > topic_presets.MAX_TOPIC_NAME_CHARS:
            raise HTTPException(400, f"topic name max {topic_presets.MAX_TOPIC_NAME_CHARS} chars")
        if name.lower() in seen:
            raise HTTPException(400, f"duplicate topic '{name}'")
        seen.add(name.lower())
        tp = (t.theme_prompt or "").strip()[:topic_presets.MAX_THEME_PROMPT_CHARS] or None
        clean.append((name, tp))
    if len(clean) != n:
        raise HTTPException(400, f"pick exactly {n} topics")
    if session.exec(select(Video.id).where(Video.channel_id == ch.id)).first() is not None:
        raise HTTPException(409, {"code": "ideas_exist",
                                  "message": "ideas already exist; edit topics in Channels"})
    # Re-submit before any idea exists: replace the wizard's topics.
    for old in session.exec(select(Topic).where(Topic.channel_id == ch.id)).all():
        session.delete(old)
    ch.niche = niche
    session.add(ch)
    session.commit()
    for name, tp in clean:
        topics_router.create_topic(TopicCreate(channel_id=ch.id, name=name, theme_prompt=tp,
                                               content_format="short"),
                                   session=session, ws=ws)
    return wiz.state(session, ws)


class WizardBudgetsBody(BaseModel):
    channel_id: int
    daily_render_budget: int | None = None
    daily_publish_budget: int | None = None


@router.post("/wizard/budgets")
def wizard_budgets(body: WizardBudgetsBody, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace)):
    ch = _own_wizard_channel(session, ws, body.channel_id)
    _need(session, ws, "budgets")
    d = wiz.default_budgets()
    vals = {}
    for key in ("daily_render_budget", "daily_publish_budget"):
        v = getattr(body, key)
        v = d[key] if v is None else v
        if not isinstance(v, int) or isinstance(v, bool) or v < 1 or v > 50:
            raise HTTPException(400, f"{key} must be an integer from 1 to 50")
        vals[key] = v
    ch.daily_render_budget = vals["daily_render_budget"]
    ch.daily_publish_budget = vals["daily_publish_budget"]
    ws.wizard_budgets_at = utcnow()
    session.add(ch)
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return wiz.state(session, ws)


class WizardConsentsBody(BaseModel):
    terms: bool = False
    privacy: bool = False
    ai_data: bool = False
    ai_data_line_version: str | None = None


@router.post("/wizard/consents")
def wizard_consents(body: WizardConsentsBody, session: Session = Depends(get_session),
                    ws: Workspace = Depends(current_workspace)):
    _need(session, ws, "consents")
    if not (body.terms is True and body.privacy is True and body.ai_data is True):
        raise HTTPException(400, "accept the Terms of Service and the Privacy Policy, "
                                 "and acknowledge the AI data line")
    if (body.ai_data_line_version or "") != wiz.AI_DATA_LINE_SHA:
        raise HTTPException(409, {"code": "ai_data_line_changed",
                                  "message": "the AI data line changed; reload and read it again"})
    now = utcnow()
    # First acceptance is kept (an audit timestamp is never moved forward).
    ws.terms_accepted_at = ws.terms_accepted_at or now
    ws.privacy_accepted_at = ws.privacy_accepted_at or now
    if ws.ai_data_ack_at is None:
        ws.ai_data_ack_at = now
        ws.ai_data_ack_version = wiz.AI_DATA_LINE_SHA
    session.add(ws)
    session.commit()
    session.refresh(ws)
    logger.info("onboarding_consents workspace_id=%s terms=1 privacy=1 ai_data=1 version=%s",
                ws.id, ws.ai_data_ack_version)
    llm_guard.clear_block(ws.id)    # S8: drop a stale "finish setup step" banner now
    session.refresh(ws)
    return wiz.state(session, ws)


class WizardFirstDraftBody(BaseModel):
    channel_id: int


@router.post("/wizard/first-draft")
def wizard_first_draft(body: WizardFirstDraftBody, session: Session = Depends(get_session),
                       ws: Workspace = Depends(current_workspace)):
    """Generate ideas for the first topic (existing route) and queue one at top
    render priority. Idempotent: a second call returns the same draft."""
    from app.routers import topics as topics_router
    from app.routers import videos as videos_router
    from app.schemas import GenerateBody
    ch = _own_wizard_channel(session, ws, body.channel_id)
    if ws.first_draft_video_id is not None:
        return wiz.state(session, ws)
    _need(session, ws, "ideas")
    topic = wiz.first_topic(session, ch)
    if topic is None:
        raise HTTPException(409, {"code": "step_out_of_order", "missing": "topics"})

    def _drafts():
        return session.exec(select(Video).where(
            Video.topic_id == topic.id, Video.status == VideoStatus.DRAFT)
            .order_by(Video.position, Video.id)).all()

    gen: dict = {}
    if not _drafts():
        try:
            gen = topics_router.generate_videos(topic.id, GenerateBody(count=wiz.WIZARD_IDEA_COUNT),
                                          session=session, ws=ws)
        except HTTPException as e:
            if e.status_code == 403:   # LLM guard refusal (S8)
                session.refresh(ws)
                blocked = llm_guard.blocked_status(ws) or llm_guard.block_message("release_off")
                raise HTTPException(409, {"code": "llm_blocked", "llm_blocked": blocked,
                                          "message": blocked["message"]})
            raise
    drafts = _drafts()
    if not drafts:
        raise HTTPException(502, {"code": "no_ideas", "reason": (gen or {}).get("reason"),
                                  "message": "no ideas came back; try again in a minute"})
    v = drafts[0]
    videos_router.produce(v.id, session=session, ws=ws)   # draft → queued, audited JobRun
    ws.first_draft_video_id = v.id
    ws.wizard_completed_at = utcnow()
    session.add(ws)
    session.commit()
    session.refresh(ws)
    logger.info("onboarding_first_draft_queued workspace_id=%s video_id=%s priority=1", ws.id, v.id)
    return wiz.state(session, ws)
