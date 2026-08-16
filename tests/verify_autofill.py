"""Dependency-free regression checks for autofill_loop (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_autofill.py

Pins the two 07-26 defects: (1) topics were iterated in id order, so low-id
anchor topics drained the shared per-channel board horizon before high-weight
winners were reached (t2/t5 filled both boards while the w3 short topics sat at
0 pending); (2) the idea generator can return more titles than asked and
_refill_topic added them all, overshooting board space (t2: asked 8, got 15).

Plus the 08-12 long-draft reserve (high-weight shorts cannot starve the 1L
publish) and the 08-13 long mix-cap (a high-weight long walking first cannot
consume every leftover seat).

This cycle extends that incident suite with the remaining tick / helper
branches: autogen off, threshold/target floors, weight-4 multiplier cap,
horizon=0 no-cap path, generate_ideas exception/empty, JobRun + kwarg
forwarding, QUEUED-counts-as-pending vs terminal statuses, inactive / parked
topics, long-only (no mix cap) and short-only (no reserve), two-long mix-cap
increment, and per-channel isolation.

Uses an in-memory SQLite DB — no network, no creds. Exits non-zero on the first
failed assertion.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

# Importing app.models defines every table=True model, registering them all on
# SQLModel.metadata so create_all() below builds the full schema.
from app.config import settings
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
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
              theme_prompt=kw.pop("theme_prompt", "x"), **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def add_video(session, channel, topic, subject, status=VideoStatus.DRAFT, position=1):
    v = Video(channel_id=channel.id, topic_id=topic.id, subject=subject,
              status=status, position=position)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


class _Cfg:
    """Stand-in for app_settings(): only the fields tick() reads."""
    def __init__(self, target=5, horizon=1, min_pending=1, enabled=True):
        self.topic_autogen_enabled = enabled
        self.topic_autogen_min_pending = min_pending
        self.topic_autogen_target = target
        self.board_horizon_days = horizon


def run_tick(session, cfg, ideas_fn, language="English"):
    """tick() with its module deps pointed at the in-memory world.

    Restores the real seams afterwards so a later case cannot inherit a stub
    (generate_ideas / channel_language live on the shared video_gen module).
    """
    orig_scope = autofill_loop.session_scope
    orig_settings = autofill_loop.app_settings
    orig_ideas = autofill_loop.video_gen.generate_ideas
    orig_lang = autofill_loop.video_gen.channel_language

    @contextmanager
    def scope():
        yield session

    autofill_loop.session_scope = scope
    autofill_loop.app_settings = lambda s: cfg
    autofill_loop.video_gen.generate_ideas = ideas_fn
    if callable(language) and not isinstance(language, str):
        autofill_loop.video_gen.channel_language = language
    else:
        autofill_loop.video_gen.channel_language = lambda s, cid: language
    try:
        autofill_loop.tick()
    finally:
        autofill_loop.session_scope = orig_scope
        autofill_loop.app_settings = orig_settings
        autofill_loop.video_gen.generate_ideas = orig_ideas
        autofill_loop.video_gen.channel_language = orig_lang


def drafts_for(session, topic_id):
    return session.exec(
        select(func.count(Video.id)).where(
            Video.topic_id == topic_id, Video.status == VideoStatus.DRAFT)
    ).one()


def pending_for(session, topic_id):
    return session.exec(
        select(func.count(Video.id)).where(
            Video.topic_id == topic_id,
            Video.status.in_((VideoStatus.DRAFT, VideoStatus.QUEUED)))
    ).one()


def generate_runs(session, channel_id=None):
    q = select(JobRun).where(JobRun.kind == "generate")
    if channel_id is not None:
        q = q.where(JobRun.channel_id == channel_id)
    return list(session.exec(q).all())


def well_behaved(topic_name, theme, existing, n, fmt, language=None):
    return [f"{topic_name} idea {i}" for i in range(n)]


def overshooting(topic_name, theme, existing, n, fmt, language=None):
    return [f"{topic_name} idea {i}" for i in range(n + 7)]


def recording(fn=None):
    """Stub that records every generate_ideas call, then delegates to fn."""
    calls = []

    def stub(topic_name, theme, existing, n, fmt, language=None):
        calls.append({
            "name": topic_name,
            "theme": theme,
            "existing": list(existing),
            "n": n,
            "fmt": fmt,
            "language": language,
        })
        if fn is None:
            return [f"{topic_name} idea {i}" for i in range(n)]
        return fn(topic_name, theme, existing, n, fmt, language=language)

    stub.calls = calls
    return stub


# ---------------------------------------------------------------------------
# Pure helpers first — so a status/format/channel-filter mutant dies on the
# helper pin, not a later tick case that happens to share the same filter.
# ---------------------------------------------------------------------------

print("helpers: _pending_count / _channel_pending_count / _long_pending_count")
s = fresh_session()
ch = make_channel(s, slug="h-ch", daily_render_budget=6)
other = make_channel(s, slug="h-other", daily_render_budget=6)
long_t = make_topic(s, ch, name="h-long", content_format="long")
short_t = make_topic(s, ch, name="h-short", content_format="short")
other_long = make_topic(s, other, name="h-other-long", content_format="long")
add_video(s, ch, long_t, "long-draft", VideoStatus.DRAFT, 1)
add_video(s, ch, long_t, "long-queued", VideoStatus.QUEUED, 2)
add_video(s, ch, long_t, "long-pub", VideoStatus.PUBLISHED, 3)
add_video(s, ch, short_t, "short-draft", VideoStatus.DRAFT, 4)
add_video(s, ch, short_t, "short-fail", VideoStatus.FAILED, 5)
add_video(s, other, other_long, "other-long-draft", VideoStatus.DRAFT, 1)

ok(autofill_loop._pending_count(s, long_t.id) == 2,
   "_pending_count: DRAFT+QUEUED on this topic only (published excluded)")
ok(autofill_loop._pending_count(s, short_t.id) == 1,
   "_pending_count: FAILED is not pending")
ok(autofill_loop._channel_pending_count(s, ch.id) == 3,
   "_channel_pending_count: 2 long + 1 short; other channel excluded")
ok(autofill_loop._channel_pending_count(s, other.id) == 1,
   "_channel_pending_count: other channel sees only its own pending")
ok(autofill_loop._long_pending_count(s, ch.id) == 2,
   "_long_pending_count: long DRAFT+QUEUED only (short + published excluded)")
ok(autofill_loop._long_pending_count(s, other.id) == 1,
   "_long_pending_count: not a global count")

# ---------------------------------------------------------------------------
# 07-26 / 08-12 / 08-13 incident pins (kept; a sort/reserve/mix-cap
# regression must still fail these first).
# ---------------------------------------------------------------------------

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

print("case: a solo weight-0 topic stays empty (0 is not missing/1)")
s = fresh_session()
ch = make_channel(s, slug="solo-parked", daily_render_budget=5)
parked = make_topic(s, ch, name="solo-parked", weight=0, content_format="short")
rec = recording()
run_tick(s, _Cfg(target=5, horizon=1), rec)
ok(drafts_for(s, parked.id) == 0, "solo weight-0 topic got no ideas")
ok(rec.calls == [], "solo weight-0 topic never called generate_ideas")

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

# ---------------------------------------------------------------------------
# tick() remaining branches
# ---------------------------------------------------------------------------

print("case: autogen disabled writes nothing and never calls the generator")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, name="off", content_format="short")
rec = recording()
run_tick(s, _Cfg(target=5, horizon=1, enabled=False), rec)
ok(drafts_for(s, t.id) == 0, "disabled autogen produced no drafts")
ok(rec.calls == [], "disabled autogen never called generate_ideas")
ok(len(generate_runs(s)) == 0, "disabled autogen wrote no generate JobRun")

print("case: min_pending=0 still refills (threshold floor is 1, not 0)")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, name="floor", content_format="short")
run_tick(s, _Cfg(target=2, horizon=1, min_pending=0), well_behaved)
ok(drafts_for(s, t.id) == 2,
   "min_pending=0 does not park every topic (threshold = max(1, 0))")

print("case: target below min_pending is raised to the threshold")
s = fresh_session()
ch = make_channel(s, daily_render_budget=8)
t = make_topic(s, ch, name="raised-target", content_format="short")
run_tick(s, _Cfg(target=1, horizon=1, min_pending=4), well_behaved)
ok(drafts_for(s, t.id) == 4,
   "target=1 with min_pending=4 tops up to 4, not 1")

print("case: exact threshold is a skip ( >= , not > )")
s = fresh_session()
ch = make_channel(s, daily_render_budget=8)
t = make_topic(s, ch, name="at-threshold", content_format="short")
add_video(s, ch, t, "pre-1", VideoStatus.DRAFT, 1)
add_video(s, ch, t, "pre-2", VideoStatus.DRAFT, 2)
run_tick(s, _Cfg(target=5, horizon=1, min_pending=2), well_behaved)
ok(drafts_for(s, t.id) == 2,
   "pending == threshold*mult writes nothing (refill in bursts)")

print("case: one below threshold tops up to the ceiling")
s = fresh_session()
ch = make_channel(s, daily_render_budget=8)
t = make_topic(s, ch, name="below-threshold", content_format="short")
add_video(s, ch, t, "pre-only", VideoStatus.DRAFT, 1)
run_tick(s, _Cfg(target=5, horizon=1, min_pending=2), well_behaved)
ok(drafts_for(s, t.id) == 5,
   "pending 1 < threshold 2 tops up to target 5 (adds 4)")

print("case: the skip compares pending to threshold*mult, not bare threshold")
s = fresh_session()
ch = make_channel(s, slug="w-mult-below", daily_render_budget=20)
t = make_topic(s, ch, name="w3-below", weight=3, content_format="short")
add_video(s, ch, t, "pre-w3", VideoStatus.DRAFT, 1)
rec = recording()
run_tick(s, _Cfg(target=2, horizon=1, min_pending=1), rec)
# threshold=1, mult=3 → skip at 3; pending=1 must top up to target*3=6 (ask 5).
# A `pending >= threshold` mutant skips here (1 >= 1) and writes nothing.
ok(drafts_for(s, t.id) == 6, "w3 with 1 pending tops up to 2*3 (not skipped at bare threshold)")
ok(len(rec.calls) == 1 and rec.calls[0]["n"] == 5,
   "w3 below threshold*mult asked for 5 (2*3-1), not 0")

s = fresh_session()
ch = make_channel(s, slug="w-mult-at", daily_render_budget=20)
t = make_topic(s, ch, name="w3-at", weight=3, content_format="short")
for i in range(3):
    add_video(s, ch, t, f"pre-w3-{i}", VideoStatus.DRAFT, i + 1)
rec = recording()
run_tick(s, _Cfg(target=2, horizon=1, min_pending=1), rec)
ok(drafts_for(s, t.id) == 3 and rec.calls == [],
   "w3 with pending == 1*3 skips (threshold*mult, not a looser >)")

print("case: QUEUED rows count as pending so we don't double-fill a producing topic")
s = fresh_session()
ch = make_channel(s, daily_render_budget=8)
t = make_topic(s, ch, name="queued-counts", content_format="short")
add_video(s, ch, t, "q1", VideoStatus.QUEUED, 1)
add_video(s, ch, t, "q2", VideoStatus.QUEUED, 2)
run_tick(s, _Cfg(target=5, horizon=1, min_pending=3), well_behaved)
ok(drafts_for(s, t.id) == 3,
   "2 QUEUED count toward pending; asked 5-2=3, not a fresh 5")
ok(pending_for(s, t.id) == 5, "after refill: 2 queued + 3 new drafts")

print("case: terminal / in-flight statuses do not count as pending")
s = fresh_session()
ch = make_channel(s, daily_render_budget=8)
t = make_topic(s, ch, name="terminals", content_format="short")
for i, st in enumerate(
    (VideoStatus.PUBLISHED, VideoStatus.FAILED, VideoStatus.REJECTED,
     VideoStatus.REVIEW, VideoStatus.APPROVED, VideoStatus.RENDERING,
     VideoStatus.RENDERED, VideoStatus.PUBLISHING), start=1
):
    add_video(s, ch, t, f"term-{st}", st, i)
run_tick(s, _Cfg(target=2, horizon=1, min_pending=1), well_behaved)
ok(drafts_for(s, t.id) == 2,
   "PUBLISHED/FAILED/REVIEW/… do not satisfy the pending threshold")

print("case: weight multiplier caps at 4 (a stray weight=10 cannot 10x the bench)")
s = fresh_session()
ch = make_channel(s, daily_render_budget=20)  # board 20 so the cap, not the board, binds
t = make_topic(s, ch, name="huge", weight=10, content_format="short")
rec = recording()
run_tick(s, _Cfg(target=1, horizon=1), rec)
ok(drafts_for(s, t.id) == 4, "weight 10 still asks only 4 (min(weight, 4) * target)")
ok(len(rec.calls) == 1 and rec.calls[0]["n"] == 4,
   "generate_ideas asked for 4, not 10")

print("case: horizon=0 is the no-cap path (still refills; not a 0-seat board)")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, name="uncapped", content_format="short")
run_tick(s, _Cfg(target=5, horizon=0), well_behaved)
ok(drafts_for(s, t.id) == 5,
   "horizon=0 does not compute board_cap=0 and skip every topic")

print("case: generate_ideas exception skips the topic (no raise, no drafts, no JobRun)")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, name="boom", content_format="short")

def boom(*_a, **_k):
    raise RuntimeError("llm down")

run_tick(s, _Cfg(target=5, horizon=1), boom)
ok(drafts_for(s, t.id) == 0, "exception path wrote no drafts")
ok(len(generate_runs(s)) == 0, "exception path wrote no generate JobRun")

print("case: empty / None idea lists write nothing and log nothing")
s = fresh_session()
ch = make_channel(s, slug="empty-ch", daily_render_budget=5)
t_empty = make_topic(s, ch, name="empty", content_format="short")
run_tick(s, _Cfg(target=5, horizon=1), lambda *_a, **_k: [])
ok(drafts_for(s, t_empty.id) == 0, "[] ideas → no drafts")
ok(len(generate_runs(s)) == 0, "[] ideas → no JobRun")

s = fresh_session()
ch = make_channel(s, slug="none-ch", daily_render_budget=5)
t_none = make_topic(s, ch, name="none", content_format="short")
run_tick(s, _Cfg(target=5, horizon=1), lambda *_a, **_k: None)
ok(drafts_for(s, t_none.id) == 0, "None ideas → no drafts")
ok(len(generate_runs(s)) == 0, "None ideas → no JobRun")

print("case: happy refill logs one generate JobRun naming the topic and count")
s = fresh_session()
ch = make_channel(s, slug="log-ch", daily_render_budget=5)
t = make_topic(s, ch, name="logged-topic", content_format="short")
run_tick(s, _Cfg(target=3, horizon=1), well_behaved)
runs = generate_runs(s, ch.id)
ok(len(runs) == 1, "exactly one generate JobRun on a successful refill")
ok(runs[0].status == "success" and runs[0].quota_cost == 0,
   "generate JobRun is success / zero quota")
ok(runs[0].channel_id == ch.id and runs[0].video_id is None,
   "generate JobRun attributed to the channel, not a video")
ok("3" in (runs[0].detail or "") and "logged-topic" in (runs[0].detail or ""),
   "generate detail names the count and the topic")

print("case: generate_ideas receives format, language, theme, existing, and asked n")
s = fresh_session()
ch = make_channel(s, slug="kw-ch", daily_render_budget=8)
t = make_topic(s, ch, name="kw-long", theme_prompt="theme-xyz",
               weight=1, content_format="long")
other = make_topic(s, ch, name="kw-other", weight=0, content_format="short")
add_video(s, ch, t, "already-there", VideoStatus.DRAFT, 1)
add_video(s, ch, t, "old-pub", VideoStatus.PUBLISHED, 2)
add_video(s, ch, other, "other-topic-title", VideoStatus.DRAFT, 3)
rec = recording()
run_tick(s, _Cfg(target=3, horizon=1, min_pending=2), rec,
         language="Brazilian Portuguese")
# weight-0 sibling is parked (and does not arm has_live_short / mix-cap).
long_calls = [c for c in rec.calls if c["name"] == "kw-long"]
ok(len(long_calls) == 1, "one generate_ideas call for the under-threshold long")
c = long_calls[0]
ok(c["name"] == "kw-long" and c["theme"] == "theme-xyz",
   "name + theme_prompt forwarded verbatim")
ok(c["fmt"] == "long", "content_format forwarded (long, not the short default)")
ok(c["language"] == "Brazilian Portuguese",
   "channel_language result forwarded as language=")
ok(set(c["existing"]) == {"already-there", "old-pub"},
   "existing is every subject on THIS topic (draft + published), not pending-only")
ok("other-topic-title" not in c["existing"],
   "existing is topic-scoped, not channel-wide")
ok(c["n"] == 2, "asked n is the remaining ceiling (3-1), not a fresh target")

print("case: new drafts continue position after the CHANNEL max")
s = fresh_session()
ch = make_channel(s, slug="pos-ch", daily_render_budget=5)
other_ch = make_channel(s, slug="pos-other", daily_render_budget=5)
t = make_topic(s, ch, name="pos-empty", content_format="short")
sibling = make_topic(s, ch, name="pos-sibling", content_format="short")
foreign = make_topic(s, other_ch, name="pos-foreign", content_format="short")
add_video(s, ch, sibling, "sib-high", VideoStatus.PUBLISHED, position=7)
add_video(s, other_ch, foreign, "foreign-high", VideoStatus.PUBLISHED, position=99)
# t is empty (topic-max would restart at 1) and walks first at equal weight.
# The only in-channel position is the sibling's 7, so channel-max → 8,9.
# An unscoped max would see the other channel's 99 → 100,101.
run_tick(s, _Cfg(target=2, horizon=1), well_behaved)
new = list(s.exec(
    select(Video).where(
        Video.topic_id == t.id, Video.status == VideoStatus.DRAFT
    ).order_by(Video.position)
).all())
ok([v.position for v in new] == [8, 9],
   "new drafts sit after the channel max (sibling pos 7), not topic-max 1 or foreign 100")
ok(s.exec(select(Video).where(Video.subject == "foreign-high")).one().position == 99,
   "other channel's published row is untouched at 99")

print("case: inactive topics are not in the walk (even a high-weight one)")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)
dead = make_topic(s, ch, name="inactive-heavy", weight=4, content_format="short",
                  active=False)
live = make_topic(s, ch, name="active-light", weight=1, content_format="short")
ok(dead.id < live.id, "fixture: inactive topic has the lower id")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, dead.id) == 0, "inactive topic got no ideas")
ok(drafts_for(s, live.id) == 3, "active topic claimed the whole board")

print("case: parked (weight-0) long does not trigger the short-side reserve")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)
parked_long = make_topic(s, ch, name="parked-long", weight=0, content_format="long")
short = make_topic(s, ch, name="only-short", weight=4, content_format="short")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, parked_long.id) == 0, "weight-0 long still parked")
ok(drafts_for(s, short.id) == 3,
   "no live long → shorts fill the full board (no reserve hole)")

print("case: short-only channel does not hold a phantom long slot")
s = fresh_session()
ch = make_channel(s, slug="short-only", daily_render_budget=3)
short = make_topic(s, ch, name="solo-short", weight=4, content_format="short")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, short.id) == 3,
   "short-only fills board_cap, not board_cap-1")

print("case: long-only channel is not mix-capped (can fill the whole board)")
s = fresh_session()
ch = make_channel(s, slug="long-only", daily_render_budget=5)
long_t = make_topic(s, ch, name="solo-long", weight=4, content_format="long")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, long_t.id) == 5,
   "no live short → mix-cap does not apply; long fills all 5 seats")

print("case: two live longs share the mix-cap (running long_pending increments)")
s = fresh_session()
ch = make_channel(s, slug="two-long", daily_render_budget=5)
long_a = make_topic(s, ch, name="long-a", weight=4, content_format="long")
long_b = make_topic(s, ch, name="long-b", weight=4, content_format="long")
short = make_topic(s, ch, name="short-c", weight=1, content_format="short")
ok(long_a.id < long_b.id < short.id, "fixture: longs walk first at equal weight")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, long_a.id) == 1, "first long takes the single mix-cap seat")
ok(drafts_for(s, long_b.id) == 0,
   "second long sees long_pending already at cap (increment is not skipped)")
ok(drafts_for(s, short.id) == 4, "shorts take the remaining 4 seats")

print("case: per-channel isolation (one channel's cap cannot starve the other)")
s = fresh_session()
ch1 = make_channel(s, slug="iso-1", name="A", daily_render_budget=2)
ch2 = make_channel(s, slug="iso-2", name="B", daily_render_budget=5)
t1 = make_topic(s, ch1, name="iso-t1", content_format="short")
t2 = make_topic(s, ch2, name="iso-t2", content_format="short")
langs = {}

def lang_for(_session, cid):
    return {ch1.id: "English", ch2.id: "Portuguese"}[cid]

rec = recording()
run_tick(s, _Cfg(target=5, horizon=1), rec, language=lang_for)
ok(drafts_for(s, t1.id) == 2, "ch1 board_cap=2")
ok(drafts_for(s, t2.id) == 5, "ch2 board_cap=5 (not sharing ch1's cap)")
by_name = {c["name"]: c["language"] for c in rec.calls}
ok(by_name == {"iso-t1": "English", "iso-t2": "Portuguese"},
   "each channel's language reaches generate_ideas independently (not swapped)")
ok(len(generate_runs(s, ch1.id)) == 1 and len(generate_runs(s, ch2.id)) == 1,
   "each channel gets its own generate JobRun")

print("case: a second tick does not refill a bench already at the ceiling")
s = fresh_session()
ch = make_channel(s, slug="second-tick", daily_render_budget=5)
t = make_topic(s, ch, name="once", content_format="short")
run_tick(s, _Cfg(target=5, horizon=1), well_behaved)
ok(drafts_for(s, t.id) == 5, "first tick filled to target")
rec = recording()
run_tick(s, _Cfg(target=5, horizon=1), rec)
ok(drafts_for(s, t.id) == 5, "second tick added nothing")
ok(rec.calls == [], "second tick never called generate_ideas")

print("case: two live longs at horizon=2 — increment is += n, not += 1")
s = fresh_session()
ch = make_channel(s, slug="two-long-h2", daily_render_budget=8)
long_a = make_topic(s, ch, name="long-a-h2", weight=4, content_format="long")
long_b = make_topic(s, ch, name="long-b-h2", weight=4, content_format="long")
short = make_topic(s, ch, name="short-h2", weight=1, content_format="short")
ok(long_a.id < long_b.id < short.id, "fixture: longs walk first at equal weight")
run_tick(s, _Cfg(target=5, horizon=2), well_behaved)
ok(drafts_for(s, long_a.id) == 2, "first long takes both mix-cap seats (horizon=2)")
ok(drafts_for(s, long_b.id) == 0,
   "second long sees long_pending += n (=2), not += 1 (which would leave 1 seat)")
ok(drafts_for(s, short.id) == 5, "shorts top up to their own target (board still has room)")

print("case: autofill_batch is the binding ask when target and board are larger")
ok(settings.autofill_batch == 8, "autofill_batch default is 8 (need formula term)")
s = fresh_session()
ch = make_channel(s, slug="batch-binds", daily_render_budget=20)
t = make_topic(s, ch, name="batchy", content_format="short")
rec = recording()
run_tick(s, _Cfg(target=12, horizon=1), rec)
ok(drafts_for(s, t.id) == 8, "ask clamped to autofill_batch (8), not target 12 or board 20")
ok(len(rec.calls) == 1 and rec.calls[0]["n"] == 8,
   "generate_ideas asked for autofill_batch, not the dropped-term 12")

print("case: horizon=0 fallback board_space is batch*4 (weight-4 makes *4 bind)")
s = fresh_session()
ch = make_channel(s, slug="nocap-star", daily_render_budget=5)
t = make_topic(s, ch, name="nocap-w4", weight=4, content_format="short")
rec = recording()
run_tick(s, _Cfg(target=20, horizon=0), rec)
# need = min(20*4, 8*4, 8*4) = 32. *4→*2 would ask 16; *4→*1 would ask 8.
ok(len(rec.calls) == 1 and rec.calls[0]["n"] == 32,
   "horizon=0 + w4 asks batch*4=32 (the no-cap fallback binds)")
ok(drafts_for(s, t.id) == 32, "horizon=0 + w4 wrote 32 drafts")

print("case: exception on one topic does not block a later topic on the same channel")
s = fresh_session()
ch = make_channel(s, slug="partial", daily_render_budget=6)
first = make_topic(s, ch, name="raises", weight=2, content_format="short")
second = make_topic(s, ch, name="survives", weight=1, content_format="short")
ok(first.id < second.id, "fixture: raising topic walks first")

def raise_on_first(topic_name, theme, existing, n, fmt, language=None):
    if topic_name == "raises":
        raise RuntimeError("first topic boom")
    return well_behaved(topic_name, theme, existing, n, fmt, language=language)

run_tick(s, _Cfg(target=3, horizon=1), raise_on_first)
ok(drafts_for(s, first.id) == 0, "raising topic produced nothing")
ok(drafts_for(s, second.id) == 3, "later topic still refilled after the skip")

print()
print(f"ALL {_checks} CHECKS PASSED")
