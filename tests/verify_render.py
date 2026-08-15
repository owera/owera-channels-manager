"""Dependency-free regression checks for render_loop: the budget policy
(_auto_produce DRAFT -> QUEUED, _submit_new QUEUED -> RENDERING) and the
non-budget lifecycle (startup recovery, timeout/retry, finalize).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_render.py

_auto_produce closes the DRAFT -> QUEUED gap that caused the 07-18..07-23 stall:
nothing else in the app makes that transition, so a full bench of drafts starved
the render loop for 5 days while board_inventory read "at capacity". These checks
pin the promotion policy: budget/active headroom, weight-0 and paused exclusions,
and the long-form buffer guarantee.

_submit_new's daily-budget gate must count in-flight renders, not just completed
ones — the 2026-07-26 overshoot (8 rendered vs a budget of 5, both channels) came
from starting a new render after each success while concurrency-many were still
in flight. The /videos/queue-plan board endpoint mirrors that gate and is pinned
here too.

The lifecycle sections pin the paths that guard auto-publish: only external
(MPT) renders survive a restart, hung renders re-queue at the timeout (and
fail only once the retry budget is spent) instead of occupying a concurrency
slot forever, transient engine errors re-queue with a bounded retry, and
_finalize refuses missing/blank artifacts as the last gate before a video
can reach APPROVED -> publish.

Uses an in-memory SQLite DB — no network, no creds. Exits non-zero on the first
failed assertion.
"""
import sys

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

# Importing app.models defines every table=True model, registering them all on
# SQLModel.metadata so create_all() below builds the full schema.
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.services import render_loop

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


def make_video(session, channel, topic, **kw):
    v = Video(channel_id=channel.id, topic_id=topic.id,
              subject=kw.pop("subject", "Test subject"),
              status=kw.pop("status", VideoStatus.DRAFT), **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def statuses(session, ids):
    return [session.get(Video, i).status for i in ids]


# --- fills free capacity, weight-first, shorts before extra longs -------------
print("auto_produce: fills free render capacity weight-first")
s = fresh_session()
ch = make_channel(s)
hi = make_topic(s, ch, name="winner", weight=3, content_format="short")
lo = make_topic(s, ch, name="normal", weight=1, content_format="short")
v_lo = make_video(s, ch, lo)
v_hi1 = make_video(s, ch, hi)
v_hi2 = make_video(s, ch, hi)
render_loop._auto_produce(s)
s.commit()
ok(statuses(s, [v_hi1.id, v_hi2.id, v_lo.id]) == [VideoStatus.QUEUED] * 3,
   "all drafts queued when budget allows")
runs = s.exec(select(JobRun).where(JobRun.kind == "produce")).all()
ok(len(runs) == 3 and all(r.status == "success" for r in runs),
   "one 'produce' JobRun logged per promotion")

# --- headroom: budget minus rendered_today minus queued/rendering -------------
print("auto_produce: respects budget and in-flight work")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)
t = make_topic(s, ch, weight=1, content_format="short")
s.add(JobRun(kind="render", status="success", channel_id=ch.id))  # 1 rendered today
s.commit()
make_video(s, ch, t, status=VideoStatus.QUEUED)                   # 1 slot claimed
d1 = make_video(s, ch, t)
d2 = make_video(s, ch, t)
render_loop._auto_produce(s)
s.commit()
promoted = [v for v in (s.get(Video, d1.id), s.get(Video, d2.id))
            if v.status == VideoStatus.QUEUED]
ok(len(promoted) == 1, "only the remaining 1 slot of 3 is filled (1 rendered + 1 queued)")

s = fresh_session()
ch = make_channel(s, daily_render_budget=2)
t = make_topic(s, ch, weight=1, content_format="short")
for _ in range(2):
    s.add(JobRun(kind="render", status="success", channel_id=ch.id))
s.commit()
d = make_video(s, ch, t)
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, d.id).status == VideoStatus.DRAFT,
   "budget already spent today -> no promotion")

# --- weight-0 / inactive topics and paused channels are never touched ---------
print("auto_produce: parked topics and paused channels excluded")
s = fresh_session()
ch = make_channel(s)
parked = make_topic(s, ch, name="parked", weight=0, content_format="short")
dead = make_topic(s, ch, name="inactive", weight=2, active=False,
                  content_format="short")
v_parked = make_video(s, ch, parked)
v_dead = make_video(s, ch, dead)
render_loop._auto_produce(s)
s.commit()
ok(statuses(s, [v_parked.id, v_dead.id]) == [VideoStatus.DRAFT] * 2,
   "weight-0 and inactive topic drafts stay drafts")

s = fresh_session()
ch = make_channel(s, paused=True)
t = make_topic(s, ch, weight=1, content_format="short")
v = make_video(s, ch, t)
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v.id).status == VideoStatus.DRAFT, "paused channel is never produced")

# --- long-form buffer guarantee ----------------------------------------------
print("auto_produce: keeps a long-form in the approved buffer")
s = fresh_session()
ch = make_channel(s, daily_render_budget=2)
t_long = make_topic(s, ch, name="anchor", weight=1, content_format="long")
t_short = make_topic(s, ch, name="shorts", weight=3, content_format="short")
v_long = make_video(s, ch, t_long)
v_s1 = make_video(s, ch, t_short)
v_s2 = make_video(s, ch, t_short)
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v_long.id).status == VideoStatus.QUEUED,
   "no approved long -> one long queued first even at lower weight")
ok([s.get(Video, v_s1.id).status, s.get(Video, v_s2.id).status].count(VideoStatus.QUEUED) == 1,
   "remaining slot goes to a short")

s = fresh_session()
ch = make_channel(s, daily_render_budget=1)
t_long = make_topic(s, ch, name="anchor", weight=1, content_format="long")
t_short = make_topic(s, ch, name="shorts", weight=1, content_format="short")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)  # a long already banked
v_long = make_video(s, ch, t_long)
v_short = make_video(s, ch, t_short)
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v_short.id).status == VideoStatus.QUEUED,
   "approved long already banked -> the slot goes to a short")
ok(s.get(Video, v_long.id).status == VideoStatus.DRAFT,
   "no second long queued while one is banked")

# With headroom ≥ 2 and exactly one approved long (none in flight), reserve one
# queued long for the day after publish so the bank doesn't hit 0L at noon.
print("auto_produce: banks a next-day long reserve under thin approved long buffer")
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
t_short = make_topic(s, ch, name="shorts", weight=3, content_format="short")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)  # thin bank (exactly 1)
v_long = make_video(s, ch, t_long)
short_ids = [make_video(s, ch, t_short).id for _ in range(5)]
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v_long.id).status == VideoStatus.QUEUED,
   "approved_longs==1 and headroom>=2 -> one long reserved in queue")
queued_shorts = sum(
    1 for sid in short_ids if s.get(Video, sid).status == VideoStatus.QUEUED
)
ok(queued_shorts == 4, "remaining 4 slots prefer shorts (1L+4S overnight mix)")

# If a long is already queued, do not force a second while one is approved.
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
t_short = make_topic(s, ch, name="shorts", weight=3, content_format="short")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)
make_video(s, ch, t_long, status=VideoStatus.QUEUED)  # reserve already present
v_long2 = make_video(s, ch, t_long)
short_ids = [make_video(s, ch, t_short).id for _ in range(5)]
# active=1 (queued long) → headroom=4
render_loop._auto_produce(s)
s.commit()
ok(s.get(Video, v_long2.id).status == VideoStatus.DRAFT,
   "in-flight long already present -> no extra long from drafts")
ok(sum(1 for sid in short_ids if s.get(Video, sid).status == VideoStatus.QUEUED) == 4,
   "headroom fills with shorts when long reserve already queued")

# --- _submit_new: in-flight renders count against the daily budget ------------
# 2026-07-26 incident: gating on completed renders alone let the loop start a new
# render after each success while `render_concurrency` others were still in
# flight, overshooting to budget+concurrency-1 (8 rendered vs a budget of 5 at
# concurrency 4, on both channels). These checks replay that burst and pin the
# fixed gate: rendered_today + in-flight-for-channel >= budget stops submission.
print("submit_new: daily budget counts in-flight renders")

from app.db import app_settings  # noqa: E402
from app.services import quota  # noqa: E402


class FakeEngine:
    def submit(self, video, params):
        return f"task-{video.id}"


render_loop.ensure_topic_playlist = lambda session, topic, channel: None
render_loop.resolve_engine = lambda session, video, topic, channel: "fake"
render_loop.get_engine = lambda name: FakeEngine()


def set_concurrency(session, n):
    cfg = app_settings(session)
    cfg.render_concurrency = n
    session.add(cfg)
    session.commit()


def by_status(session, ch, status):
    return session.exec(
        select(Video).where(Video.channel_id == ch.id, Video.status == status)
    ).all()


def complete_one(session, video):
    """Mirror _finalize's accounting: RENDERING -> REVIEW + a success JobRun."""
    video.status = VideoStatus.REVIEW
    session.add(video)
    quota.log(session, kind="render", status="success", video_id=video.id,
              channel_id=video.channel_id)
    session.commit()


# Incident replay: budget 5, concurrency 4, 10 queued; complete-one/tick until
# the pipeline drains. Exactly 5 videos may ever render (was 8 before the fix).
s = fresh_session()
set_concurrency(s, 4)
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, content_format="short")
for _ in range(10):
    make_video(s, ch, t, status=VideoStatus.QUEUED)
render_loop._submit_new(s)
s.commit()
ok(len(by_status(s, ch, VideoStatus.RENDERING)) == 4,
   "first tick fills the concurrency slots (4 in flight)")
for _ in range(20):  # drain: complete one in-flight render, then tick again
    in_flight = by_status(s, ch, VideoStatus.RENDERING)
    if not in_flight:
        break
    complete_one(s, in_flight[0])
    render_loop._submit_new(s)
    s.commit()
ok(len(by_status(s, ch, VideoStatus.REVIEW)) == 5,
   "exactly budget (5) videos rendered across the burst — not budget+concurrency-1")
ok(len(by_status(s, ch, VideoStatus.QUEUED)) == 5,
   "the rest stay QUEUED for tomorrow's budget")

# Within one tick: concurrency above budget must not oversubmit — submissions
# earlier in the same tick count as in-flight for the later candidates.
s = fresh_session()
set_concurrency(s, 8)
ch = make_channel(s, daily_render_budget=3)
t = make_topic(s, ch, content_format="short")
for _ in range(6):
    make_video(s, ch, t, status=VideoStatus.QUEUED)
render_loop._submit_new(s)
s.commit()
ok(len(by_status(s, ch, VideoStatus.RENDERING)) == 3,
   "single tick with concurrency 8 submits only budget (3)")

# Long-first when the approved long buffer is empty (2026-08-07 ch2): auto_produce
# queues a long, but FIFO by id let earlier short ids burn the 5/day budget first,
# so publish found 11 approved shorts / 0 longs. Prefer the queued long on submit.
print("submit_new: prefers queued long when no approved long banked")
s = fresh_session()
set_concurrency(s, 1)
ch = make_channel(s, daily_render_budget=1)
t_short = make_topic(s, ch, name="shorts", weight=4, content_format="short")
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
# Shorts get lower ids (would win pure FIFO); long is created last on purpose.
v_s1 = make_video(s, ch, t_short, status=VideoStatus.QUEUED)
v_s2 = make_video(s, ch, t_short, status=VideoStatus.QUEUED)
v_long = make_video(s, ch, t_long, status=VideoStatus.QUEUED)
ok(v_s1.id < v_long.id, "precondition: shorts have lower ids than the long")
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v_long.id).status == VideoStatus.RENDERING,
   "no approved long -> queued long submits before lower-id shorts")
ok(s.get(Video, v_s1.id).status == VideoStatus.QUEUED
   and s.get(Video, v_s2.id).status == VideoStatus.QUEUED,
   "shorts stay queued when budget is 1 and the long took the slot")

s = fresh_session()
set_concurrency(s, 1)
ch = make_channel(s, daily_render_budget=1)
t_short = make_topic(s, ch, name="shorts", weight=4, content_format="short")
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)  # long already banked
v_s = make_video(s, ch, t_short, status=VideoStatus.QUEUED)
v_long = make_video(s, ch, t_long, status=VideoStatus.QUEUED)
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v_s.id).status == VideoStatus.RENDERING,
   "approved long already banked -> short preferred over additional long")
ok(s.get(Video, v_long.id).status == VideoStatus.QUEUED,
   "second long is not force-prioritized when one is already approved")

# Dual of long-first: long has LOWER id (would win pure FIFO) but short still
# submits first when an approved long is already banked.
print("submit_new: prefers queued short when approved long banked (even if long id is lower)")
s = fresh_session()
set_concurrency(s, 1)
ch = make_channel(s, daily_render_budget=1)
t_short = make_topic(s, ch, name="shorts", weight=4, content_format="short")
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)
v_long = make_video(s, ch, t_long, status=VideoStatus.QUEUED)  # lower id
v_s = make_video(s, ch, t_short, status=VideoStatus.QUEUED)
ok(v_long.id < v_s.id, "precondition: long has lower id than short")
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v_s.id).status == VideoStatus.RENDERING,
   "banked long -> short submits before lower-id queued long")
ok(s.get(Video, v_long.id).status == VideoStatus.QUEUED,
   "queued long waits when shorts need the mix slots")

# Rebalance: pure-long QUEUED + short drafts + banked approved long → demote
# excess longs to DRAFT so _auto_produce can fill shorts.
print("rebalance_queued_mix: demotes excess longs when short drafts exist")
s = fresh_session()
set_concurrency(s, 1)
ch = make_channel(s, daily_render_budget=5)
t_short = make_topic(s, ch, name="shorts", weight=4, content_format="short")
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)
queued_longs = [make_video(s, ch, t_long, status=VideoStatus.QUEUED) for _ in range(5)]
short_drafts = [make_video(s, ch, t_short, status=VideoStatus.DRAFT) for _ in range(4)]
render_loop._rebalance_queued_mix(s)
s.commit()
still_queued = [v for v in queued_longs if s.get(Video, v.id).status == VideoStatus.QUEUED]
demoted = [v for v in queued_longs if s.get(Video, v.id).status == VideoStatus.DRAFT]
ok(len(still_queued) == 1, "rebalance keeps exactly 1 queued long as reserve")
ok(len(demoted) == 4, "rebalance demotes 4 excess longs to DRAFT")
ok(all(s.get(Video, v.id).status == VideoStatus.DRAFT for v in short_drafts),
   "short drafts untouched by rebalance itself")

# No demotion when there are no short drafts to promote into the freed slots.
s = fresh_session()
ch = make_channel(s, daily_render_budget=5)
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)
ql = [make_video(s, ch, t_long, status=VideoStatus.QUEUED) for _ in range(3)]
render_loop._rebalance_queued_mix(s)
s.commit()
ok(all(s.get(Video, v.id).status == VideoStatus.QUEUED for v in ql),
   "rebalance is a no-op when no short drafts exist")

# Full tick path: rebalance + auto_produce fills shorts into freed headroom
# when rendered_today=0 (budget free).
print("tick path: rebalance + auto_produce restores short queue under banked long")
s = fresh_session()
set_concurrency(s, 1)
ch = make_channel(s, daily_render_budget=5)
t_short = make_topic(s, ch, name="shorts", weight=4, content_format="short")
t_long = make_topic(s, ch, name="anchor", weight=2, content_format="long")
make_video(s, ch, t_long, status=VideoStatus.APPROVED)
for _ in range(5):
    make_video(s, ch, t_long, status=VideoStatus.QUEUED)
for _ in range(4):
    make_video(s, ch, t_short, status=VideoStatus.DRAFT)
render_loop._rebalance_queued_mix(s)
render_loop._auto_produce(s)
s.commit()
queued_now = s.exec(select(Video).where(
    Video.channel_id == ch.id, Video.status == VideoStatus.QUEUED)).all()
formats = []
for v in queued_now:
    t = s.get(Topic, v.topic_id)
    formats.append(t.content_format if t else "?")
ok(formats.count("long") == 1, "after rebalance+produce: exactly 1 long remains queued")
ok(formats.count("short") == 4, "after rebalance+produce: 4 shorts fill the freed slots")
ok(len(queued_now) == 5, "queue still fills to budget size")

# Per-channel isolation: one channel exhausted by successes + in-flight must not
# block another channel's submissions. ch2's budget (2) is <= ch1's in-flight
# count (2) on purpose: a gate that counted in-flight renders globally instead
# of per-channel would wrongly starve ch2 here.
s = fresh_session()
set_concurrency(s, 8)
ch1 = make_channel(s, slug="ch-a", daily_render_budget=5)
ch2 = make_channel(s, slug="ch-b", daily_render_budget=2)
t1 = make_topic(s, ch1, content_format="short")
t2 = make_topic(s, ch2, content_format="short")
for _ in range(3):
    s.add(JobRun(kind="render", status="success", channel_id=ch1.id))
s.commit()
for _ in range(2):
    make_video(s, ch1, t1, status=VideoStatus.RENDERING)
q1 = make_video(s, ch1, t1, status=VideoStatus.QUEUED)
q2 = make_video(s, ch2, t2, status=VideoStatus.QUEUED)
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, q1.id).status == VideoStatus.QUEUED,
   "3 done + 2 in flight = budget 5 -> exhausted channel submits nothing")
ok(s.get(Video, q2.id).status == VideoStatus.RENDERING,
   "fresh channel still submits — the gate is per-channel")

# The board's /videos/queue-plan endpoint documents that it mirrors _submit_new's
# gates; it must count in-flight renders too, or it labels cards "renders today"
# that the fixed loop will never submit today.
from app.routers.videos import queue_plan  # noqa: E402

s = fresh_session()
set_concurrency(s, 8)
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, content_format="short")
for _ in range(2):
    s.add(JobRun(kind="render", status="success", channel_id=ch.id))
s.commit()
for _ in range(3):
    make_video(s, ch, t, status=VideoStatus.RENDERING)
qs = [make_video(s, ch, t, status=VideoStatus.QUEUED) for _ in range(2)]
plan = queue_plan(channel_id=ch.id, session=s)
ok(all(plan[str(v.id)]["reason"].startswith("render budget full") for v in qs),
   "queue-plan: 2 done + 3 in flight = budget 5 -> queued cards labeled budget-full")
ok("2+3 rendering/5" in plan[str(qs[0].id)]["reason"],
   "queue-plan reason spells out done + in-flight against the budget")

# ══ Non-budget lifecycle: recovery, timeout/retry, finalize (backlog 7) ════════

import json  # noqa: E402
import tempfile  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import RenderProfile  # noqa: E402
from app.services import issues  # noqa: E402
from app.services.engines import (  # noqa: E402
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_PROCESSING,
)


class StubEngine:
    """Scripted engine: poll returns/raises as configured, final_path is fixed."""
    def __init__(self, poll_result=None, poll_raises=False, final=None):
        self.poll_result = poll_result
        self.poll_raises = poll_raises
        self.final = final
        self.poll_calls = []

    def submit(self, video, params):
        return f"task-{video.id}"

    def poll(self, task_id):
        self.poll_calls.append(task_id)
        if self.poll_raises:
            raise RuntimeError("engine unreachable")
        return self.poll_result

    def final_path(self, task_id):
        return self.final


def use_engine(e):
    render_loop.get_engine = lambda name: e
    return e


@contextmanager
def _scoped(session):
    yield session
    session.commit()


def use_scope(session):
    """Point render_loop's own session_scope() (recover/tick) at the test DB."""
    render_loop.session_scope = lambda: _scoped(session)


def render_runs(session, status):
    return session.exec(select(JobRun).where(JobRun.kind == "render")
                        .where(JobRun.status == status)).all()


NOW = datetime.now(timezone.utc)

# --- pure helpers: profile params and long-form overrides ---------------------
print("profile_params: malformed profiles resolve to empty overrides")
s = fresh_session()
ok(render_loop._profile_params(s, None) == {}, "no profile id -> {}")
ok(render_loop._profile_params(s, 999) == {}, "missing profile row -> {}")
p_good = RenderProfile(name="good", params_json='{"video_aspect": "9:16"}')
p_bad = RenderProfile(name="bad", params_json="{not json")
p_null = RenderProfile(name="null", params_json=None)
s.add(p_good); s.add(p_bad); s.add(p_null)
s.commit()
ok(render_loop._profile_params(s, p_good.id) == {"video_aspect": "9:16"},
   "valid params_json parsed")
ok(render_loop._profile_params(s, p_bad.id) == {},
   "corrupt params_json -> {} — a bad profile must not block rendering")
ok(render_loop._profile_params(s, p_null.id) == {}, "NULL params_json -> {}")
ok(render_loop._format_overrides("long")
   == {"video_aspect": "16:9", "paragraph_number": 8},
   "long-form forces landscape + longer script")
ok(render_loop._format_overrides("short") == {},
   "shorts keep profile-driven params untouched")

# --- recover_orphaned_renders: only external (MPT) renders survive a restart --
# HyperFrames renders run on in-process daemon threads that die with the manager,
# so a RENDERING row from a previous process will never advance on its own.
print("recover_orphaned_renders: in-process orphans re-queued, MPT left alone")
s = fresh_session()
use_scope(s)
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v_hf = make_video(s, ch, t, status=VideoStatus.RENDERING, engine="hyperframes",
                  mpt_task_id="hf-1", render_progress=40, error="boom")
v_legacy = make_video(s, ch, t, status=VideoStatus.RENDERING,
                      mpt_task_id="old-1", render_progress=10)
v_mpt = make_video(s, ch, t, status=VideoStatus.RENDERING, engine="mpt",
                   mpt_task_id="mpt-1", render_progress=70)
v_done = make_video(s, ch, t, status=VideoStatus.APPROVED, engine="hyperframes")
render_loop.recover_orphaned_renders()
v = s.get(Video, v_hf.id)
ok(v.status == VideoStatus.QUEUED and v.mpt_task_id is None
   and v.render_progress == 0 and v.error is None,
   "in-process orphan re-queued clean (task id / progress / error all reset)")
ok(s.get(Video, v_legacy.id).status == VideoStatus.QUEUED,
   "engine=None row treated as in-process and re-queued — only 'mpt' survives")
m = s.get(Video, v_mpt.id)
ok(m.status == VideoStatus.RENDERING and m.mpt_task_id == "mpt-1",
   "MPT render untouched — its external task survives the restart and is re-polled")
ok(s.get(Video, v_done.id).status == VideoStatus.APPROVED,
   "non-RENDERING rows untouched")
recov = [r for r in render_runs(s, "error") if "recovered orphaned render" in (r.detail or "")]
ok(len(recov) == 2, "one recovery JobRun per re-queued orphan")

# --- _advance_in_flight: hung renders retry at the timeout --------------------
print("advance_in_flight: hung renders time out after a last poll")
s = fresh_session()
eng = use_engine(StubEngine(poll_result={"state": STATE_PROCESSING, "progress": 50}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
stale = timedelta(seconds=settings.render_timeout_seconds + 3600)
v_aware = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="a",
                     last_attempt_at=NOW - stale, render_progress=5)
v_naive = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="b",
                     last_attempt_at=(NOW - stale).replace(tzinfo=None),
                     render_progress=25)
# SQLite strips tzinfo on the round-trip, so a committed row always reads back
# naive (review finding, 2026-08-02): to actually drive the tzinfo-present
# branch, re-assign the aware value on the identity-mapped instance AFTER the
# commit — _advance_in_flight's select returns this same in-session object.
v_aware.last_attempt_at = NOW - stale
s.add(v_aware)
ok(v_aware.last_attempt_at.tzinfo is not None,
   "aware-leg precondition: tzinfo intact on the in-session instance")
render_loop._advance_in_flight(s)
s.commit()
ok(all(s.get(Video, v.id).status == VideoStatus.QUEUED
       and s.get(Video, v.id).retry_count == 1
       and s.get(Video, v.id).mpt_task_id is None
       and s.get(Video, v.id).render_progress == 0
       and s.get(Video, v.id).error is None
       for v in (v_aware, v_naive)),
   "tz-aware AND naive last_attempt_at re-queue at the timeout (08-14 majority)")
ok(sorted(eng.poll_calls) == ["a", "b"],
   "timed-out still-PROCESSING renders are polled before the hang retry")
ok(len(render_runs(s, "error")) == 2, "one error JobRun per timed-out render")
ok(all("transient error (retry 1/2): render timed out" in (r.detail or "")
       for r in render_runs(s, "error")),
   "timeout JobRun uses the transient-retry detail, not a terminal error")

# Poll-first: a just-finished COMPLETE/FAILED is not abandoned as a hang.
s = fresh_session()
eng = use_engine(StubEngine(
    poll_result={"state": STATE_COMPLETE, "progress": 100},
    final=Path("/nonexistent/verify-render-missing.mp4"),
))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="done",
               last_attempt_at=NOW - stale)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.FAILED and "missing" in (v.error or ""),
   "timeout + COMPLETE is finalized (missing artifact), not re-queued as a hang")
ok(eng.poll_calls == ["done"], "COMPLETE-at-timeout was polled")

s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED, "error": "ffmpeg exploded"}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="ff",
               last_attempt_at=NOW - stale)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.FAILED and v.error == "ffmpeg exploded" and v.retry_count == 0,
   "timeout + terminal engine error fails immediately, not as a hang retry")

s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED, "error": "upstream 529 overloaded"}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="ov",
               last_attempt_at=NOW - stale)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.QUEUED and v.retry_count == 1,
   "timeout + transient engine error uses the engine signature, not 'render timed out'")
ok(any("529" in (r.detail or "") for r in render_runs(s, "error")),
   "timeout + 529 JobRun names the engine error")

# Midpoint + exhausted budget on the wall-clock path (same bound as engine
# transients). The retry_count=0 case above is QUEUED; these two legs kill
# always-retry and always-FAILED mutants.
s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_PROCESSING, "progress": 50}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v_mid = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="mid",
                   last_attempt_at=NOW - stale, retry_count=1, render_progress=40)
v_spent = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="spent",
                     last_attempt_at=NOW - stale, retry_count=2)
render_loop._advance_in_flight(s)
s.commit()
m = s.get(Video, v_mid.id)
ok(m.status == VideoStatus.QUEUED and m.retry_count == 2
   and m.mpt_task_id is None and m.render_progress == 0 and m.error is None,
   "timeout at retry_count=1 still gets the second retry (handle/progress cleared)")
ok(s.get(Video, v_spent.id).status == VideoStatus.FAILED
   and s.get(Video, v_spent.id).error == "render timed out"
   and s.get(Video, v_spent.id).retry_count == 2,
   "timeout with the retry budget spent -> FAILED, error kept")

# The retry is not a status-only flip: _submit_new must pick the row back up
# (review mutant: skip retry_count>0 would leave it QUEUED forever).
s = fresh_session()
use_scope(s)
set_concurrency(s, 1)
eng = use_engine(StubEngine(poll_result={"state": STATE_PROCESSING, "progress": 5}))
ch = make_channel(s, daily_render_budget=5)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="old-h",
               last_attempt_at=NOW - stale, render_progress=5)
render_loop._advance_in_flight(s)
s.commit()
ok(s.get(Video, v.id).status == VideoStatus.QUEUED, "precondition: timeout re-queued")
render_loop._submit_new(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.RENDERING and v.mpt_task_id == f"task-{v.id}"
   and v.retry_count == 1 and v.last_attempt_at is not None,
   "timeout retry is resubmitted with a fresh handle (not a status-only flip)")

# --- _advance_in_flight: poll dispatch ----------------------------------------
print("advance_in_flight: poll progress, engine outages, missing handles")
s = fresh_session()
eng = use_engine(StubEngine(poll_result={"state": STATE_PROCESSING, "progress": 55}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v_new = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id=None,
                   last_attempt_at=NOW)
v_run = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="run-1",
                   last_attempt_at=NOW, render_progress=10)
render_loop._advance_in_flight(s)
s.commit()
ok(s.get(Video, v_new.id).status == VideoStatus.RENDERING and eng.poll_calls == ["run-1"],
   "a render without an engine handle is skipped; the handled one is polled")
ok(s.get(Video, v_run.id).render_progress == 55, "polled progress lands on the video")

s = fresh_session()
eng = use_engine(StubEngine(poll_result={"state": STATE_PROCESSING, "progress": None}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="k",
               last_attempt_at=NOW, render_progress=33)
render_loop._advance_in_flight(s)
s.commit()
ok(s.get(Video, v.id).render_progress == 33,
   "falsy polled progress keeps the previous value")

s = fresh_session()
eng = use_engine(StubEngine(poll_raises=True))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="x",
               last_attempt_at=NOW, render_progress=20)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.RENDERING and v.render_progress == 20 and v.error is None,
   "engine unreachable -> video untouched, retried next tick")
ok(render_runs(s, "error") == [], "an engine outage logs no error JobRun")

# --- _advance_in_flight: engine-reported failures -----------------------------
print("advance_in_flight: transient failures re-queue with a bounded retry")
s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED, "error": "upstream 529 overloaded"}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t1",
               last_attempt_at=NOW, render_progress=80)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.QUEUED and v.retry_count == 1 and v.mpt_task_id is None
   and v.render_progress == 0 and v.error is None,
   "transient error -> QUEUED for a clean re-render (handle/progress/error reset)")
ok(any("transient error (retry 1/2)" in (r.detail or "") for r in render_runs(s, "error")),
   "retry JobRun names the attempt budget")

# Midpoint of the retry bound (review finding: endpoints alone let a halved
# budget pass), with a rate-limit-only signature so classification is pinned
# beyond one multi-matching error string.
s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED,
                                   "error": "RateLimitError: too many requests"}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t1b",
               last_attempt_at=NOW, retry_count=1)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.QUEUED and v.retry_count == 2,
   "retry_count=1 still gets the second retry; a rate-limit-only signature is transient")
ok(any("transient error (retry 2/2)" in (r.detail or "") for r in render_runs(s, "error")),
   "second-retry JobRun says 2/2")

s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED, "error": "upstream 529 overloaded"}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t2",
               last_attempt_at=NOW, retry_count=2)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.FAILED and v.error == "upstream 529 overloaded",
   "transient error with the retry budget spent -> FAILED, error kept")

s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED, "error": "ffmpeg exploded"}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t3",
               last_attempt_at=NOW)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.FAILED and v.error == "ffmpeg exploded" and v.retry_count == 0,
   "non-transient error -> FAILED immediately, no retry burned")
ok(set(issues.TRANSIENT_SIGNATURES) == set(render_loop._TRANSIENT),
   "issues.TRANSIENT_SIGNATURES stays in lockstep with render_loop._TRANSIENT")

# 2026-08-13 overnight: litellm 600s / Anthropic disconnect were treated as
# terminal. Same retry path as 529. Real error strings from v956 / v962.
s = fresh_session()
use_engine(StubEngine(poll_result={
    "state": STATE_FAILED,
    "error": "Timeout: litellm.Timeout: AnthropicException - "
             "litellm.Timeout: Connection timed out after 600.0 seconds.",
}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t-llm-to",
               last_attempt_at=NOW, render_progress=5)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.QUEUED and v.retry_count == 1 and v.mpt_task_id is None
   and v.render_progress == 0 and v.error is None,
   "litellm/Anthropic Timeout -> QUEUED transient retry (08-13 v956 shape)")

s = fresh_session()
use_engine(StubEngine(poll_result={
    "state": STATE_FAILED,
    "error": "InternalServerError: litellm.InternalServerError: AnthropicException - "
             "Server disconnected without sending a response.. "
             "Handle with `litellm.InternalServerError`.",
}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t-llm-dc",
               last_attempt_at=NOW, render_progress=5)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.QUEUED and v.retry_count == 1 and v.mpt_task_id is None,
   "Anthropic Server disconnected / InternalServerError -> QUEUED (08-13 v962 shape)")

# Isolated signatures: a _TRANSIENT list that dropped one new token would
# still pass the multi-matching v956/v962 strings (08-02 endpoints-only lesson).
# Bare "timed out" is deliberately NOT a token — CLI TimeoutExpired must fail.
for _sig, _label in (
    ("litellm.Timeout", "litellm.Timeout alone"),
    ("Connection timed out after 600.0 seconds.", "Connection timed out alone"),
    ("InternalServerError", "InternalServerError alone"),
    ("Server disconnected", "Server disconnected alone"),
):
    s = fresh_session()
    use_engine(StubEngine(poll_result={"state": STATE_FAILED, "error": _sig}))
    ch = make_channel(s)
    t = make_topic(s, ch, content_format="short")
    v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="iso",
                   last_attempt_at=NOW)
    render_loop._advance_in_flight(s)
    s.commit()
    ok(s.get(Video, v.id).status == VideoStatus.QUEUED,
       f"{_label} is classified transient")

s = fresh_session()
use_engine(StubEngine(poll_result={
    "state": STATE_FAILED,
    "error": "TimeoutExpired: Command '['npx', 'hyperframes']' timed out after 1800 seconds",
}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="cli-to",
               last_attempt_at=NOW)
render_loop._advance_in_flight(s)
s.commit()
v = s.get(Video, v.id)
ok(v.status == VideoStatus.FAILED and v.retry_count == 0
   and "timed out after 1800" in (v.error or ""),
   "CLI/npx TimeoutExpired is terminal (bare 'timed out' is not a transient token)")

s = fresh_session()
use_engine(StubEngine(poll_result={"state": STATE_FAILED}))
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="t4",
               engine="fake", last_attempt_at=NOW)
render_loop._advance_in_flight(s)
s.commit()
ok(s.get(Video, v.id).error == "fake reported render failure",
   "failure without an engine error message gets the per-engine default")

# --- _finalize: the last gate before APPROVED -> auto-publish -----------------
_tmp = tempfile.mkdtemp(prefix="verify-render-")
_orig_storage = settings.storage_dir
settings.storage_dir = _tmp

frames_ok = {"value": True}
frames_calls = []


def _fake_frames(p):
    frames_calls.append(Path(p))
    return frames_ok["value"]


thumb_ok = {"value": True}


def _fake_thumb(src, out):
    if thumb_ok["value"]:
        Path(out).write_bytes(b"jpg")
        return True
    return False


class MetaStub:
    def __init__(self):
        self.calls = []

    def generate(self, subject, script, content_format="short", language=None):
        self.calls.append((subject, script, content_format, language))
        return {"title": "gen-title", "description": "gen-desc", "tags": ["t1", "t2"]}


render_loop._has_visible_frames = _fake_frames
render_loop._make_thumbnail = _fake_thumb
meta_stub = MetaStub()
render_loop.metadata = meta_stub

_src_n = 0


def drive_complete(s, ch, t, task=None, final=None, **video_kw):
    """Create a RENDERING video and drive it through STATE_COMPLETE -> _finalize."""
    global _src_n
    if final is None:
        _src_n += 1
        final = Path(_tmp) / f"src-{_src_n}.mp4"
        final.write_bytes(b"good-bytes")
    use_engine(StubEngine(poll_result={"state": STATE_COMPLETE, **(task or {})},
                          final=final))
    v = make_video(s, ch, t, status=VideoStatus.RENDERING, mpt_task_id="fin",
                   engine="fake", last_attempt_at=NOW, **video_kw)
    render_loop._advance_in_flight(s)
    s.commit()
    return s.get(Video, v.id)


print("finalize: complete-but-missing artifact fails the video")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = drive_complete(s, ch, t, final=Path(_tmp) / "nowhere" / "final.mp4")
ok(v.status == VideoStatus.FAILED and "missing" in (v.error or ""),
   "engine says complete but the file is gone -> FAILED, error says missing")
ok(v.video_path is None, "no artifact path recorded for a missing render")
ok(len(render_runs(s, "error")) == 1 and render_runs(s, "success") == [],
   "missing artifact logs an error JobRun, never a success")

print("finalize: blank frames refused at the last gate before publish")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
frames_ok["value"] = False
v = drive_complete(s, ch, t)
frames_ok["value"] = True
dest = Path(settings.storage_dir) / "videos" / str(v.id) / "video.mp4"
ok(v.status == VideoStatus.FAILED
   and v.error == "post-render frames blank at finalize — not publishing",
   "blank render -> FAILED, never REVIEW/APPROVED")
ok(dest.exists() and v.video_path == str(dest),
   "blank artifact still copied into storage for inspection")
ok(frames_calls[-1] == dest,
   "the blank check judges the COPIED artifact, not the engine's source file")
ok(render_runs(s, "success") == [], "no success JobRun for a blank render")

print("finalize: happy path copies, thumbnails, generates metadata -> REVIEW")
s = fresh_session()
ch = make_channel(s)                                  # default_skip_gate False
# Bind a real pt-BR voice profile: with no profile, channel_language returns
# None, which a severed `language=` wire would also produce — the pin below
# must distinguish the two (review finding, 2026-08-02).
prof = RenderProfile(name="pt-voice", channel_id=ch.id,
                     params_json='{"voice_name": "pt-BR-AntonioNeural-Male"}')
s.add(prof)
s.commit()
ch.default_render_profile_id = prof.id
s.add(ch)
s.commit()
t = make_topic(s, ch, content_format="long")
v = drive_complete(s, ch, t, task={"script": "spoken words",
                                   "creation_config": {"beat": 7}})
dest = Path(settings.storage_dir) / "videos" / str(v.id) / "video.mp4"
ok(v.status == VideoStatus.REVIEW and v.render_progress == 100,
   "gated channel routes the finished render to REVIEW at 100%")
ok(dest.read_bytes() == b"good-bytes" and v.video_path == str(dest),
   "artifact copied to storage/videos/<id>/video.mp4")
ok(v.script == "spoken words", "engine-reported script adopted")
ok(v.creation_config == json.dumps({"beat": 7}), "creation_config stored as JSON")
ok(v.thumb_path == str(dest.parent / "thumb.jpg"), "thumbnail path recorded")
ok(v.metadata_generated and v.title == "gen-title" and v.description == "gen-desc"
   and v.tags_json == json.dumps(["t1", "t2"]),
   "missing metadata generated and stored")
ok(meta_stub.calls[-1] == ("Test subject", "spoken words", "long", "Brazilian Portuguese"),
   "metadata.generate is wired to the channel's real voice language, not a constant")
ok(len(render_runs(s, "success")) == 1, "success JobRun logged once")

print("finalize: existing fields survive; metadata_generated short-circuits")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = drive_complete(s, ch, t, title="Keep me", script="original")
ok(v.title == "Keep me" and v.description == "gen-desc",
   "pre-set title kept, only the missing fields are filled")
ok(v.script == "original", "task without a script keeps the video's own")
n_calls = len(meta_stub.calls)
v2 = drive_complete(s, ch, t, metadata_generated=True)
ok(len(meta_stub.calls) == n_calls and v2.title is None,
   "metadata_generated=True -> generate() never called again")

thumb_ok["value"] = False
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
v = drive_complete(s, ch, t)
thumb_ok["value"] = True
ok(v.status == VideoStatus.REVIEW and v.thumb_path is None,
   "thumbnail failure is best-effort — video still finishes, thumb_path unset")

print("finalize: skip-gate resolution routes REVIEW vs APPROVED")
s = fresh_session()
ch = make_channel(s, default_skip_gate=True)
t = make_topic(s, ch, content_format="short")
v = drive_complete(s, ch, t)                          # video.skip_gate None
ok(v.status == VideoStatus.APPROVED and v.approved_at is not None,
   "skip_gate None inherits channel default True -> APPROVED with approved_at")
v = drive_complete(s, ch, t, skip_gate=False)
ok(v.status == VideoStatus.REVIEW,
   "video skip_gate False overrides channel True -> REVIEW")
s = fresh_session()
ch = make_channel(s)                                  # default_skip_gate False
t = make_topic(s, ch, content_format="short")
v = drive_complete(s, ch, t, skip_gate=True)
ok(v.status == VideoStatus.APPROVED,
   "video skip_gate True overrides channel False -> APPROVED")

settings.storage_dir = _orig_storage

# --- tick: scheduler_paused halts the whole loop ------------------------------
print("tick: scheduler_paused gates every stage")
s = fresh_session()
use_scope(s)
use_engine(StubEngine())
cfg = app_settings(s)
cfg.scheduler_paused = True
s.add(cfg)
s.commit()
ch = make_channel(s)
t = make_topic(s, ch, content_format="short")
q = make_video(s, ch, t, status=VideoStatus.QUEUED)
render_loop.tick()
ok(s.get(Video, q.id).status == VideoStatus.QUEUED,
   "paused: queued work is not submitted")
cfg.scheduler_paused = False
s.add(cfg)
s.commit()
render_loop.tick()
ok(s.get(Video, q.id).status == VideoStatus.RENDERING,
   "unpaused: the same tick submits it")

print(f"\nALL {_checks} CHECKS PASSED")
