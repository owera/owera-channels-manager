"""P2: disconnect revokes the platform grant at Google, then deletes our copy.

- POST https://oauth2.googleapis.com/revoke with the refresh token (form body);
- a revoke failure (HTTP 5xx, network error, already-invalid) never blocks the
  local delete; logs carry the outcome, never the token;
- legacy ``legacy_file`` channels are not revoked (owner's own client) but their
  token files are still removed;
- deleting a platform channel revokes + removes the encrypted row too.

Run: PYTHONPATH=. uv run python tests/verify_google_revoke.py
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Channel, CredentialSource, OAuthStatus, Workspace
from app.routers import channels as channels_router
from app.services import token_store, youtube
from app.workspace import current_workspace

_checks = 0
REFRESH = "1//refresh-token-SECRET-value"
ACCESS = "ya29.access-token-SECRET-value"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


settings.token_enc_key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
settings.google_client_id = "web-client-id"
settings.google_client_secret = "web-client-secret"
settings.credentials_dir = tempfile.mkdtemp()

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.commit()

app = FastAPI()
app.include_router(channels_router.router)


def _sess():
    with Session(engine) as session:
        yield session


def _ws():
    with Session(engine) as session:
        return session.get(Workspace, 1)


app.dependency_overrides[get_session] = _sess
app.dependency_overrides[current_workspace] = _ws
client = TestClient(app)


def seed_platform(cid: int) -> None:
    with Session(engine) as s:
        ch = Channel(id=cid, workspace_id=1, slug=f"p{cid}", name=f"P{cid}",
                     credential_source=CredentialSource.PLATFORM_OAUTH,
                     oauth_status=OAuthStatus.CONNECTED, yt_channel_id=f"UC{cid}")
        s.add(ch)
        s.commit()
        s.refresh(ch)
        creds = Credentials(token=ACCESS, refresh_token=REFRESH,
                            token_uri="https://oauth2.googleapis.com/token",
                            client_id="web-client-id", client_secret="web-client-secret",
                            scopes=youtube.CONSENT_SCOPES)
        creds.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        token_store.save_credentials(s, ch, creds)
        s.commit()


def has_cred(cid: int) -> bool:
    with Session(engine) as s:
        return token_store.has_credential(s, cid)


class Rec:
    def __init__(self, status=200, exc=None):
        self.calls, self.status, self.exc = [], status, exc

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, request=httpx.Request("POST", url))


log_buf = io.StringIO()
h = logging.StreamHandler(log_buf)
logging.getLogger().addHandler(h)
logging.getLogger().setLevel(logging.INFO)

# --- 1. happy path ---
seed_platform(10)
rec = Rec(200)
with patch("httpx.post", side_effect=rec):
    r = client.post("/api/channels/10/disconnect")
ok(r.status_code == 200, "disconnect 200")
ok(len(rec.calls) == 1, "one revoke call")
url, kw = rec.calls[0]
ok(url == "https://oauth2.googleapis.com/revoke", "revoke endpoint is oauth2.googleapis.com/revoke")
ok(kw.get("data") == {"token": REFRESH}, "revokes the refresh token in a form body (not the URL)")
ok(kw["headers"]["Content-Type"] == "application/x-www-form-urlencoded", "form content type")
ok(not has_cred(10), "local encrypted credential deleted")
ok(r.json()["oauth_status"] == OAuthStatus.DISCONNECTED, "status disconnected")

# --- 2. Google 5xx, network error, already-invalid: local delete still happens ---
for cid, rec in ((11, Rec(503)), (12, Rec(exc=httpx.ConnectTimeout("timeout"))), (13, Rec(400))):
    seed_platform(cid)
    with patch("httpx.post", side_effect=rec):
        r = client.post(f"/api/channels/{cid}/disconnect")
    ok(r.status_code == 200 and not has_cred(cid),
       f"revoke outcome {rec.status if not rec.exc else 'network error'} → still deleted locally")

# --- 3. unreadable token (wrong key) → no revoke call, still deleted ---
seed_platform(14)
good_key = settings.token_enc_key
settings.token_enc_key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
rec = Rec(200)
with patch("httpx.post", side_effect=rec):
    r = client.post("/api/channels/14/disconnect")
settings.token_enc_key = good_key
ok(r.status_code == 200 and not has_cred(14) and not rec.calls,
   "undecryptable token: no revoke, local row still deleted")

# --- 4. outcomes ---
with patch("httpx.post", side_effect=Rec(200)):
    ok(youtube.revoke_token_at_google("x") == "revoked", "200 → revoked")
with patch("httpx.post", side_effect=Rec(400)):
    ok(youtube.revoke_token_at_google("x") == "already_invalid", "400 → already_invalid")
with patch("httpx.post", side_effect=Rec(500)):
    ok(youtube.revoke_token_at_google("x") == "http_500", "500 → http_500")
with patch("httpx.post", side_effect=Rec(exc=httpx.ConnectError("x"))):
    ok(youtube.revoke_token_at_google("x") == "error_ConnectError", "network → error_<Class>")
ok(youtube.revoke_token_at_google("") == "no_token", "empty token → no call")

# --- 5. legacy channel: no revoke, token files removed ---
with Session(engine) as s:
    s.add(Channel(id=20, workspace_id=1, slug="legacy", name="L",
                  credential_source=CredentialSource.LEGACY_FILE, oauth_status=OAuthStatus.CONNECTED))
    s.commit()
tp = youtube.token_path("legacy")
tp.parent.mkdir(parents=True, exist_ok=True)
tp.write_text('{"token":"%s","refresh_token":"%s"}' % (ACCESS, REFRESH))
rec = Rec(200)
with patch("httpx.post", side_effect=rec):
    r = client.post("/api/channels/20/disconnect")
ok(r.status_code == 200 and not rec.calls, "legacy channel: no Google revoke (owner's own client)")
ok(not tp.exists(), "legacy token.json removed")

# --- 6. delete platform channel revokes + removes row ---
seed_platform(30)
rec = Rec(200)
with patch("httpx.post", side_effect=rec):
    r = client.delete("/api/channels/30")
ok(r.status_code == 204, "delete 204")
ok(len(rec.calls) == 1 and not has_cred(30), "delete platform channel: revoked + credential removed")

# --- 7. no token in logs ---
logs = log_buf.getvalue()
ok("google revoke for channel id=10" in logs and "revoked" in logs, "outcome logged")
ok(REFRESH not in logs and ACCESS not in logs and "SECRET" not in logs, "logs never contain the token")

print(f"\nALL {_checks} CHECKS PASSED")
