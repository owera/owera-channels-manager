"""WS4 PR-E: platform Google OAuth routes (flag-gated)."""
from __future__ import annotations

import base64
import os
import sys
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Channel, Workspace
from app.routers import platform_oauth as po_router
from app.services import youtube

_checks = 0
_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(id=5, workspace_id=1, slug="ea", name="EA"))
    s.commit()

app = FastAPI()
app.include_router(po_router.router)


def _sess():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _sess
client = TestClient(app)

_orig = {
    "multi_workspace": settings.multi_workspace,
    "allow_workspace_header": settings.allow_workspace_header,
    "google_web_oauth": settings.google_web_oauth,
    "google_client_id": settings.google_client_id,
    "google_client_secret": settings.google_client_secret,
    "token_enc_key": settings.token_enc_key,
    "oauth_redirect_uri": settings.oauth_redirect_uri,
}


def _basic_slug(slug: str) -> dict[str, str]:
    """Production claw0: workspace slug is the Basic Auth username (password = app password)."""
    token = base64.b64encode(f"{slug}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _oauth_start(headers: dict[str, str] | None = None):
    return client.post(
        "/api/oauth/google/start",
        json={"channel_id": 5},
        headers=headers or {},
    )


# --- single-workspace (default): env MANAGER_MULTI_WORKSPACE must not break verify ---
settings.multi_workspace = False
settings.allow_workspace_header = False
settings.google_web_oauth = False
r = _oauth_start()
ok(r.status_code == 404, "start returns 404 when MANAGER_GOOGLE_WEB_OAUTH=0 (multi_workspace=0)")

settings.google_web_oauth = True
settings.google_client_id = "cid"
settings.google_client_secret = "sec"
settings.token_enc_key = _KEY
settings.oauth_redirect_uri = "https://channels.owera.com/api/oauth/google/callback"

class _FakeFlow:
    code_verifier = None
    credentials = None

    def fetch_token(self, code):
        from google.oauth2.credentials import Credentials
        self.credentials = Credentials(
            token="t", refresh_token="rt",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="cid", client_secret="sec",
            scopes=youtube.CONSENT_SCOPES,
        )


with patch.object(youtube, "build_platform_flow", return_value=_FakeFlow()), \
     patch.object(youtube, "authorization_url", return_value=("https://accounts.google.com/o", "st")), \
     patch.object(youtube, "identity_for_creds", return_value={"id": "UC_test", "title": "Test"}), \
     patch.object(youtube, "verify_grant", wraps=youtube.verify_grant):
    r = _oauth_start()
    ok(r.status_code == 200 and "auth_url" in r.json(), "start returns auth_url when enabled (multi_workspace=0)")

    cb = client.get("/api/oauth/google/callback?code=abc&state=st")
    ok(cb.status_code == 200 and b"Connected" in cb.content, "callback completes consent HTML")

with Session(engine) as s:
    ch = s.get(Channel, 5)
    ok(ch.oauth_status == "connected", "channel flipped connected")
    ok(ch.credential_source == "platform_oauth", "credential_source platform_oauth")

# --- multi-workspace ON (claw0 post-flip): slug from Basic username, not empty user ---
settings.multi_workspace = True
settings.allow_workspace_header = False
settings.google_web_oauth = False
r = _oauth_start()
ok(r.status_code == 401, "multi_workspace: empty Basic username → 401 before route logic")
r = _oauth_start(headers={"X-Workspace-Slug": "owera"})
ok(r.status_code == 401, "multi_workspace: X-Workspace-Slug ignored when allow_workspace_header=0")
r = _oauth_start(headers=_basic_slug("owera"))
ok(r.status_code == 404, "start returns 404 when MANAGER_GOOGLE_WEB_OAUTH=0 (multi_workspace=1 + slug Basic)")

settings.google_web_oauth = True
with patch.object(youtube, "build_platform_flow", return_value=_FakeFlow()), \
     patch.object(youtube, "authorization_url", return_value=("https://accounts.google.com/o", "st2")), \
     patch.object(youtube, "identity_for_creds", return_value={"id": "UC_test", "title": "Test"}), \
     patch.object(youtube, "verify_grant", wraps=youtube.verify_grant):
    r = _oauth_start(headers=_basic_slug("owera"))
    ok(r.status_code == 200 and "auth_url" in r.json(), "start auth_url when enabled (multi_workspace=1 + slug Basic)")

for key, val in _orig.items():
    setattr(settings, key, val)

print(f"\nALL {_checks} CHECKS PASSED")
