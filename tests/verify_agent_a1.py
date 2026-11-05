"""A1: compound Basic Auth + workspace-scoped agent/dashboard/videos."""
from __future__ import annotations

import base64
import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Channel, Topic, Video, VideoStatus, Workspace
from app.routers import queue, videos
from app.services import review_guard
from app.workspace import parse_basic_auth_username, resolve_workspace_slug
from starlette.requests import Request

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def _req(user: str) -> Request:
    cred = base64.b64encode(f"{user}:secret".encode()).decode()
    scope = {"type": "http", "headers": [(b"authorization", f"Basic {cred}".encode())]}
    return Request(scope)


# --- username parsing ---
ws, actor = parse_basic_auth_username("tenant-b")
ok(ws == "tenant-b" and actor == "tenant-b", "slug-only → workspace and actor = slug")
ws, actor = parse_basic_auth_username("tenant-b+channels")
ok(ws == "tenant-b" and actor == "channels", "tenant-b+channels → WS B, actor channels")
ws, actor = parse_basic_auth_username("tenant-b+video-maker")
ok(ws == "tenant-b" and actor == "vm", "tenant-b+video-maker → actor vm")
ws, actor = parse_basic_auth_username("channels")
ok(ws == "owera" and actor == "channels", "legacy channels → owera")
ws, actor = parse_basic_auth_username("admin")
ok(ws == "owera" and actor == "admin", "admin → owera workspace")

settings.multi_workspace = True
settings.allow_workspace_header = False
ok(resolve_workspace_slug(_req("tenant-b+channels")) == "tenant-b", "resolve slug from compound user")

scope = {"type": "http", "headers": [(b"authorization", b"Basic " + base64.b64encode(b"tenant-b+channels:secret"))]}
ok(review_guard.actor_of(Request(scope)) == "channels", "actor_of sees channels from compound user")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(slug="tenant-b", name="Tenant B"))
    s.commit()
    ws_b = s.exec(__import__("sqlmodel").select(Workspace).where(Workspace.slug == "tenant-b")).one()
    s.add(Channel(id=1, workspace_id=1, slug="ow-ch", name="Owera CH"))
    s.add(Channel(id=2, workspace_id=ws_b.id, slug="tb-ch", name="Tenant B CH"))
    s.add(Topic(id=1, channel_id=1, name="T1", position=1))
    s.add(Topic(id=2, channel_id=2, name="T2", position=1))
    s.add(Video(id=10, channel_id=1, topic_id=1, subject="ow video", status=VideoStatus.DRAFT, position=1))
    s.add(Video(id=20, channel_id=2, topic_id=2, subject="tb video", status=VideoStatus.DRAFT, position=1))
    s.commit()

app = FastAPI()
app.include_router(queue.router)
app.include_router(videos.router)


def _sess():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _sess
client = TestClient(app)
auth_b = {"Authorization": "Basic " + base64.b64encode(b"tenant-b:secret").decode()}
auth_b_ch = {"Authorization": "Basic " + base64.b64encode(b"tenant-b+channels:secret").decode()}
auth_ow = {"Authorization": "Basic " + base64.b64encode(b"owera:secret").decode()}
auth_legacy_ch = {"Authorization": "Basic " + base64.b64encode(b"channels:secret").decode()}

dash_b = client.get("/api/dashboard", headers=auth_b).json()
ok(len(dash_b) == 1 and dash_b[0]["channel"]["slug"] == "tb-ch", "tenant-b dashboard only TB channel")
dash_ow = client.get("/api/dashboard", headers=auth_ow).json()
ok(len(dash_ow) == 1 and dash_ow[0]["channel"]["slug"] == "ow-ch", "owera dashboard only owera channel")

state_b = client.get("/api/agent/state", headers=auth_b).json()
ok(len(state_b["dashboard"]) == 1, "agent/state scoped to tenant-b")
issues_b = client.get("/api/agent/issues", headers=auth_b).json()
ok(isinstance(issues_b, dict), "agent/issues returns digest for tenant-b")

lst_b = client.get("/api/videos", headers=auth_b).json()
ok(len(lst_b) == 1 and lst_b[0]["id"] == 20, "tenant-b video list excludes owera")
miss = client.get("/api/videos/10", headers=auth_b)
ok(miss.status_code == 404, "tenant-b GET owera video id → 404")

# Cross-tenant approve: tenant-b+channels cannot touch owera video (404)
cross = client.post("/api/videos/10/approve", headers=auth_b_ch, json={})
ok(cross.status_code == 404, "tenant-b+channels approve owera video → 404")
in_ws = client.post("/api/videos/20/approve", headers=auth_b_ch, json={})
ok(in_ws.status_code != 404, "tenant-b+channels reaches tenant-b video (not 404)")
legacy = client.post("/api/videos/10/approve", headers=auth_legacy_ch, json={})
ok(legacy.status_code != 404, "legacy channels reaches owera video (not 404)")

# OpenAPI behind Basic (full app)
from app.main import app as main_app

_saved_pw = settings.app_password
settings.app_password = "secret"
try:
    oc = TestClient(main_app)
    no_auth = oc.get("/openapi.json")
    ok(no_auth.status_code == 401, "openapi.json without Basic → 401")
    with_auth = oc.get("/openapi.json", headers={"Authorization": "Basic " + base64.b64encode(b"owera:secret").decode()})
    ok(with_auth.status_code == 200 and "openapi" in with_auth.json(), "openapi.json 200 with Basic")
finally:
    settings.app_password = _saved_pw

print(f"\nALL {_checks} CHECKS PASSED")
