"""WS4 PR-G: per-workspace YouTube quota cap."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, JobRun, Workspace
from app.services import quota

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
now = datetime.now(timezone.utc)

with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera", daily_youtube_quota_cap=4000))
    s.add(Channel(id=1, workspace_id=1, slug="a", name="A"))
    s.add(Channel(id=2, workspace_id=1, slug="b", name="B"))
    s.commit()

with Session(engine) as s:
    ok(quota.youtube_quota_cap_for_workspace(s, 1) == 4000, "workspace cap from row")
    quota.log(s, kind="publish", status="success", channel_id=1, quota_cost=1000)
    quota.log(s, kind="publish", status="success", channel_id=2, quota_cost=500)
    s.commit()
    ok(quota.quota_spent_workspace_today(s, 1) == 1500, "workspace spend sums channels")
    ok(quota.workspace_quota_would_exceed(s, 1, 2500) is False, "2500 more fits in 4000")
    ok(quota.workspace_quota_would_exceed(s, 1, 2501) is True, "2501 more exceeds 4000")

print(f"\nALL {_checks} CHECKS PASSED")
