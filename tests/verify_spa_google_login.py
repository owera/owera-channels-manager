"""SPA Google Sign-In for humans — session cookie, allowlist, agent Basic unchanged.

Run: PYTHONPATH=. .venv/bin/python tests/verify_spa_google_login.py
"""
from __future__ import annotations

import base64
import copy
import os
import sys
from unittest.mock import patch

# Env before any app.main import (SessionMiddleware registers at import time).
# Force off: tip/CI machines may have MANAGER_SESSION_COOKIE_SECURE=1 in manager/.env.
# TestClient is HTTP-only; Secure cookies are not stored → OAuth callback would not
# persist session and /api/auth/me after login would not reach 200.
os.environ["MANAGER_SESSION_COOKIE_SECURE"] = "0"
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
from app.routers import spa_auth as spa_auth_mod
from fastapi.responses import JSONResponse
from starlette.requests import Request
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

# --- Logout Set-Cookie shape: #193 vs dual-delete (headers, not TestClient jar) ---
# Browsers key cm_session by (name, domain, path, Secure). A deletion that only sets
# Secure does not remove a legacy cookie that was issued without Secure — live HTTPS
# then kept /api/auth/me at 200 after Sign out. Dual-delete sends both variants.
# TestClient + SessionMiddleware cannot model that jar split (middleware also clears
# the cookie when session is empty), so we assert raw Set-Cookie lines only.


def _minimal_request_with_session(sess: dict) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"spec_version": "2.0", "version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "root_path": "",
            "path": "/api/auth/logout",
            "raw_path": b"/api/auth/logout",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 443),
            "session": sess,
        }
    )


def _spa_session_dict():
    return {
        spa_google_login.SESSION_KEY: {
            "email": "rodrigo@owera.com",
            "workspace_slug": "owera",
            "actor": "rodrigo",
        }
    }


# #193-style: single delete_cookie(..., secure=True) — cannot evict non-Secure jar slot
bad = JSONResponse({"ok": True})
bad.delete_cookie("cm_session", path="/", secure=True, httponly=True, samesite="lax")
bad_lines = [v.decode("latin-1") for k, v in bad.raw_headers if k == b"set-cookie"]
ok(
    len([x for x in bad_lines if "cm_session" in x and "max-age=0" in x.replace(" ", "").lower()]) == 1,
    "#193-style logout: exactly one cm_session Max-Age=0 (Secure-only delete)",
)
ok(
    not any("secure" not in ln.lower() for ln in bad_lines if "cm_session" in ln),
    "#193-style: all cm_session deletes include Secure (none evict non-Secure jar slot)",
)

with patch.object(settings, "session_cookie_secure", True):
    good = spa_auth_mod._logout_response(
        _minimal_request_with_session(copy.deepcopy(_spa_session_dict()))
    )
good_lines = [v.decode("latin-1") for k, v in good.raw_headers if k == b"set-cookie"]
clears_good = [
    x for x in good_lines
    if "cm_session" in x and "max-age=0" in x.replace(" ", "").lower()
]
ok(len(clears_good) >= 2, "dual-delete: >=2 cm_session Max-Age=0 on JSONResponse")
ok(any("secure" in x.lower() for x in clears_good), "dual-delete includes Secure cm_session clear")
ok(any("secure" not in x.lower() for x in clears_good), "dual-delete includes non-Secure cm_session clear")

# --- Dual delete when MANAGER_SESSION_COOKIE_SECURE=1: both Secure and non-Secure eviction ---
# No logged-in session required: we only assert Set-Cookie shape (HTTP TestClient cannot
# hold Secure jar entries, but prod HTTPS browsers need both delete lines).
with patch.object(settings, "session_cookie_secure", True):
    dual_logout = client.post("/api/auth/logout")
ok(dual_logout.status_code == 200, "dual-delete probe: POST logout returns 200")
dual_headers = dual_logout.headers.get_list("set-cookie") or []
clears = [
    h for h in dual_headers
    if "cm_session=" in h and "max-age=0" in h.replace(" ", "").lower()
]
ok(len(clears) >= 2, "dual-delete: at least two cm_session Max-Age=0 Set-Cookie lines")
ok(any("secure" in h.lower() for h in clears), "dual-delete: one Set-Cookie includes Secure")
ok(any("secure" not in h.lower() for h in clears), "dual-delete: one Set-Cookie omits Secure")

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
