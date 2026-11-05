"""Google Sign-In for SPA humans (server session + email→workspace allowlist).

Separate from platform YouTube OAuth (``MANAGER_GOOGLE_*`` / ``/api/oauth/google``).
Uses ``MANAGER_SPA_GOOGLE_*`` credentials on GCP project 618513602444.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request

from app.config import settings

LOGIN_SCOPES = ("openid", "email", "profile")
SESSION_KEY = "cm_spa_auth"
OAUTH_STATE_SESSION_KEY = "cm_spa_oauth_state"

# v1 allowlist (CoS locked): only this Google account may use SPA human login.
_SPA_EMAIL_WORKSPACE: dict[str, str] = {"rodrigo@owera.com": "owera"}

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"


def spa_login_enabled() -> bool:
    return bool(settings.spa_google_login)


def normalized_email(raw: str) -> str:
    return (raw or "").strip().lower()


def workspace_for_email(email: str) -> str | None:
    """Allowlisted email → workspace slug, or None if denied."""
    key = normalized_email(email)
    if not key:
        return None
    return _SPA_EMAIL_WORKSPACE.get(key)


def spa_session_identity(request: Request) -> tuple[str, str] | None:
    """(workspace_slug, actor) from the signed session cookie, or None."""
    if not spa_login_enabled():
        return None
    if "session" not in request.scope:
        return None
    payload = request.session.get(SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    email = normalized_email(str(payload.get("email") or ""))
    ws = (payload.get("workspace_slug") or "").strip()
    if not email or not ws:
        return None
    if workspace_for_email(email) != ws:
        return None
    actor = str(payload.get("actor") or email.split("@", 1)[0])
    return ws, actor


def establish_session(request: Request, *, email: str, workspace_slug: str) -> None:
    email_n = normalized_email(email)
    request.session[SESSION_KEY] = {
        "email": email_n,
        "workspace_slug": workspace_slug,
        "actor": email_n.split("@", 1)[0],
    }


def clear_session(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)
    request.session.pop(OAUTH_STATE_SESSION_KEY, None)


def _require_oauth_config() -> tuple[str, str, str]:
    cid = settings.spa_google_client_id.strip()
    secret = settings.spa_google_client_secret.strip()
    redirect = settings.spa_google_redirect_uri.strip()
    if not cid or not secret or not redirect:
        raise ValueError("SPA Google OAuth client is not configured")
    return cid, secret, redirect


def authorization_url(request: Request) -> str:
    client_id, _, redirect_uri = _require_oauth_config()
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_SESSION_KEY] = state
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(LOGIN_SCOPES),
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URI}?{urlencode(params)}"


def consume_oauth_state(request: Request, state: str | None) -> bool:
    expected = request.session.pop(OAUTH_STATE_SESSION_KEY, None)
    if not expected or not state or not secrets.compare_digest(expected, state):
        return False
    return True


def exchange_code_and_profile(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = _require_oauth_config()
    with httpx.Client(timeout=30.0) as client:
        tok = client.post(
            GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        tok.raise_for_status()
        access_token = tok.json().get("access_token")
        if not access_token:
            raise ValueError("token response missing access_token")
        ui = client.get(
            GOOGLE_USERINFO_URI,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        ui.raise_for_status()
        return ui.json()
