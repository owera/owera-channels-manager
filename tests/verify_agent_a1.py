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
from app.models import Channel, JobRun, Topic, Video, VideoStatus, Workspace
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
    s.add(Workspace(slug="tenant-c", name="Tenant C"))
    s.commit()
    ws_b = s.exec(__import__("sqlmodel").select(Workspace).where(Workspace.slug == "tenant-b")).one()
    s.add(Channel(id=1, workspace_id=1, slug="ow-ch", name="Owera CH"))
    s.add(Channel(id=2, workspace_id=ws_b.id, slug="tb-ch", name="Tenant B CH"))
    s.add(Topic(id=1, channel_id=1, name="T1", position=1))
    s.add(Topic(id=2, channel_id=2, name="T2", position=1))
    s.add(Video(id=10, channel_id=1, topic_id=1, subject="ow video", status=VideoStatus.DRAFT, position=1))
    s.add(Video(id=20, channel_id=2, topic_id=2, subject="tb video", status=VideoStatus.DRAFT, position=1))
    s.add(JobRun(channel_id=1, video_id=10, kind="delete", status="success",
                 detail="SECRET-OWERA"))
    s.add(JobRun(channel_id=2, video_id=20, kind="delete", status="success",
                 detail="SECRET-TB"))
    s.add(JobRun(channel_id=None, video_id=None, kind="music_gen", status="success",
                 detail="GLOBAL-POOL"))
    # Render errors sometimes omit channel_id and keep only video_id.
    # That row still belongs to the video's workspace.
    s.add(JobRun(channel_id=None, video_id=10, kind="render", status="error",
                 detail="SECRET-OWERA-NULLCH"))
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


def _details(resp):
    return {row["detail"] for row in resp.json()}


no_auth_runs = client.get("/api/runs")
ok(no_auth_runs.status_code == 401, "multi_workspace: GET /runs without identity → 401")

runs_b = client.get("/api/runs", headers=auth_b)
ok(runs_b.status_code == 200, "tenant-b GET /runs is 200")
ok(_details(runs_b) == {"SECRET-TB", "GLOBAL-POOL"},
   "tenant-b unfiltered /runs is own channel + global pool, not owera")
runs_ow = client.get("/api/runs", headers=auth_ow)
ok(_details(runs_ow) == {"SECRET-OWERA", "SECRET-OWERA-NULLCH", "GLOBAL-POOL"},
   "owera unfiltered /runs keeps its null-channel video row and hides tenant-b")

own_ch = client.get("/api/runs?channel_id=2", headers=auth_b)
ok(own_ch.status_code == 200 and _details(own_ch) == {"SECRET-TB"},
   "channel filter is that channel only, not the global pool")
foreign_ch = client.get("/api/runs?channel_id=1", headers=auth_b)
missing_ch = client.get("/api/runs?channel_id=99999", headers=auth_b)
ok(foreign_ch.status_code == 404 and missing_ch.status_code == 404,
   "foreign and missing channel_id are both 404")
ok(foreign_ch.json()["detail"] == missing_ch.json()["detail"] == "channel not found",
   "foreign channel_id does not differ from a missing id")

own_vid = client.get("/api/runs?video_id=10", headers=auth_ow)
ok(own_vid.status_code == 200
   and _details(own_vid) == {"SECRET-OWERA", "SECRET-OWERA-NULLCH"},
   "video filter returns the channel row and the null-channel row for that video")
foreign_vid = client.get("/api/runs?video_id=10", headers=auth_b)
missing_vid = client.get("/api/runs?video_id=99999", headers=auth_b)
ok(foreign_vid.status_code == 404 and missing_vid.status_code == 404,
   "foreign and missing video_id are both 404")
ok(foreign_vid.json()["detail"] == missing_vid.json()["detail"] == "video not found",
   "foreign video_id does not differ from a missing id")
mix = client.get("/api/runs?channel_id=2&video_id=10", headers=auth_b)
ok(mix.status_code == 404, "own channel + foreign video is 404, not the channel's rows")

state_runs = {r["detail"] for r in state_b["recent_runs"]}
ok(state_runs == {"SECRET-TB", "GLOBAL-POOL"},
   "agent/state recent_runs uses the same scope as /runs")
state_ow = client.get("/api/agent/state", headers=auth_ow).json()
ow_runs = {r["detail"] for r in state_ow["recent_runs"]}
ok("SECRET-OWERA-NULLCH" in ow_runs and "SECRET-TB" not in ow_runs,
   "agent/state keeps owera's null-channel video row and hides tenant-b")

auth_c = {"Authorization": "Basic " + base64.b64encode(b"tenant-c:secret").decode()}
empty_ws = client.get("/api/runs", headers=auth_c)
ok(empty_ws.status_code == 200 and _details(empty_ws) == {"GLOBAL-POOL"},
   "workspace with no channels sees only global rows")

settings.multi_workspace = False
try:
    legacy_runs = client.get("/api/runs")
    ok(legacy_runs.status_code == 200, "multi_workspace off: /runs stays open")
    ok(_details(legacy_runs) == {"SECRET-OWERA", "SECRET-OWERA-NULLCH", "GLOBAL-POOL"},
       "multi_workspace off still resolves owera, not every workspace")
finally:
    settings.multi_workspace = True

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
