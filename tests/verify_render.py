"""Dependency-free regression checks for render_loop's budget policy:
_auto_produce (DRAFT -> QUEUED promotion) and _submit_new (QUEUED -> RENDERING).

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

print(f"\nALL {_checks} CHECKS PASSED")
