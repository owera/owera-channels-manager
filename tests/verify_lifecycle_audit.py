"""Regression checks for review-gate audit logging (reject/requeue/retry/approve).

Run: PYTHONPATH=. .venv/bin/python tests/verify_lifecycle_audit.py

The 2026-08-02 forensics concluded "every video lifecycle mutation writes a
JobRun" — that was overbroad: produce, delete and the loop transitions are
covered, but the four review-gate endpoints had ZERO jobrun rows ever
(verified 2026-08-03), so operator actions on those paths were only
reconstructable from the uvicorn access log. These checks pin that each of
the four transitions writes exactly one JobRun riding the same commit as the
status flip — kind = the transition, detail tagged 'via API' and carrying the
pre-transition status (and the reject reason / retry branch) — and that
refused calls (404s, approve's 409) write nothing.

A second channel exercises every log site cross-channel (a hardcoded
channel_id survives a single-channel fixture — adversarial-review finding),
retry details are pinned on the PRE-transition status ("failed", not the
target status the template always contains — same review), and each endpoint's
response is pinned as the video row (no suite POSTed these endpoints before,
so a swapped return shape was uncatchable).

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
    s.add(Video(channel_id=1, topic_id=1, subject="in review",
                status=VideoStatus.REVIEW))                       # id 1
    s.add(Video(channel_id=1, topic_id=1, subject="rendered, unreviewed",
                status=VideoStatus.RENDERED))                     # id 2
    s.add(Video(channel_id=1, topic_id=1, subject="still a draft",
                status=VideoStatus.DRAFT))                        # id 3
    s.add(Video(channel_id=1, topic_id=1, subject="weak hook",
                status=VideoStatus.REVIEW))                       # id 4
    s.add(Video(channel_id=1, topic_id=1, subject="render crashed",
                status=VideoStatus.FAILED, error="mpt exploded",
                mpt_task_id="t-dead", render_progress=40))        # id 5
    s.add(Video(channel_id=1, topic_id=1, subject="upload failed",
                status=VideoStatus.FAILED, error="upload 500",
                video_path="storage/videos/5/video.mp4"))         # id 6
    s.add(Video(channel_id=1, topic_id=1, subject="render failed too",
                status=VideoStatus.FAILED, error="mpt died"))     # id 7
    s.commit()
    s.add(Channel(slug="b", name="B", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=2, name="T2"))
    s.commit()
    s.add(Video(channel_id=2, topic_id=2, subject="ch2 in review",
                status=VideoStatus.REVIEW))                       # id 8
    s.add(Video(channel_id=2, topic_id=2, subject="ch2 render failed",
                status=VideoStatus.FAILED, error="mpt died"))     # id 9
    s.add(Video(channel_id=2, topic_id=2, subject="ch2 upload failed",
                status=VideoStatus.FAILED, error="upload 500",
                video_path="storage/videos/9/video.mp4"))         # id 10
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


def runs(s, kind):
    return s.exec(select(JobRun).where(JobRun.kind == kind)).all()


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

print("POST /api/videos/{id}/approve audit trail")
r = client.post("/api/videos/1/approve", auth=auth)
ok(r.status_code == 200, "approve from review returns 200")
ok(r.json().get("id") == 1 and r.json().get("status") == VideoStatus.APPROVED,
   "approve still returns the video row (response shape unchanged)")
with Session(engine) as s:
    ok(s.get(Video, 1).status == VideoStatus.APPROVED, "video 1 is approved")
    rr = runs(s, "approve")
    ok(len(rr) == 1, "exactly one approve JobRun written")
    ok(rr[0].status == "success", "approve JobRun status is success")
    ok(rr[0].video_id == 1 and rr[0].channel_id == 1,
       "approve JobRun carries video_id and channel_id")
    ok("via API" in rr[0].detail, "approve detail marks the API path")
    ok("review" in rr[0].detail, "approve detail records the pre-transition status")

r = client.post("/api/videos/2/approve", auth=auth)
ok(r.status_code == 200, "approve from rendered returns 200")
with Session(engine) as s:
    rr = runs(s, "approve")
    ok(len(rr) == 2, "second approve writes a second JobRun")
    latest = max(rr, key=lambda x: x.id)
    ok(latest.video_id == 2 and "rendered" in latest.detail,
       "detail carries the ACTUAL prior status (rendered), not a constant")

r = client.post("/api/videos/3/approve", auth=auth)
ok(r.status_code == 409, "approving a draft is refused with 409")
r = client.post("/api/videos/999/approve", auth=auth)
ok(r.status_code == 404, "approving a missing video is 404")
with Session(engine) as s:
    ok(len(runs(s, "approve")) == 2, "refused approves write no JobRun")
    ok(s.get(Video, 3).status == VideoStatus.DRAFT, "refused draft untouched")

print("POST /api/videos/{id}/reject audit trail")
r = client.post("/api/videos/4/reject", auth=auth, json={"reason": "hook buries the lede"})
ok(r.status_code == 200, "reject returns 200")
ok(r.json().get("id") == 4 and r.json().get("rejected_reason") == "hook buries the lede",
   "reject still returns the video row (response shape unchanged)")
with Session(engine) as s:
    v = s.get(Video, 4)
    ok(v.status == VideoStatus.REJECTED and v.rejected_reason == "hook buries the lede",
       "video 4 is rejected with the reason stored")
    rr = runs(s, "reject")
    ok(len(rr) == 1, "exactly one reject JobRun written")
    ok(rr[0].video_id == 4 and rr[0].channel_id == 1 and rr[0].status == "success",
       "reject JobRun carries ids and success status")
    ok("via API" in rr[0].detail, "reject detail marks the API path")
    ok("review" in rr[0].detail, "reject detail records the pre-transition status")
    ok("hook buries the lede" in rr[0].detail, "reject detail records the reason")

r = client.post("/api/videos/999/reject", auth=auth, json={"reason": "x"})
ok(r.status_code == 404, "rejecting a missing video is 404")
with Session(engine) as s:
    ok(len(runs(s, "reject")) == 1, "a refused reject writes no JobRun")

print("POST /api/videos/{id}/requeue audit trail")
r = client.post("/api/videos/5/requeue", auth=auth)
ok(r.status_code == 200, "requeue returns 200")
ok(r.json().get("id") == 5 and r.json().get("status") == VideoStatus.QUEUED,
   "requeue still returns the video row (response shape unchanged)")
with Session(engine) as s:
    v = s.get(Video, 5)
    ok(v.status == VideoStatus.QUEUED and v.error is None and v.mpt_task_id is None,
       "video 5 is queued with error/task cleared")
    rr = runs(s, "requeue")
    ok(len(rr) == 1, "exactly one requeue JobRun written")
    ok(rr[0].video_id == 5 and rr[0].channel_id == 1 and rr[0].status == "success",
       "requeue JobRun carries ids and success status")
    ok("via API" in rr[0].detail, "requeue detail marks the API path")
    ok("failed" in rr[0].detail, "requeue detail records the pre-transition status")

r = client.post("/api/videos/999/requeue", auth=auth)
ok(r.status_code == 404, "requeueing a missing video is 404")
with Session(engine) as s:
    ok(len(runs(s, "requeue")) == 1, "a refused requeue writes no JobRun")

print("POST /api/videos/{id}/retry audit trail (both branches)")
r = client.post("/api/videos/6/retry", auth=auth)
ok(r.status_code == 200, "retry with an artifact returns 200")
ok(r.json().get("id") == 6 and r.json().get("status") == VideoStatus.APPROVED,
   "retry still returns the video row (response shape unchanged)")
with Session(engine) as s:
    ok(s.get(Video, 6).status == VideoStatus.APPROVED,
       "video 6 (has video_path) goes back to approved for re-publish")
    rr = runs(s, "retry")
    ok(len(rr) == 1, "exactly one retry JobRun written")
    ok(rr[0].video_id == 6 and rr[0].channel_id == 1 and rr[0].status == "success",
       "retry JobRun carries ids and success status")
    ok("via API" in rr[0].detail, "retry detail marks the API path")
    ok("re-publish" in rr[0].detail and "approved" in rr[0].detail,
       "retry detail names the re-publish branch")
    ok("failed" in rr[0].detail,
       "re-publish detail records the PRE-transition status, not just the target")

r = client.post("/api/videos/7/retry", auth=auth)
ok(r.status_code == 200, "retry without an artifact returns 200")
with Session(engine) as s:
    ok(s.get(Video, 7).status == VideoStatus.QUEUED,
       "video 7 (no video_path) goes back to queued for re-render")
    rr = runs(s, "retry")
    ok(len(rr) == 2, "second retry writes a second JobRun")
    latest = max(rr, key=lambda x: x.id)
    ok("re-render" in latest.detail and "queued" in latest.detail,
       "retry detail names the re-render branch")
    ok("failed" in latest.detail,
       "re-render detail records the PRE-transition status, not just the target")

r = client.post("/api/videos/999/retry", auth=auth)
ok(r.status_code == 404, "retrying a missing video is 404")
with Session(engine) as s:
    ok(len(runs(s, "retry")) == 2, "a refused retry writes no JobRun")

print("cross-channel attribution (every log site once from channel 2)")
r = client.post("/api/videos/8/approve", auth=auth)
ok(r.status_code == 200, "ch2 approve returns 200")
r = client.post("/api/videos/8/reject", auth=auth, json={"reason": "ch2 probe"})
ok(r.status_code == 200, "ch2 reject returns 200")
r = client.post("/api/videos/9/requeue", auth=auth)
ok(r.status_code == 200, "ch2 requeue returns 200")
r = client.post("/api/videos/9/retry", auth=auth)
ok(r.status_code == 200, "ch2 retry (re-render) returns 200")
r = client.post("/api/videos/10/retry", auth=auth)
ok(r.status_code == 200, "ch2 retry (re-publish) returns 200")
with Session(engine) as s:
    latest = {k: max(runs(s, k), key=lambda x: x.id) for k in
              ("approve", "reject", "requeue")}
    ok(latest["approve"].video_id == 8 and latest["approve"].channel_id == 2,
       "ch2 approve row carries channel_id 2 (not a hardcoded 1)")
    ok(latest["reject"].video_id == 8 and latest["reject"].channel_id == 2,
       "ch2 reject row carries channel_id 2")
    ok("approved" in latest["reject"].detail,
       "ch2 reject detail shows the approved -> rejected transition")
    ok(latest["requeue"].video_id == 9 and latest["requeue"].channel_id == 2,
       "ch2 requeue row carries channel_id 2")
    retries = sorted(runs(s, "retry"), key=lambda x: x.id)[-2:]
    ok([x.video_id for x in retries] == [9, 10]
       and all(x.channel_id == 2 for x in retries),
       "both ch2 retry rows (re-render site AND re-publish site) carry channel_id 2")
    counts = {k: len(runs(s, k)) for k in ("approve", "reject", "requeue", "retry")}
    ok(counts == {"approve": 3, "reject": 2, "requeue": 2, "retry": 4},
       f"final per-kind JobRun counts exact (no double writes): {counts}")

main.app.dependency_overrides.clear()
settings.app_password = _orig_pw
print(f"all {_checks} checks passed")
