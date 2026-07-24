"""Dependency-free regression checks for render_loop._auto_produce.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_render.py

_auto_produce closes the DRAFT -> QUEUED gap that caused the 07-18..07-23 stall:
nothing else in the app makes that transition, so a full bench of drafts starved
the render loop for 5 days while board_inventory read "at capacity". These checks
pin the promotion policy: budget/active headroom, weight-0 and paused exclusions,
and the long-form buffer guarantee.

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

print(f"\nALL {_checks} CHECKS PASSED")
