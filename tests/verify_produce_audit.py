"""Regression checks for produce-endpoint audit logging (single + bulk).

Run: PYTHONPATH=. .venv/bin/python tests/verify_produce_audit.py

Draft->queued via the API was the last unaudited lifecycle transition: on
2026-08-01 an operator bulk-produce queued 20 drafts across both channels
(including five from a weight-0 parked topic) and the only forensic source was
the uvicorn access log — no JobRun, no /api/runs row, nothing attributable.
These checks pin that both produce endpoints now write a JobRun per queued
video, with a detail string that distinguishes operator/API queueing from the
scheduler's auto-produce rows, and that refused or no-op calls write nothing.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no network,
and the app lifespan/scheduler are never started). Exits non-zero on the first
failure.
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="T"))
    s.commit()
    s.add(Video(channel_id=1, topic_id=1, subject="draft one",
                status=VideoStatus.DRAFT))          # id 1
    s.add(Video(channel_id=1, topic_id=1, subject="draft two",
                status=VideoStatus.DRAFT))          # id 2
    s.add(Video(channel_id=1, topic_id=1, subject="already queued",
                status=VideoStatus.QUEUED))         # id 3
    s.add(Video(channel_id=1, topic_id=1, subject="draft three",
                status=VideoStatus.DRAFT))          # id 4
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


def produce_runs(s):
    return s.exec(select(JobRun).where(JobRun.kind == "produce")).all()


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

print("POST /api/videos/{id}/produce audit trail")
r = client.post("/api/videos/1/produce", auth=auth)
ok(r.status_code == 200, "single produce returns 200")
with Session(engine) as s:
    ok(s.get(Video, 1).status == VideoStatus.QUEUED, "video 1 is queued")
    runs = produce_runs(s)
    ok(len(runs) == 1, "exactly one produce JobRun written")
    run = runs[0]
    ok(run.status == "success", "JobRun status is success")
    ok(run.video_id == 1 and run.channel_id == 1,
       "JobRun carries video_id and channel_id")
    ok("via API" in run.detail, "detail marks the API path (not auto-produce)")

r = client.post("/api/videos/1/produce", auth=auth)
ok(r.status_code == 409, "producing a non-draft is refused with 409")
with Session(engine) as s:
    ok(len(produce_runs(s)) == 1, "a refused produce writes no JobRun")

print("POST /api/videos/produce (bulk) audit trail")
r = client.post("/api/videos/produce", auth=auth,
                json={"channel_id": 1, "ordered_ids": [2, 3, 4, 999]})
ok(r.status_code == 200 and r.json()["produced"] == 2,
   "bulk produce queues only the two drafts (skips queued + missing)")
with Session(engine) as s:
    ok(s.get(Video, 2).status == VideoStatus.QUEUED
       and s.get(Video, 4).status == VideoStatus.QUEUED,
       "both drafts are queued")
    bulk = [x for x in produce_runs(s) if "bulk" in x.detail]
    ok(len(bulk) == 2, "exactly two bulk-produce JobRuns written")
    ok({x.video_id for x in bulk} == {2, 4},
       "bulk JobRuns name the produced video ids")
    ok(all(x.channel_id == 1 and x.status == "success" for x in bulk),
       "bulk JobRuns carry channel_id and success status")

r = client.post("/api/videos/produce", auth=auth,
                json={"channel_id": 1, "ordered_ids": [3, 999]})
ok(r.status_code == 200 and r.json()["produced"] == 0,
   "no-op bulk produce reports 0")
with Session(engine) as s:
    ok(len(produce_runs(s)) == 3, "a no-op bulk produce writes no JobRun")

main.app.dependency_overrides.clear()
settings.app_password = _orig_pw
print(f"all {_checks} checks passed")
