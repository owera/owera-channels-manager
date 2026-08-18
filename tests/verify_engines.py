"""Dependency-free regression checks for app/services/engines/__init__.py
(backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_engines.py

``resolve_engine`` / ``get_engine`` are the choke point render_loop uses to
pick MPT vs HyperFrames for every submit (and chapters uses get_engine to
find the job dir). Previously only ``get_engine("hyperframes")`` was pinned
(via verify_hyperframes); the video → topic → channel profile walk and the
unknown-name fallback had zero direct coverage. A crossed wire here sends
every render to the wrong adapter.

Covers, dependency-free (in-memory SQLite, no network, no MPT, no
HyperFrames CLI):
  - module contracts: DEFAULT_ENGINE, STATE_* identity with base, exact
    engine_names set + insertion order
  - get_engine: None/"" → default singleton, named adapters, unknown /
    wrong-case names fall back to the SAME default singleton
  - resolve_engine precedence: video > topic > channel > DEFAULT_ENGINE,
    with all three layers populated so a swapped walk fails
  - empty-engine and missing-profile rows skip to the NEXT layer (not
    straight to default)
  - topic=None / channel=None do not AttributeError
  - pid=0 is treated as missing (the ``if not pid`` gate)
  - resolve_engine returns a profile's engine string as-is (including a
    typo); get_engine is the fallback, not resolve_engine

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, RenderProfile, Topic, Video
from app.services.engines import (
    DEFAULT_ENGINE,
    engine_names,
    get_engine,
    resolve_engine,
)
from app.services.engines import base as base_mod
from app.services.engines.hyperframes import HyperFramesEngine
from app.services.engines.mpt import MPTEngine

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh_session() -> Session:
    """A private in-memory DB per case, so rows can't leak across checks."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def make_profile(session, *, name, engine="mpt", profile_id=None) -> RenderProfile:
    kw = {"name": name, "engine": engine}
    if profile_id is not None:
        kw["id"] = profile_id
    p = RenderProfile(**kw)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def make_channel(session, **kw) -> Channel:
    ch = Channel(
        slug=kw.pop("slug", "ch-eng"),
        name=kw.pop("name", "Eng Test"),
        **kw,
    )
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_topic(session, channel, **kw) -> Topic:
    t = Topic(
        channel_id=channel.id,
        name=kw.pop("name", "Topic"),
        **kw,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def make_video(session, channel, topic, **kw) -> Video:
    v = Video(
        channel_id=channel.id,
        topic_id=topic.id,
        subject=kw.pop("subject", "subj"),
        **kw,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------
print("module contracts: DEFAULT_ENGINE, STATE_*, engine_names, adapters")

ok(DEFAULT_ENGINE == "mpt",
   "DEFAULT_ENGINE is 'mpt' (the pre-abstraction render path)")
ok(base_mod.STATE_FAILED == -1
   and base_mod.STATE_COMPLETE == 1
   and base_mod.STATE_PROCESSING == 4,
   "base STATE_* stay pinned to MPT's values (render_loop compares on these)")

from app.services.engines import STATE_COMPLETE, STATE_FAILED, STATE_PROCESSING
ok(STATE_FAILED is base_mod.STATE_FAILED
   and STATE_COMPLETE is base_mod.STATE_COMPLETE
   and STATE_PROCESSING is base_mod.STATE_PROCESSING,
   "package re-exports STATE_* from base (same objects, not copies)")

names = engine_names()
ok(names == ["mpt", "hyperframes"],
   "engine_names is exactly [mpt, hyperframes] in registry insertion order")
ok(DEFAULT_ENGINE in names,
   "DEFAULT_ENGINE is a registered name (get_engine fallback cannot KeyError)")

ok(MPTEngine.name == "mpt", "MPTEngine.name is the registry key 'mpt'")
ok(HyperFramesEngine.name == "hyperframes",
   "HyperFramesEngine.name is the registry key 'hyperframes'")

# Wiring: render_loop binds the names at import. A suite that only drives
# engines.resolve_engine would stay green if render_loop reimplemented a
# private walk — pin the identity the submit path actually calls.
from app.services import render_loop
ok(render_loop.resolve_engine is resolve_engine,
   "render_loop.resolve_engine IS engines.resolve_engine (submit walk)")
ok(render_loop.get_engine is get_engine,
   "render_loop.get_engine IS engines.get_engine (submit + in-flight poll)")


# ---------------------------------------------------------------------------
# get_engine — named lookup + silent fallback (the safety net chapters and
# render_loop both rely on when video.engine is None or a typo)
# ---------------------------------------------------------------------------
print("get_engine: named adapters, None/empty default, unknown-name fallback")

mpt = get_engine("mpt")
hf = get_engine("hyperframes")
ok(type(mpt) is MPTEngine, "get_engine('mpt') returns MPTEngine")
ok(type(hf) is HyperFramesEngine, "get_engine('hyperframes') returns HyperFramesEngine")
ok(mpt is get_engine("mpt"),
   "get_engine('mpt') is the registry singleton (not a fresh instance)")
ok(hf is get_engine("hyperframes"),
   "get_engine('hyperframes') is the registry singleton")
ok(mpt is not hf, "mpt and hyperframes are distinct adapters")

default = get_engine(None)
ok(default is mpt, "get_engine(None) is the mpt singleton")
ok(get_engine("") is mpt, "get_engine('') is the mpt singleton (falsy → default)")
ok(get_engine(DEFAULT_ENGINE) is mpt,
   "get_engine(DEFAULT_ENGINE) is the mpt singleton")

# Unknown / wrong-case names must fall back to the SAME default singleton —
# a KeyError here would crash render_loop._submit_new / chapters lookup, and
# a fresh MPTEngine() would desync any adapter-level state.
try:
    unknown = get_engine("nope")
except Exception as e:  # KeyError if the fallback `.get(..., default)` is dropped
    ok(False, f"unknown name falls back to the mpt singleton (raised {type(e).__name__})")
else:
    ok(unknown is mpt, "unknown name falls back to the mpt singleton")
ok(get_engine("MPT") is mpt,
   "wrong-case 'MPT' falls back to default (names are case-sensitive)")
ok(get_engine("HyperFrames") is mpt,
   "wrong-case 'HyperFrames' falls back to default, not the hf adapter")
ok(get_engine("hyperframe") is mpt,
   "near-miss typo 'hyperframe' falls back to default (not a prefix match)")


# ---------------------------------------------------------------------------
# resolve_engine — video > topic > channel > DEFAULT
# All three layers populated so a swapped walk (topic-first, channel-first)
# cannot hide behind a missing upper layer.
# ---------------------------------------------------------------------------
print("resolve_engine: video > topic > channel > default")

s = fresh_session()
# Each layer is a DISTINCT name, and the winner is NOT DEFAULT_ENGINE — an
# always-default mutant survived the first cut when video was also "mpt".
p_video = make_profile(s, name="v-hf", engine="hyperframes")
p_topic = make_profile(s, name="t-named", engine="from-topic")
p_chan = make_profile(s, name="c-named", engine="from-channel")
ok("hyperframes" != DEFAULT_ENGINE
   and "from-topic" != DEFAULT_ENGINE
   and "from-channel" != DEFAULT_ENGINE,
   "precondition: every precedence layer is a distinct non-default name")
ch = make_channel(s, slug="prec", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch, render_profile_id=p_topic.id)
video = make_video(s, ch, topic, render_profile_id=p_video.id)
ok(resolve_engine(s, video, topic, ch) == "hyperframes",
   "video profile wins even when topic=from-topic and channel=from-channel")

# Drop only the video profile → topic must win (not channel, not default).
video.render_profile_id = None
s.add(video)
s.commit()
s.refresh(video)
ok(resolve_engine(s, video, topic, ch) == "from-topic",
   "topic profile wins when video has no profile (channel from-channel ignored)")

# Drop the topic profile too → channel must win (not default, not a leftover).
topic.render_profile_id = None
s.add(topic)
s.commit()
s.refresh(topic)
ok(resolve_engine(s, video, topic, ch) == "from-channel",
   "channel profile wins when video and topic are unbound")

# Unbind the channel too → DEFAULT_ENGINE, not a leftover string.
ch.default_render_profile_id = None
s.add(ch)
s.commit()
s.refresh(ch)
ok(resolve_engine(s, video, topic, ch) == DEFAULT_ENGINE,
   "no bound profile anywhere → DEFAULT_ENGINE (not a stale layer)")


# ---------------------------------------------------------------------------
# Empty engine / missing profile skip to the NEXT layer
# (a skip-to-default mutant would fail these: the lower layer is a distinct
# name that is not DEFAULT_ENGINE)
# ---------------------------------------------------------------------------
print("resolve_engine: empty engine and missing profile skip to next layer")

s = fresh_session()
p_empty = make_profile(s, name="empty", engine="")
p_next = make_profile(s, name="next-hf", engine="hyperframes")
ch = make_channel(s, slug="empty-eng", default_render_profile_id=p_next.id)
topic = make_topic(s, ch, render_profile_id=p_empty.id)
video = make_video(s, ch, topic)
ok(resolve_engine(s, video, topic, ch) == "hyperframes",
   "topic engine='' skips to the channel profile (not DEFAULT, not '')")

# Video empty-engine, topic hyperframes: skip video, land on topic.
s = fresh_session()
p_empty = make_profile(s, name="v-empty", engine="")
p_topic = make_profile(s, name="t-hf", engine="hyperframes")
p_chan = make_profile(s, name="c-mpt", engine="mpt")
ch = make_channel(s, slug="v-empty", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch, render_profile_id=p_topic.id)
video = make_video(s, ch, topic, render_profile_id=p_empty.id)
ok(resolve_engine(s, video, topic, ch) == "hyperframes",
   "video engine='' skips to topic hyperframes (not channel mpt, not default)")

# Missing profile row (id set, session.get returns None) skips to next.
s = fresh_session()
p_chan = make_profile(s, name="c-hf", engine="hyperframes")
ch = make_channel(s, slug="ghost", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch)
video = make_video(s, ch, topic, render_profile_id=4242)
ok(resolve_engine(s, video, topic, ch) == "hyperframes",
   "video profile id with no row skips to channel hyperframes")

# Topic missing row, channel bound.
s = fresh_session()
p_chan = make_profile(s, name="c-typo", engine="custom")
ch = make_channel(s, slug="ghost-t", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch, render_profile_id=7777)
video = make_video(s, ch, topic)
ok(resolve_engine(s, video, topic, ch) == "custom",
   "topic profile id with no row skips to channel (string returned as-is)")


# ---------------------------------------------------------------------------
# None topic / None channel must not AttributeError
# ---------------------------------------------------------------------------
print("resolve_engine: None topic/channel")

s = fresh_session()
p_video = make_profile(s, name="solo-v", engine="hyperframes")
p_chan = make_profile(s, name="solo-c", engine="from-channel-none")
ch = make_channel(s, slug="none-t", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch)
video = make_video(s, ch, topic, render_profile_id=p_video.id)
try:
    none_topic = resolve_engine(s, video, None, ch)
except Exception as e:  # AttributeError if `topic else None` is dropped
    ok(False, f"topic=None still honors the video profile (raised {type(e).__name__})")
else:
    ok(none_topic == "hyperframes",
       "topic=None still honors the video profile")

# No video profile, topic=None → channel.
video.render_profile_id = None
s.add(video)
s.commit()
s.refresh(video)
ok(resolve_engine(s, video, None, ch) == "from-channel-none",
   "topic=None + unbound video → channel profile")

ok(resolve_engine(s, video, None, None) == DEFAULT_ENGINE,
   "topic=None and channel=None + unbound video → DEFAULT_ENGINE")

# Video bound, both parents None.
video.render_profile_id = p_video.id
s.add(video)
s.commit()
s.refresh(video)
ok(resolve_engine(s, video, None, None) == "hyperframes",
   "video profile still wins with topic=None and channel=None")


# ---------------------------------------------------------------------------
# pid=0 is missing (``if not pid``). Discriminates `if pid is None`.
# SQLite will store an explicit id=0.
# ---------------------------------------------------------------------------
print("resolve_engine: pid=0 is treated as missing")

s = fresh_session()
p_zero = make_profile(s, name="zero", engine="hyperframes", profile_id=0)
ok(p_zero.id == 0, "precondition: stored a RenderProfile with id=0")
p_chan = make_profile(s, name="c-custom", engine="custom")
ch = make_channel(s, slug="zero-pid", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch)
# Use a real Video row whose render_profile_id is 0.
video = make_video(s, ch, topic, render_profile_id=0)
ok(video.render_profile_id == 0, "precondition: video.render_profile_id is 0")
ok(resolve_engine(s, video, topic, ch) == "custom",
   "render_profile_id=0 is treated as unbound (does NOT load the id=0 profile)")

# Same gate on the topic slot: topic.render_profile_id=0 must not win.
s = fresh_session()
p_zero = make_profile(s, name="zero-t", engine="hyperframes", profile_id=0)
p_chan = make_profile(s, name="c-mpt", engine="from-channel")
ch = make_channel(s, slug="zero-tid", default_render_profile_id=p_chan.id)
topic = make_topic(s, ch, render_profile_id=0)
video = make_video(s, ch, topic)
ok(resolve_engine(s, video, topic, ch) == "from-channel",
   "topic.render_profile_id=0 is treated as unbound (channel wins)")


# ---------------------------------------------------------------------------
# resolve_engine returns the profile engine string as-is.
# get_engine (not resolve_engine) is the unknown-name fallback.
# A resolve_engine that coerced unknown names to DEFAULT would hide a typo
# from the operator AND break this pin; chapters then looks up video.engine
# via get_engine, which is where the fallback belongs.
# ---------------------------------------------------------------------------
print("resolve_engine: engine string returned as-is (typo is the caller's)")

s = fresh_session()
p_typo = make_profile(s, name="typo", engine="hyperframe")  # missing s
ch = make_channel(s, slug="typo")
topic = make_topic(s, ch, render_profile_id=p_typo.id)
video = make_video(s, ch, topic)
resolved = resolve_engine(s, video, topic, ch)
ok(resolved == "hyperframe",
   "resolve_engine returns the typo as-is (does not coerce to DEFAULT)")
ok(get_engine(resolved) is get_engine("mpt"),
   "get_engine then falls the typo back to the mpt singleton")


# ---------------------------------------------------------------------------
# Cross-row isolation: another video's / channel's profile must not leak.
# ---------------------------------------------------------------------------
print("resolve_engine: per-video isolation")

s = fresh_session()
p_a = make_profile(s, name="a-hf", engine="hyperframes")
p_b = make_profile(s, name="b-named", engine="from-b")
ch_a = make_channel(s, slug="iso-a", default_render_profile_id=p_a.id)
ch_b = make_channel(s, slug="iso-b", default_render_profile_id=p_b.id)
t_a = make_topic(s, ch_a)
t_b = make_topic(s, ch_b)
v_a = make_video(s, ch_a, t_a)
v_b = make_video(s, ch_b, t_b)
ok(resolve_engine(s, v_a, t_a, ch_a) == "hyperframes",
   "channel A default is hyperframes")
ok(resolve_engine(s, v_b, t_b, ch_b) == "from-b",
   "channel B default is from-b (A's profile does not leak)")

# Passing the wrong channel object with the right video must still walk
# THAT channel's default (the function does not look up video.channel_id).
ok(resolve_engine(s, v_a, t_a, ch_b) == "from-b",
   "the channel argument is used as given (no implicit video.channel_id lookup)")


# ---------------------------------------------------------------------------
# SimpleNamespace stand-in: render_loop can theoretically pass any object
# with the three FK attributes. Pin that we only read those attributes.
# ---------------------------------------------------------------------------
print("resolve_engine: attribute surface is the three profile FKs")

s = fresh_session()
p = make_profile(s, name="ns", engine="hyperframes")
fake_video = SimpleNamespace(render_profile_id=p.id)
fake_topic = SimpleNamespace(render_profile_id=None)
fake_channel = SimpleNamespace(default_render_profile_id=None)
ok(resolve_engine(s, fake_video, fake_topic, fake_channel) == "hyperframes",
   "only render_profile_id / default_render_profile_id are read")


print()
print(f"ALL {_checks} CHECKS PASSED")
