"""WS3 onboarding checklist API + S2 guided onboarding wizard.

Wizard routes (``/api/onboarding/wizard*``) are scoped to the caller's workspace
(``current_workspace``) and every channel id goes through
``workspace.require_channel`` (S4: a foreign id is a 404). Each step reuses the
existing route function instead of a parallel code path:

  channel      → channels.create_channel
  topics       → topics.create_topic (+ Channel.niche)
  first-draft  → topics.generate_videos (1 idea per topic, LLM guard scope
                 "ideas") and videos.produce (draft → queued); autofill stays a
                 global flag and is never flipped.

Google Limited Use consent (``/api/onboarding/ai-data*``): the ack-only page
for any external workspace without a current consent, and the Settings
withdraw control (app/services/google_limited_use.py).

Locale: ``?locale=`` (the SPA sends the L6 detection result), else the
Accept-Language header; presets and banners come back in EN or PT.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.models import Topic, Video, VideoStatus, utcnow
from app.services import google_limited_use, llm_guard, onboarding_wizard as wiz, topic_presets
from app.services import onboarding as onboarding_svc
from app.workspace import Workspace, current_workspace, require_channel

logger = logging.getLogger("manager.onboarding")

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def ui_locale(request: Request) -> str:
    q = request.query_params.get("locale")
    if q:
        return llm_guard.ui_locale(q)
    al = (request.headers.get("accept-language") or "").split(",")[0]
    return llm_guard.ui_locale(al)


@router.get("/status")
def onboarding_status(
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    locale: str = Depends(ui_locale),
):
    return onboarding_svc.evaluate(session, ws, locale)


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
                 ws: Workspace = Depends(current_workspace),
                 locale: str = Depends(ui_locale)):
    return wiz.state(session, ws, locale)


class WizardChannelBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/wizard/channel")
def wizard_channel(body: WizardChannelBody, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace),
                   locale: str = Depends(ui_locale)):
    from app.routers import channels as channels_router
    from app.schemas import ChannelCreate
    existing = wiz.wizard_channel(session, ws)
    if existing is not None:
        if ws.wizard_started_at is None:
            raise HTTPException(409, {"code": "workspace_has_channels",
                                      "message": "this workspace already has channels"})
        return wiz.state(session, ws, locale)   # idempotent re-submit
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "channel name is required")
    slug = channels_router._slugify(name) or "channel"
    channels_router.create_channel(
        ChannelCreate(name=name, slug=slug, **wiz.default_budgets(ws)), session=session, ws=ws)
    ws.wizard_started_at = utcnow()
    session.add(ws)
    session.commit()
    session.refresh(ws)
    return wiz.state(session, ws, locale)


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
                  ws: Workspace = Depends(current_workspace),
                  locale: str = Depends(ui_locale)):
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
    return wiz.state(session, ws, locale)


class WizardBudgetsBody(BaseModel):
    channel_id: int
    daily_render_budget: int | None = None
    daily_publish_budget: int | None = None


@router.post("/wizard/budgets")
def wizard_budgets(body: WizardBudgetsBody, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace),
                   locale: str = Depends(ui_locale)):
    ch = _own_wizard_channel(session, ws, body.channel_id)
    _need(session, ws, "budgets")
    d = wiz.default_budgets(ws)
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
    return wiz.state(session, ws, locale)


class WizardConsentsBody(BaseModel):
    # LegalOps 14/11: ONE box "accept the Terms of Use and have read the
    # Privacy Notice" (``terms``) + the sha / locale of the exact sentence shown.
    terms: bool = False
    terms_text_sha: str | None = None
    terms_locale: str | None = None
    ai_data: bool = False
    # Google Limited Use consent: sha256 of the exact checkbox text displayed
    # (EN or PT) and the locale it was displayed in.
    ai_data_text_sha: str | None = None
    ai_data_locale: str | None = None


@router.post("/wizard/consents")
def wizard_consents(body: WizardConsentsBody, request: Request,
                    session: Session = Depends(get_session),
                    ws: Workspace = Depends(current_workspace),
                    locale: str = Depends(ui_locale)):
    _need(session, ws, "consents")
    if not (body.terms is True and body.ai_data is True):
        raise HTTPException(400, "accept the Terms of Use (and confirm you read the "
                                 "Privacy Notice), and tick the AI data box")
    from app.services import terms_consent
    try:
        terms_loc = terms_consent.validate(body.terms_text_sha, body.terms_locale)
        google_limited_use.validate(body.ai_data_locale, body.ai_data_text_sha)
    except terms_consent.TermsError as e:
        raise HTTPException(409, {"code": e.code, "message": e.message})
    except google_limited_use.ConsentError as e:
        raise HTTPException(409, {"code": e.code, "message": e.message})
    now = utcnow()
    # First acceptance is kept (an audit timestamp is never moved forward).
    ws.terms_accepted_at = ws.terms_accepted_at or now
    ws.privacy_accepted_at = ws.privacy_accepted_at or now
    # Stored once; replaced only when the stored one doesn't count (a PT acceptance
    # while the PT gate is closed), so the record shows the sentence that does.
    if not ws.terms_accepted_version or terms_consent.block_reason(ws) == "terms_pt_not_live":
        ws.terms_accepted_version = (body.terms_text_sha or "").strip().lower()
        ws.terms_accepted_locale = terms_loc
        # LegalOps 14/11: the full consent record (sentence + document shas, URLs, who).
        terms_consent.record(session, ws, sentence_sha=ws.terms_accepted_version, locale=terms_loc,
                             user_id=google_limited_use.request_user(request))
    session.add(ws)
    session.commit()
    session.refresh(ws)
    if not google_limited_use.is_valid(ws):
        google_limited_use.record_ack(session, ws, user=google_limited_use.request_user(request),
                                      locale=body.ai_data_locale, sha=body.ai_data_text_sha)
    logger.info("onboarding_consents workspace_id=%s terms=1 privacy_read=1 terms_sha=%s "
                "google_limited_use_consent=1 text_sha=%s", ws.id, (ws.terms_accepted_version or "")[:12],
                (ws.ai_data_ack_version or "")[:12])
    llm_guard.clear_block(ws.id)    # S8: drop a stale "finish setup step" banner now
    session.refresh(ws)
    return wiz.state(session, ws, locale)


class WizardFirstDraftBody(BaseModel):
    channel_id: int


@router.post("/wizard/first-draft")
def wizard_first_draft(body: WizardFirstDraftBody, session: Session = Depends(get_session),
                       ws: Workspace = Depends(current_workspace),
                       locale: str = Depends(ui_locale)):
    """1 idea for each of the 3 topics (existing generate route), then queue the
    first topic's idea at top render priority. Idempotent: a second call returns
    the same draft; a topic that already has ideas is not generated again."""
    from app.routers import topics as topics_router
    from app.routers import videos as videos_router
    from app.schemas import GenerateBody
    ch = _own_wizard_channel(session, ws, body.channel_id)
    if ws.first_draft_video_id is not None:
        return wiz.state(session, ws, locale)
    _need(session, ws, "ideas")
    topics = wiz._topics(session, ch)
    if not topics:
        raise HTTPException(409, {"code": "step_out_of_order", "missing": "topics"})

    def _drafts(topic_id):
        return session.exec(select(Video).where(
            Video.topic_id == topic_id, Video.status == VideoStatus.DRAFT)
            .order_by(Video.position, Video.id)).all()

    def _has_ideas(topic_id) -> bool:
        return session.exec(select(Video.id).where(Video.topic_id == topic_id)).first() is not None

    gen: dict = {}
    blocked_err: HTTPException | None = None
    for topic in topics:
        if _has_ideas(topic.id):
            continue
        try:
            gen = topics_router.generate_videos(topic.id, GenerateBody(count=wiz.IDEAS_PER_TOPIC),
                                                session=session, ws=ws)
        except HTTPException as e:
            if e.status_code == 403:   # LLM guard refusal (S8): stop, don't hammer
                session.refresh(ws)
                blocked = (llm_guard.blocked_status(ws, locale)
                           or llm_guard.block_message("release_off", locale))
                blocked_err = HTTPException(409, {"code": "llm_blocked", "llm_blocked": blocked,
                                                  "message": blocked["message"]})
                break
            raise
    drafts = [v for t in topics for v in _drafts(t.id)]
    if not drafts:
        if blocked_err is not None:
            raise blocked_err
        raise HTTPException(502, {"code": "no_ideas", "reason": (gen or {}).get("reason"),
                                  "message": "no ideas came back; try again in a minute"})
    v = drafts[0]
    videos_router.produce(v.id, session=session, ws=ws)   # draft → queued, audited JobRun
    ws.first_draft_video_id = v.id
    ws.wizard_completed_at = utcnow()
    session.add(ws)
    session.commit()
    session.refresh(ws)
    logger.info("onboarding_first_draft_queued workspace_id=%s video_id=%s priority=1 ideas=%s",
                ws.id, v.id, len(drafts))
    return wiz.state(session, ws, locale)


# ------------------------------------------- Google Limited Use consent (S2)

@router.get("/ai-data")
def ai_data_status(session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace),
                   locale: str = Depends(ui_locale)):
    return {**google_limited_use.status(ws),
            "text_shas": {loc: google_limited_use.text_sha(t)
                          for loc, t in google_limited_use.TEXTS.items()},
            "llm_blocked": llm_guard.blocked_status(ws, locale)}


class AiDataAckBody(BaseModel):
    ai_data: bool = False
    text_sha: str | None = None
    locale: str | None = None


@router.post("/ai-data/ack")
def ai_data_ack(body: AiDataAckBody, request: Request,
                session: Session = Depends(get_session),
                ws: Workspace = Depends(current_workspace),
                locale: str = Depends(ui_locale)):
    """Ack-only page: the Google Limited Use consent alone (terms are not
    re-asked here; the release check still requires them)."""
    if body.ai_data is not True:
        raise HTTPException(400, "tick the AI data box")
    try:
        google_limited_use.record_ack(session, ws, user=google_limited_use.request_user(request),
                                      locale=body.locale, sha=body.text_sha)
    except google_limited_use.ConsentError as e:
        raise HTTPException(409, {"code": e.code, "message": e.message})
    return ai_data_status(session=session, ws=ws, locale=locale)


@router.post("/ai-data/withdraw")
def ai_data_withdraw(request: Request, session: Session = Depends(get_session),
                     ws: Workspace = Depends(current_workspace),
                     locale: str = Depends(ui_locale)):
    """Settings: withdraw the Google Limited Use consent. Cuts the LLM at the next
    call; YouTube data deletion stays on the #214 disconnect / deletion path."""
    google_limited_use.withdraw(session, ws, user=google_limited_use.request_user(request),
                                reason="user")
    return ai_data_status(session=session, ws=ws, locale=locale)
