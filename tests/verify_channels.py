"""Regression checks for PATCH /api/channels/{id} integer budget floors.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_channels.py

#35 stopped the SPA from PATCHing ``Number("x") === NaN`` → JSON ``null``
into ``daily_publish_budget`` / ``daily_render_budget``. The API still
``setattr``s whatever ``exclude_unset`` forwards, and JSON null is set.
Live failure modes (same class as #33 on Settings):

  * budget=null: ``setattr(ch, "daily_publish_budget", None)``. The next
    ``published_today >= channel.daily_publish_budget`` TypeErrors the
    publish tick (``_safe`` logs it; nothing publishes that cycle). The
    render gate ``rendered_today + in_flight >= channel.daily_render_budget``
    TypeErrors the same way. Growth-agent / curl still reach this path.
  * budget negative: the loops treat ``<= 0`` as a skip (legal stall),
    but a typo should 400 rather than silently park the channel.
  * JSON bool: lax ``Optional[int]`` coerces ``false→0`` / ``true→1``
    before the handler. 0 is a legal stall, so ``false`` would silently
    park the channel. Rejected on ``ChannelUpdate`` with ``mode="before"``.

0 remains legal (the stall #27/#28 spent cycles labeling). A 400 mixed
body writes none of the fields. ``default_render_profile_id`` is
legitimately nullable and is not floored.

Uses an in-memory SQLite DB and FastAPI's TestClient (no real manager.db,
no network, lifespan/scheduler never started). Exits non-zero on the
first failed assertion.
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
from app.models import Channel, OAuthStatus
from app.routers import channels as channels_router
from app.services import publish_loop, render_loop

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
ok(Path(channels_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "channels module loaded from this tree")

# Why null is fatal — a `or 0` / `is None` rescue would make the 400 a
# policy choice rather than a stall-preventer. Pin the live comparisons
# so this suite's "must be an int >= 0" stays coupled to the loops.
_pub_src = Path(publish_loop.__file__).read_text()
ok("quota.published_today(session, channel.id) >= channel.daily_publish_budget" in _pub_src,
   "publish_loop still compares published_today >= daily_publish_budget (None TypeErrors)")
ok("published_today(session, channel.id) >= (channel.daily_publish_budget or 0)" not in _pub_src,
   "publish_loop does not rescue a null budget with or 0")

_ren_src = Path(render_loop.__file__).read_text()
ok(">= channel.daily_render_budget" in _ren_src,
   "render_loop still compares against daily_render_budget (None TypeErrors)")
ok(">= (channel.daily_render_budget or 0)" not in _ren_src,
   "render_loop does not rescue a null budget with or 0")

# setattr-then-400 is observationally equivalent today (get_session does
# not commit on HTTPException), but a later auto-commit would persist the
# mixed body. Pin source order so that mutant dies here, not in prod.
_ch_src = Path(channels_router.__file__).read_text()
_req_render = _ch_src.find('_require_int(fields, "daily_render_budget"')
_req_publish = _ch_src.find('_require_int(fields, "daily_publish_budget"')
_setattr = _ch_src.find("for k, v in fields.items():")
ok(0 <= _req_render < _req_publish < _setattr,
   "_require_int for both budgets runs before setattr")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="ch-a", name="A", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5, daily_publish_budget=5))
    s.add(Channel(slug="ch-b", name="B", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=7, daily_publish_budget=7))
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
        "paused": ch.paused,
        "daily_render_budget": ch.daily_render_budget,
        "daily_publish_budget": ch.daily_publish_budget,
        "default_render_profile_id": ch.default_render_profile_id,
        "oauth_status": ch.oauth_status,
    }


def patch(cid: int = 1, **body):
    return client.patch(f"/api/channels/{cid}", auth=auth, json=body)


try:
    print("GET /api/channels/{id}")
    r = client.get("/api/channels/1", auth=auth)
    ok(r.status_code == 200, "GET /api/channels/1 is 200")
    body = r.json()
    ok(body.get("daily_render_budget") == 5, "GET surfaces seeded render budget")
    ok(body.get("daily_publish_budget") == 5, "GET surfaces seeded publish budget")
    ok(body.get("slug") == "ch-a", "GET surfaces the seeded slug")

    r = client.get("/api/channels/999", auth=auth)
    ok(r.status_code == 404, "GET missing channel is 404")

    print("PATCH /api/channels/{id}: valid persist + omitted fields stay put")
    before_b = snapshot(2)
    r = patch(daily_publish_budget=3, paused=True)
    ok(r.status_code == 200, "valid PATCH is 200")
    got = r.json()
    ok(got.get("daily_publish_budget") == 3, "response publish budget is the new value")
    ok(got.get("paused") is True, "response paused is the new value")
    ok(got.get("daily_render_budget") == 5,
       "omitted render budget is not reset (exclude_unset)")
    after = snapshot(1)
    ok(after["daily_publish_budget"] == 3, "publish budget persisted")
    ok(after["paused"] is True, "paused persisted")
    ok(after["daily_render_budget"] == 5, "omitted render budget unchanged in DB")
    ok(snapshot(2) == before_b, "PATCH on ch-a left sibling ch-b untouched")

    r = patch(paused=False)
    ok(r.status_code == 200, "PATCH paused back to false is 200")
    ok(snapshot()["daily_publish_budget"] == 3, "paused-only PATCH left publish budget")

    print("PATCH /api/channels/{id}: 0 is allowed (legal stall)")
    r = patch(daily_publish_budget=0)
    ok(r.status_code == 200, "publish budget=0 is 200 (legal stall)")
    ok(snapshot()["daily_publish_budget"] == 0, "publish budget=0 persisted")
    ok(snapshot()["daily_render_budget"] == 5, "publish=0 PATCH left render budget")

    r = patch(daily_render_budget=0)
    ok(r.status_code == 200, "render budget=0 is 200 (legal stall)")
    ok(snapshot()["daily_render_budget"] == 0, "render budget=0 persisted")
    ok(snapshot()["daily_publish_budget"] == 0, "render=0 PATCH left publish budget")

    r = patch(daily_render_budget=4, daily_publish_budget=2)
    ok(r.status_code == 200, "restore both budgets is 200")
    ok(snapshot()["daily_render_budget"] == 4, "render budget restored to 4")
    ok(snapshot()["daily_publish_budget"] == 2, "publish budget restored to 2")

    print("PATCH /api/channels/{id}: negative / null budgets are 400, write nothing")
    before = snapshot()
    r = patch(daily_publish_budget=-1)
    ok(r.status_code == 400, "publish budget=-1 is 400")
    ok(">= 0" in r.text, "negative-publish 400 names the floor")
    ok(snapshot() == before, "publish budget=-1 writes nothing")

    r = patch(daily_render_budget=-3)
    ok(r.status_code == 400, "render budget=-3 is 400")
    ok(">= 0" in r.text, "negative-render 400 names the floor")
    ok(snapshot() == before, "render budget=-3 writes nothing")

    r = patch(daily_publish_budget=None)
    ok(r.status_code == 400, "publish budget=null is 400 (NaN blur → JSON null)")
    ok(">= 0" in r.text or "null" in r.text.lower(),
       "null-publish 400 names the rejection")
    ok(snapshot() == before, "publish budget=null writes nothing")
    ok(row().daily_publish_budget is not None,
       "null PATCH did not persist SQL NULL into daily_publish_budget")

    r = patch(daily_render_budget=None)
    ok(r.status_code == 400, "render budget=null is 400")
    ok(snapshot() == before, "render budget=null writes nothing")
    ok(row().daily_render_budget is not None,
       "null PATCH did not persist SQL NULL into daily_render_budget")

    print("PATCH /api/channels/{id}: JSON bool is 4xx (false must not coerce to 0)")
    # Pydantic lax Optional[int] coerces false→0 / true→1 BEFORE the handler.
    # 0 is a legal stall, so false would silently park the channel — the same
    # class #35 just stopped the SPA from sending via Number('').
    before = snapshot()
    r = patch(daily_publish_budget=False)
    ok(r.status_code in (400, 422),
       "publish budget=false is 4xx (must not coerce to 0)")
    ok(snapshot() == before, "publish budget=false writes nothing")
    ok(snapshot()["daily_publish_budget"] != 0 or before["daily_publish_budget"] == 0,
       "false did not persist a 0 stall")

    r = patch(daily_publish_budget=True)
    ok(r.status_code in (400, 422),
       "publish budget=true is 4xx (must not coerce to 1)")
    ok(snapshot() == before, "publish budget=true writes nothing")

    r = patch(daily_render_budget=False)
    ok(r.status_code in (400, 422), "render budget=false is 4xx")
    ok(snapshot() == before, "render budget=false writes nothing")

    r = patch(daily_render_budget=True)
    ok(r.status_code in (400, 422), "render budget=true is 4xx")
    ok(snapshot() == before, "render budget=true writes nothing")

    print("PATCH /api/channels/{id}: mixed body cannot smuggle a stall past a 400")
    before = snapshot()
    r = patch(daily_publish_budget=None, paused=True, name="smuggled")
    ok(r.status_code == 400, "mixed body with publish budget=null is 400")
    ok(snapshot() == before,
       "a 400 mixed body writes none of the fields (paused/name stay put)")

    r = patch(daily_render_budget=-1, daily_publish_budget=9, paused=True)
    ok(r.status_code == 400, "mixed body with render budget=-1 is 400")
    ok(snapshot() == before,
       "a 400 mixed body does not persist the valid sibling budget either")

    print("PATCH /api/channels/{id}: nullable profile id is not floored")
    r = patch(default_render_profile_id=None)
    ok(r.status_code == 200, "default_render_profile_id=null is 200 (clear is legal)")
    ok(snapshot()["default_render_profile_id"] is None, "profile id cleared")
    ok(snapshot()["daily_publish_budget"] == 2, "profile-null PATCH left publish budget")

    print("PATCH /api/channels/{id}: empty body + auth + missing")
    before = snapshot()
    r = patch()
    ok(r.status_code == 200, "empty PATCH is 200 (no-op)")
    ok(snapshot() == before, "empty PATCH writes nothing")

    r = client.patch("/api/channels/1", json={"daily_publish_budget": 9})
    ok(r.status_code == 401, "PATCH still requires auth")
    ok(snapshot() == before, "unauthenticated PATCH writes nothing")

    r = client.get("/api/channels/1")
    ok(r.status_code == 401, "GET still requires auth")

    r = patch(999, daily_publish_budget=1)
    ok(r.status_code == 404, "PATCH missing channel is 404")
    ok(snapshot() == before, "404 PATCH writes nothing on the live row")

    ok(snapshot(2) == before_b, "ch-b still at seeded 7/7 after every ch-a probe")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


print(f"\nALL {_checks} CHECKS PASSED")
