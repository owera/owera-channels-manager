"""Regression checks for PATCH /api/videos/{id} subject floor.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_videos.py

``Video.subject`` is NOT NULL. ``PATCH /api/videos/{id}`` ``setattr``s
whatever ``exclude_unset`` forwards, so JSON null persists SQL NULL.
``metadata.generate(v.subject, …)`` then TypeErrors
(``(meta.get("title") or subject)[:100]``) and the board title fallback
``v.title || v.subject`` goes blank. The live SPA path is Board.tsx
save: after #47 an empty Save is a 400 instead of a wipe. Empty /
whitespace-only restore ``video.subject`` and skip the PATCH (same
class as #34 after the API floor). Growth-agent / curl still send
null and still 400.

title / description / privacy / skip_gate / render_profile_id stay
nullable (null is inherit or "not yet generated"). A 400 mixed body
writes none of the fields. Sibling videos untouched.

Uses an in-memory SQLite DB and FastAPI's TestClient (no real
manager.db, no network, lifespan/scheduler never started). Exits
non-zero on the first failed assertion.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.routers import videos as videos_router
from app.services import metadata

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# Isolated-copy batteries pin this so a stale pyc / wrong PYTHONPATH cannot
# silently test a different checkout (08-01 lesson).
ok(Path(videos_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "videos module loaded from this tree")

# Why null/blank is fatal — a `or ""` rescue would make the 400 a policy
# choice rather than a stall-preventer. Pin the live slice so this suite's
# "must be a non-empty string" stays coupled to metadata.generate.
_meta_src = Path(metadata.__file__).read_text()
ok('(meta.get("title") or subject)[:100]' in _meta_src,
   "metadata._from_meta still slices subject (None TypeErrors)")
ok("(meta.get(\"title\") or subject or \"\")[:100]" not in _meta_src,
   "metadata does not rescue a null subject with or empty")

_vid_src = Path(videos_router.__file__).read_text()
ok("metadata.generate(v.subject," in _vid_src,
   "POST /api/videos/{id}/metadata still forwards v.subject (None TypeErrors)")

# Board save is the live SPA path. After #47 empty-Save is a 400
# instead of a wipe; restore the seeded subject and skip PATCH
# (same class as #34 after the API floor).
_board = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Board.tsx").read_text()
_save_start = _board.find("const save =")
ok(_save_start >= 0, "Board.tsx VideoModal still has a save handler")
_save_end = _board.find("return (", _save_start)
_save = _board[_save_start:_save_end]
ok("const save =" in _save and "updateVideo.mutate" in _save,
   "save slice covers the handler through mutate (not a later return)")
ok("body: { subject, render_profile_id:" not in _save,
   "save does not PATCH the raw textarea (empty is \"\")")
ok("subject.trim()" in _save,
   "save trims before the empty gate (whitespace-only is also empty)")
ok("if (!trimmed) {\n      setSubject(video.subject);\n      return;\n    }" in _save,
   "empty/whitespace gate restores video.subject and returns (does not PATCH)")
ok("setSubject(video.subject)" in _save,
   "empty save restores video.subject (not \"\" / not a placeholder)")
_restore = _save.find("setSubject(video.subject)")
_ret = _save.find("return;", _restore)
_mut = _save.find("updateVideo.mutate")
ok(0 <= _save.find("subject.trim()") < _restore < _ret < _mut,
   "trim then restore+return BEFORE mutate (empty never PATCHes)")
ok("body: { subject: trimmed" in _save,
   "valid save PATCHes the trimmed subject (not the raw textarea)")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="ch-a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.add(Channel(slug="ch-b", name="B", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="T-a", content_format="short"))
    s.add(Topic(channel_id=2, name="T-b", content_format="short"))
    s.commit()
    s.add(Video(channel_id=1, topic_id=1, subject="keep-me",
                status=VideoStatus.DRAFT, title="Existing title"))
    s.add(Video(channel_id=2, topic_id=2, subject="sibling-subject",
                status=VideoStatus.DRAFT, title="Sibling title"))
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


def snapshot(vid: int = 1):
    v = row(vid)
    return {
        "subject": v.subject,
        "title": v.title,
        "description": v.description,
        "status": v.status,
        "skip_gate": v.skip_gate,
        "render_profile_id": v.render_profile_id,
        "privacy": v.privacy,
        "channel_id": v.channel_id,
    }


def patch(vid: int = 1, **body):
    return client.patch(f"/api/videos/{vid}", auth=auth, json=body)


try:
    print("GET /api/videos/{id}")
    r = client.get("/api/videos/1", auth=auth)
    ok(r.status_code == 200, "GET /api/videos/1 is 200")
    ok(r.json().get("subject") == "keep-me", "GET surfaces the seeded subject")
    r = client.get("/api/videos/999", auth=auth)
    ok(r.status_code == 404, "GET missing video is 404")

    print("PATCH /api/videos/{id}: valid persist + omitted fields stay put")
    before_b = snapshot(2)
    r = patch(subject="new subject")
    ok(r.status_code == 200, "valid PATCH subject is 200")
    ok(r.json().get("subject") == "new subject", "response subject is the new value")
    ok(r.json().get("title") == "Existing title",
       "omitted title is not reset (exclude_unset)")
    ok(snapshot()["subject"] == "new subject", "subject persisted")
    ok(snapshot()["title"] == "Existing title", "omitted title unchanged in DB")
    ok(snapshot(2) == before_b, "PATCH on video 1 left sibling video 2 untouched")

    r = patch(subject="  padded subject  ")
    ok(r.status_code == 200, "whitespace-padded subject is 200")
    ok(snapshot()["subject"] == "padded subject",
       "PATCH strips surrounding whitespace (same as create)")

    r = patch(title="Title only")
    ok(r.status_code == 200, "title-only PATCH is 200")
    ok(snapshot()["subject"] == "padded subject",
       "title-only PATCH left subject")
    ok(snapshot()["title"] == "Title only", "title-only PATCH persisted title")

    print("PATCH /api/videos/{id}: null/blank subject is 400, writes nothing")
    before = snapshot()
    r = patch(subject=None)
    ok(r.status_code == 400, "subject=null is 400")
    ok("non-empty" in r.text and "subject" in r.text.lower(),
       "null-subject 400 names the subject floor (not a generic 400)")
    ok(snapshot() == before, "subject=null writes nothing")
    ok(row().subject is not None, "null PATCH did not persist SQL NULL into subject")

    r = patch(subject="")
    ok(r.status_code == 400, "subject=\"\" is 400 (Board empty-save)")
    ok(snapshot() == before, "empty subject writes nothing")

    r = patch(subject="   ")
    ok(r.status_code == 400, "whitespace-only subject is 400")
    ok(snapshot() == before, "whitespace-only subject writes nothing")

    print("PATCH /api/videos/{id}: mixed body cannot smuggle a title past a 400")
    r = patch(subject=None, title="smuggled")
    ok(r.status_code == 400, "mixed PATCH with subject=null is 400")
    ok(snapshot() == before, "400 mixed PATCH writes none of the fields")
    ok(snapshot()["title"] != "smuggled", "400 mixed PATCH did not persist title")

    r = patch(subject="", title="smuggled-empty")
    ok(r.status_code == 400, "mixed PATCH with empty subject is 400")
    ok(snapshot() == before, "400 empty-subject mixed PATCH writes nothing")

    print("PATCH /api/videos/{id}: JSON bool is 4xx")
    r = patch(subject=False)
    ok(r.status_code in (400, 422),
       "subject=false is 4xx (must not coerce to 'False')")
    ok(snapshot() == before, "subject=false writes nothing")

    r = patch(subject=True)
    ok(r.status_code in (400, 422), "subject=true is 4xx")
    ok(snapshot() == before, "subject=true writes nothing")

    print("PATCH /api/videos/{id}: nullable fields still accept null")
    r = patch(title=None)
    ok(r.status_code == 200, "title=null is 200 (not-yet-generated is legal)")
    ok(snapshot()["title"] is None, "title=null persisted")
    ok(snapshot()["subject"] == before["subject"],
       "title=null PATCH left subject")

    r = patch(skip_gate=None, render_profile_id=None)
    ok(r.status_code == 200,
       "skip_gate/render_profile_id null is 200 (inherit / unbound)")
    ok(snapshot()["skip_gate"] is None, "skip_gate=null persisted")
    ok(snapshot()["render_profile_id"] is None, "render_profile_id=null persisted")

    r = patch(title="Existing title")
    ok(r.status_code == 200, "restore title is 200")

    print("PATCH /api/videos/{id}: 404 + auth")
    r = patch(999, subject="nope")
    ok(r.status_code == 404, "PATCH missing video is 404")
    ok(snapshot()["subject"] == before["subject"], "404 PATCH left video 1")

    r = client.patch("/api/videos/1", json={"subject": "no-auth"})
    ok(r.status_code == 401, "PATCH still requires auth")
    ok(snapshot()["subject"] == before["subject"],
       "unauthenticated PATCH left subject")

    ok(snapshot(2) == before_b, "sibling video 2 untouched after every probe")

    # setattr-then-400 is observationally equivalent today (get_session does
    # not commit on HTTPException), but a later auto-commit would persist the
    # mixed body. Pin source order so that mutant dies here, not in prod.
    _update_def = _vid_src.find("def update_video")
    _req = _vid_src.find('_require_str(data, "subject"', _update_def)
    _setattr = _vid_src.find("for k, val in data.items():", _update_def)
    ok(0 <= _update_def < _req < _setattr,
       "update_video floors subject before setattr")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


print(f"\nALL {_checks} CHECKS PASSED")
