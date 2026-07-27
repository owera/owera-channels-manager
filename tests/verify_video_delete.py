"""Regression checks for DELETE /api/videos/{id} audit logging.

Run: PYTHONPATH=. .venv/bin/python tests/verify_video_delete.py

Deletion is the one lifecycle exit that leaves no other trace (2026-07-25/26: an
operator mass-deleted ~35 videos via the UI and the only forensic source was the
uvicorn access log). These checks pin that every delete now writes a JobRun with
enough detail to reconstruct what was removed, and that a no-op delete does not.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no network, and
the app lifespan/scheduler are never started). Exits non-zero on the first failure.
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
    s.add(Video(channel_id=1, topic_id=1, subject="doomed video",
                status=VideoStatus.REJECTED, yt_video_id="ytx123"))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)

print("DELETE /api/videos/{id} audit trail")
r = client.delete("/api/videos/1", auth=("x", "testpw"))
ok(r.status_code == 204, "delete returns 204")
with Session(engine) as s:
    ok(s.get(Video, 1) is None, "video row is gone")
    runs = s.exec(select(JobRun).where(JobRun.kind == "delete")).all()
    ok(len(runs) == 1, "exactly one delete JobRun written")
    run = runs[0]
    ok(run.status == "success", "delete JobRun status is success")
    ok(run.video_id == 1 and run.channel_id == 1,
       "delete JobRun carries video_id and channel_id")
    ok("rejected" in run.detail and "doomed video" in run.detail
       and "ytx123" in run.detail,
       "detail records the pre-delete status, subject, and yt id")

r2 = client.delete("/api/videos/999", auth=("x", "testpw"))
ok(r2.status_code == 204, "deleting a missing id is still a 204 no-op")
with Session(engine) as s:
    n = len(s.exec(select(JobRun).where(JobRun.kind == "delete")).all())
    ok(n == 1, "a no-op delete writes no JobRun")

main.app.dependency_overrides.clear()
settings.app_password = _orig_pw

print(f"\nALL {_checks} CHECKS PASSED")
