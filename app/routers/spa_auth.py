"""SPA human login: Google OAuth → session cookie (not agent Basic, not YT Connect)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services import spa_google_login as spa

logger = logging.getLogger("manager.spa_auth")

router = APIRouter(prefix="/api/auth", tags=["spa-auth"])


def _require_enabled() -> None:
    if not spa.spa_login_enabled():
        raise HTTPException(404, "SPA Google login is disabled")


def _deny_html(email: str) -> HTMLResponse:
    safe = (email or "unknown")[:120]
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Access denied</title></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;">
<h1>Access denied</h1>
<p>The Google account <strong>{safe}</strong> is not allowlisted for Channels Manager.</p>
<p>Contact your operator if you need access.</p>
</body></html>"""
    return HTMLResponse(body, status_code=403)


@router.get("/me")
def auth_me(request: Request):
    _require_enabled()
    ident = spa.spa_session_identity(request)
    if not ident:
        raise HTTPException(401, "not signed in")
    ws_slug, actor = ident
    payload = request.session.get(spa.SESSION_KEY) or {}
    email = spa.normalized_email(str(payload.get("email") or ""))
    return {"email": email, "workspace_slug": ws_slug, "actor": actor}


@router.get("/google/start")
def google_start(request: Request):
    _require_enabled()
    try:
        url = spa.authorization_url(request)
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    return RedirectResponse(url, status_code=302)


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    _require_enabled()
    if error or not code:
        return HTMLResponse(
            "<html><body><h1>Sign-in cancelled</h1>"
            f"<p>{error or 'No authorization code was returned.'}</p></body></html>",
            status_code=400,
        )
    if not spa.consume_oauth_state(request, state):
        return HTMLResponse(
            "<html><body><h1>Sign-in failed</h1><p>Invalid or expired sign-in state.</p></body></html>",
            status_code=400,
        )
    try:
        profile = spa.exchange_code_and_profile(code)
    except Exception as e:
        logger.warning("SPA Google token exchange failed: %s", e)
        return HTMLResponse(
            f"<html><body><h1>Sign-in failed</h1><p>{str(e)[:200]}</p></body></html>",
            status_code=502,
        )
    email = spa.normalized_email(str(profile.get("email") or ""))
    if not email or not profile.get("email_verified", True):
        return HTMLResponse(
            "<html><body><h1>Sign-in failed</h1><p>Google did not return a verified email.</p></body></html>",
            status_code=400,
        )
    ws = spa.workspace_for_email(email)
    if not ws:
        logger.info("SPA login denied for non-allowlisted email %s", email)
        return _deny_html(email)
    spa.establish_session(request, email=email, workspace_slug=ws)
    return RedirectResponse("/", status_code=302)


@router.post("/logout")
def logout(request: Request):
    _require_enabled()
    spa.clear_session(request)
    return {"ok": True}
