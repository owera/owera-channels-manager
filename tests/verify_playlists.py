"""Regression checks for playlist routes' workspace isolation.

PR #196 scoped remaining topic routes through require_channel /
require_topic. playlists.py was the leftover: `_require_channel` is a
bare `session.get(Channel)`, so a tenant who knows another workspace's
channel id can LIST its playlists, POST /sync (YouTube list + 50-unit
creates on a miss, and mark_dead on a dead token), and POST create
(live playlists.insert on that tenant's YouTube). These checks pin
every route to the current workspace (404, no write, no YouTube call)
while own-workspace traffic still lands.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, no live YouTube). youtube.get_service / list_playlists /
create_playlist are stubbed and recorded. Exits non-zero on the first
failed assertion.
"""
import inspect
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Playlist, Workspace
from app.routers import playlists as playlists_router
from app.services import youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(playlists_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "playlists module loaded from this tree")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(slug="tenant-b", name="Tenant B"))
    s.commit()
    ws_b = s.exec(select(Workspace).where(Workspace.slug == "tenant-b")).one()
    s.add(Channel(slug="a-ch", name="A", workspace_id=1,
                  oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Playlist(channel_id=1, yt_playlist_id="PLowera00000000000000000001",
                   title="Owera Series", description="keep", privacy="public"))
    s.add(Channel(slug="b-ch", name="B", workspace_id=ws_b.id,
                  oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    ch_b = s.exec(select(Channel).where(Channel.slug == "b-ch")).one()
    CHANNEL_B_ID = ch_b.id


def _override_session():
    with Session(engine) as session:
        yield session


def n_playlists(channel_id: int) -> int:
    with Session(engine) as s:
        return len(s.exec(select(Playlist).where(Playlist.channel_id == channel_id)).all())


def playlist_titles(channel_id: int) -> list[str]:
    with Session(engine) as s:
        return [p.title for p in s.exec(
            select(Playlist).where(Playlist.channel_id == channel_id)).all()]


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
_orig_mw = settings.multi_workspace
settings.app_password = "testpw"
settings.multi_workspace = True
client = TestClient(main.app)
auth_ow = ("owera", "testpw")
auth_b = ("tenant-b", "testpw")

yt_calls: list[tuple] = []
_orig_get = youtube.get_service
_orig_list = youtube.list_playlists
_orig_create = youtube.create_playlist


def fake_get_service(slug, channel=None, session=None):
    yt_calls.append(("get_service", slug))
    return object()


def fake_list_playlists(service):
    yt_calls.append(("list_playlists",))
    return [{
        "yt_playlist_id": "PLremote00000000000000000001",
        "title": "Remote Hijack",
        "description": "from youtube",
        "privacy": "public",
    }]


def fake_create_playlist(service, title, description="", privacy="public"):
    yt_calls.append(("create_playlist", title))
    return {
        "yt_playlist_id": "PLcreated0000000000000000001",
        "title": title,
        "description": description,
        "privacy": privacy,
    }


youtube.get_service = fake_get_service
youtube.list_playlists = fake_list_playlists
youtube.create_playlist = fake_create_playlist

try:
    print("workspace isolation: playlist routes follow require_channel")

    r = client.get("/api/channels/1/playlists", auth=auth_b)
    ok(r.status_code == 404 and "channel not found" in r.text,
       "tenant-b LIST owera channel → 404 channel not found "
       f"({r.status_code} {r.text[:80]})")
    ok("Owera Series" not in r.text,
       "tenant-b LIST does not leak owera playlist titles")

    n_before = n_playlists(1)
    n_calls = len(yt_calls)
    r = client.post("/api/channels/1/playlists/sync", auth=auth_b)
    ok(r.status_code == 404 and "channel not found" in r.text,
       "tenant-b SYNC owera channel → 404")
    ok(n_playlists(1) == n_before
       and "Remote Hijack" not in playlist_titles(1),
       "tenant-b SYNC writes no playlist on owera")
    ok(len(yt_calls) == n_calls,
       "tenant-b SYNC never called get_service / list_playlists")

    n_before = n_playlists(1)
    n_calls = len(yt_calls)
    r = client.post("/api/channels/1/playlists", auth=auth_b,
                    json={"title": "CrossTenantMint", "description": "x"})
    ok(r.status_code == 404 and "channel not found" in r.text,
       "tenant-b CREATE on owera channel → 404")
    ok(n_playlists(1) == n_before
       and "CrossTenantMint" not in playlist_titles(1),
       "tenant-b CREATE writes no playlist on owera")
    ok(len(yt_calls) == n_calls,
       "tenant-b CREATE never called get_service / create_playlist")

    r = client.get("/api/channels/1/playlists", auth=auth_ow)
    ok(r.status_code == 200
       and any(p.get("title") == "Owera Series" for p in r.json()),
       "owera LIST own channel still 200")

    r = client.get(f"/api/channels/{CHANNEL_B_ID}/playlists", auth=auth_b)
    ok(r.status_code == 200 and r.json() == [],
       "tenant-b LIST own empty channel still 200")

    n_b_before = n_playlists(CHANNEL_B_ID)
    r = client.post(f"/api/channels/{CHANNEL_B_ID}/playlists", auth=auth_b,
                    json={"title": "TB Series", "description": "own"})
    ok(r.status_code == 201 and r.json().get("title") == "TB Series",
       f"tenant-b CREATE own playlist still 201 ({r.status_code} {r.text[:80]})")
    ok(n_playlists(CHANNEL_B_ID) == n_b_before + 1,
       "tenant-b CREATE persists on its own channel")
    ok(("create_playlist", "TB Series") in yt_calls,
       "own CREATE still reaches youtube.create_playlist")

    n_calls = len(yt_calls)
    r = client.post(f"/api/channels/{CHANNEL_B_ID}/playlists/sync", auth=auth_b)
    ok(r.status_code == 200,
       f"tenant-b SYNC own channel still 200 ({r.status_code} {r.text[:80]})")
    ok("Remote Hijack" in playlist_titles(CHANNEL_B_ID),
       "own SYNC still upserts the stubbed remote playlist")
    ok(len(yt_calls) > n_calls and ("list_playlists",) in yt_calls,
       "own SYNC still reaches youtube.list_playlists "
       "(an always-404 on sync would hide behind the foreign 404 pin)")

    r = client.get("/api/channels/99999/playlists", auth=auth_ow)
    ok(r.status_code == 404 and "channel not found" in r.text,
       "missing channel LIST is 404")

    r = client.get("/api/channels/1/playlists")
    ok(r.status_code == 401, "playlist LIST still requires auth")

    src_list = inspect.getsource(playlists_router.list_local)
    src_sync = inspect.getsource(playlists_router.sync)
    src_create = inspect.getsource(playlists_router.create)
    ok("require_channel" in src_list and "current_workspace" in src_list,
       "list_local gates through require_channel + current_workspace")
    ok("require_channel" in src_sync and "current_workspace" in src_sync,
       "sync gates through require_channel + current_workspace "
       "(session.get alone is the cross-tenant leak)")
    ok("require_channel" in src_create and "current_workspace" in src_create,
       "create gates through require_channel + current_workspace")
    ok("_require_channel" not in src_list
       and "_require_channel" not in src_sync
       and "_require_channel" not in src_create,
       "local session.get _require_channel is gone "
       "(a wrapper that ignores workspace is the pre-fix leak)")
finally:
    youtube.get_service = _orig_get
    youtube.list_playlists = _orig_list
    youtube.create_playlist = _orig_create
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw
    settings.multi_workspace = _orig_mw

print(f"ALL {_checks} CHECKS PASSED")
