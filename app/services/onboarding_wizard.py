"""S2 guided onboarding: one wizard from login to the first draft.

Steps (in order; each is derived from the data, so a reload resumes):

  channel     create the workspace's first channel
  youtube     Connect YouTube (existing OAuth routes; consent note #215 + L6 line)
  topics      niche + 3 topics (presets in ``topic_presets`` or free text)
  budgets     confirm the default daily render / publish budgets
  consents    Terms of Service + Privacy Policy acceptance and the Google Limited
              Use consent (LegalOps checkbox text, ``google_limited_use``; the L6
              line stays displayed above it)
  ideas       1 idea for each of the 3 topics (existing generate route)
  first_draft the first topic's idea queued at top render priority

The wizard is per instance: nothing here names an Owera workspace, topic or
channel. It is active for a workspace that has no channel yet, or that started
the wizard and has not finished it. Legacy workspaces (channels created before
S2, ``wizard_started_at`` null) never see it.
"""

from __future__ import annotations

from sqlmodel import Session, func, select

from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus, Workspace
from app.services import activation, llm_guard, topic_presets

# CM-CONNECT-AI-LINE.md (LegalOps FINAL). The SPA shows the verbatim EN/PT L6
# line from frontend/src/l6ConnectAiLine.ts (informational; the consent itself
# is the Google Limited Use checkbox text, google_limited_use.KNOWN_SHAS).
AI_DATA_LINE_SHA = "845c22005853"

# PO CM 14/11: 1 idea from each of the 3 topics (3 total); the first topic's
# idea becomes the first draft, the other two stay drafts.
IDEAS_PER_TOPIC = 1
WIZARD_IDEA_COUNT = IDEAS_PER_TOPIC * topic_presets.WIZARD_TOPIC_COUNT

STEP_IDS = ("channel", "youtube", "topics", "budgets", "consents", "ideas", "first_draft")


def default_budgets(ws: Workspace | None = None) -> dict:
    """Week-1 buyer defaults (2/day, ``buyer_budget``) for a non-internal
    workspace going through the wizard; the app's ChannelCreate defaults otherwise."""
    from app.schemas import ChannelCreate
    from app.services import buyer_budget
    if ws is not None and ws.id is not None and ws.id not in llm_guard.internal_workspace_ids():
        return buyer_budget.buyer_budgets()
    f = ChannelCreate.model_fields
    return {"daily_render_budget": int(f["daily_render_budget"].default),
            "daily_publish_budget": int(f["daily_publish_budget"].default)}


def wizard_channel(session: Session, ws: Workspace) -> Channel | None:
    return session.exec(select(Channel).where(Channel.workspace_id == ws.id)
                        .order_by(Channel.id)).first()


def channel_count(session: Session, ws: Workspace) -> int:
    return int(session.exec(select(func.count(Channel.id))
                            .where(Channel.workspace_id == ws.id)).one())


def is_active(session: Session, ws: Workspace) -> bool:
    if ws.wizard_completed_at is not None:
        return False
    if ws.wizard_started_at is not None:
        return True
    return channel_count(session, ws) == 0


def is_first_login(session: Session, ws: Workspace) -> bool:
    """Wizard active, or finished but the first draft hasn't rendered yet."""
    if is_active(session, ws):
        return True
    return ws.wizard_started_at is not None and ws.first_draft_at is None


def consents_done(ws: Workspace) -> bool:
    from app.services import google_limited_use
    return (ws.terms_accepted_at is not None and ws.privacy_accepted_at is not None
            and google_limited_use.is_valid(ws))


def _topics(session: Session, ch: Channel | None) -> list[Topic]:
    if ch is None:
        return []
    return list(session.exec(select(Topic).where(Topic.channel_id == ch.id)
                             .order_by(Topic.position, Topic.id)).all())


def first_topic(session: Session, ch: Channel | None) -> Topic | None:
    ts = _topics(session, ch)
    return ts[0] if ts else None


def _idea_count(session: Session, ch: Channel | None) -> int:
    if ch is None:
        return 0
    return int(session.exec(select(func.count(Video.id)).where(Video.channel_id == ch.id)).one())


def step_done(session: Session, ws: Workspace) -> dict[str, bool]:
    ch = wizard_channel(session, ws)
    topics = _topics(session, ch)
    return {
        "channel": ch is not None,
        "youtube": ch is not None and ch.oauth_status == OAuthStatus.CONNECTED,
        "topics": ch is not None and len(topics) >= topic_presets.WIZARD_TOPIC_COUNT,
        "budgets": ws.wizard_budgets_at is not None,
        "consents": consents_done(ws),
        "ideas": _idea_count(session, ch) > 0,
        "first_draft": ws.first_draft_video_id is not None,
    }


def current_step(done: dict[str, bool]) -> str | None:
    for sid in STEP_IDS:
        if not done[sid]:
            return sid
    return None


def missing_before(done: dict[str, bool], step: str) -> str | None:
    """First earlier step that is not done (the wizard is strictly ordered)."""
    for sid in STEP_IDS:
        if sid == step:
            return None
        if not done[sid]:
            return sid
    return None


def state(session: Session, ws: Workspace, locale: str | None = None) -> dict:
    from app.services import google_limited_use
    ch = wizard_channel(session, ws)
    done = step_done(session, ws)
    fd = session.get(Video, ws.first_draft_video_id) if ws.first_draft_video_id else None
    if fd is not None and (ch is None or fd.channel_id != ch.id):
        fd = None
    return {
        "workspace_id": ws.id,
        "active": is_active(session, ws),
        # First login: the SPA hides Trends/Settings until the first draft rendered.
        "first_login": is_first_login(session, ws),
        "current_step": current_step(done),
        "steps": [{"id": sid, "done": done[sid]} for sid in STEP_IDS],
        "channel": None if ch is None else {
            "id": ch.id, "name": ch.name, "niche": ch.niche,
            "oauth_status": ch.oauth_status, "yt_channel_title": ch.yt_channel_title,
            "daily_render_budget": ch.daily_render_budget,
            "daily_publish_budget": ch.daily_publish_budget,
        },
        "topics": [{"id": t.id, "name": t.name, "theme_prompt": t.theme_prompt}
                   for t in _topics(session, ch)],
        "default_budgets": default_budgets(ws),
        "presets": topic_presets.public_presets(locale),
        "locale": llm_guard.ui_locale(locale),
        "consents": {
            "terms_accepted_at": activation.iso_utc(ws.terms_accepted_at),
            "privacy_accepted_at": activation.iso_utc(ws.privacy_accepted_at),
            "ai_data_ack_at": activation.iso_utc(ws.ai_data_ack_at),
            "ai_data_ack_version": ws.ai_data_ack_version,
            "ai_data_ack_locale": ws.ai_data_ack_locale,
        },
        "ai_data_line_version": AI_DATA_LINE_SHA,
        # Google Limited Use consent: the shas the server accepts (per locale).
        "ai_data_text_shas": {loc: google_limited_use.text_sha(t)
                              for loc, t in google_limited_use.TEXTS.items()},
        "google_limited_use": google_limited_use.status(ws),
        "first_draft": None if fd is None else {
            "video_id": fd.id, "status": fd.status, "subject": fd.subject,
            "priority": ws.first_draft_at is None,
        },
        "first_draft_at": activation.iso_utc(ws.first_draft_at),
        # S8 banner ("Drafts paused: finish setup step …"); null when allowed / flag OFF.
        "llm_blocked": llm_guard.blocked_status(ws, locale),
    }


def priority_video_ids(session: Session) -> set[int]:
    """Videos holding first-draft render priority: the one the wizard queued, per
    workspace, until S1 stamps ``first_draft_at`` (first successful render). The
    join keeps it to a video on a channel of that same workspace."""
    rows = session.exec(
        select(Workspace.first_draft_video_id)
        .join(Video, Video.id == Workspace.first_draft_video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(Workspace.first_draft_video_id.is_not(None),
               Workspace.first_draft_at.is_(None),
               Channel.workspace_id == Workspace.id)).all()
    return {int(r) for r in rows if r is not None}
