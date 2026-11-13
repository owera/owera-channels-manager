"""P1 (PO CM cut C2): MANAGER_YOUTUBE_SUBSCRIPTIONS_ADMIN, default OFF, global.

Flag off: GET/POST/DELETE /subscriptions and GET /subscribers return 404 before
any YouTube call (no quota). Flag on: the handlers run as before. /api/features
exposes the flag so the SPA hides SubscriptionsSection.

Run: PYTHONPATH=. uv run python tests/verify_subscriptions_flag.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.pop("MANAGER_YOUTUBE_SUBSCRIPTIONS_ADMIN", None)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings, settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Workspace
from app.routers import features as features_router
from app.routers import youtube_admin

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Settings.model_fields["youtube_subscriptions_admin"].default is False,
   "default is OFF when MANAGER_YOUTUBE_SUBSCRIPTIONS_ADMIN is unset")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(id=1, workspace_id=1, slug="owera-software", name="Owera Software",
                  oauth_status=OAuthStatus.CONNECTED))
    s.commit()

app = FastAPI()
app.include_router(youtube_admin.router)
app.include_router(features_router.router)


def _sess():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _sess
client = TestClient(app)

CALLS = []


def _svc(*a, **k):
    CALLS.append("get_service")
    return object()


REQS = [
    ("get", "/api/channels/1/subscriptions", None),
    ("post", "/api/channels/1/subscriptions", {"channel": "@someone"}),
    ("delete", "/api/channels/1/subscriptions/sub123", None),
    ("get", "/api/channels/1/subscribers", None),
]


def call(method, url, body):
    return getattr(client, method)(url, json=body) if body else getattr(client, method)(url)


# --- flag OFF (default), including the owera workspace ---
settings.youtube_subscriptions_admin = False
with patch("app.services.youtube.get_service", side_effect=_svc):
    for m, u, b in REQS:
        r = call(m, u, b)
        ok(r.status_code == 404, f"OFF: {m.upper()} {u.split('/1/')[1]} → 404 (owera workspace too)")
ok(CALLS == [], "OFF: no YouTube client built, no quota spent")
ok(client.get("/api/features").json().get("youtube_subscriptions_admin") is False,
   "OFF: /api/features reports youtube_subscriptions_admin=false")
with patch("app.services.youtube.get_service", side_effect=_svc), \
        patch("app.services.youtube.fetch_channel", return_value={"id": "UC1"}):
    ok(client.get("/api/channels/1/youtube").status_code == 200, "OFF: channel overview still works")

# --- flag ON ---
settings.youtube_subscriptions_admin = True
with patch("app.services.youtube.get_service", side_effect=_svc), \
        patch("app.services.youtube.list_subscriptions", return_value=[{"sub_id": "s1"}]), \
        patch("app.services.youtube.list_subscribers", return_value=[{"title": "x"}]), \
        patch("app.services.youtube.resolve_channel_id", return_value="UCx"), \
        patch("app.services.youtube.subscribe", return_value={"sub_id": "s2"}), \
        patch("app.services.youtube.unsubscribe", return_value=None):
    codes = [call(m, u, b).status_code for m, u, b in REQS]
ok(codes == [200, 201, 204, 200], f"ON: the four endpoints work as before ({codes})")
ok(client.get("/api/features").json().get("youtube_subscriptions_admin") is True,
   "ON: /api/features reports youtube_subscriptions_admin=true")
settings.youtube_subscriptions_admin = False

# --- SPA hides the section unless the flag is on (static check) ---
src = (Path(__file__).resolve().parent.parent / "frontend/src/pages/ChannelYoutube.tsx").read_text()
ok("{showSubscriptions && <Subscriptions" in src and "youtube_subscriptions_admin === true" in src,
   "SPA renders SubscriptionsSection only when the flag is true")
ok("<Subscriptions channel={channel} />\n" not in src.replace("{showSubscriptions && <Subscriptions channel={channel} />}", ""),
   "no unconditional SubscriptionsSection left")
ok("def list_subscribers" in (Path(__file__).resolve().parent.parent / "app/services/youtube.py").read_text(),
   "code kept (not deleted)")

print(f"\nALL {_checks} CHECKS PASSED")
