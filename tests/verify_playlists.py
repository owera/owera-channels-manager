"""Regression checks for POST /api/channels/{id}/playlists title floor.

``PlaylistCreate.title`` is a required ``str``, so JSON null / bool / int
already 422. ``""`` and ``"   "`` persist and the handler forwarded them
to ``youtube.create_playlist`` — ``playlists.insert``, 50 quota units —
before anything checked that the title was non-empty. Growth-agent / curl
still reach this path; the SPA auto-creates playlists on first produce.

``_require_str`` is the choke point: blank / whitespace 400 BEFORE
``get_service``, so a bad title never probes the token and never hits
YouTube. A padded title is stripped. Description / privacy stay as sent.
A 400 writes no row and does not call create_playlist. Missing channel
still 404 even with a blank title. NeedsConnect on a real title still
400s and still flips. Unauthenticated still 401.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, no live YouTube). ``youtube.get_service`` / ``create_playlist``
are stubbed and recorded. Exits non-zero on the first failed assertion.
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
from app.models import Channel, OAuthStatus, Playlist
from app.routers import playlists as playlists_router
from app.schemas import PlaylistCreate
from app.services import youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(playlists_router.__file__).resolve().parents[2]
   == Path(__file__).resolve().parents[1],
   "playlists module loaded from this tree")
ok(Path(youtube.__file__).resolve().parents[2]
   == Path(__file__).resolve().parents[1],
   "youtube module loaded from this tree")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.add(Channel(slug="b", name="B", oauth_status=OAuthStatus.CONNECTED))
    s.add(Channel(slug="dead", name="Dead", oauth_status=OAuthStatus.CONNECTED))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

_calls = {"get_service": [], "create_playlist": []}
_orig_get = playlists_router.youtube.get_service
_orig_create = playlists_router.youtube.create_playlist


def _fake_get_service(slug):
    _calls["get_service"].append(slug)
    if slug == "dead":
        raise youtube.NeedsConnect("no token")
    return object()


def _fake_create_playlist(service, title, description="", privacy="public"):
    _calls["create_playlist"].append(
        {"title": title, "description": description, "privacy": privacy})
    n = len(_calls["create_playlist"])
    return {
        "yt_playlist_id": f"PL{'x' * 32}{n:02d}"[:34],
        "title": title,
        "description": description,
        "privacy": privacy,
    }


playlists_router.youtube.get_service = _fake_get_service
playlists_router.youtube.create_playlist = _fake_create_playlist


def post(channel_id, **body):
    return client.post(f"/api/channels/{channel_id}/playlists", auth=auth,
                       json=body)


def n_playlists(channel_id=None):
    with Session(engine) as s:
        q = select(Playlist)
        if channel_id is not None:
            q = q.where(Playlist.channel_id == channel_id)
        return len(s.exec(q).all())


def titles(channel_id):
    with Session(engine) as s:
        return [p.title for p in s.exec(
            select(Playlist).where(Playlist.channel_id == channel_id)).all()]


try:
    print("POST /api/channels/{id}/playlists: blank title is 400 before YouTube")
    n_before = n_playlists()
    n_a = n_playlists(1)
    n_b = n_playlists(2)
    get_before = list(_calls["get_service"])
    create_before = list(_calls["create_playlist"])

    r = post(1, title="")
    ok(r.status_code == 400, "POST title='' is 400")
    ok("title" in r.text.lower(), "empty-title 400 names the field")
    ok(_calls["get_service"] == get_before,
       "empty title does not call get_service "
       "(a floor after the token probe still spends the NeedsConnect path)")
    ok(_calls["create_playlist"] == create_before,
       "empty title does not call create_playlist")
    ok(n_playlists() == n_before, "empty title writes no Playlist row")
    ok(n_playlists(2) == n_b, "empty title left sibling channel B untouched")

    r = post(1, title="   ")
    ok(r.status_code == 400, "POST whitespace title is 400")
    ok("title" in r.text.lower(), "whitespace-title 400 names the field")
    ok(_calls["get_service"] == get_before,
       "whitespace title does not call get_service")
    ok(_calls["create_playlist"] == create_before,
       "whitespace title does not call create_playlist")
    ok(n_playlists() == n_before, "whitespace title writes no row")

    r = post(1, title="", description="smuggled")
    ok(r.status_code == 400, "mixed POST with empty title is 400")
    ok(_calls["create_playlist"] == create_before,
       "4xx mixed empty title did not smuggle a YouTube insert")
    ok(n_playlists() == n_before, "4xx mixed empty title wrote no row")

    r = post(1, title="  Series Alpha  ", description="theme", privacy="unlisted")
    ok(r.status_code == 201, "padded title is 201")
    ok(r.json().get("title") == "Series Alpha",
       "padded title is stored stripped")
    ok(r.json().get("description") == "theme", "description forwarded")
    ok(r.json().get("privacy") == "unlisted", "privacy forwarded")
    ok(r.json().get("channel_id") == 1, "create lands on the requested channel")
    ok(_calls["get_service"][len(get_before):] == ["a"],
       "valid create probes slug a (channel 1), not b")
    ok(_calls["create_playlist"][-1] == {
        "title": "Series Alpha", "description": "theme", "privacy": "unlisted",
    }, "create_playlist got the stripped title (raw padded would leak spaces to YT)")
    ok("Series Alpha" in titles(1), "stripped title persisted on channel 1")
    ok(n_playlists(2) == n_b, "valid create on A left channel B untouched")

    r = post(1, title="Series Beta")
    ok(r.status_code == 201, "omitted description/privacy is 201")
    ok(_calls["create_playlist"][-1]["title"] == "Series Beta",
       "canonical title forwarded as-is")
    ok(_calls["create_playlist"][-1]["description"] == "",
       "omitted description stays the PlaylistCreate default ''")
    ok(_calls["create_playlist"][-1]["privacy"] == "public",
       "omitted privacy stays the PlaylistCreate default public")

    print("POST /api/channels/{id}/playlists: null/bool/omitted title stay 422")
    create_mid = list(_calls["create_playlist"])
    r = post(1)
    ok(r.status_code == 422, "omitted title is 422 (required str)")
    ok("boolean" not in r.text.lower(),
       "omitted title is the required-str error, not a bool rejection")
    ok(_calls["create_playlist"] == create_mid, "omitted title did not insert")

    r = client.post("/api/channels/1/playlists", auth=auth, json={"title": None})
    ok(r.status_code == 422, "POST title=null is 422")
    ok("title" in r.text.lower(), "null title 422 names the field")
    ok(_calls["create_playlist"] == create_mid, "null title did not insert")

    r = client.post("/api/channels/1/playlists", auth=auth, json={"title": True})
    ok(r.status_code == 422, "POST title=true is 422")
    ok("string" in r.text.lower(),
       "bool title is the string-type error (str does not coerce true→'True')")
    ok(_calls["create_playlist"] == create_mid, "bool title did not insert")

    r = client.post("/api/channels/1/playlists", auth=auth, json={"title": False})
    ok(r.status_code == 422, "POST title=false is 422")
    ok(_calls["create_playlist"] == create_mid, "false title did not insert")

    print("POST /api/channels/{id}/playlists: missing channel 404s before the floor")
    r = post(999, title="")
    ok(r.status_code == 404, "missing channel + empty title is 404")
    ok("channel not found" in r.text.lower(),
       "missing channel 404 names the channel (empty-title 400 would hide it)")
    ok(_calls["create_playlist"] == create_mid,
       "missing-channel empty title did not insert")

    r = post(999, title="Orphan")
    ok(r.status_code == 404, "missing channel + real title is 404")
    ok("Orphan" not in titles(1) and "Orphan" not in titles(2),
       "missing-channel create wrote no row on either channel")

    print("POST /api/channels/{id}/playlists: NeedsConnect on a real title still 400s")
    dead_id = 3
    with Session(engine) as s:
        dead = s.exec(select(Channel).where(Channel.slug == "dead")).one()
        ok(dead.id == dead_id, "precondition: dead channel is id 3")
        ok(dead.oauth_status == OAuthStatus.CONNECTED,
           "precondition: dead channel starts CONNECTED")
    n_dead = n_playlists(dead_id)
    r = post(dead_id, title="Should Not Land")
    ok(r.status_code == 400, "NeedsConnect on a real title is still 400")
    ok("connect the channel first" in r.text.lower(),
       "NeedsConnect 400 still names the reconnect")
    ok("Should Not Land" not in titles(dead_id),
       "NeedsConnect create wrote no Playlist row")
    ok(n_playlists(dead_id) == n_dead, "NeedsConnect wrote no row")
    with Session(engine) as s:
        dead = s.get(Channel, dead_id)
        ok(dead.oauth_status != OAuthStatus.CONNECTED,
           "NeedsConnect still flips off CONNECTED (floor must not swallow it; "
           "EXPIRED vs DISCONNECTED is has_token, not this floor)")
    dead_gets = list(_calls["get_service"])

    r = post(dead_id, title="")
    ok(r.status_code == 400, "empty title on a dead channel is 400")
    ok("title" in r.text.lower(),
       "empty title on a dead channel names the title floor "
       "(must not hide behind NeedsConnect / reconnect copy)")
    ok(_calls["get_service"] == dead_gets,
       "empty title on an already-dead channel does not re-enter get_service")

    print("POST /api/channels/{id}/playlists: unauthenticated is 401")
    n_all = n_playlists()
    r = client.post("/api/channels/1/playlists", json={"title": "NoAuth"})
    ok(r.status_code == 401, "unauthenticated create is 401")
    ok(n_playlists() == n_all, "401 writes no row")
    ok("NoAuth" not in titles(1), "401 did not persist a title")

    src = inspect.getsource(playlists_router.create)
    ok("_require_str" in src, "create floors title through _require_str")
    req = src.find("_require_str")
    get = src.find("get_service")
    insert = src.find("create_playlist")
    add = src.find("session.add")
    ok(0 <= req < get < insert < add,
       "create floors title after the channel 404 and before get_service "
       "(a floor after the insert still spends 50 quota units)")
    ok('_require_str(fields, "title"' in src,
       "create floors the title key (a description floor dies here; "
       "the hint also says 'title' so a membership pin on the slice is vacuous)")

    pc_src = inspect.getsource(PlaylistCreate)
    ok("title: str" in pc_src, "PlaylistCreate.title is a required str")
finally:
    playlists_router.youtube.get_service = _orig_get
    playlists_router.youtube.create_playlist = _orig_create
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
