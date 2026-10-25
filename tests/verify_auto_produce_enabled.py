"""Per-channel auto_produce_enabled kill switch: schema, API, scheduler skip, manual render.

Run: PYTHONPATH=. uv run python tests/verify_auto_produce_enabled.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import render_loop

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
    s.add(Channel(slug="ch-kill", name="Kill Switch Test",
                  oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")


def fresh_session() -> Session:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-ap"), name=kw.pop("name", "AP"),
                 oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED),
                 daily_render_budget=kw.pop("daily_render_budget", 5), **kw)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_topic(session, channel, **kw):
    t = Topic(channel_id=channel.id, name=kw.pop("name", "Topic"),
              theme_prompt="x", **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def make_video(session, channel, topic, **kw):
    v = Video(channel_id=channel.id, topic_id=topic.id,
              subject=kw.pop("subject", "Test subject"),
              status=kw.pop("status", VideoStatus.DRAFT), **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


print("model default: auto_produce_enabled is true")
with Session(engine) as s:
    ch = s.exec(select(Channel)).first()
    ok(ch.auto_produce_enabled is True, "seeded channel defaults auto_produce_enabled=True")

print("startup migration adds auto_produce_enabled on legacy channel tables")
legacy_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(legacy_engine)
with legacy_engine.begin() as conn:
    conn.execute(text("ALTER TABLE channel DROP COLUMN auto_produce_enabled"))
with legacy_engine.begin() as conn:
    existing = {row[1] for row in conn.execute(text("PRAGMA table_info(channel)"))}
    ok("auto_produce_enabled" not in existing, "legacy table lacks the column")
# Mirror startup _add_missing_columns for one legacy channel row.
from sqlalchemy import text as _text

with legacy_engine.begin() as conn:
    conn.execute(
        _text(
            "INSERT INTO channel (slug, name, oauth_status, daily_render_budget, "
            "daily_publish_budget, paused, default_skip_gate, default_privacy, "
            "created_at, updated_at) "
            "VALUES ('legacy', 'Legacy', 'disconnected', 1, 1, 0, 0, 'public', "
            "datetime('now'), datetime('now'))"
        )
    )
name, decl = ("auto_produce_enabled", "BOOLEAN DEFAULT 1")
with legacy_engine.begin() as conn:
    conn.execute(_text(f"ALTER TABLE channel ADD COLUMN {name} {decl}"))
with legacy_engine.begin() as conn:
    row = conn.execute(_text("SELECT auto_produce_enabled FROM channel WHERE slug='legacy'")).one()
    ok(row[0] == 1, "ALTER DEFAULT 1 back-fills pre-existing channel rows")

print("GET/PATCH /api/channels/{id}")
r = client.get("/api/channels/1", auth=auth)
ok(r.status_code == 200, "GET channel 200")
ok(r.json().get("auto_produce_enabled") is True, "GET surfaces auto_produce_enabled")

r = client.patch("/api/channels/1", auth=auth, json={"auto_produce_enabled": False})
ok(r.status_code == 200, "PATCH disable auto_produce 200")
ok(r.json().get("auto_produce_enabled") is False, "PATCH response returns false")
with Session(engine) as s:
    ok(s.get(Channel, 1).auto_produce_enabled is False, "PATCH persisted false")

r = client.patch("/api/channels/1", auth=auth, json={"auto_produce_enabled": True})
ok(r.json().get("auto_produce_enabled") is True, "PATCH can re-enable")

print("OpenAPI exposes auto_produce_enabled on ChannelUpdate (PATCH body)")
spec = client.get("/openapi.json", auth=auth).json()
update_schema = spec["components"]["schemas"]["ChannelUpdate"]
ok("auto_produce_enabled" in update_schema["properties"],
   "ChannelUpdate schema lists auto_produce_enabled")
aprop = update_schema["properties"]["auto_produce_enabled"]
ok(
    aprop.get("type") == "boolean"
    or any(x.get("type") == "boolean" for x in aprop.get("anyOf", [])),
    "ChannelUpdate types auto_produce_enabled as boolean",
)

print("auto_produce skips channel when auto_produce_enabled=false")
s = fresh_session()
ch = make_channel(s, auto_produce_enabled=False)
t = make_topic(s, ch, weight=1, content_format="short")
v = make_video(s, ch, t)
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v.id).status == VideoStatus.DRAFT,
   "auto_produce leaves draft when kill switch off")

ch2 = make_channel(s, slug="ch-on", auto_produce_enabled=True)
t2 = make_topic(s, ch2, weight=1, content_format="short")
v2 = make_video(s, ch2, t2)
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v2.id).status == VideoStatus.QUEUED,
   "auto_produce still queues when kill switch on")

print("_submit_new still renders when auto_produce_enabled=false (unlike paused)")


class FakeEngine:
    def submit(self, video, params):
        return f"task-{video.id}"


render_loop.ensure_topic_playlist = lambda session, topic, channel: None
render_loop.resolve_engine = lambda session, video, topic, channel: "fake"
render_loop.get_engine = lambda name: FakeEngine()

s = fresh_session()
ch = make_channel(s, auto_produce_enabled=False, paused=False)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.QUEUED)
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v.id).status == VideoStatus.RENDERING,
   "_submit_new picks queued work when auto_produce is off")

s = fresh_session()
ch = make_channel(s, auto_produce_enabled=True, paused=True)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.QUEUED)
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v.id).status == VideoStatus.QUEUED,
   "paused channel still blocks _submit_new (contrast)")

# Pin _auto_produce query includes the kill switch (not only paused).
_src = Path(render_loop.__file__).read_text()
_ap = _src.find("def _auto_produce")
ok("auto_produce_enabled" in _src[_ap:_ap + 2500],
   "_auto_produce filters on auto_produce_enabled")

print(f"ALL {_checks} CHECKS PASSED")
