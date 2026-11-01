"""WS4 PR-C: Workspace model + channel isolation."""
from __future__ import annotations

import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Channel, Workspace
from app.routers import channels as channels_router

_checks = 0


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
    s.add(Workspace(slug="tenant-b", name="Tenant B"))
    s.commit()
    ws_b = s.exec(__import__("sqlmodel").select(Workspace).where(Workspace.slug == "tenant-b")).one()
    s.add(Channel(workspace_id=1, slug="a-ch", name="A"))
    s.add(Channel(workspace_id=ws_b.id, slug="b-ch", name="B"))
    s.commit()
    ch_b = s.exec(__import__("sqlmodel").select(Channel).where(Channel.slug == "b-ch")).one()

settings.multi_workspace = True
settings.allow_workspace_header = True

app = FastAPI()
app.include_router(channels_router.router)


def _sess():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _sess
client = TestClient(app)

lst = client.get("/api/channels", headers={"X-Workspace-Slug": "owera"}).json()
ok(len(lst) == 1 and lst[0]["slug"] == "a-ch", "default workspace lists its channel")
miss = client.get(f"/api/channels/{ch_b.id}", headers={"X-Workspace-Slug": "owera"})
ok(miss.status_code == 404, "cross-workspace channel GET is 404")

print(f"\nALL {_checks} CHECKS PASSED")
