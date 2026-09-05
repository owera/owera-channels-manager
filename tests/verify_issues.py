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
  - remaining detect() branches the 07-19 first cut skipped (backlog #7,
    2026-08-23): board_inventory.by_format (the growth-agent 1L+4S mix
    signal shipped 08-10 with zero pins), overflow weight-4 cap / inactive
    skip / parked weight<=0 skip / exact-ceiling, daily_render_budget<=0 inventory skip, pipeline
    remaining (inactive/missing-topic render_starved, publish-budget 0,
    in-flight projection, producible cap at one render-budget-day),
    stuck-rendering updated_at fallback, daily_limit_hit quota wall.
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


def make_topic(session, channel, **kw):
    t = Topic(channel_id=channel.id, name=kw.pop("name", "T"),
              theme_prompt=kw.pop("theme_prompt", "x"), **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


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
ok(issues._is_transient("grok.Timeout: grok -p timed out after 300s") is True,
   "_is_transient matches grok.Timeout (CLI adapter, not npx TimeoutExpired)")
ok(issues._is_transient(
       "NoAudioReceived: No audio was received. Please verify that your parameters are correct.")
   is True,
   "_is_transient matches NoAudioReceived (edge-tts empty stream, 08-31 v1213)")
ok(issues._is_transient(
       "BlockingIOError: [Errno 35] Resource temporarily unavailable")
   is True,
   "_is_transient matches BlockingIOError (09-03 midnight EAGAIN storm)")
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
               "board_overflow", "bgm_pool_low", "board_inventory",
               "pipeline_starved"):
    ok(bucket in d, f"digest always carries the '{bucket}' bucket")
ok(d["pipeline_starved"] == [],
   "a channel that has never published is 'not started', not starved (no false positive)")


# --- detect(): pipeline starvation ------------------------------------------
# The 2026-07-18 -> 07-23 silent stall (agent stopped producing; render loop starved
# behind a full draft bench) is now owned by render_loop._auto_produce, which queues
# active-topic drafts into free render capacity every tick. The digest must therefore
# only call starvation on what auto-produce CANNOT heal: drafts stranded on
# parked/inactive/missing topics, or a projected approved buffer (ready + in-flight +
# producible capped at one render-budget-day) that still misses a publish day.
print("detect (pipeline starvation: only what auto-produce cannot heal)")

# Active-topic drafts + free render slots + a thin approved buffer: auto-produce's
# next pass owns this. A mid-cycle digest read must NOT cry starvation — this exact
# shape (ready 3 vs budget 5 at noon, bench full of producible drafts) fired
# publish_starved every day 07-27→30 while the midnight refill covered it every time.
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
t_active = Topic(channel_id=ch.id, name="active", theme_prompt="x", weight=1, active=True)
s.add(t_active)
s.commit()
s.refresh(t_active)
for i in range(10):
    make_video(s, ch, topic_id=t_active.id, status=VideoStatus.DRAFT, position=i)
for i in range(3):
    make_video(s, ch, topic_id=t_active.id, status=VideoStatus.APPROVED, position=50 + i)
make_video(s, ch, topic_id=t_active.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
ok(d["pipeline_starved"] == [],
   "producible drafts cover the publish gap (3 approved + 5 of 10 drafts >= 5/day) "
   "-> no starvation from a mid-cycle read")

# The same shape with every draft on a weight-0 topic: auto-produce skips them all
# (same filter), so both starvation kinds fire — the parked-bench deadlock is the
# one the agent must actually judge.
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
t_parked = Topic(channel_id=ch.id, name="parked", theme_prompt="x", weight=0, active=True)
s.add(t_parked)
s.commit()
s.refresh(t_parked)
for i in range(10):
    make_video(s, ch, topic_id=t_parked.id, status=VideoStatus.DRAFT, position=i)
make_video(s, ch, topic_id=t_parked.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
kinds = {e["kind"] for e in d["pipeline_starved"]}
ok("render_starved" in kinds,
   "drafts stranded on a weight-0 topic + free render slots -> render_starved")
ok("publish_starved" in kinds,
   "no producible drafts and 0 approved against a 5/day budget -> publish_starved")
ok(all(e["auto"] for e in d["pipeline_starved"]),
   "both starvation kinds are agent-fixable (auto), not operator escalations")
ok(d["summary"]["clean"] is False, "a starved pipeline is NOT reported as a clean system")
inv = d["board_inventory"][0]
ok(inv["drafts"] == 10 and inv["queued"] == 0,
   "board_inventory splits drafts vs queued so 'at_capacity' can't hide an empty render queue")
ok(inv["at_capacity"] is True,
   "the misleading at_capacity=True still holds — which is exactly why the split is needed")

# An empty bench (no drafts at all) with a thin approved buffer -> publish_starved
# still fires (nothing can refill overnight), while render_starved stays quiet
# (nothing to produce is autofill's problem, not a production stall).
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
t_active = Topic(channel_id=ch.id, name="active", theme_prompt="x", weight=1, active=True)
s.add(t_active)
s.commit()
s.refresh(t_active)
make_video(s, ch, topic_id=t_active.id, status=VideoStatus.APPROVED, position=0)
make_video(s, ch, topic_id=t_active.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
kinds = {e["kind"] for e in d["pipeline_starved"]}
ok(kinds == {"publish_starved"},
   "empty bench + 1 approved vs 5/day -> publish_starved fires, render_starved does not")
e = [x for x in d["pipeline_starved"] if x["kind"] == "publish_starved"][0]
ok(e["projected"] == 1 and e["producible"] == 0,
   "publish_starved reports the projection breakdown (approved+in_flight+producible)")

# work actually queued -> the render loop has something to do -> not render_starved
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
for i in range(10):
    make_video(s, ch, status=VideoStatus.DRAFT, position=i)
for i in range(5):
    make_video(s, ch, status=VideoStatus.QUEUED, position=50 + i)
for i in range(9):
    make_video(s, ch, status=VideoStatus.APPROVED, position=70 + i)
make_video(s, ch, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
ok(d["pipeline_starved"] == [],
   "queued work + a full approved buffer -> no starvation issue at all")

# drafts waiting but the day's render budget is already spent -> normal, not starved
s = fresh_session()
ch = make_channel(s, daily_render_budget=2, daily_publish_budget=5)
for i in range(10):
    make_video(s, ch, status=VideoStatus.DRAFT, position=i)
for i in range(9):
    make_video(s, ch, status=VideoStatus.APPROVED, position=70 + i)
make_video(s, ch, status=VideoStatus.PUBLISHED, position=99)
for i in range(2):     # two renders already completed today = budget spent
    s.add(JobRun(channel_id=ch.id, kind="render", status="success"))
s.commit()
d = issues.detect(s)
ok(d["pipeline_starved"] == [],
   "no render headroom left today -> an empty queue is expected, not starvation")

# a paused channel is the operator's choice, never a starvation alarm
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5, paused=True)
for i in range(10):
    make_video(s, ch, status=VideoStatus.DRAFT, position=i)
make_video(s, ch, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
ok(d["pipeline_starved"] == [], "a paused channel is never flagged as starved")


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


# --- detect(): remaining branches (by_format mix + overflow/pipeline/stuck/quota)
# The 08-10 by_format split is the growth agent's 1L+4S mix signal (quoted every
# run from GET /api/agent/issues). It shipped with zero pins: a swapped
# long/short predicate, a dropped status filter, or a cross-channel leak would
# silently poison every mix decision. Same pass covers the overflow/pipeline
# gates the 07-19 cut named but did not discriminate.
print("detect (board_inventory.by_format — growth-agent mix signal)")

s = fresh_session()
ch = make_channel(s, slug="mix-a", name="MixA", daily_render_budget=20)
sib = make_channel(s, slug="mix-b", name="MixB", daily_render_budget=20)
t_long = make_topic(s, ch, name="long-a", content_format="long")
t_short = make_topic(s, ch, name="short-a", content_format="short")
t_empty = make_topic(s, ch, name="empty-a", content_format="")
t_case = make_topic(s, ch, name="case-a", content_format="LONG")
t_sib_long = make_topic(s, sib, name="long-b", content_format="long")
t_sib_short = make_topic(s, sib, name="short-b", content_format="short")
# ch mix: draft 2L+3S(+1 empty as S)(+1 LONG as S), queued 1L+4S, approved 1L+2S
# + 1 draft whose topic row is missing (inner join drops it from by_format).
for i in range(2):
    make_video(s, ch, topic_id=t_long.id, status=VideoStatus.DRAFT, position=i)
for i in range(3):
    make_video(s, ch, topic_id=t_short.id, status=VideoStatus.DRAFT, position=10 + i)
make_video(s, ch, topic_id=t_empty.id, status=VideoStatus.DRAFT, position=20)
make_video(s, ch, topic_id=t_case.id, status=VideoStatus.DRAFT, position=21)
make_video(s, ch, topic_id=999, status=VideoStatus.DRAFT, position=22)  # no Topic row
make_video(s, ch, topic_id=t_long.id, status=VideoStatus.QUEUED, position=30)
for i in range(4):
    make_video(s, ch, topic_id=t_short.id, status=VideoStatus.QUEUED, position=40 + i)
make_video(s, ch, topic_id=t_long.id, status=VideoStatus.APPROVED, position=50)
for i in range(2):
    make_video(s, ch, topic_id=t_short.id, status=VideoStatus.APPROVED, position=60 + i)
# sibling inverted mix: draft 5L+0S, queued 0L+1S, approved 0 — a dropped
# channel_id filter mutant leaks these into MixA or MixA into MixB.
for i in range(5):
    make_video(s, sib, topic_id=t_sib_long.id, status=VideoStatus.DRAFT, position=i)
make_video(s, sib, topic_id=t_sib_short.id, status=VideoStatus.QUEUED, position=10)
d = issues.detect(s)
inv_by_id = {row["channel_id"]: row for row in d["board_inventory"]}
ok(set(inv_by_id) == {ch.id, sib.id},
   "board_inventory has one row per channel with daily_render_budget > 0")
a = inv_by_id[ch.id]
ok(set(a["by_format"]) == {"draft", "queued", "approved"},
   "by_format keys are exactly draft/queued/approved")
ok(a["by_format"]["draft"] == {"long": 2, "short": 5},
   "draft split: 2 long + 3 short + empty-format + LONG-case as short "
   "(!= 'long', not == 'short'); missing-topic draft is NOT in the split")
ok(a["by_format"]["queued"] == {"long": 1, "short": 4},
   "queued split is 1 long + 4 short (status filter is per-bucket, not pending)")
ok(a["by_format"]["approved"] == {"long": 1, "short": 2},
   "approved split is 1 long + 2 short — the mix-buffer the agent quotes")
ok(a["drafts"] == 8 and a["queued"] == 5,
   "drafts/queued totals still count the missing-topic row (join is by_format-only)")
ok(a["by_format"]["draft"]["long"] + a["by_format"]["draft"]["short"] == 7,
   "by_format.draft longs+shorts is 7 against drafts=8 — inner join drops unbound")
ok(a["at_capacity"] is False,
   "pending 13 / budget 20 = 0.7d < horizon 2 → not at_capacity")
b = inv_by_id[sib.id]
ok(b["by_format"]["draft"] == {"long": 5, "short": 0},
   "sibling draft is 5 long / 0 short — MixA's shorts did not leak")
ok(b["by_format"]["queued"] == {"long": 0, "short": 1},
   "sibling queued is the one short, not MixA's 1L+4S")
ok(b["by_format"]["approved"] == {"long": 0, "short": 0},
   "sibling approved stays zero (MixA's 1L+2S did not leak)")

# daily_render_budget <= 0 skips the inventory row entirely (no /0, no phantom).
s = fresh_session()
zero = make_channel(s, slug="zero", name="Zero", daily_render_budget=0)
live = make_channel(s, slug="live", name="Live", daily_render_budget=5)
t_zero = make_topic(s, zero, name="z")
t_live = make_topic(s, live, name="l")
make_video(s, zero, topic_id=t_zero.id, status=VideoStatus.DRAFT)
make_video(s, live, topic_id=t_live.id, status=VideoStatus.DRAFT)
d = issues.detect(s)
ok(len(d["board_inventory"]) == 1 and d["board_inventory"][0]["channel_id"] == live.id,
   "daily_render_budget=0 emits no inventory row; budget>0 sibling still does")
ok(d["board_inventory"][0]["by_format"]["draft"] == {"long": 0, "short": 1},
   "the surviving row's by_format is the live channel's, not the zero-budget one")

# empty board still carries the zero split (agent can quote without a KeyError)
s = fresh_session()
make_channel(s, daily_render_budget=6)
d = issues.detect(s)
bf = d["board_inventory"][0]["by_format"]
ok(bf == {"draft": {"long": 0, "short": 0},
          "queued": {"long": 0, "short": 0},
          "approved": {"long": 0, "short": 0}},
   "a channel with no videos still exposes by_format zeros, not a missing key")


print("detect (overflow remaining: inactive skip, weight-4 cap, parked skip, exact ceiling)")

# inactive topics are excluded from overflow even with a huge pending pile
s = fresh_session()
ch = make_channel(s)
t_off = make_topic(s, ch, name="off", active=False, weight=4)
for i in range(30):
    make_video(s, ch, topic_id=t_off.id, status=VideoStatus.DRAFT, position=i)
d = issues.detect(s)
ok(d["board_overflow"] == [],
   "an inactive topic never overflows (overflow iterates active==True only)")

# weight-4 cap: weight=5 still multiplies by 4, not 5. ceiling_base=6 → 24.
# 24 pending is NOT overflow (`>` not `>=`); 25 is.
s = fresh_session()
ch = make_channel(s, daily_render_budget=40)
t_w5 = make_topic(s, ch, name="w5", weight=5, active=True)
for i in range(24):
    make_video(s, ch, topic_id=t_w5.id, status=VideoStatus.DRAFT, position=i)
d = issues.detect(s)
ok(d["board_overflow"] == [],
   "weight=5 ceiling is 6*4=24, not 6*5; pending==ceiling is not overflow")
make_video(s, ch, topic_id=t_w5.id, status=VideoStatus.DRAFT, position=24)
d = issues.detect(s)
ok(len(d["board_overflow"]) == 1 and d["board_overflow"][0]["ceiling"] == 24
   and d["board_overflow"][0]["pending"] == 25,
   "the 25th pending on a weight-5 topic overflows at the weight-4-capped ceiling")
ok(d["board_overflow"][0]["auto"] is True, "board overflow is agent-fixable (auto)")

# parked weight=0 is skipped (same gate as autofill/auto-produce). A `t.weight
# or 1` ceiling would treat 0 as 1 and fire overflow with auto=True "produce
# or trim drafts" — undoing the growth agent's park. Skip, don't re-scale.
s = fresh_session()
ch = make_channel(s)
t_park = make_topic(s, ch, name="parked", weight=0, active=True)
t_live = make_topic(s, ch, name="live", weight=1, active=True)
for i in range(7):
    make_video(s, ch, topic_id=t_park.id, status=VideoStatus.DRAFT, position=i)
    make_video(s, ch, topic_id=t_live.id, status=VideoStatus.DRAFT, position=i)
d = issues.detect(s)
ok(len(d["board_overflow"]) == 1 and d["board_overflow"][0]["name"] == "live",
   "a parked weight=0 topic never overflows, even with pending > the weight-1 ceiling")
ok(d["board_overflow"][0]["pending"] == 7 and d["board_overflow"][0]["ceiling"] == 6,
   "the sibling live topic still overflows at the weight-1 ceiling (isolation)")

# Negative weight is also parked (autofill `weight <= 0`). Distinguishes
# `if not t.weight` (0-only, because NOT NULL) from `<= 0`. Schema forbids
# NULL so a None-defaults-to-1 pin would be vacuous.
s = fresh_session()
ch = make_channel(s)
t_neg = make_topic(s, ch, name="neg", weight=-1, active=True)
for i in range(7):
    make_video(s, ch, topic_id=t_neg.id, status=VideoStatus.DRAFT, position=i)
d = issues.detect(s)
ok(d["board_overflow"] == [],
   "weight=-1 is parked (weight<=0), not overflowing at a 1× ceiling")

# exact ceiling on a weight-1 topic (the existing 7-pending case is one-over)
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch, name="exact", weight=1, active=True)
for i in range(6):
    make_video(s, ch, topic_id=t.id, status=VideoStatus.DRAFT, position=i)
d = issues.detect(s)
ok(d["board_overflow"] == [],
   "pending == ceiling (6) is not overflow; the gate is `>` not `>=`")


print("detect (pipeline remaining: inactive/missing topic, budget 0, projection)")

# drafts on an inactive (not parked) topic: auto-produce's active==True filter
# skips them → render_starved. Distinct from the weight-0 case already pinned.
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
t_inact = make_topic(s, ch, name="inact", weight=1, active=False)
for i in range(4):
    make_video(s, ch, topic_id=t_inact.id, status=VideoStatus.DRAFT, position=i)
make_video(s, ch, topic_id=t_inact.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
kinds = {e["kind"] for e in d["pipeline_starved"]}
ok("render_starved" in kinds,
   "drafts on an inactive topic + free render slots → render_starved")
ok("publish_starved" in kinds,
   "producible=0 (inactive filter) + 0 approved vs 5/day → publish_starved")

# missing topic row (inner join): same starvation, even with weight/active
# defaults the video can't see.
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
make_video(s, ch, topic_id=999, status=VideoStatus.DRAFT)
make_video(s, ch, topic_id=999, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
ok("render_starved" in {e["kind"] for e in d["pipeline_starved"]},
   "a draft whose topic row is missing is not producible → render_starved")

# daily_publish_budget=0: never publish_starved (the channel isn't trying to)
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=0)
t = make_topic(s, ch, name="live", weight=1, active=True)
make_video(s, ch, topic_id=t.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
ok(d["pipeline_starved"] == [],
   "daily_publish_budget=0 never flags publish_starved, even with 0 approved")

# in-flight RENDERING counts toward projected and blocks publish_starved
# (1 approved + 4 rendering + 0 producible = 5 == budget).
s = fresh_session()
ch = make_channel(s, daily_render_budget=5, daily_publish_budget=5)
t = make_topic(s, ch, name="live", weight=1, active=True)
make_video(s, ch, topic_id=t.id, status=VideoStatus.APPROVED, position=0)
for i in range(4):
    make_video(s, ch, topic_id=t.id, status=VideoStatus.RENDERING, position=10 + i)
make_video(s, ch, topic_id=t.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
ok(d["pipeline_starved"] == [],
   "1 approved + 4 rendering projects to the 5/day budget → not publish_starved")

# producible cap: 10 producible drafts, render budget 2, 0 approved/in-flight.
# projected = min(10, 2) = 2 < publish budget 5 → publish_starved.
# A dropped min() (projected = 10) would miss the day-budget cap and stay quiet.
s = fresh_session()
ch = make_channel(s, daily_render_budget=2, daily_publish_budget=5)
t = make_topic(s, ch, name="live", weight=1, active=True)
for i in range(10):
    make_video(s, ch, topic_id=t.id, status=VideoStatus.DRAFT, position=i)
make_video(s, ch, topic_id=t.id, status=VideoStatus.PUBLISHED, position=99)
d = issues.detect(s)
kinds = {e["kind"] for e in d["pipeline_starved"]}
ok(kinds == {"publish_starved"},
   "10 producible drafts but only 2 render slots still miss a 5/day publish budget")
e = d["pipeline_starved"][0]
ok(e["producible"] == 10 and e["projected"] == 2,
   "publish_starved.projected caps producible at daily_render_budget (min(10, 2))")


print("detect (stuck rendering falls back to updated_at; quota daily_limit_hit)")

s = fresh_session()
ch = make_channel(s)
old = utcnow() - timedelta(seconds=settings.render_timeout_seconds + 60)
make_video(s, ch, status=VideoStatus.RENDERING, last_attempt_at=None, updated_at=old)
d = issues.detect(s)
ok(len(d["stuck_rendering"]) == 1,
   "RENDERING with last_attempt_at=None is still stuck via updated_at")

# daily_limit_hit is a quota wall even with zero spend (the existing pin only
# covers the near-cap spend fraction). Prefix must match `quota exceeded:%`.
s = fresh_session()
ch = make_channel(s)
from app.services import quota
quota.log(s, kind="publish", status="error", channel_id=ch.id, quota_cost=0,
          detail="quota exceeded: [quotaExceeded] cooldown until 2026-08-24")
s.commit()
d = issues.detect(s)
ok(len(d["quota"]) == 1 and d["quota"][0]["daily_limit_hit"] is True
   and d["quota"][0]["quota_spent_today"] == 0,
   "daily_limit_hit (quota exceeded: prefix) is a quota wall at zero spend")
ok(d["quota"][0]["auto"] is False, "a daily-limit-hit wall is not auto")

# auto-only issues inflate total but not needs_operator
s = fresh_session()
ch = make_channel(s)
make_video(s, ch, status=VideoStatus.FAILED, error="ValueError",
           video_path=None, retry_count=0)
d = issues.detect(s)
ok(d["summary"]["total_issues"] >= 1 and d["summary"]["needs_operator"] == 0,
   "an auto-retryable FAILED video counts in total_issues, not needs_operator")

# last_detail of a signature group is the newest row (order_by created_at.desc)
s = fresh_session()
ch = make_channel(s)
s.add(JobRun(kind="render", status="error", channel_id=ch.id,
             detail="boom at attempt 1",
             created_at=utcnow() - timedelta(hours=2)))
s.add(JobRun(kind="render", status="error", channel_id=ch.id,
             detail="boom at attempt 2",
             created_at=utcnow() - timedelta(hours=1)))
s.commit()
groups = issues.detect(s)["error_runs_24h"]
ok(len(groups) == 1 and groups[0]["last_detail"] == "boom at attempt 2",
   "error_runs_24h.last_detail is the most recent row of the group, not the oldest")


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
