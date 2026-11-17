"""Regression: POST/PATCH /api/channels reject a blank or illegal default_privacy.

``publish_loop`` uploads ``video.privacy or channel.default_privacy``.
``ensure_topic_playlist`` passes ``channel.default_privacy`` into
``playlists.insert`` with no ``or "public"`` rescue. ``""`` is forwarded
on the playlist path (the publish ``or`` only replaces it when
``video.privacy`` is also missing). ``"   "`` is truthy, so both paths
send it. YouTube ``privacyStatus`` is ``public`` | ``unlisted`` |
``private``. Growth-agent / curl still reach this body; the channel
select only offers the three legal values.

Uses an in-memory SQLite DB and FastAPI's TestClient (no real manager.db,
no network, lifespan/scheduler never started). Exits non-zero on the
first failed assertion.
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
from app.models import Channel, OAuthStatus
from app.schemas import ChannelCreate, ChannelUpdate
from app.services import publish_loop
from app.services import topic_playlist

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(topic_playlist.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "topic_playlist module loaded from this tree")

_pub_src = Path(publish_loop.__file__).read_text()
ok("privacy = video.privacy or channel.default_privacy" in _pub_src,
   "publish_loop still uploads video.privacy or channel.default_privacy")
ok("channel.default_privacy or " not in _pub_src,
   "publish_loop does not rescue a blank channel default_privacy")

_pl_src = Path(topic_playlist.__file__).read_text()
ok("channel.default_privacy)" in _pl_src,
   "ensure_topic_playlist still passes channel.default_privacy to create_playlist")
ok('channel.default_privacy or "public"' not in _pl_src,
   "ensure_topic_playlist does not rescue a blank default_privacy with or public")

_spa = (Path(__file__).resolve().parents[1] / "frontend/src/pages/Channels.tsx").read_text()
ok('{["public", "unlisted", "private"].map((p) => <option key={p}>{p}</option>)}' in _spa,
   "channel privacy select only offers the three values the API allows")

_create_src = inspect.getsource(ChannelCreate._reject_blank_privacy)
_update_src = inspect.getsource(ChannelUpdate._reject_blank_privacy)
ok('mode="before"' in _create_src and "_privacy_or_reject" in _create_src,
   "ChannelCreate._reject_blank_privacy is mode=before via _privacy_or_reject")
ok('mode="before"' in _update_src and "_privacy_or_reject" in _update_src,
   "ChannelUpdate._reject_blank_privacy is mode=before via _privacy_or_reject")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="ch-a", name="Alpha", oauth_status=OAuthStatus.CONNECTED,
                  default_privacy="public", daily_publish_budget=5,
                  publish_windows="12:00-13:00"))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")


def row(cid: int = 1) -> Channel:
    with Session(engine) as s:
        ch = s.get(Channel, cid)
        s.expunge(ch)
        return ch


def snapshot(cid: int = 1):
    ch = row(cid)
    return {
        "name": ch.name,
        "slug": ch.slug,
        "paused": ch.paused,
        "default_privacy": ch.default_privacy,
        "daily_publish_budget": ch.daily_publish_budget,
        "publish_windows": ch.publish_windows,
        "oauth_status": ch.oauth_status,
    }


def slugs() -> set[str]:
    with Session(engine) as s:
        return {c.slug for c in s.exec(select(Channel)).all()}


def n_channels() -> int:
    with Session(engine) as s:
        return len(list(s.exec(select(Channel)).all()))


def post(**body):
    return client.post("/api/channels", auth=auth, json=body)


def patch(cid: int = 1, **body):
    return client.patch(f"/api/channels/{cid}", auth=auth, json=body)


def compact(resp) -> str:
    return "".join(resp.text.split())


try:
    before = snapshot()
    ok(before["default_privacy"] == "public", "seeded channel default_privacy is public")
    n_before = n_channels()
    seed_slugs = slugs()

    print("PATCH default_privacy blank / illegal writes nothing")
    for label, value in (
        ("empty", ""),
        ("whitespace", "   "),
        ("newline-pad", " \n "),
        ("null", None),
        ("bool-true", True),
        ("bool-false", False),
        ("secret", "secret"),
        ("capital-public", "Public"),
    ):
        before = snapshot()
        r = patch(default_privacy=value)
        ok(r.status_code == 422, f"PATCH default_privacy={label} is 422 (got {r.status_code})")
        ok("public" in r.text and "unlisted" in r.text and "private" in r.text,
           f"PATCH {label} names the three legal privacy values")
        ok(snapshot() == before, f"PATCH default_privacy={label} writes nothing")

    r = patch(default_privacy=True)
    ok('"input":true' in compact(r),
       "PATCH true keeps input true (mode=after would already have coerced)")
    ok(snapshot()["default_privacy"] == "public", "PATCH true did not store a privacy")

    print("PATCH mixed body cannot smuggle a write past a bad privacy")
    before = snapshot()
    r = patch(default_privacy="", paused=True, name="Smuggled", daily_publish_budget=1)
    ok(r.status_code == 422, "mixed PATCH with default_privacy='' is 422")
    ok(snapshot() == before, "422 mixed PATCH writes none of the fields")
    ok(snapshot()["name"] == "Alpha", "422 mixed PATCH did not rename the channel")
    ok(snapshot()["paused"] is False, "422 mixed PATCH did not pause the channel")

    print("PATCH legal privacy is stored stripped; siblings still apply")
    r = patch(default_privacy="  unlisted  ")
    ok(r.status_code == 200, f"PATCH padded unlisted is 200 (got {r.status_code})")
    ok(snapshot()["default_privacy"] == "unlisted",
       "padded unlisted is stored stripped")
    ok(r.json().get("default_privacy") == "unlisted",
       "PATCH response returns the stripped privacy")

    r = patch(default_privacy="private")
    ok(r.status_code == 200, "PATCH private is 200")
    ok(snapshot()["default_privacy"] == "private", "PATCH private is stored")

    r = patch(paused=True)
    ok(r.status_code == 200, "PATCH without default_privacy is 200")
    ok(snapshot()["paused"] is True, "omitted privacy still applies paused")
    ok(snapshot()["default_privacy"] == "private",
       "omitted privacy does not clobber the stored value")

    r = patch(publish_windows="")
    ok(r.status_code == 200, "PATCH publish_windows='' still clears (not a privacy reject)")
    ok(snapshot()["publish_windows"] is None, "empty publish_windows still clears to null")
    ok(snapshot()["default_privacy"] == "private",
       "clearing publish_windows leaves privacy alone")

    r = patch(daily_publish_budget=0)
    ok(r.status_code == 200, "PATCH budget 0 is still the legal stall")
    ok(snapshot()["daily_publish_budget"] == 0, "budget 0 persisted")
    ok(snapshot()["default_privacy"] == "private", "budget PATCH left privacy alone")

    print("PATCH missing channel / auth")
    r = client.patch("/api/channels/999", auth=auth, json={"default_privacy": ""})
    ok(r.status_code == 422, "missing channel + blank privacy is 422 (body is invalid)")
    ok(n_channels() == n_before, "missing-channel probe adds no row")

    r = client.patch("/api/channels/1", json={"default_privacy": "secret"})
    ok(r.status_code == 401, "unauthenticated PATCH is 401")
    ok(snapshot()["default_privacy"] == "private",
       "unauthenticated PATCH does not change privacy")

    print("POST default_privacy blank / illegal creates no row")
    for label, value in (
        ("empty", ""),
        ("whitespace", "   "),
        ("null", None),
        ("bool-true", True),
        ("secret", "nope"),
        ("capital-public", "Public"),
    ):
        r = post(name=f"Bad {label}", slug=f"bad-{label}", default_privacy=value)
        ok(r.status_code == 422, f"POST default_privacy={label} is 422 (got {r.status_code})")
        ok(f"bad-{label}" not in slugs(), f"POST default_privacy={label} creates no row")

    r = post(name="Smuggle", slug="smuggle", default_privacy="", daily_publish_budget=3)
    ok(r.status_code == 422, "mixed POST with blank privacy is 422")
    ok("smuggle" not in slugs(), "422 mixed POST creates no row")
    ok(n_channels() == n_before, "invalid POSTs added no row")

    print("POST legal privacy")
    r = post(name="Omitted", slug="omitted-privacy")
    ok(r.status_code == 201, f"POST omitting default_privacy is 201 (got {r.status_code})")
    ok(r.json().get("default_privacy") == "public",
       "omitted default_privacy stays the public default")
    omitted = row(r.json()["id"])
    ok(omitted.default_privacy == "public", "omitted privacy row is public")

    r = post(name="Padded", slug="padded-privacy", default_privacy="  private  ")
    ok(r.status_code == 201, f"POST padded private is 201 (got {r.status_code})")
    ok(r.json().get("default_privacy") == "private",
       "POST padded private is stored stripped")

    r = client.post("/api/channels", json={"name": "NoAuth", "slug": "no-auth",
                                           "default_privacy": ""})
    ok(r.status_code == 401, "unauthenticated POST is 401")
    ok("no-auth" not in slugs(), "unauthenticated POST creates no row")

    ok(slugs() == seed_slugs | {"omitted-privacy", "padded-privacy"},
       "only the two valid POSTs added rows")
    ok(snapshot()["default_privacy"] == "private", "ch-a privacy survived the POST probes")
    ok(snapshot()["name"] == "Alpha", "ch-a name survived the POST probes")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


print(f"\nALL {_checks} CHECKS PASSED")
