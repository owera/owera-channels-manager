"""Dependency-free regression checks for the OAuth redirect_uri (portal reconnect).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_oauth_redirect.py

Reproduces the redirect_uri_mismatch incident that kept ch2 disconnected for
~3 days: oauth_start built the redirect_uri from the incoming Host header, so a
reconnect initiated through the reverse proxy (Host: channels.owera.com) produced
a non-loopback redirect_uri the Desktop OAuth client rejects. With
MANAGER_PUBLIC_BASE_URL set, the redirect_uri is pinned to a registered base no
matter which Host the request arrived on; unset keeps the old request-derived
behavior so localhost reconnects are unchanged.

Drives the real /oauth/start endpoint with a real (offline) Google Flow built
from a fake Desktop client_secret.json — no network, no creds, temp dirs only.
Exits non-zero on the first failed assertion.
"""
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus
from app.routers import channels as channels_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---- fixture: temp credentials dir with a Desktop-type client_secret --------
SLUG = "verify-oauth"
tmp = Path(tempfile.mkdtemp(prefix="verify-oauth-redirect-"))
settings.credentials_dir = str(tmp)
(tmp / SLUG).mkdir(parents=True)
(tmp / SLUG / "client_secret.json").write_text(json.dumps({
    "installed": {
        "client_id": "verify.apps.googleusercontent.com",
        "client_secret": "verify-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}))

# ---- fixture: app with just the channels router + a private in-memory DB ----
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    ch = Channel(slug=SLUG, name="Verify", oauth_status=OAuthStatus.EXPIRED)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    CH_ID = ch.id

app = FastAPI()
app.include_router(channels_router.router)


def _session_override():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _session_override
client = TestClient(app)


def start_redirect_uri(host: str) -> str:
    """POST /oauth/start as if the request arrived on `host`; return the
    redirect_uri embedded in the returned Google authorization URL."""
    r = client.post(f"/api/channels/{CH_ID}/oauth/start", headers={"host": host})
    assert r.status_code == 200, f"/oauth/start -> {r.status_code}: {r.text[:200]}"
    return parse_qs(urlparse(r.json()["auth_url"]).query)["redirect_uri"][0]


CALLBACK = f"/api/channels/{CH_ID}/oauth/callback"

print("redirect_uri derivation (public_base_url unset — legacy behavior)")
settings.public_base_url = ""
ok(start_redirect_uri("localhost:7070") == f"http://localhost:7070{CALLBACK}",
   "localhost Host still yields the loopback redirect_uri")
ok(start_redirect_uri("channels.owera.com").startswith("http://channels.owera.com/"),
   "proxied Host yields a non-loopback redirect_uri (the incident's mechanism)")

print("redirect_uri pinned (MANAGER_PUBLIC_BASE_URL set — the fix)")
settings.public_base_url = "http://localhost:7070"
ok(start_redirect_uri("channels.owera.com") == f"http://localhost:7070{CALLBACK}",
   "proxied Host now yields the pinned loopback redirect_uri")
ok(start_redirect_uri("localhost:7070") == f"http://localhost:7070{CALLBACK}",
   "localhost Host yields the same pinned redirect_uri")

settings.public_base_url = "http://localhost:7070/"
ok(start_redirect_uri("channels.owera.com") == f"http://localhost:7070{CALLBACK}",
   "trailing slash in the setting does not double the slash")

settings.public_base_url = ""
print(f"ALL {_checks} CHECKS PASSED")
