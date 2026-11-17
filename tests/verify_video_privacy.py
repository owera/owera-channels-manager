"""Regression: PATCH /api/videos and approve reject a blank or illegal privacy.

``publish_loop`` uploads ``video.privacy or channel.default_privacy``.
``""`` is falsy, so it inherits — and still sits on the row as an
override the review page treats as missing. ``"   "`` is truthy, so
the upload sends it as ``privacyStatus``. YouTube only accepts
``public`` | ``unlisted`` | ``private``. ``Video.privacy`` null is the
documented inherit (clear the override). The review select only offers
the three legal values; growth-agent / curl still send the body on
PATCH and on approve.

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
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.routers import videos as videos_router
from app.schemas import VideoUpdate
from app.services import publish_loop

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

_pub_src = Path(publish_loop.__file__).read_text()
ok("privacy = video.privacy or channel.default_privacy" in _pub_src,
   "publish_loop still uploads video.privacy or the channel default")
ok('video.privacy or channel.default_privacy or "public"' not in _pub_src,
   "publish_loop does not rescue a blank video privacy with or public")

_approve_src = inspect.getsource(videos_router.approve)
ok('for k in ("title", "description", "privacy")' in _approve_src,
   "approve still copies privacy from the validated VideoUpdate body")

_review = (Path(__file__).resolve().parents[1] / "frontend/src/pages/Review.tsx").read_text()
ok('{["public", "unlisted", "private"].map((p) => <option key={p}>{p}</option>)}' in _review,
   "review privacy select only offers the three values the API allows")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="ch-a", name="Alpha", oauth_status=OAuthStatus.CONNECTED,
                  default_privacy="unlisted"))
    s.add(Channel(slug="ch-b", name="Beta", oauth_status=OAuthStatus.CONNECTED,
                  default_privacy="public"))
    s.commit()
    s.add(Topic(channel_id=1, name="T-a", content_format="short"))
    s.add(Topic(channel_id=2, name="T-b", content_format="short"))
    s.commit()
    s.add(Video(channel_id=1, topic_id=1, subject="keep-me",
                status=VideoStatus.REVIEW, title="Existing title",
                privacy="private"))
    s.add(Video(channel_id=2, topic_id=2, subject="sibling",
                status=VideoStatus.DRAFT, title="Sibling",
                privacy="public"))
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
        "privacy": v.privacy,
        "skip_gate": v.skip_gate,
        "channel_id": v.channel_id,
    }


def patch(vid: int = 1, **body):
    return client.patch(f"/api/videos/{vid}", auth=auth, json=body)


def approve(vid: int = 1, **body):
    return client.post(f"/api/videos/{vid}/approve", auth=auth, json=body)


def compact(resp) -> str:
    return "".join(resp.text.split())


try:
    before = snapshot()
    ok(before["privacy"] == "private", "seeded video privacy is private")
    ok(before["status"] == VideoStatus.REVIEW, "seeded video is in review")
    ok(snapshot(2)["privacy"] == "public", "sibling privacy is public")

    print("PATCH privacy blank / illegal writes nothing")
    for label, value in (
        ("empty", ""),
        ("whitespace", "   "),
        ("newline-pad", " \n "),
        ("bool-true", True),
        ("bool-false", False),
        ("secret", "secret"),
        ("capital-public", "Public"),
    ):
        before = snapshot()
        sib = snapshot(2)
        r = patch(privacy=value)
        ok(r.status_code == 422, f"PATCH privacy={label} is 422 (got {r.status_code})")
        ok("public" in r.text and "unlisted" in r.text and "private" in r.text,
           f"PATCH {label} names the three legal privacy values")
        ok(snapshot() == before, f"PATCH privacy={label} writes nothing")
        ok(snapshot(2) == sib, f"PATCH privacy={label} leaves the sibling")

    r = patch(privacy=True)
    ok('"input":true' in compact(r),
       "PATCH true keeps input true (mode=after would already have coerced)")
    ok(snapshot()["privacy"] == "private", "PATCH true did not store a privacy")

    print("PATCH mixed body cannot smuggle a write past a bad privacy")
    before = snapshot()
    r = patch(privacy="", title="Smuggled", skip_gate=True, subject="smuggled subject")
    ok(r.status_code == 422, "mixed PATCH with privacy='' is 422")
    ok(snapshot() == before, "422 mixed PATCH writes none of the fields")
    ok(snapshot()["title"] == "Existing title", "422 mixed PATCH did not retitle")
    ok(snapshot()["subject"] == "keep-me", "422 mixed PATCH did not change subject")

    print("PATCH legal privacy is stored stripped; null clears; omit leaves it")
    r = patch(privacy="  unlisted  ")
    ok(r.status_code == 200, f"PATCH padded unlisted is 200 (got {r.status_code})")
    ok(snapshot()["privacy"] == "unlisted", "padded unlisted is stored stripped")
    ok(r.json().get("privacy") == "unlisted", "PATCH response returns the stripped privacy")
    ok(snapshot()["title"] == "Existing title", "privacy PATCH left the title")

    r = patch(privacy="public")
    ok(r.status_code == 200, "PATCH public is 200")
    ok(snapshot()["privacy"] == "public", "PATCH public is stored")

    r = patch(title="  kept padding  ")
    ok(r.status_code == 200, "PATCH title padding is still 200 (not this floor)")
    ok(snapshot()["title"] == "  kept padding  ",
       "title padding is stored as sent (privacy floor does not strip title)")
    ok(snapshot()["privacy"] == "public", "title PATCH left privacy")

    r = patch(description="")
    ok(r.status_code == 200, "PATCH description='' is still legal")
    ok(snapshot()["description"] == "", "empty description persisted")
    ok(snapshot()["privacy"] == "public", "description PATCH left privacy")

    r = patch(privacy=None)
    ok(r.status_code == 200, f"PATCH privacy=null is 200 (got {r.status_code})")
    ok(snapshot()["privacy"] is None, "null clears the override (inherit channel default)")
    ok(r.json().get("privacy") is None, "response privacy is null after the clear")

    r = patch(skip_gate=False)
    ok(r.status_code == 200, "PATCH without privacy is 200")
    ok(snapshot()["skip_gate"] is False, "omitted privacy still applies skip_gate")
    ok(snapshot()["privacy"] is None, "omitted privacy does not clobber the clear")

    r = patch(privacy="private", title="Restored title")
    ok(r.status_code == 200, "PATCH private plus title is 200")
    ok(snapshot()["privacy"] == "private", "private stored")
    ok(snapshot()["title"] == "Restored title", "sibling field on a legal privacy PATCH applied")

    print("approve uses the same body")
    before = snapshot()
    r = approve(privacy="")
    ok(r.status_code == 422, f"approve privacy='' is 422 (got {r.status_code})")
    ok("unlisted" in r.text, "approve blank privacy names a legal value")
    ok(snapshot() == before, "approve blank privacy does not flip status or privacy")
    ok(snapshot()["status"] == VideoStatus.REVIEW, "approve blank privacy left review")

    r = approve(privacy="   ")
    ok(r.status_code == 422, "approve whitespace privacy is 422")
    ok(snapshot()["privacy"] == "private", "approve whitespace did not store the pad")
    ok(snapshot()["status"] == VideoStatus.REVIEW, "approve whitespace left review")

    r = approve(privacy="secret")
    ok(r.status_code == 422, "approve secret privacy is 422")
    ok(snapshot() == before, "approve secret writes nothing")

    # Legal privacy passes the schema. Approve still refuses a draft
    # before it copies the body, so this must be 409 and must not store.
    r = client.post("/api/videos/2/approve", auth=auth, json={"privacy": "  private  "})
    ok(r.status_code == 409, f"draft approve with padded privacy is 409 (got {r.status_code})")
    ok("cannot approve" in r.text, "draft approve refusal is the status gate")
    ok(snapshot(2)["privacy"] == "public",
       "409 approve does not store the stripped privacy before the status gate")
    ok(snapshot(2)["status"] == VideoStatus.DRAFT, "draft approve left the sibling a draft")
    ok(snapshot()["privacy"] == "private", "draft approve left video 1")

    print("missing video / auth")
    r = client.patch("/api/videos/999", auth=auth, json={"privacy": ""})
    ok(r.status_code == 422, "missing video + blank privacy is 422 (body is invalid)")
    r = client.patch("/api/videos/999", auth=auth, json={"privacy": "public"})
    ok(r.status_code == 404, "missing video + legal privacy is 404")
    r = client.patch("/api/videos/1", json={"privacy": "secret"})
    ok(r.status_code == 401, "unauthenticated PATCH is 401")
    ok(snapshot()["privacy"] == "private", "unauthenticated PATCH does not change privacy")

    with Session(engine) as s:
        n = len(list(s.exec(select(Video)).all()))
    ok(n == 2, "no extra video rows")

    _val_src = inspect.getsource(VideoUpdate._reject_blank_privacy)
    ok('mode="before"' in _val_src and "_video_privacy_or_reject" in _val_src,
       "VideoUpdate._reject_blank_privacy is mode=before via _video_privacy_or_reject")

    print(f"\nALL {_checks} CHECKS PASSED")
finally:
    settings.app_password = _orig_pw
    main.app.dependency_overrides.pop(get_session, None)
