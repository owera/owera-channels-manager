"""SPA Google Sign-In for humans — session cookie, allowlist, agent Basic unchanged.

Run: PYTHONPATH=. .venv/bin/python tests/verify_spa_google_login.py
"""
from __future__ import annotations

import base64
import os
import sys
from unittest.mock import patch

# Env before any app.main import (SessionMiddleware registers at import time).
os.environ["MANAGER_SPA_GOOGLE_LOGIN"] = "1"
os.environ["MANAGER_APP_PASSWORD"] = "agent-secret"
os.environ["MANAGER_SESSION_SECRET"] = "test-session-secret-key-32bytes!!"
os.environ["MANAGER_SPA_GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["MANAGER_SPA_GOOGLE_CLIENT_SECRET"] = "test-client-secret"
os.environ["MANAGER_SPA_GOOGLE_REDIRECT_URI"] = "http://127.0.0.1:7070/api/auth/google/callback"
os.environ["MANAGER_MULTI_WORKSPACE"] = "1"

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main_mod
from app.config import settings
from app.db import get_session
from app.models import Channel, Workspace
from app.services import spa_google_login

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(settings.spa_google_login is True, "settings.spa_google_login enabled for test")

def make_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


engine = make_engine()
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(id=1, workspace_id=1, slug="rr", name="RR"))
    s.add(Channel(id=2, workspace_id=1, slug="os", name="OS"))
    s.commit()


def _override_session():
    with Session(engine) as session:
        yield session


main_mod.app.dependency_overrides[get_session] = _override_session
client = TestClient(main_mod.app)

agent_basic = {"Authorization": "Basic " + base64.b64encode(b"owera:agent-secret").decode()}

# --- allowlist (locked v1) ---
ok(spa_google_login.workspace_for_email("rodrigo@owera.com") == "owera", "rodrigo@owera.com → owera")
ok(spa_google_login.workspace_for_email("Rodrigo@Owera.com") == "owera", "email normalized")
ok(spa_google_login.workspace_for_email("rjrecio@gmail.com") is None, "rjrecio denied")
ok(spa_google_login.workspace_for_email("rodrigo@recio.com") is None, "recio.com denied")

# --- routing / callback path ---
ok(settings.spa_google_redirect_uri.endswith("/api/auth/google/callback"),
   "redirect URI uses /api/auth/google/callback")

# --- features + login routes without session ---
r_feat = client.get("/api/features")
ok(r_feat.status_code == 200 and r_feat.json().get("spa_google_login") is True,
   "/api/features reachable without auth when SPA login on")

r_start = client.get("/api/auth/google/start", follow_redirects=False)
ok(r_start.status_code == 302 and "accounts.google.com" in r_start.headers.get("location", ""),
   "google/start redirects to Google")

# --- API requires session or Basic ---
r_api = client.get("/api/channels")
ok(r_api.status_code == 401 and "WWW-Authenticate" not in r_api.headers,
   "unauthenticated API 401 without Basic prompt when SPA login on")

r_agent = client.get("/api/channels", headers=agent_basic)
ok(r_agent.status_code == 200 and len(r_agent.json()) == 2,
   "agent Basic still lists workspace channels")

# --- OAuth callback: allowlisted vs denied ---
MOCK_PROFILE_ALLOW = {"email": "rodrigo@owera.com", "email_verified": True}
MOCK_PROFILE_DENY = {"email": "rjrecio@gmail.com", "email_verified": True}

c2 = TestClient(main_mod.app)
with patch.object(spa_google_login, "consume_oauth_state", return_value=True):
    with patch.object(spa_google_login, "exchange_code_and_profile",
                      return_value=MOCK_PROFILE_ALLOW):
        cb = c2.get("/api/auth/google/callback?code=abc&state=x", follow_redirects=False)
ok(cb.status_code == 302 and cb.headers.get("location") == "/",
   "allowlisted callback establishes session and redirects home")

c3 = TestClient(main_mod.app)
with patch.object(spa_google_login, "consume_oauth_state", return_value=True):
    with patch.object(spa_google_login, "exchange_code_and_profile",
                      return_value=MOCK_PROFILE_DENY):
        deny = c3.get("/api/auth/google/callback?code=abc&state=x")
ok(deny.status_code == 403 and "Access denied" in deny.text,
   "non-allowlisted email gets clear deny")

# --- session cookie scopes API ---
c4 = TestClient(main_mod.app)
with patch.object(spa_google_login, "consume_oauth_state", return_value=True):
    with patch.object(spa_google_login, "exchange_code_and_profile",
                      return_value=MOCK_PROFILE_ALLOW):
        c4.get("/api/auth/google/callback?code=abc&state=x", follow_redirects=False)
me = c4.get("/api/auth/me")
ok(me.status_code == 200 and me.json()["workspace_slug"] == "owera", "/api/auth/me after login")
ch = c4.get("/api/channels")
ok(ch.status_code == 200 and len(ch.json()) == 2, "session cookie loads owera channels")

logout_r = c4.post("/api/auth/logout")
ok(logout_r.status_code == 200, "POST logout returns 200")
set_cookies = logout_r.headers.get_list("set-cookie") or []
ok(
    any(
        "cm_session=" in h and "max-age=0" in h.replace(" ", "").lower()
        for h in set_cookies
    ),
    "logout response includes cm_session cleared with Max-Age=0",
)

# Same client still has no session until next login
ok(c4.get("/api/auth/me").status_code == 401, "logout clears session")
ok(c4.get("/api/channels").status_code == 401, "API requires sign-in after logout")

# --- GET /logout matches POST (navigations, proxies) ---
c5 = TestClient(main_mod.app)
with patch.object(spa_google_login, "consume_oauth_state", return_value=True):
    with patch.object(spa_google_login, "exchange_code_and_profile",
                      return_value=MOCK_PROFILE_ALLOW):
        c5.get("/api/auth/google/callback?code=abc&state=x", follow_redirects=False)
ok(c5.get("/api/auth/me").status_code == 200, "GET logout precheck: session ok")
get_logout = c5.get("/api/auth/logout")
ok(get_logout.status_code == 200, "GET logout returns 200")
ok(c5.get("/api/auth/me").status_code == 401, "GET logout clears session")

# --- platform YouTube OAuth path unchanged (still 404 when disabled) ---
settings.google_web_oauth = False
r_plat = client.post("/api/oauth/google/start", json={"channel_id": 1}, headers=agent_basic)
ok(r_plat.status_code == 404, "platform YT Connect untouched when MANAGER_GOOGLE_WEB_OAUTH=0")

# --- SPA static without auth ---
r_index = client.get("/")
ok(r_index.status_code in (200, 404), "non-/api routes do not require auth when SPA login on")

print(f"\nALL {_checks} CHECKS PASSED")
