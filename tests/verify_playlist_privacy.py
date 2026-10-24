"""Regression: POST /api/channels/{id}/playlists rejects a blank privacy.

``create`` forwards ``body.privacy`` to ``youtube.create_playlist``
(``playlists.insert``, 50 quota units) after ``get_service``. ``""`` and
``"   "`` are not a YouTube ``privacyStatus``. The channel select is not
this route; growth-agent / curl still send the body. Title blank is a
separate floor. Description may stay empty.

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
from app.models import Channel, OAuthStatus, Playlist
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


ok(Path(youtube.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "youtube module loaded from this tree")

_src = inspect.getsource(PlaylistCreate._reject_blank_privacy)
ok('mode="before"' in _src,
   "PlaylistCreate._reject_blank_privacy is mode=before")
ok("v.strip()" in _src,
   "privacy floor strips before the allowlist check")

_router = (Path(__file__).resolve().parents[1] / "app/routers/playlists.py").read_text()
_create = _router.find("def create(")
_get = _router.find("youtube.get_service", _create)
_insert = _router.find("youtube.create_playlist", _create)
ok(0 <= _create < _get < _insert,
   "create still calls get_service before create_playlist")
ok("body.privacy" in _router[_create:_create + 800],
   "create still forwards body.privacy (the schema strips it)")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="ch-a", name="Alpha", oauth_status=OAuthStatus.CONNECTED))
    s.add(Channel(slug="ch-b", name="Beta", oauth_status=OAuthStatus.CONNECTED))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

_calls = {"get": [], "create": []}


def _fake_get(slug):
    _calls["get"].append(slug)
    return object()


def _fake_create(service, title, description="", privacy="public"):
    _calls["create"].append((title, description, privacy))
    return {
        "yt_playlist_id": "PLxxxxxxxxxxxxxxxxxxxxxx123456",
        "title": title,
        "description": description,
        "privacy": privacy,
    }


def _boom_get(slug):
    _calls["get"].append(slug)
    raise youtube.NeedsConnect("token missing")


_orig_get, _orig_create = youtube.get_service, youtube.create_playlist
youtube.get_service = _fake_get
youtube.create_playlist = _fake_create


def reset_calls():
    _calls["get"].clear()
    _calls["create"].clear()


def n_playlists() -> int:
    with Session(engine) as s:
        return len(list(s.exec(select(Playlist)).all()))


def channel(cid: int) -> Channel:
    with Session(engine) as s:
        ch = s.get(Channel, cid)
        s.expunge(ch)
        return ch


def post(cid: int = 1, **body):
    return client.post(f"/api/channels/{cid}/playlists", auth=auth, json=body)


def compact(resp) -> str:
    return "".join(resp.text.split())


try:
    print("POST privacy blank / illegal does not touch YouTube")
    for label, value in (
        ("empty", ""),
        ("whitespace", "   "),
        ("newline-pad", " \n "),
        ("null", None),
        ("bool-true", True),
        ("secret", "secret"),
        ("capital-public", "Public"),
    ):
        reset_calls()
        n_before = n_playlists()
        r = post(title="Series", description="notes", privacy=value)
        ok(r.status_code == 422, f"POST privacy={label} is 422 (got {r.status_code})")
        ok("public" in r.text and "unlisted" in r.text and "private" in r.text,
           f"POST {label} names the three legal privacy values")
        ok(_calls["get"] == [] and _calls["create"] == [],
           f"POST privacy={label} does not call get_service or create_playlist")
        ok(n_playlists() == n_before, f"POST privacy={label} writes no playlist")

    reset_calls()
    r = post(title="Series", privacy=True)
    ok('"input":true' in compact(r),
       "POST true keeps input true (mode=after would already have coerced)")

    print("POST legal privacy is stripped; title and description are not")
    reset_calls()
    r = post(title="Series Alpha", description="  notes  ", privacy="  private  ")
    ok(r.status_code == 201, f"POST padded private is 201 (got {r.status_code})")
    ok(_calls["get"] == ["ch-a"], "padded private probes slug ch-a")
    ok(_calls["create"] == [("Series Alpha", "  notes  ", "private")],
       "privacy is stripped; description is forwarded with its padding")
    ok(r.json().get("privacy") == "private", "response privacy is the stripped value")
    ok(n_playlists() == 1, "legal POST writes one playlist")

    reset_calls()
    r = post(cid=2, title="Omitted")
    ok(r.status_code == 201, f"POST omitting privacy is 201 (got {r.status_code})")
    ok(_calls["get"] == ["ch-b"], "omitted privacy probes slug ch-b")
    ok(_calls["create"] == [("Omitted", "", "public")],
       "omitted privacy uses the public default and keeps description blank")

    print("missing channel / dead token / auth")
    reset_calls()
    r = post(cid=999, title="Series", privacy="")
    ok(r.status_code == 422, "missing channel + blank privacy is 422")
    ok(_calls["get"] == [], "blank privacy does not probe a missing channel")

    reset_calls()
    r = post(cid=999, title="Series", privacy="public")
    ok(r.status_code == 404, f"missing channel + public privacy is 404 (got {r.status_code})")
    ok(_calls["get"] == [] and _calls["create"] == [],
       "missing channel returns before get_service")

    reset_calls()
    before = channel(1).oauth_status
    ok(before == OAuthStatus.CONNECTED, "ch-a starts CONNECTED")
    youtube.get_service = _boom_get
    r = post(title="Series", privacy="unlisted")
    ok(r.status_code == 400, f"dead token + real privacy is 400 (got {r.status_code})")
    ok("connect the channel first" in r.text, "dead token 400 names the connect error")
    ok(_calls["get"] == ["ch-a"], "dead token still probes the slug")
    ok(_calls["create"] == [], "dead token does not call create_playlist")
    ok(channel(1).oauth_status == OAuthStatus.DISCONNECTED,
       "dead token flips CONNECTED to DISCONNECTED")
    youtube.get_service = _fake_get

    reset_calls()
    n_before = n_playlists()
    r = client.post("/api/channels/1/playlists", json={"title": "Series", "privacy": ""})
    ok(r.status_code == 401, "unauthenticated POST is 401")
    ok(_calls["get"] == [] and _calls["create"] == [],
       "unauthenticated POST does not call YouTube")
    ok(n_playlists() == n_before, "unauthenticated POST writes no playlist")
    ok(channel(2).oauth_status == OAuthStatus.CONNECTED,
       "ch-b stayed CONNECTED")
finally:
    youtube.get_service = _orig_get
    youtube.create_playlist = _orig_create
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


print(f"\nALL {_checks} CHECKS PASSED")
