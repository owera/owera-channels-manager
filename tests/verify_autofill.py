"""Dependency-free regression checks for autofill_loop.tick().

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_autofill.py

Pins the two 07-26 defects: (1) topics were iterated in id order, so low-id
anchor topics drained the shared per-channel board horizon before high-weight
winners were reached (t2/t5 filled both boards while the w3 short topics sat at
0 pending); (2) the idea generator can return more titles than asked and
_refill_topic added them all, overshooting board space (t2: asked 8, got 15).

Uses an in-memory SQLite DB — no network, no creds. Exits non-zero on the first
failed assertion.
"""
import sys
from contextlib import contextmanager

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

# Importing app.models defines every table=True model, registering them all on
# SQLModel.metadata so create_all() below builds the full schema.
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import autofill_loop

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh_session() -> Session:
    """A private in-memory DB per test, so cases can't leak into each other."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-test"), name=kw.pop("name", "Test"),
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


class _Cfg:
    """Stand-in for app_settings(): only the fields tick() reads."""
    def __init__(self, target=5, horizon=1):
        self.topic_autogen_enabled = True
        self.topic_autogen_min_pending = 1
        self.topic_autogen_target = target
        self.board_horizon_days = horizon


def run_tick(session, cfg, ideas_fn):
    """tick() with its module deps pointed at the in-memory world."""
    @contextmanager
    def scope():
        yield session

    autofill_loop.session_scope = scope
    autofill_loop.app_settings = lambda s: cfg
    autofill_loop.video_gen.generate_ideas = ideas_fn
    autofill_loop.video_gen.channel_language = lambda s, cid: "English"
    autofill_loop.tick()


def drafts_for(session, topic_id):
    return session.exec(
        select(func.count(Video.id)).where(
            Video.topic_id == topic_id, Video.status == VideoStatus.DRAFT)
    ).one()


def exact(n_asked, **_kw):
    return [f"idea {i}" for i in range(n_asked)]


def well_behaved(topic_name, theme, existing, n, fmt, language=None):
    return [f"{topic_name} idea {i}" for i in range(n)]


def overshooting(topic_name, theme, existing, n, fmt, language=None):
    return [f"{topic_name} idea {i}" for i in range(n + 7)]


print("case: high-weight short claims most board space but long keeps 1 reserved slot")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)  # board cap 3 with horizon 1
anchor = make_topic(s, ch, name="anchor-long", weight=2, content_format="long")
wedge = make_topic(s, ch, name="wedge-short", weight=3, content_format="short")
ok(anchor.id < wedge.id, "fixture: anchor has the lower id (the old iteration winner)")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, wedge.id) == 2, "w3 wedge fills short-cap (board-1) first")
ok(drafts_for(s, anchor.id) == 1, "w2 long anchor claims the reserved long slot")

print("case: remaining board space flows down to the next topic by weight")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)  # board cap 5
anchor = make_topic(s, ch, name="anchor-long", weight=2, content_format="long")
wedge = make_topic(s, ch, name="wedge-short", weight=3, content_format="short")
run_tick(s, _Cfg(target=1, horizon=1), well_behaved)  # ceiling = weight * 1
ok(drafts_for(s, wedge.id) == 3, "wedge topped up to its own ceiling (1x3)")
ok(drafts_for(s, anchor.id) == 1, "long mix-cap is 1/horizon-day (not the leftover 2)")

print("case: an overshooting generator cannot push a topic past its ask")
s = fresh_session()
ch = make_channel(s, daily_render_budget=4)  # board cap 4
t = make_topic(s, ch, name="solo", weight=1, content_format="long")
run_tick(s, _Cfg(target=5, horizon=1), overshooting)  # asked 4 (board), returns 11
ok(drafts_for(s, t.id) == 4, "refill clamped to the asked batch (board space respected)")

print("case: weight-0 topics stay parked regardless of ordering")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
parked = make_topic(s, ch, name="parked", weight=0, content_format="short")
live = make_topic(s, ch, name="live", weight=1, content_format="short")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, parked.id) == 0, "weight-0 topic got no ideas")
ok(drafts_for(s, live.id) == 5, "weight-1 topic refilled to target")

print("case: NULL weight sorts as weight 1 and still refills")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
nullw = make_topic(s, ch, name="null-weight", weight=None, content_format="short")
heavy = make_topic(s, ch, name="heavy", weight=2, content_format="short")
run_tick(s, _Cfg(target=2, horizon=1), well_behaved)
ok(drafts_for(s, heavy.id) == 4, "w2 topic refilled first (2x2)")
ok(drafts_for(s, nullw.id) == 1, "NULL-weight topic treated as w1, got the remainder")

print("case: board full of shorts still seeds one long draft (overshoot reserve)")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)  # board cap 3
anchor = make_topic(s, ch, name="anchor-long", weight=2, content_format="long")
wedge = make_topic(s, ch, name="wedge-short", weight=4, content_format="short")
# Pre-fill board with 3 short drafts so channel_pending == board_cap and long=0.
for i in range(3):
    s.add(Video(channel_id=ch.id, topic_id=wedge.id, subject=f"pre-short {i}",
                status=VideoStatus.DRAFT, position=i + 1))
s.commit()
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, wedge.id) == 3, "shorts stay at the pre-filled board cap")
ok(drafts_for(s, anchor.id) == 1, "empty long bench gets exactly one reserved seed (+1 overshoot)")

print("case: equal-weight long walking first seeds 1, not the whole open board")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)  # board cap 5
anchor = make_topic(s, ch, name="anchor-long", weight=4, content_format="long")
wedge = make_topic(s, ch, name="wedge-short", weight=4, content_format="short")
ok(anchor.id < wedge.id, "fixture: long has the lower id (walks first at equal weight)")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, anchor.id) == 1, "empty long bench seeds exactly 1 while shorts are hungry")
ok(drafts_for(s, wedge.id) == 4, "shorts claim the remaining open board slots")

print("case: 08-13 shape — 5 queued shorts, 0 long, horizon 2, equal w4 — longs cap at 2")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)  # board cap 10
anchor = make_topic(s, ch, name="anchor-long", weight=4, content_format="long")
wedge = make_topic(s, ch, name="wedge-short", weight=4, content_format="short")
ok(anchor.id < wedge.id, "fixture: long walks first at equal weight")
for i in range(5):
    s.add(Video(channel_id=ch.id, topic_id=wedge.id, subject=f"queued-short {i}",
                status=VideoStatus.QUEUED, position=i + 1))
s.commit()
run_tick(s, _Cfg(target=5, horizon=2), well_behaved)
ok(drafts_for(s, anchor.id) == 2, "long mix-cap = horizon_days (2), not the 5 open slots")
ok(drafts_for(s, wedge.id) == 0, "queued shorts already at trigger — no extra short drafts")

print("case: long reserve does not apply when a long draft already exists")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)
anchor = make_topic(s, ch, name="anchor-long", weight=1, content_format="long")
wedge = make_topic(s, ch, name="wedge-short", weight=4, content_format="short")
s.add(Video(channel_id=ch.id, topic_id=anchor.id, subject="existing long",
            status=VideoStatus.DRAFT, position=1))
s.commit()
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, anchor.id) == 1, "existing long draft counts — no extra long forced")
ok(drafts_for(s, wedge.id) == 2, "shorts fill remaining board slots (cap 3 − 1 long)")

print(f"all {_checks} checks passed")
