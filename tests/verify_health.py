"""Regression checks for the /health endpoint and its auth exemption.

Run: PYTHONPATH=. .venv/bin/python tests/verify_health.py

Covers the aggregate health snapshot (degraded logic + aggregate-only payload) and
that /health is reachable WITHOUT auth while the rest of the API still requires it —
so the middleware exemption can't silently widen to leak authed routes.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no network, and the
app lifespan/scheduler are never started). Exits non-zero on the first failed assertion.
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.main import _health_snapshot
from app.models import Channel, OAuthStatus, Video, VideoStatus

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def make_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


def seeded_engine():
    """3 channels (connected / expired / disconnected-but-paused) + 1 failed video."""
    engine = make_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
        s.add(Channel(slug="b", name="B", oauth_status=OAuthStatus.EXPIRED))
        s.add(Channel(slug="c", name="C", oauth_status=OAuthStatus.DISCONNECTED, paused=True))
        s.commit()
        s.add(Video(channel_id=1, topic_id=1, subject="x", status=VideoStatus.FAILED))
        s.commit()
    return engine


# --- unit: _health_snapshot --------------------------------------------------
print("_health_snapshot")
with Session(seeded_engine()) as s:
    snap = _health_snapshot(s)
ok(snap["status"] == "degraded", "degraded when a channel is expired or a video failed")
ok(snap["channels_total"] == 3, "counts all channels")
ok(snap["channels_connected"] == 1, "counts connected channels")
ok(snap["channels_paused"] == 1, "counts paused channels")
ok(snap["channels_needing_attention"] == 1,
   "expired-and-not-paused needs attention; a paused channel does not")
ok(snap["videos_failed"] == 1, "counts failed videos")
ok(set(snap.keys()) == {"status", "channels_total", "channels_connected",
                        "channels_paused", "channels_needing_attention", "videos_failed"},
   "payload is aggregate-only (no names, slugs, ids, or tokens)")

# healthy: all connected, nothing failed -> ok
h_engine = make_engine()
SQLModel.metadata.create_all(h_engine)
with Session(h_engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
with Session(h_engine) as s:
    ok(_health_snapshot(s)["status"] == "ok",
       "status ok when all channels are connected and nothing failed")

# --- endpoint + auth exemption (TestClient) ----------------------------------
print("/health endpoint + auth exemption")
engine = seeded_engine()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"          # turn auth ON so the exemption is meaningful
client = TestClient(main.app)

r = client.get("/health")
ok(r.status_code == 200, "/health is reachable without auth")
ok(r.json()["status"] == "degraded", "/health returns the snapshot payload")

r2 = client.get("/api/channels")
ok(r2.status_code == 401, "other API routes still require auth (exemption did not widen)")

r3 = client.get("/api/channels", auth=("x", "testpw"))
ok(r3.status_code == 200, "an authenticated API request still passes")

main.app.dependency_overrides.clear()
settings.app_password = _orig_pw

print(f"\nALL {_checks} CHECKS PASSED")
