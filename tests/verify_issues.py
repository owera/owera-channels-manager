"""Dependency-free regression checks for the operational issues digest
(``app/services/issues.py``) — the growth agent's triage signal.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_issues.py

`issues.detect` classifies the whole system into buckets the growth agent
remediates over (via GET /api/agent/issues). It was entirely untested, yet its
`_failed_action` decision table encodes real publish-retry semantics — the
exact "auto-retry a permanently-stalling upload vs. escalate to the operator"
call that caused the ch2 stall incidents. A silent regression there would make
the agent auto-retry a wedged channel forever, or strand a fixable video.

Covers, dependency-free (in-memory SQLite, no network/creds):
  - the pure helpers: _aware / _age_hours / _is_transient / _signature
  - every branch of _failed_action (the publish-retry decision table)
  - detect(): each issue bucket populated, the auto vs needs-operator split,
    the recurring-error-signature grouping, board overflow/inventory, and the
    summary (total_issues / needs_operator / clean).
Exits non-zero on the first failed assertion.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# Importing app.models registers every table on SQLModel.metadata so
# create_all() below builds the full schema.
from app.config import settings
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video,
                        VideoStatus, utcnow)
from app.services import issues

CAP = settings.publish_max_retries              # 5
DEAD_DAYS = issues.DEAD_VIDEO_AGE_DAYS          # 7
MAX_RETRIES = issues.MAX_RETRIES                # 2 (transient render cap)
REVIEW_STALE = issues.REVIEW_STALE_HOURS        # 48
NEAR_CAP = issues.QUOTA_NEAR_CAP_FRACTION       # 0.9
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
                 oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED), **kw)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_video(session, channel, **kw):
    v = Video(channel_id=channel.id, topic_id=kw.pop("topic_id", 1),
              subject=kw.pop("subject", "Test subject"), **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


# --- pure helpers -----------------------------------------------------------
print("pure helpers (_aware / _age_hours / _is_transient / _signature)")

now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

ok(issues._aware(None) is None, "_aware(None) stays None")
naive = datetime(2026, 7, 19, 10, 0, 0)
ok(issues._aware(naive).tzinfo == timezone.utc, "_aware attaches UTC to a naive datetime")
aware = datetime(2026, 7, 19, 10, 0, 0, tzinfo=timezone.utc)
ok(issues._aware(aware) is aware, "_aware leaves an already-aware datetime untouched")

ok(issues._age_hours(None, now) is None, "_age_hours(None) is None")
ok(issues._age_hours(now - timedelta(hours=3), now) == 3.0, "_age_hours computes whole hours")
ok(issues._age_hours(naive, now) == 2.0,
   "_age_hours normalizes a naive (SQLite) datetime before subtracting")

ok(issues._is_transient("anthropic overloaded_error: retry") is True,
   "_is_transient matches a known transient signature")
ok(issues._is_transient("boom 503 upstream") is True, "_is_transient matches a bare 503")
ok(issues._is_transient("ValueError: bad script") is False,
   "_is_transient is False for a non-transient error")
ok(issues._is_transient(None) is False, "_is_transient(None) is False, never raises")

ok(issues._signature("Error at line 42 (attempt 3)") == "error at line (attempt )",
   "_signature lowercases and strips digits so ids/counts don't fragment groups")
ok(issues._signature("A   B\t C") == "a b c", "_signature collapses whitespace")
ok(issues._signature(None) == "", "_signature(None) is empty")
ok(len(issues._signature("x" * 200)) == 80, "_signature truncates to 80 chars")


# --- _failed_action: the publish-retry decision table -----------------------
print("_failed_action (every branch of the FAILED-video decision table)")

# has file + retries exhausted -> escalate (NOT auto): the stall incident guard
v = Video(channel_id=1, topic_id=1, subject="x", video_path="/tmp/x.mp4",
          retry_count=CAP)
ok(issues._failed_action(v, 1.0) == ("retry", False),
   "file present + retries exhausted -> retry but needs operator (no auto-retry into the same stall)")

# has file + under the cap -> auto re-approve (rendered ok, failed at publish)
v = Video(channel_id=1, topic_id=1, subject="x", video_path="/tmp/x.mp4",
          retry_count=CAP - 1)
ok(issues._failed_action(v, 1.0) == ("retry", True),
   "file present + under the retry cap -> auto retry (re-approve)")

# no file + transient error + under MAX_RETRIES -> auto requeue (re-render)
v = Video(channel_id=1, topic_id=1, subject="x", video_path=None,
          error="overloaded_error", retry_count=MAX_RETRIES - 1)
ok(issues._failed_action(v, 1.0) == ("requeue", True),
   "no file + transient error under the render cap -> auto requeue")

# no file + transient error but AT MAX_RETRIES + old -> falls through to delete
v = Video(channel_id=1, topic_id=1, subject="x", video_path=None,
          error="overloaded_error", retry_count=MAX_RETRIES)
ok(issues._failed_action(v, DEAD_DAYS * 24 + 1) == ("delete", True),
   "transient but render-cap-exhausted + old -> delete (not an infinite re-render)")

# no file + non-transient + old -> delete
v = Video(channel_id=1, topic_id=1, subject="x", video_path=None,
          error="ValueError", retry_count=0)
ok(issues._failed_action(v, DEAD_DAYS * 24 + 1) == ("delete", True),
   "no file + non-transient + older than the dead threshold -> auto delete")

# no file + non-transient + recent -> one more requeue
v = Video(channel_id=1, topic_id=1, subject="x", video_path=None,
          error="ValueError", retry_count=0)
ok(issues._failed_action(v, 1.0) == ("requeue", True),
   "no file + non-transient + recent -> one more render attempt")

# no file + unknown age (None) -> not old, so requeue
v = Video(channel_id=1, topic_id=1, subject="x", video_path=None,
          error="ValueError", retry_count=0)
ok(issues._failed_action(v, None) == ("requeue", True),
   "unknown age is treated as not-old -> requeue rather than delete")


# The BGM-pool bucket reads the real filesystem (settings.bgm_dir), which is
# not part of the in-memory DB. Neutralize it for the DB-focused cases below by
# dropping the low-pool threshold to 0 (pool_count is never < 0); a dedicated
# case at the end exercises the bucket with a controlled temp dir. Each verify_*
# file is its own process, so this global tweak can't leak into other suites.
settings.bgm_pool_min = 0

# --- detect(): a clean system ----------------------------------------------
print("detect (clean system)")

s = fresh_session()
make_channel(s)
d = issues.detect(s)
ok(d["summary"]["clean"] is True, "no issues -> clean is True")
ok(d["summary"]["total_issues"] == 0, "clean system reports zero total issues")
ok(d["summary"]["needs_operator"] == 0, "clean system needs no operator")
for bucket in ("failed", "rejected", "stuck_rendering", "stuck_publishing",
               "stuck_review", "oauth", "cooldown", "quota", "error_runs_24h",
               "board_overflow", "bgm_pool_low", "board_inventory"):
    ok(bucket in d, f"digest always carries the '{bucket}' bucket")


# --- detect(): failed / rejected buckets ------------------------------------
print("detect (failed + rejected classification)")

s = fresh_session()
ch = make_channel(s)
make_video(s, ch, status=VideoStatus.FAILED, error="ValueError",
           video_path=None, retry_count=0)
old = utcnow() - timedelta(days=DEAD_DAYS + 1)
make_video(s, ch, status=VideoStatus.REJECTED, rejected_reason="off-topic",
           updated_at=old)
make_video(s, ch, status=VideoStatus.REJECTED, rejected_reason="fresh")
d = issues.detect(s)
ok(len(d["failed"]) == 1, "one FAILED video surfaces in the failed bucket")
ok(d["failed"][0]["transient"] is False, "the failed entry carries a transient flag")
ok(len(d["rejected"]) == 2, "both REJECTED videos surface")
actions = {r["reason"]: r["suggested_action"] for r in d["rejected"]}
ok(actions["off-topic"] == "delete", "an old rejected video is a delete candidate")
ok(actions["fresh"] == "leave", "a recent rejected video is left in place")


# --- detect(): stuck buckets (age gates) ------------------------------------
print("detect (stuck rendering / publishing / review honor their age gates)")

s = fresh_session()
ch = make_channel(s)
# rendering past the render timeout -> stuck; a fresh one is ignored
make_video(s, ch, status=VideoStatus.RENDERING,
           last_attempt_at=utcnow() - timedelta(seconds=settings.render_timeout_seconds + 60))
make_video(s, ch, status=VideoStatus.RENDERING, last_attempt_at=utcnow())
# publishing past the publish timeout -> stuck
make_video(s, ch, status=VideoStatus.PUBLISHING,
           last_attempt_at=utcnow() - timedelta(seconds=settings.publish_timeout_seconds + 60))
# review older than the stale window -> backlog; a fresh review is ignored
make_video(s, ch, status=VideoStatus.REVIEW,
           updated_at=utcnow() - timedelta(hours=REVIEW_STALE + 1))
make_video(s, ch, status=VideoStatus.REVIEW, updated_at=utcnow())
d = issues.detect(s)
ok(len(d["stuck_rendering"]) == 1, "only the render past its timeout is stuck (fresh one ignored)")
ok(len(d["stuck_publishing"]) == 1, "the publish past its timeout is stuck")
ok(len(d["stuck_review"]) == 1, "only the stale review is a gate backlog (fresh one ignored)")


# --- detect(): channel health (oauth / cooldown / quota) --------------------
print("detect (channel health escalations)")

s = fresh_session()
# a disconnected channel -> oauth escalation (needs_operator)
bad = make_channel(s, slug="dead", name="Dead", oauth_status=OAuthStatus.EXPIRED,
                   oauth_error="invalid_grant")
# a healthy channel in cooldown -> monitor (needs_operator, not auto)
cool = make_channel(s, slug="cool", name="Cooling",
                    cooldown_until=utcnow() + timedelta(hours=2))
d = issues.detect(s)
ok(len(d["oauth"]) == 1 and d["oauth"][0]["channel_id"] == bad.id,
   "a non-CONNECTED channel surfaces in the oauth bucket")
ok(d["oauth"][0]["auto"] is False, "oauth reconnect is never auto — needs the operator")
ok(len(d["cooldown"]) == 1 and d["cooldown"][0]["channel_id"] == cool.id,
   "a channel whose cooldown is in the future surfaces in the cooldown bucket")
# needs_operator counts every auto=False item (oauth + cooldown here)
ok(d["summary"]["needs_operator"] == 2, "needs_operator counts each non-auto item")
ok(d["summary"]["clean"] is False, "a system with issues is not clean")

# a past cooldown does NOT surface
s = fresh_session()
make_channel(s, cooldown_until=utcnow() - timedelta(hours=1))
ok(len(issues.detect(s)["cooldown"]) == 0, "an expired cooldown is not reported")

# quota wall: spend at/over the near-cap fraction surfaces the channel
s = fresh_session()
ch = make_channel(s)
from app.services import quota
spend = int(settings.youtube_daily_quota_cap * NEAR_CAP) + 1
quota.log(s, kind="publish", status="success", channel_id=ch.id, quota_cost=spend)
s.commit()
d = issues.detect(s)
ok(len(d["quota"]) == 1, "a channel over the near-cap spend fraction hits the quota bucket")
ok(d["quota"][0]["auto"] is False, "a quota wall is a monitor/operator signal, not auto")


# --- detect(): recurring error signatures ----------------------------------
print("detect (recurring-error signature grouping over the last 24h)")

s = fresh_session()
ch = make_channel(s)
# three same-signature errors (differing only by digits) collapse to one group
for i in range(3):
    s.add(JobRun(kind="render", status="error", channel_id=ch.id,
                 detail=f"boom at attempt {i}",
                 created_at=utcnow() - timedelta(hours=1)))
# a different-kind error is its own group
s.add(JobRun(kind="publish", status="error", channel_id=ch.id, detail="quota exceeded",
             created_at=utcnow() - timedelta(hours=1)))
# an old error (>24h) is excluded from the window
s.add(JobRun(kind="render", status="error", channel_id=ch.id, detail="boom at attempt 9",
             created_at=utcnow() - timedelta(hours=30)))
# a success is never a signature
s.add(JobRun(kind="render", status="success", channel_id=ch.id,
             created_at=utcnow() - timedelta(hours=1)))
s.commit()
groups = issues.detect(s)["error_runs_24h"]
ok(len(groups) == 2, "two distinct (kind, signature) groups in the 24h window")
top = groups[0]  # sorted by count desc
ok(top["kind"] == "render" and top["count"] == 3,
   "the recurring render error collapses digit-varying details into one group of 3")


# --- detect(): board overflow + inventory -----------------------------------
print("detect (idea-board overflow + informational inventory)")

s = fresh_session()
ch = make_channel(s, daily_render_budget=6)
# ceiling_base = max(topic_autogen_min_pending=3, topic_autogen_target=6) = 6;
# weight 1 -> ceiling 6. Seed 7 pending (DRAFT+QUEUED) to overflow.
t = Topic(channel_id=ch.id, name="Overflowing", active=True, weight=1)
s.add(t)
s.commit()
s.refresh(t)
for i in range(7):
    st = VideoStatus.DRAFT if i % 2 == 0 else VideoStatus.QUEUED
    make_video(s, ch, topic_id=t.id, status=st)
d = issues.detect(s)
ok(len(d["board_overflow"]) == 1, "a topic over its pending ceiling overflows")
ok(d["board_overflow"][0]["pending"] == 7 and d["board_overflow"][0]["ceiling"] == 6,
   "overflow entry reports the pending count and the weight-scaled ceiling")
inv = d["board_inventory"]
ok(len(inv) == 1 and inv[0]["pending"] == 7,
   "board_inventory reports pending DRAFT+QUEUED per channel")
ok(inv[0]["days_of_inventory"] == round(7 / 6, 1),
   "days_of_inventory = pending / daily_render_budget")
# board_inventory is informational — it must NOT inflate the issue total
ok(d["summary"]["total_issues"] == len(d["board_overflow"]),
   "board_inventory is excluded from total_issues (only board_overflow counts here)")


# --- detect(): BGM pool health (filesystem-backed, controlled temp dir) ------
print("detect (BGM pool low -> replenish signal)")

with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = Path(tmp)
    settings.bgm_dir = str(tmp_dir)
    settings.bgm_pool_min = 5
    settings.bgm_pool_target = 15
    # empty dir -> pool_count 0 < min -> low-pool issue with a replenish action
    s = fresh_session()
    make_channel(s)
    d = issues.detect(s)
    ok(len(d["bgm_pool_low"]) == 1, "an empty BGM pool below the min surfaces one issue")
    entry = d["bgm_pool_low"][0]
    ok(entry["count"] == 0 and entry["need"] == 15,
       "low-pool entry reports the current count and the top-up need (target - count)")
    ok(entry["auto"] is True, "BGM replenish is auto-fixable (agent triggers a top-up)")
    # fill the pool to the min -> the issue clears (only techno_*.wav count)
    for i in range(5):
        (tmp_dir / f"techno_{i}.wav").write_bytes(b"x")
    (tmp_dir / "ignored.mp3").write_bytes(b"x")   # wrong ext -> not counted
    d = issues.detect(s)
    ok(len(d["bgm_pool_low"]) == 0, "a pool at the min threshold reports no BGM issue")

print(f"\nALL {_checks} CHECKS PASSED")
