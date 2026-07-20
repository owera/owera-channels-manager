"""Dependency-free regression checks for the daily-counter / quota accounting
service (``app/services/quota.py``) — the money-path throttle that decides how
many videos a channel may publish/render today and when it may retry after
YouTube hands back a daily cap.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_quota.py

Why this matters: every counter here gates the publish loop. If
``published_today`` counted the wrong day, the drip would over- or under-publish;
if ``daily_limit_hit`` missed a ``quota exceeded:`` row the loop would hammer the
API every tick after a cap; if ``cooldown_until_for`` resumed at UTC midnight
instead of Pacific it would retry ~8h early and burn a guaranteed failure (the
exact reasoning in the docstrings). None of it was tested.

Covers, dependency-free (in-memory SQLite, no network/creds):
  - the time helpers: _next_pt_midnight_utc / next_quota_reset / cooldown_until_for
    / _quota_day_start / _day_start — tz-awareness, forward-only, and the
    upload-limit-vs-quota-reset branch of cooldown_until_for.
  - the DB counters against a controlled JobRun/Video/Topic set: the quota-day vs
    UTC-day boundary, the kind/status/channel filters, quota_spent coalesce, the
    long-form join, in-flight renders, last_publish_at, and log() truncation.
Exits non-zero on the first failed assertion.
"""
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

# Importing app.models registers every table on SQLModel.metadata so
# create_all() below builds the full schema.
from app.models import JobRun, Topic, Video, VideoStatus
from app.services import quota

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


def add_run(session, *, channel_id, kind, status, created_at,
            detail=None, quota_cost=0, video_id=None):
    session.add(JobRun(channel_id=channel_id, kind=kind, status=status,
                       detail=detail, quota_cost=quota_cost, video_id=video_id,
                       created_at=created_at))
    session.commit()


# --- time helpers -----------------------------------------------------------
print("time helpers (_next_pt_midnight_utc / next_quota_reset / _quota_day_start / _day_start)")

# A deterministic instant in the middle of a UTC day, well away from any midnight
# so tz math can't land on a boundary by accident.
NOW = datetime(2026, 7, 20, 15, 30, 0, tzinfo=timezone.utc)

nxt = quota._next_pt_midnight_utc(NOW)
ok(nxt.tzinfo is not None, "_next_pt_midnight_utc returns a tz-aware datetime")
ok(nxt > NOW, "_next_pt_midnight_utc is strictly in the future")
ok(nxt - NOW <= timedelta(hours=24), "next Pacific midnight is within 24h ahead")
# Converted to America/Los_Angeles it must be exactly midnight.
try:
    from zoneinfo import ZoneInfo
    nxt_pt = nxt.astimezone(ZoneInfo("America/Los_Angeles"))
    ok((nxt_pt.hour, nxt_pt.minute, nxt_pt.second) == (0, 0, 0),
       "next reset lands on Pacific midnight (00:00:00 local)")
except Exception:
    ok(True, "zoneinfo unavailable — Pacific-midnight local check skipped (fallback path)")

# next_quota_reset() is _next_pt_midnight_utc(now) — always ahead of real now.
nqr = quota.next_quota_reset()
ok(nqr > datetime.now(timezone.utc), "next_quota_reset() is in the future")
ok(nqr.tzinfo is not None, "next_quota_reset() is tz-aware")

# _quota_day_start(): most-recent Pacific midnight, <= now, and < now+ .. i.e. in the past.
qds = quota._quota_day_start()
now_real = datetime.now(timezone.utc)
ok(qds.tzinfo is not None, "_quota_day_start is tz-aware")
ok(qds <= now_real, "_quota_day_start is at or before now (a past boundary)")
ok(now_real - qds < timedelta(hours=25), "_quota_day_start is within the last ~24h")

# _day_start(): today's UTC midnight.
ds = quota._day_start()
ok((ds.hour, ds.minute, ds.second, ds.microsecond) == (0, 0, 0, 0),
   "_day_start is exactly UTC midnight")
ok(ds <= now_real and now_real - ds < timedelta(hours=24),
   "_day_start is today's UTC midnight (<= now, within 24h)")

# --- cooldown_until_for branch --------------------------------------------
print("\ncooldown_until_for (upload-limit rolling 24h vs quota Pacific-midnight reset)")

before = datetime.now(timezone.utc)
up = quota.cooldown_until_for("uploadLimitExceeded")
after = datetime.now(timezone.utc)
# Rolling 24h window: now + 24h (allow the tiny execution window).
ok(before + timedelta(hours=24) <= up <= after + timedelta(hours=24),
   "uploadLimitExceeded cools down ~24h from now (rolling window)")
ok(quota.cooldown_until_for("UPLOADLIMITEXCEEDED") - datetime.now(timezone.utc)
   > timedelta(hours=23), "uploadLimitExceeded match is case-insensitive")

# quotaExceeded / dailyLimitExceeded / unknown / None all wait for Pacific midnight.
qmid = quota._next_pt_midnight_utc(datetime.now(timezone.utc))
for reason in ("quotaExceeded", "dailyLimitExceeded", "somethingElse", None):
    got = quota.cooldown_until_for(reason)
    ok(abs((got - qmid).total_seconds()) < 5,
       f"cooldown_until_for({reason!r}) waits for the next Pacific midnight")

# --- DB counters: quota-day boundary + filters -----------------------------
print("\npublished_today / rendered_today (quota-day vs UTC-day boundary + filters)")

s = fresh_session()
CH, OTHER = 1, 2
qday = quota._quota_day_start()
uday = quota._day_start()
# Publishes this quota day for CH: two successes + noise that must NOT count.
add_run(s, channel_id=CH, kind="publish", status="success", created_at=qday + timedelta(minutes=1))
add_run(s, channel_id=CH, kind="publish", status="success", created_at=qday + timedelta(hours=2))
add_run(s, channel_id=CH, kind="publish", status="error", created_at=qday + timedelta(minutes=5))     # not success
add_run(s, channel_id=CH, kind="render", status="success", created_at=qday + timedelta(minutes=5))    # wrong kind
add_run(s, channel_id=CH, kind="publish", status="success", created_at=qday - timedelta(minutes=1))   # before boundary
add_run(s, channel_id=OTHER, kind="publish", status="success", created_at=qday + timedelta(minutes=1))  # other channel
ok(quota.published_today(s, CH) == 2, "published_today counts only this-quota-day successful publishes for the channel")
ok(quota.published_today(s, OTHER) == 1, "published_today is per-channel")
ok(quota.published_today(s, 999) == 0, "published_today is 0 for a channel with no runs")

# rendered_today keys off the UTC day, not the quota day.
s2 = fresh_session()
add_run(s2, channel_id=CH, kind="render", status="success", created_at=uday + timedelta(minutes=1))
add_run(s2, channel_id=CH, kind="render", status="error", created_at=uday + timedelta(minutes=1))      # not success
add_run(s2, channel_id=CH, kind="render", status="success", created_at=uday - timedelta(minutes=1))    # before UTC midnight
add_run(s2, channel_id=CH, kind="publish", status="success", created_at=uday + timedelta(minutes=1))   # wrong kind
ok(quota.rendered_today(s2, CH) == 1, "rendered_today counts only today's (UTC) successful renders")

# --- quota_spent_today: sum over all kinds/statuses, coalesce to 0 ----------
print("\nquota_spent_today (sum across kinds/statuses since quota-day start; coalesce)")

s3 = fresh_session()
ok(quota.quota_spent_today(s3, CH) == 0, "quota_spent_today is 0 (coalesced) when the channel has no runs")
add_run(s3, channel_id=CH, kind="publish", status="success", created_at=qday + timedelta(minutes=1), quota_cost=1600)
add_run(s3, channel_id=CH, kind="publish", status="error", created_at=qday + timedelta(minutes=2), quota_cost=50)   # errors still cost
add_run(s3, channel_id=CH, kind="render", status="success", created_at=qday + timedelta(minutes=3), quota_cost=0)
add_run(s3, channel_id=CH, kind="publish", status="success", created_at=qday - timedelta(minutes=1), quota_cost=999)  # before boundary
add_run(s3, channel_id=OTHER, kind="publish", status="success", created_at=qday + timedelta(minutes=1), quota_cost=1600)
ok(quota.quota_spent_today(s3, CH) == 1650,
   "quota_spent_today sums this-quota-day cost across kinds and statuses (1600+50+0)")

# --- last_publish_at --------------------------------------------------------
print("\nlast_publish_at (max created_at of successful publishes, else None)")

s4 = fresh_session()
ok(quota.last_publish_at(s4, CH) is None, "last_publish_at is None with no successful publishes")
early = qday + timedelta(hours=1)
late = qday + timedelta(hours=5)
add_run(s4, channel_id=CH, kind="publish", status="success", created_at=early)
add_run(s4, channel_id=CH, kind="publish", status="success", created_at=late)
add_run(s4, channel_id=CH, kind="publish", status="error", created_at=late + timedelta(hours=1))   # errors ignored
got_last = quota.last_publish_at(s4, CH)
# SQLite returns the datetime as naive; compare on the wall-clock value.
ok(got_last is not None and got_last.replace(tzinfo=timezone.utc) == late,
   "last_publish_at returns the most recent successful publish, ignoring errors")

# --- daily_limit_hit --------------------------------------------------------
print("\ndaily_limit_hit (a 'quota exceeded:' publish error this quota day)")

s5 = fresh_session()
ok(quota.daily_limit_hit(s5, CH) is False, "daily_limit_hit is False with no cap errors")
add_run(s5, channel_id=CH, kind="publish", status="error",
        detail="upload failed: transient network blip", created_at=qday + timedelta(minutes=1))
ok(quota.daily_limit_hit(s5, CH) is False, "an unrelated publish error does not trip daily_limit_hit")
add_run(s5, channel_id=CH, kind="publish", status="error",
        detail="quota exceeded: dailyLimitExceeded", created_at=qday + timedelta(minutes=2))
ok(quota.daily_limit_hit(s5, CH) is True, "a 'quota exceeded:' publish error trips daily_limit_hit")

s6 = fresh_session()
add_run(s6, channel_id=CH, kind="publish", status="error",
        detail="quota exceeded: quotaExceeded", created_at=qday - timedelta(minutes=1))  # previous quota day
ok(quota.daily_limit_hit(s6, CH) is False, "a cap error from before the quota-day start does not count")

# --- published_long_today (Video PUBLISHED joined to long-format Topic) -----
print("\npublished_long_today (long-form publishes this quota day, via Topic join)")

s7 = fresh_session()
long_topic = Topic(channel_id=CH, name="Deep dives", content_format="long")
short_topic = Topic(channel_id=CH, name="Shorts", content_format="short")
s7.add(long_topic); s7.add(short_topic); s7.commit()
s7.refresh(long_topic); s7.refresh(short_topic)


def add_video(session, *, channel_id, topic_id, status, published_at):
    v = Video(channel_id=channel_id, topic_id=topic_id, subject="x",
              status=status, published_at=published_at)
    session.add(v); session.commit()


ok(quota.published_long_today(s7, CH) == 0, "published_long_today is 0 before any long publish")
add_video(s7, channel_id=CH, topic_id=long_topic.id, status=VideoStatus.PUBLISHED,
          published_at=qday + timedelta(hours=1))
add_video(s7, channel_id=CH, topic_id=short_topic.id, status=VideoStatus.PUBLISHED,
          published_at=qday + timedelta(hours=1))                                   # short -> excluded
add_video(s7, channel_id=CH, topic_id=long_topic.id, status=VideoStatus.PUBLISHING,
          published_at=qday + timedelta(hours=1))                                   # not published -> excluded
add_video(s7, channel_id=CH, topic_id=long_topic.id, status=VideoStatus.PUBLISHED,
          published_at=qday - timedelta(hours=1))                                   # previous quota day -> excluded
add_video(s7, channel_id=OTHER, topic_id=long_topic.id, status=VideoStatus.PUBLISHED,
          published_at=qday + timedelta(hours=1))                                   # other channel -> excluded
ok(quota.published_long_today(s7, CH) == 1,
   "published_long_today counts only this-channel, this-quota-day, PUBLISHED, long-format videos")

# --- in_flight_renders (global RENDERING count) -----------------------------
print("\nin_flight_renders (global count of videos in RENDERING)")

s8 = fresh_session()
ok(quota.in_flight_renders(s8) == 0, "in_flight_renders is 0 when nothing is rendering")
add_video(s8, channel_id=CH, topic_id=1, status=VideoStatus.RENDERING, published_at=None)
add_video(s8, channel_id=OTHER, topic_id=1, status=VideoStatus.RENDERING, published_at=None)
add_video(s8, channel_id=CH, topic_id=1, status=VideoStatus.PUBLISHED, published_at=qday)
ok(quota.in_flight_renders(s8) == 2, "in_flight_renders counts RENDERING videos across all channels")

# --- log() ------------------------------------------------------------------
print("\nlog() (writes a JobRun, defaults, and truncates detail to 1000 chars)")

s9 = fresh_session()
quota.log(s9, kind="publish", status="success", channel_id=CH, video_id=7, quota_cost=1600)
s9.commit()
row = s9.exec(select(JobRun)).one()
ok(row.kind == "publish" and row.status == "success" and row.channel_id == CH
   and row.video_id == 7 and row.quota_cost == 1600,
   "log() persists a JobRun with the given fields")
ok(row.detail == "", "log() defaults a missing detail to '' (not None)")

s10 = fresh_session()
quota.log(s10, kind="publish", status="error", channel_id=CH, detail="e" * 5000)
s10.commit()
row2 = s10.exec(select(JobRun)).one()
ok(len(row2.detail) == 1000, "log() truncates an over-long detail to 1000 chars")
ok(row2.quota_cost == 0, "log() defaults quota_cost to 0")

print(f"\nALL {_checks} CHECKS PASSED")
