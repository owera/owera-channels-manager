"""Platform Google Web OAuth (WS4 PR-E). Inert unless MANAGER_GOOGLE_WEB_OAUTH=1."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.config import settings
from app.db import get_session
from app.models import Channel, Workspace
from app.routers.channels import _callback_html, _fail_consent, _GRANT_HINTS
from app.services import oauth_pending, youtube
from app.workspace import current_workspace, require_channel as require_ws_channel

logger = logging.getLogger("manager.platform_oauth")

router = APIRouter(prefix="/api/oauth/google", tags=["platform-oauth"])


def _require_enabled() -> None:
    if not settings.google_web_oauth:
        raise HTTPException(404, "platform Google OAuth is disabled")


class PlatformOAuthStartBody(BaseModel):
    channel_id: int


@router.post("/start")
def platform_oauth_start(
    body: PlatformOAuthStartBody,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    _require_enabled()
    ch = require_ws_channel(session, ws, body.channel_id)
    redirect_uri = settings.oauth_redirect_uri
    try:
        verifier, challenge = oauth_pending.new_pkce_pair()
        flow = youtube.build_platform_flow(redirect_uri)
        url, state = youtube.authorization_url(
            flow, code_challenge=challenge, code_verifier=verifier,
        )
    except Exception as e:
        raise HTTPException(400, f"could not start OAuth: {e}") from e
    oauth_pending.create_pending(
        session,
        state=state,
        channel_id=ch.id,
        workspace_id=ch.workspace_id,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    return {"auth_url": url}


@router.get("/callback")
def platform_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
):
    _require_enabled()
    if error or not code or not state:
        return HTMLResponse(
            _callback_html("Connection failed", error or "consent cancelled", False),
        )
    pending = oauth_pending.consume_pending_by_state(session, state=state)
    if pending is None:
        return HTMLResponse(_callback_html("Connection failed", "invalid or expired state", False))
    ch = session.get(Channel, pending.channel_id)
    if not ch or ch.workspace_id != pending.workspace_id:
        return HTMLResponse(_callback_html("Connection failed", "channel not found", False))
    try:
        flow = youtube.build_platform_flow(pending.redirect_uri)
        flow.code_verifier = pending.code_verifier
        identity = youtube.finish_flow(session, ch, flow, code, store_platform=True)
    except youtube.GrantRejected as e:
        logger.warning("platform consent for channel '%s' rejected (%s): %s", ch.slug, e.code, e)
        msg = str(e)[:400] + _GRANT_HINTS.get(e.code, "")
        return HTMLResponse(_callback_html("Consent rejected", msg, False))
    except youtube.StatusFlipFailed as e:
        oauth_pending.supersede_channel(session, ch.id)
        display = e.identity.get("title") or ch.name
        return HTMLResponse(_callback_html(
            "Token saved, status update failed",
            f"The new token for {display} is saved and working, but the dashboard status "
            "could not be updated — do NOT redo the consent.", False,
        ))
    except Exception as e:
        _fail_consent(session, ch, str(e))
        return HTMLResponse(_callback_html("Connection failed", str(e)[:200], False))
    oauth_pending.supersede_channel(session, ch.id)
    display = identity.get("title") or ch.name
    return HTMLResponse(_callback_html(
        "Connected", f"{display} is linked. You can close this tab.", True,
    ))
