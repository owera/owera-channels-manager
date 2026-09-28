"""Regression checks for PATCH /api/videos/{id} and POST /api/videos
subject floors.

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

#47 floored PATCH. POST /api/videos still did ``body.subject.strip()``
straight onto the row, so ``""`` / ``"   "`` persisted an empty
subject — the same TypeError on the next metadata generate, and a
blank board card. Null is already 422 on create (required ``str``,
not ``Optional[str]``). ``createVideo`` is unused in the SPA;
growth-agent / curl still reach this path.

title / description / privacy / skip_gate stay nullable (null is
inherit or "not yet generated"). ``render_profile_id`` null is still
legal (unbound / inherit). JSON bool is not: lax ``Optional[int]``
coerces ``false→0`` / ``true→1`` before the handler. ``true`` silently
rebinds the video to profile id=1; ``false`` writes 0, which
``resolve_engine`` treats as unbound today (``if not pid``) but is
not None. Rejected on ``VideoUpdate`` with ``mode="before"``. A 400
mixed body writes none of the fields. Sibling videos untouched. A
400 create writes no row.

``ReorderBody`` is shared by ``POST /api/videos/reorder`` and
``POST /api/videos/produce``. ``channel_id`` was a bare ``int`` and
``ordered_ids`` a ``list[int]``, so JSON ``true`` became channel 1 /
video 1 and ``false`` became 0. Reorder applies positions only when
the video's channel matches, so ``true`` reordered channel 1; produce
ignores ``channel_id`` and queues every draft id in the list, so
``false`` still queued video 1. Both fields reject bools with
``mode="before"``. Integer ids, including integer 0, stay as they are.

Uses an in-memory SQLite DB and FastAPI's TestClient (no real
manager.db, no network, lifespan/scheduler never started). Exits
non-zero on the first failed assertion.
"""
from __future__ import annotations

import inspect
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
from app.schemas import ReorderBody, VideoCreate, VideoUpdate
from app.services import metadata
from app.services import engines as engines_mod

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

# Why true→1 is a silent rebind and false→0 is not None: resolve_engine
# skips a falsy pid (`if not pid`), so 0 is unbound today while 1 loads
# profile id=1. Pin the live gate so this suite's "not a boolean" stays
# coupled to engine selection.
_eng_src = Path(engines_mod.__file__).read_text()
ok("if not pid:" in _eng_src,
   "resolve_engine still treats pid=0 as unbound (if not pid)")
ok("if pid is None:" not in _eng_src,
   "resolve_engine does not distinguish 0 from None (false→0 is a latent bind)")

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


def post(**body):
    return client.post("/api/videos", auth=auth, json=body)


def n_videos() -> int:
    with Session(engine) as s:
        return len(s.exec(select(Video)).all())


def video_ids() -> set[int]:
    with Session(engine) as s:
        return {v.id for v in s.exec(select(Video)).all()}


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

    print("PATCH /api/videos/{id}: JSON bool render_profile_id is 4xx")
    # Lax Optional[int] coerces false→0 / true→1 BEFORE the handler.
    # Seed id=2 so true→1 is a visible rebind (id=1 would hide it).
    r = patch(render_profile_id=2)
    ok(r.status_code == 200, "render_profile_id=2 (integer) is 200")
    ok(snapshot()["render_profile_id"] == 2, "integer 2 persisted")
    ok(snapshot()["subject"] == before["subject"],
       "integer profile PATCH left subject")

    r = patch(skip_gate=True)
    ok(r.status_code == 200,
       "skip_gate=true is 200 (bool field; the floor is render_profile_id only)")
    ok(snapshot()["skip_gate"] is True, "skip_gate=true persisted")
    ok(snapshot()["render_profile_id"] == 2,
       "skip_gate PATCH left render_profile_id")

    before_profile = snapshot()
    r = patch(render_profile_id=False)
    ok(r.status_code in (400, 422),
       "render_profile_id=false is 4xx (must not coerce to 0)")
    ok("boolean" in r.text.lower(),
       "false-profile 4xx names the boolean rejection")
    ok(snapshot() == before_profile, "render_profile_id=false writes nothing")
    ok(snapshot()["render_profile_id"] == 2,
       "false did not persist a 0 unbind")

    r = patch(render_profile_id=True)
    ok(r.status_code in (400, 422),
       "render_profile_id=true is 4xx (must not coerce to 1)")
    ok(snapshot() == before_profile, "render_profile_id=true writes nothing")
    ok(snapshot()["render_profile_id"] == 2,
       "true did not rebind profile id=2 to 1")

    r = patch(render_profile_id=True, subject="smuggled-profile")
    ok(r.status_code in (400, 422),
       "mixed PATCH with render_profile_id=true is 4xx")
    ok(snapshot() == before_profile,
       "4xx mixed profile PATCH writes none of the fields")
    ok(snapshot()["subject"] != "smuggled-profile",
       "4xx mixed profile PATCH did not persist subject")

    r = patch(subject=before["subject"])
    ok(r.status_code == 200, "subject-only PATCH (profile omitted) is 200")
    ok(snapshot()["render_profile_id"] == 2,
       "subject-only PATCH left render_profile_id "
       "(exclude_unset; always-reject-profile would 400)")

    r = patch(render_profile_id=1)
    ok(r.status_code == 200, "render_profile_id=1 (integer) is 200")
    ok(snapshot()["render_profile_id"] == 1,
       "integer 1 persisted (true-coercion target is a legal int; "
       "a mode=after 0/1 reject would 400 here)")

    r = patch(render_profile_id=None, skip_gate=None)
    ok(r.status_code == 200, "restore render_profile_id=null is 200")
    ok(snapshot()["render_profile_id"] is None, "profile restored to unbound")
    ok(snapshot()["skip_gate"] is None, "skip_gate restored")

    upd_src = inspect.getsource(VideoUpdate)
    ok('_reject_bool_profile' in upd_src
       and '@field_validator("render_profile_id", mode="before")' in upd_src,
       "VideoUpdate._reject_bool_profile is mode=before on render_profile_id "
       "(mode=after sees the already-coerced 0/1)")

    print("POST /api/videos: valid persist + strip + queue flag")
    before_1 = snapshot(1)
    before_2 = snapshot(2)
    ids_before = video_ids()
    n_before = n_videos()

    r = post(topic_id=1, subject="new idea")
    ok(r.status_code == 201, "POST valid subject is 201")
    got = r.json()
    ok(got.get("subject") == "new idea", "response subject is the requested value")
    ok(got.get("channel_id") == 1, "create binds channel_id from the topic")
    ok(got.get("topic_id") == 1, "create binds the requested topic")
    ok(got.get("status") == VideoStatus.DRAFT,
       "omitted queue defaults to draft (not queued)")
    ok(got.get("id") not in ids_before, "create minted a new id")
    ok(snapshot(got["id"])["subject"] == "new idea", "valid subject persisted")
    ok(snapshot(1) == before_1, "POST left video 1 untouched")
    ok(snapshot(2) == before_2, "POST left video 2 untouched")
    ok(n_videos() == n_before + 1, "valid POST added exactly one row")
    created_id = got["id"]

    r = post(topic_id=1, subject="  padded create  ")
    ok(r.status_code == 201, "whitespace-padded create subject is 201")
    ok(r.json().get("subject") == "padded create",
       "POST strips surrounding whitespace (same as PATCH)")
    ok(snapshot(r.json()["id"])["subject"] == "padded create",
       "stripped create subject persisted")

    r = post(topic_id=1, subject="queued idea", queue=True)
    ok(r.status_code == 201, "POST queue=true is 201")
    ok(r.json().get("status") == VideoStatus.QUEUED,
       "queue=true lands QUEUED (the flag is not dropped by the floor)")
    ok(r.json().get("subject") == "queued idea", "queue=true kept the subject")

    print("POST /api/videos: blank/whitespace subject is 400, writes no row")
    n_before = n_videos()
    ids_before = video_ids()
    before_1 = snapshot(1)
    before_2 = snapshot(2)

    r = post(topic_id=1, subject="")
    ok(r.status_code == 400, "POST subject=\"\" is 400")
    ok("non-empty" in r.text and "subject" in r.text.lower(),
       "empty-subject POST 400 names the subject floor (not a generic 400)")
    ok(n_videos() == n_before, "POST subject=\"\" creates no row")
    ok(video_ids() == ids_before, "POST subject=\"\" did not mint an id")

    r = post(topic_id=1, subject="   ")
    ok(r.status_code == 400, "POST whitespace-only subject is 400")
    ok(n_videos() == n_before, "POST whitespace-only subject creates no row")

    r = post(topic_id=1, subject=None)
    ok(r.status_code == 422, "POST subject=null is 422 (required str, not Optional)")
    ok(n_videos() == n_before, "POST subject=null creates no row")

    r = post(topic_id=1)
    ok(r.status_code == 422, "POST omitted subject is 422")
    ok(n_videos() == n_before, "POST omitted subject creates no row")

    print("POST /api/videos: mixed body cannot smuggle queue past a 400")
    r = post(topic_id=1, subject="", queue=True)
    ok(r.status_code == 400, "mixed POST with empty subject is 400")
    ok(n_videos() == n_before, "400 mixed POST creates no row")
    ok(video_ids() == ids_before, "400 mixed POST did not persist a queued blank")

    print("POST /api/videos: JSON bool is 4xx")
    r = post(topic_id=1, subject=False)
    ok(r.status_code in (400, 422),
       "POST subject=false is 4xx (must not coerce to 'False')")
    ok(n_videos() == n_before, "POST subject=false creates no row")

    r = post(topic_id=1, subject=True)
    ok(r.status_code in (400, 422), "POST subject=true is 4xx")
    ok(n_videos() == n_before, "POST subject=true creates no row")

    print("POST /api/videos: 404 topic + auth")
    r = post(topic_id=999, subject="nope")
    ok(r.status_code == 404, "POST missing topic is 404")
    ok(n_videos() == n_before, "404 POST creates no row")

    r = post(topic_id=999, subject="")
    ok(r.status_code == 404,
       "POST empty subject on a missing topic is still 404 (topic gate first)")
    ok(n_videos() == n_before, "404 empty-subject POST creates no row")

    r = client.post("/api/videos", json={"topic_id": 1, "subject": "no-auth"})
    ok(r.status_code == 401, "POST still requires auth")
    ok(n_videos() == n_before, "unauthenticated POST creates no row")

    ok(snapshot(1) == before_1, "video 1 untouched after every create probe")
    ok(snapshot(2) == before_2, "video 2 untouched after every create probe")
    ok(snapshot(created_id)["subject"] == "new idea",
       "earlier valid create was not clobbered by the 400s")

    print("POST /api/videos: JSON bool topic_id is 422")
    # true coerces to topic 1 and creates a video on channel 1. false
    # coerces to 0 and 404s only because no topic 0 exists — that 404
    # must not count as the rejection (it writes nothing for the wrong
    # reason and would not if a topic 0 were ever inserted).

    def n_on_topic(tid: int) -> int:
        with Session(engine) as s:
            return len(s.exec(select(Video).where(Video.topic_id == tid)).all())

    def _compact(resp):
        return resp.text.replace(" ", "")

    n_before = n_videos()
    ids_before = video_ids()
    on_1 = n_on_topic(1)
    on_2 = n_on_topic(2)
    ok(on_2 >= 1 and on_1 > on_2,
       "precondition: topic 2 exists and is not topic 1 (true coerces to 1)")

    r = post(topic_id=True, subject="bool topic true", queue=True)
    ok(r.status_code == 422,
       "POST topic_id=true is 422 (must not coerce to 1 and 201)")
    ok("boolean" in r.text and '"input":true' in _compact(r),
       "topic true names boolean and keeps input true (mode=after would show 1)")
    ok(n_videos() == n_before and video_ids() == ids_before,
       "POST topic_id=true writes no row")
    ok(n_on_topic(1) == on_1, "POST topic_id=true added nothing on topic 1")

    r = post(topic_id=False, subject="bool topic false")
    ok(r.status_code == 422,
       "POST topic_id=false is 422 (a missing topic 0 would be 404)")
    ok("boolean" in r.text and '"input":false' in _compact(r),
       "topic false names boolean (topic-not-found 404 would not)")
    ok(n_videos() == n_before, "POST topic_id=false writes no row")

    r = post(topic_id=None, subject="null topic")
    ok(r.status_code == 422, "POST topic_id=null is 422")
    ok("boolean" not in r.text,
       "null topic_id is the required-int error, not the bool rejection")
    ok(n_videos() == n_before, "POST topic_id=null writes no row")

    r = post(topic_id=0, subject="zero topic")
    ok(r.status_code == 404, "POST topic_id=0 is still 404 (no topic 0)")
    ok("topic not found" in r.text and "boolean" not in r.text,
       "integer 0 stays the topic 404 (reject-zero would say boolean)")
    ok(n_videos() == n_before, "POST topic_id=0 writes no row")

    r = post(topic_id=2, subject="on topic b", queue=False)
    ok(r.status_code == 201, "POST integer topic_id=2 is 201")
    got = r.json()
    ok(got.get("topic_id") == 2 and got.get("channel_id") == 2,
       "integer topic 2 binds channel B, not channel 1 (always-raise dies here)")
    ok(got.get("status") == VideoStatus.DRAFT,
       "queue=false still creates a draft (queue stays a real bool)")
    ok(n_on_topic(2) == on_2 + 1, "integer topic 2 added one video on topic 2")
    ok(n_on_topic(1) == on_1, "integer topic 2 added nothing on topic 1")
    ok(snapshot(1) == before_1 and snapshot(2) == before_2,
       "topic-bool probes left the seeded videos untouched")

    create_src = inspect.getsource(VideoCreate)
    ok('_reject_bool_topic' in create_src
       and '@field_validator("topic_id", mode="before")' in create_src,
       "VideoCreate._reject_bool_topic is mode=before on topic_id")

    # add-then-400 is observationally equivalent today (get_session does
    # not commit on HTTPException), but a later auto-commit would persist
    # the blank row. Pin source order so that mutant dies here, not in prod.
    _create_def = _vid_src.find("def create_video")
    _topic_404 = _vid_src.find("topic not found", _create_def)
    _create_req = _vid_src.find('_require_str(', _create_def)
    _add = _vid_src.find("session.add(v)", _create_def)
    _update_req = _vid_src.find('_require_str(data, "subject"', _update_def)
    ok(0 <= _create_def < _topic_404 < _create_req < _add < _update_def,
       "create_video floors subject after the topic 404 and before session.add")
    ok(_create_req != _update_req,
       "create_video has its own _require_str call (not the PATCH one)")
    ok('"subject"' in _vid_src[_create_req:_create_req + 80],
       "create_video floors the subject key (not a different field)")

    print("POST /api/videos/reorder and /produce: JSON bool ids are 422")
    # true coerces to channel 1 / video 1. false coerces to 0: reorder
    # then matches no channel and returns 200, and produce ignores
    # channel_id so false still queues the listed drafts. Neither 200
    # is a rejection.

    with Session(engine) as s:
        s.add(Video(channel_id=1, topic_id=1, subject="ch1-extra",
                    status=VideoStatus.DRAFT, position=5))
        s.add(Video(channel_id=2, topic_id=2, subject="ch2-extra",
                    status=VideoStatus.DRAFT, position=7))
        v1 = s.get(Video, 1)
        v2 = s.get(Video, 2)
        v1.position = 4
        v2.position = 8
        s.add(v1)
        s.add(v2)
        s.commit()

    def vid_by_subject(subj: str) -> int:
        with Session(engine) as s:
            return s.exec(select(Video).where(Video.subject == subj)).one().id

    def board_state():
        with Session(engine) as s:
            rows = s.exec(select(Video).order_by(Video.id)).all()
            return tuple(
                (v.id, v.channel_id, v.position, v.status) for v in rows
            )

    def n_produce_runs() -> int:
        with Session(engine) as s:
            return len(s.exec(select(JobRun).where(JobRun.kind == "produce")).all())

    def post_reorder(**body):
        return client.post("/api/videos/reorder", auth=auth, json=body)

    def post_produce(**body):
        return client.post("/api/videos/produce", auth=auth, json=body)

    def first_err(resp):
        return resp.json()["detail"][0]

    ch1_extra = vid_by_subject("ch1-extra")
    ch2_extra = vid_by_subject("ch2-extra")
    topic_b = vid_by_subject("on topic b")
    ok(ch1_extra != 1 and ch2_extra != 2 and topic_b not in (1, 2),
       "precondition: extra drafts are not the seeded video ids")
    base = board_state()
    base_by_id = {row[0]: row for row in base}
    ok(base_by_id[1][2] == 4 and base_by_id[ch1_extra][2] == 5,
       "precondition: channel 1 positions are not already the true-coerced order")
    ok(base_by_id[2][2] == 8 and base_by_id[ch2_extra][2] == 7,
       "precondition: channel 2 positions will change under an integer reorder")
    # An earlier create probe queued one video on purpose (queue=true).
    # The ids this section reorders or produces are still drafts.
    ok(base_by_id[1][3] == VideoStatus.DRAFT
       and base_by_id[2][3] == VideoStatus.DRAFT
       and base_by_id[ch1_extra][3] == VideoStatus.DRAFT
       and base_by_id[ch2_extra][3] == VideoStatus.DRAFT
       and base_by_id[topic_b][3] == VideoStatus.DRAFT,
       "precondition: reorder/produce targets are still drafts")
    runs0 = n_produce_runs()
    ok(runs0 == 0, "precondition: no produce JobRuns yet")

    def unchanged(msg):
        ok(board_state() == base and n_produce_runs() == runs0, msg)

    r = post_reorder(channel_id=True, ordered_ids=[ch1_extra, 1])
    ok(r.status_code == 422,
       "reorder channel_id=true is 422 (must not reorder channel 1)")
    err = first_err(r)
    ok(err["loc"][-1] == "channel_id", "reorder channel true error is on channel_id")
    ok(err["input"] is True,
       "reorder channel true keeps input true (mode=after would show 1)")
    ok("boolean" in r.text, "reorder channel true names boolean")
    unchanged("reorder channel_id=true writes nothing")

    r = post_reorder(channel_id=False, ordered_ids=[ch1_extra, 1])
    ok(r.status_code == 422,
       "reorder channel_id=false is 422 (0 would be a silent no-op 200)")
    err = first_err(r)
    ok(err["input"] is False,
       "reorder channel false keeps input false (coerced 0 would 200)")
    ok("boolean" in r.text, "reorder channel false names boolean")
    unchanged("reorder channel_id=false writes nothing")

    r = post_reorder(channel_id=1, ordered_ids=[True])
    ok(r.status_code == 422,
       "reorder ordered_ids=[true] is 422 (must not move video 1 to position 0)")
    err = first_err(r)
    ok(err["loc"][-1] == "ordered_ids", "reorder ids true error is on ordered_ids")
    ok(err["input"] == [True],
       "reorder ids true keeps [true] (mode=after would show [1])")
    ok("boolean" in r.text, "reorder ids true names boolean")
    unchanged("reorder ordered_ids=[true] writes nothing")

    r = post_reorder(channel_id=1, ordered_ids=[False])
    ok(r.status_code == 422,
       "reorder ordered_ids=[false] is 422 (video 0 is a silent skip, not a rejection)")
    err = first_err(r)
    ok(err["input"] == [False], "reorder ids false keeps [false]")
    ok("boolean" in r.text, "reorder ids false names boolean")
    unchanged("reorder ordered_ids=[false] writes nothing")

    r = post_reorder(channel_id=1, ordered_ids=[ch1_extra, True])
    ok(r.status_code == 422,
       "reorder mixed [id, true] is 422 (must not apply the integer prefix)")
    err = first_err(r)
    ok(err["input"] == [ch1_extra, True],
       "reorder mixed ids keep the bool (a strip-bools mutant returns 200)")
    unchanged("reorder mixed [id, true] writes nothing")

    r = post_produce(channel_id=True, ordered_ids=[1])
    ok(r.status_code == 422,
       "produce channel_id=true is 422 (handler ignores channel_id and would queue)")
    err = first_err(r)
    ok(err["loc"][-1] == "channel_id" and err["input"] is True,
       "produce channel true stays on channel_id with input true")
    ok("boolean" in r.text, "produce channel true names boolean")
    unchanged("produce channel_id=true writes nothing")

    r = post_produce(channel_id=False, ordered_ids=[1])
    ok(r.status_code == 422,
       "produce channel_id=false is 422 (false→0 would still queue video 1)")
    err = first_err(r)
    ok(err["input"] is False and "boolean" in r.text,
       "produce channel false keeps input false and names boolean")
    unchanged("produce channel_id=false writes nothing")

    r = post_produce(channel_id=1, ordered_ids=[True])
    ok(r.status_code == 422,
       "produce ordered_ids=[true] is 422 (must not queue video 1)")
    err = first_err(r)
    ok(err["loc"][-1] == "ordered_ids" and err["input"] == [True],
       "produce ids true keeps [true] on ordered_ids")
    unchanged("produce ordered_ids=[true] writes nothing")

    r = post_produce(channel_id=1, ordered_ids=[False])
    ok(r.status_code == 422, "produce ordered_ids=[false] is 422")
    ok(first_err(r)["input"] == [False], "produce ids false keeps [false]")
    unchanged("produce ordered_ids=[false] writes nothing")

    r = post_produce(channel_id=2, ordered_ids=[ch1_extra, True])
    ok(r.status_code == 422,
       "produce mixed [id, true] is 422 (must not queue the integer prefix)")
    ok(first_err(r)["input"] == [ch1_extra, True],
       "produce mixed ids keep the bool")
    unchanged("produce mixed [id, true] writes nothing")

    r = post_reorder(channel_id=2, ordered_ids=[ch2_extra, 2])
    ok(r.status_code == 200 and r.json() == {"ok": True},
       "integer reorder of channel 2 is 200 (always-raise dies here)")
    moved = board_state()
    moved_by_id = {row[0]: row for row in moved}
    ok(moved_by_id[ch2_extra][2] == 0 and moved_by_id[2][2] == 1,
       "channel 2 order is extra then video 2")
    ok(moved_by_id[1] == base_by_id[1] and moved_by_id[ch1_extra] == base_by_id[ch1_extra],
       "channel 2 reorder left channel 1 positions and drafts")
    ok(moved_by_id[topic_b] == base_by_id[topic_b],
       "unlisted channel 2 video keeps its position")
    ok(n_produce_runs() == runs0, "reorder writes no produce JobRun")

    r = post_reorder(channel_id=0, ordered_ids=[ch1_extra, 1])
    ok(r.status_code == 200 and r.json() == {"ok": True},
       "reorder channel_id=0 is still 200 (reject-zero would 422)")
    ok("boolean" not in r.text, "integer 0 channel_id is not the bool rejection")
    ok(board_state() == moved, "reorder channel_id=0 moves nothing")

    r = post_reorder(channel_id=1, ordered_ids=[0, 1])
    ok(r.status_code == 200 and r.json() == {"ok": True},
       "reorder ordered_ids=[0, 1] is 200 (integer 0 is not a bool)")
    zero_ids = {row[0]: row for row in board_state()}
    ok(zero_ids[1][2] == 1 and zero_ids[1][3] == VideoStatus.DRAFT,
       "integer 0 in ordered_ids still repositions video 1 at index 1")
    ok(zero_ids[ch1_extra] == moved_by_id[ch1_extra],
       "a video absent from ordered_ids keeps its position")
    ok(zero_ids[2] == moved_by_id[2] and zero_ids[ch2_extra] == moved_by_id[ch2_extra],
       "channel 1 reorder left channel 2 positions")

    r = post_produce(channel_id=2, ordered_ids=[ch2_extra, 2, 999])
    ok(r.status_code == 200 and r.json() == {"produced": 2},
       "integer produce queues the two listed drafts and skips the missing id")
    produced = {row[0]: row for row in board_state()}
    ok(produced[ch2_extra][3] == VideoStatus.QUEUED
       and produced[2][3] == VideoStatus.QUEUED,
       "the listed channel 2 drafts are queued")
    ok(produced[1][3] == VideoStatus.DRAFT and produced[ch1_extra][3] == VideoStatus.DRAFT,
       "produce did not queue drafts that were not listed")
    ok(produced[topic_b][3] == VideoStatus.DRAFT,
       "unlisted channel 2 draft stays a draft")
    ok(n_produce_runs() == 2, "integer produce wrote one JobRun per queued draft")

    r = post_produce(channel_id=0, ordered_ids=[ch1_extra])
    ok(r.status_code == 200 and r.json() == {"produced": 1},
       "produce channel_id=0 still queues the listed draft (0 is not a bool)")
    ok("boolean" not in r.text, "produce integer 0 is not the bool rejection")
    after0 = {row[0]: row for row in board_state()}
    ok(after0[ch1_extra][3] == VideoStatus.QUEUED,
       "channel_id=0 produce queued ch1-extra")
    ok(after0[1][3] == VideoStatus.DRAFT, "channel_id=0 produce left video 1 a draft")
    ok(n_produce_runs() == 3, "channel_id=0 produce wrote one JobRun")

    r = post_produce(channel_id=2, ordered_ids=[2])
    ok(r.status_code == 200 and r.json() == {"produced": 0},
       "produce of a non-draft reports 0")
    ok(n_produce_runs() == 3, "no-op produce writes no JobRun")
    ok(board_state() == tuple(after0[i] for i in sorted(after0)),
       "no-op produce leaves the board unchanged")

    r = post_reorder(channel_id=None, ordered_ids=[1])
    ok(r.status_code == 422, "reorder channel_id=null is 422")
    ok("boolean" not in r.text,
       "null channel_id is the required-int error, not the bool rejection")
    r = post_reorder(channel_id=1, ordered_ids=None)
    ok(r.status_code == 422, "reorder ordered_ids=null is 422")
    ok("boolean" not in r.text,
       "null ordered_ids is the list-type error, not the bool rejection")
    r = post_reorder(channel_id=1, ordered_ids=True)
    ok(r.status_code == 422, "reorder ordered_ids=true (not a list) is 422, not 500")
    r = client.post("/api/videos/reorder", auth=auth, json={"ordered_ids": [1]})
    ok(r.status_code == 422, "reorder omitted channel_id is 422")
    ok(board_state() == tuple(after0[i] for i in sorted(after0)),
       "null and omitted ids write nothing")

    r = client.post("/api/videos/reorder",
                    json={"channel_id": True, "ordered_ids": [True]})
    ok(r.status_code == 401, "reorder still requires auth")
    r = client.post("/api/videos/produce",
                    json={"channel_id": False, "ordered_ids": [1]})
    ok(r.status_code == 401, "produce still requires auth")
    ok(board_state() == tuple(after0[i] for i in sorted(after0))
       and n_produce_runs() == 3,
       "unauthenticated reorder/produce write nothing")

    reorder_src = inspect.getsource(ReorderBody)
    ok('_reject_bool_channel' in reorder_src
       and '@field_validator("channel_id", mode="before")' in reorder_src,
       "ReorderBody._reject_bool_channel is mode=before on channel_id")
    ok('_reject_bool_ids' in reorder_src
       and '@field_validator("ordered_ids", mode="before")' in reorder_src,
       "ReorderBody._reject_bool_ids is mode=before on ordered_ids")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


print(f"\nALL {_checks} CHECKS PASSED")
