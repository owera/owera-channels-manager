"""Regression checks for PATCH /api/videos/{id}/craft (#1257).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_craft_persist.py

Root cause: ``VideoUpdate`` omits ``script`` / ``creation_config``, so
``PATCH /api/videos/{id}`` silently drops them (exclude_unset never
sees the keys). Correcting spoken $N / hook beats on an approved
video used to require ``POST …/requeue``, which burns a render-budget
slot. This suite pins the narrow craft-persist endpoint:

- persists script and/or creation_config on approved|review|rendered
- leaves status / video_path / mpt_task_id / render_progress untouched
- rejects draft/queued/published (and empty body / blank script)
- wide PATCH still ignores script/creation_config (VideoUpdate pin)

Uses an in-memory SQLite DB and FastAPI's TestClient. Exits non-zero
on the first failed assertion.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.routers import videos as videos_router
from app.schemas import VideoCraftPersist, VideoUpdate

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(videos_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "videos module loaded from this tree")

# Pin on model fields (not getsource): VideoUpdate docstring/comments may say "script".
ok("script" not in VideoUpdate.model_fields and "creation_config" not in VideoUpdate.model_fields,
   "VideoUpdate still omits script/creation_config (wide PATCH must not rewrite craft)")
ok("script" in VideoCraftPersist.model_fields and "creation_config" in VideoCraftPersist.model_fields,
   "VideoCraftPersist carries script + creation_config")

_vid_src = Path(videos_router.__file__).read_text()
ok('@router.patch("/{video_id}/craft")' in _vid_src, "PATCH /{id}/craft route exists")
ok("def persist_craft" in _vid_src, "persist_craft handler exists")
ok("kind=\"craft_persist\"" in _vid_src or "kind='craft_persist'" in _vid_src,
   "craft persist writes a JobRun audit row")
# Must not call requeue / flip to QUEUED inside persist_craft.
_pc = _vid_src.find("def persist_craft")
_next = _vid_src.find("\n@router.", _pc + 1)
_body = _vid_src[_pc:_next]
ok("VideoStatus.QUEUED" not in _body, "persist_craft never sets QUEUED")
ok("requeue" not in _body.lower() or "re-render" in _body.lower(),
   "persist_craft does not call requeue (hint text about requeue is ok)")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

_SEED_CC = {"beats": [{"type": "hook", "spoken": "It costs fifty three dollars."}],
            "theme": "credits"}
_SEED_SCRIPT = "It costs fifty three dollars. Here is why Credits matter."

with Session(engine) as s:
    s.add(Channel(slug="ch-a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.add(Channel(slug="ch-b", name="B", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="T-a", content_format="short"))
    s.add(Topic(channel_id=2, name="T-b", content_format="short"))
    s.commit()
    # id=1 approved with artifact — the #1257 target
    s.add(Video(
        channel_id=1, topic_id=1, subject="credits-53",
        status=VideoStatus.APPROVED, title="Credits cost $53",
        script=_SEED_SCRIPT,
        creation_config=json.dumps(_SEED_CC),
        video_path="/tmp/v1.mp4", thumb_path="/tmp/v1.jpg",
        mpt_task_id="task-keep", render_progress=100,
    ))
    # id=2 sibling approved — must stay untouched
    s.add(Video(
        channel_id=2, topic_id=2, subject="sibling",
        status=VideoStatus.APPROVED, title="Sibling",
        script="sibling script",
        creation_config=json.dumps({"beats": []}),
        video_path="/tmp/v2.mp4", mpt_task_id="task-sib", render_progress=100,
    ))
    # id=3 draft — craft persist must 409
    s.add(Video(
        channel_id=1, topic_id=1, subject="draft-idea",
        status=VideoStatus.DRAFT,
    ))
    # id=4 review — allowed
    s.add(Video(
        channel_id=1, topic_id=1, subject="in-review",
        status=VideoStatus.REVIEW, title="Review me",
        script="old review script",
        creation_config=json.dumps({"beats": [{"type": "hook", "spoken": "old"}]}),
        video_path="/tmp/v4.mp4", mpt_task_id="task-rev", render_progress=100,
    ))
    # id=5 queued — 409
    s.add(Video(
        channel_id=1, topic_id=1, subject="queued",
        status=VideoStatus.QUEUED,
    ))
    # id=6 published — 409
    s.add(Video(
        channel_id=1, topic_id=1, subject="published",
        status=VideoStatus.PUBLISHED, video_path="/tmp/v6.mp4",
        yt_video_id="yt123",
    ))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")


def row(vid: int = 1) -> Video:
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


def craft_snap(vid: int = 1):
    v = row(vid)
    return {
        "script": v.script,
        "creation_config": v.creation_config,
        "status": v.status,
        "video_path": v.video_path,
        "mpt_task_id": v.mpt_task_id,
        "render_progress": v.render_progress,
        "title": v.title,
        "subject": v.subject,
    }


def patch_craft(vid: int = 1, **body):
    return client.patch(f"/api/videos/{vid}/craft", auth=auth, json=body)


def patch_wide(vid: int = 1, **body):
    return client.patch(f"/api/videos/{vid}", auth=auth, json=body)


def jobruns(kind: str = "craft_persist") -> list[JobRun]:
    with Session(engine) as s:
        rows = list(s.exec(select(JobRun).where(JobRun.kind == kind)).all())
        for r in rows:
            s.expunge(r)
        return rows


try:
    print("PATCH /api/videos still strips script/creation_config (root cause)")
    before = craft_snap(1)
    r = patch_wide(1, script="WIDE SHOULD IGNORE",
                   creation_config={"beats": [{"type": "hook", "spoken": "$53"}]})
    ok(r.status_code == 200, "wide PATCH with craft fields is still 200 (fields stripped)")
    after = craft_snap(1)
    ok(after["script"] == before["script"],
       "wide PATCH left script (VideoUpdate omitted the key)")
    ok(after["creation_config"] == before["creation_config"],
       "wide PATCH left creation_config")
    ok(after["status"] == VideoStatus.APPROVED, "wide PATCH left approved status")

    print("PATCH /api/videos/{id}/craft: persist script on approved")
    before_sib = craft_snap(2)
    new_script = "It costs $53. Here is why Credits matter."
    r = patch_craft(1, script=new_script)
    ok(r.status_code == 200, "craft persist script is 200")
    ok(r.json().get("script") == new_script, "response script is the new value")
    ok(craft_snap(1)["script"] == new_script, "script persisted")
    ok(craft_snap(1)["status"] == VideoStatus.APPROVED, "status stayed approved")
    ok(craft_snap(1)["video_path"] == "/tmp/v1.mp4", "video_path untouched")
    ok(craft_snap(1)["mpt_task_id"] == "task-keep", "mpt_task_id untouched")
    ok(craft_snap(1)["render_progress"] == 100, "render_progress untouched")
    ok(craft_snap(1)["creation_config"] == before["creation_config"],
       "script-only persist left creation_config")
    ok(craft_snap(2) == before_sib, "sibling video 2 untouched")
    ok(len(jobruns()) >= 1, "craft_persist JobRun written")
    ok("no requeue" in (jobruns()[-1].detail or ""),
       "JobRun detail says no requeue")

    print("PATCH /api/videos/{id}/craft: persist creation_config (beats / $N)")
    new_cc = {"beats": [{"type": "hook", "spoken": "It costs $53."}],
              "theme": "credits"}
    r = patch_craft(1, creation_config=new_cc)
    ok(r.status_code == 200, "craft persist creation_config dict is 200")
    stored = craft_snap(1)["creation_config"]
    ok(json.loads(stored) == new_cc, "creation_config persisted as JSON object")
    ok(craft_snap(1)["script"] == new_script, "cc-only persist left script")
    ok(craft_snap(1)["status"] == VideoStatus.APPROVED, "status still approved after cc")
    ok(craft_snap(1)["video_path"] == "/tmp/v1.mp4", "video_path still untouched")

    r = patch_craft(1, creation_config=json.dumps(
        {"beats": [{"type": "hook", "spoken": "It costs $53 dollars."}]}))
    ok(r.status_code == 200, "craft persist creation_config JSON string is 200")
    ok("$53 dollars" in craft_snap(1)["creation_config"],
       "JSON-string creation_config re-serialized into the column")

    print("PATCH /api/videos/{id}/craft: both fields + strip")
    r = patch_craft(1, script="  padded $53 script  ",
                    creation_config={"beats": [{"type": "hook", "spoken": "$53"}]})
    ok(r.status_code == 200, "both-fields persist is 200")
    ok(craft_snap(1)["script"] == "padded $53 script", "script stripped")
    ok(json.loads(craft_snap(1)["creation_config"])["beats"][0]["spoken"] == "$53",
       "both-fields creation_config persisted")

    print("PATCH /api/videos/{id}/craft: floors")
    before = craft_snap(1)
    r = patch_craft(1)
    ok(r.status_code == 400, "empty body is 400")
    ok("script" in r.text.lower() or "creation_config" in r.text.lower(),
       "empty-body 400 names the required fields")
    ok(craft_snap(1) == before, "empty body writes nothing")

    r = patch_craft(1, script="")
    ok(r.status_code == 400, "blank script is 400")
    ok(craft_snap(1) == before, "blank script writes nothing")

    r = patch_craft(1, script="   ")
    ok(r.status_code == 400, "whitespace-only script is 400")
    ok(craft_snap(1) == before, "whitespace-only script writes nothing")

    r = patch_craft(1, script=None)
    ok(r.status_code == 400, "script=null is 400")
    ok(craft_snap(1) == before, "script=null writes nothing")

    r = patch_craft(1, creation_config=None)
    ok(r.status_code == 400, "creation_config=null is 400")
    ok(craft_snap(1) == before, "creation_config=null writes nothing")

    r = patch_craft(1, creation_config="not-json")
    ok(r.status_code == 400, "invalid JSON creation_config string is 400")
    ok(craft_snap(1) == before, "invalid JSON writes nothing")

    r = patch_craft(1, creation_config=["not", "an", "object"])
    ok(r.status_code == 400, "list creation_config is 400")
    ok(craft_snap(1) == before, "list creation_config writes nothing")

    r = patch_craft(1, script="", creation_config={"beats": []})
    ok(r.status_code == 400, "mixed blank-script + cc is 400")
    ok(craft_snap(1) == before, "400 mixed writes nothing")

    print("PATCH /api/videos/{id}/craft: status gate")
    r = patch_craft(3, script="nope")
    ok(r.status_code == 409, "draft craft persist is 409")
    ok("draft" in r.text.lower(), "409 names draft status")

    r = patch_craft(5, script="nope")
    ok(r.status_code == 409, "queued craft persist is 409")

    r = patch_craft(6, script="nope")
    ok(r.status_code == 409, "published craft persist is 409")

    r = patch_craft(4, script="review corrected $53")
    ok(r.status_code == 200, "review craft persist is 200")
    ok(craft_snap(4)["script"] == "review corrected $53", "review script persisted")
    ok(craft_snap(4)["status"] == VideoStatus.REVIEW, "review status unchanged")
    ok(craft_snap(4)["mpt_task_id"] == "task-rev", "review mpt_task_id untouched")

    print("PATCH /api/videos/{id}/craft: 404 + auth")
    r = patch_craft(999, script="nope")
    ok(r.status_code == 404, "missing video is 404")

    r = client.patch("/api/videos/1/craft", json={"script": "no-auth"})
    ok(r.status_code == 401, "craft persist still requires auth")
    ok(craft_snap(1)["script"] == "padded $53 script",
       "unauthenticated craft persist left script")

    ok(craft_snap(2) == before_sib, "sibling video 2 untouched after every probe")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


print(f"\nALL {_checks} CHECKS PASSED")
