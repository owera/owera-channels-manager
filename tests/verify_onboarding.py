"""WS3 onboarding checklist."""
from __future__ import annotations

import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Workspace
from app.routers import onboarding as onboarding_router

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
    s.commit()

settings.multi_workspace = False
app = FastAPI()
app.include_router(onboarding_router.router)


def _sess():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _sess
client = TestClient(app)

r = client.get("/api/onboarding/status")
ok(r.status_code == 200, "status 200")
body = r.json()
ok(body["onboarding_complete"] is False, "incomplete with no channel")
ok(len(body["steps"]) >= 5, "checklist steps present")

with Session(engine) as s:
    s.add(Channel(workspace_id=1, slug="c1", name="C1", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=6, daily_publish_budget=6))
    s.commit()

r2 = client.get("/api/onboarding/status")
steps = {s["id"]: s["done"] for s in r2.json()["steps"]}
ok(steps.get("channel") and steps.get("youtube") and steps.get("budgets"), "channel+youtube+budgets done")

print(f"\nALL {_checks} CHECKS PASSED")
