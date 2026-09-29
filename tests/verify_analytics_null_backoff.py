"""P1 2026-09-29 — analytics NULL (not 0) inside the reporting lag, 5xx backoff,
and one failed video no longer ends a channel's analytics pass.

Run: PYTHONPATH=. .venv/bin/python tests/verify_analytics_null_backoff.py

Before: ``fetch_video_analytics`` zero-filled an EMPTY API answer and
``record_video_snapshot`` stored views=0 / avg_view_pct=0 … for a 30h-old video
that simply had not been reported yet (24–72h lag) — a fabricated measurement
that dragged topic/format averages down. A single HttpError 5xx failed the
video, and the first failure of a pass stopped the whole channel for the tick.

Pins:
- fetch_video_analytics flags ``empty`` (values still zero-filled for callers)
- record_video_snapshot: empty + <72h → every metric NULL (traffic not queried);
  empty + ≥72h → real zeros; non-empty young → real values
- _execute_with_backoff: 5xx retried (3 attempts, 0.5s → 1s), 4xx never,
  quotaExceeded still classified; wired into the core + traffic queries
- _snapshot_channel: a failure followed by successes keeps going; 3 consecutive
  failures with no success stop the pass; tick isolates channel exceptions
- VideoMetric columns nullable; _relax_videometric_notnull rebuilds a pre-P1
  NOT NULL table once (rows / ids / indexes kept, idempotent)
- consumers (leaderboard, by-topic, monetization, backfill helper) treat NULL
  as "no data", never as zero, and never TypeError

In-memory / temp-file SQLite, stubbed analytics client, no network, never the
real manager.db. Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httplib2
from googleapiclient.errors import HttpError
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as app_db
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video, VideoMetric,
                        VideoStatus)
from app.routers import youtube_admin
from app.services import analytics_loop, youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(analytics_loop.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "analytics_loop loaded from this tree")


def http_error(status: int, reason: str = "") -> HttpError:
    resp = httplib2.Response({"status": status})
    resp.reason = "error"
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode() if reason else b"{}"
    return HttpError(resp, body)


class _Request:
    """Like googleapiclient's HttpRequest: execute() can be called again on the
    same object (that is what the backoff does); each call is one HTTP attempt."""

    def __init__(self, owner, kw):
        self.owner, self.kw = owner, kw

    def execute(self):
        self.owner.queries.append(self.kw)
        if not self.owner.responses:
            raise AssertionError(f"unexpected analytics call: {self.kw}")
        item = self.owner.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAnalytics:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []           # one entry per HTTP attempt

    def reports(self):
        return self

    def query(self, **kw):
        return _Request(self, kw)


EMPTY = {"rows": [], "columnHeaders": [{"name": "views"}]}
FULL = {"rows": [[9, 3, 40.0, 12.5, 2, 1, 0]], "columnHeaders": [
    {"name": "views"}, {"name": "estimatedMinutesWatched"},
    {"name": "averageViewPercentage"}, {"name": "averageViewDuration"},
    {"name": "likes"}, {"name": "comments"}, {"name": "subscribersGained"}]}
TRAFFIC = {"rows": [["SHORTS", 9, 3]]}
sleeps: list[float] = []
youtube._sleep = sleeps.append        # no real waiting in this suite

# ---------------------------------------------------------------------------
print("fetch_video_analytics flags empty answers")
got = youtube.fetch_video_analytics(FakeAnalytics([EMPTY]), "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["empty"] is True and got["views"] == 0, "empty rows → empty=True (values still zero-filled)")
got = youtube.fetch_video_analytics(FakeAnalytics([FULL]), "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["empty"] is False and got["views"] == 9, "a real row → empty=False")

# ---------------------------------------------------------------------------
print("5xx backoff")
sleeps.clear()
ana = FakeAnalytics([http_error(503, "backendError"), http_error(500), FULL])
got = youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["views"] == 9 and len(ana.queries) == 3, "two 5xx then success → value (3 attempts)")
ok(sleeps == [0.5, 1.0], f"exponential short backoff 0.5s → 1s (got {sleeps})")
sleeps.clear()
ana = FakeAnalytics([http_error(503)] * 3)
try:
    youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
    ok(False, "3× 5xx must raise")
except HttpError as e:
    ok(e.resp.status == 503 and len(ana.queries) == 3 and sleeps == [0.5, 1.0],
       "3× 5xx → raises after exactly 3 attempts (no sleep after the last)")
sleeps.clear()
ana = FakeAnalytics([http_error(403, "forbidden")])
try:
    youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
    ok(False, "403 must raise")
except HttpError:
    ok(len(ana.queries) == 1 and sleeps == [], "4xx is never retried")
ana = FakeAnalytics([http_error(403, "quotaExceeded")])
try:
    youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
    ok(False, "quota must raise")
except youtube.QuotaExceeded:
    ok(len(ana.queries) == 1, "quotaExceeded still classified, not retried")
sleeps.clear()
ana = FakeAnalytics([http_error(502), TRAFFIC])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(traf["sources"] == {"SHORTS": {"views": 9, "watch_min": 3}} and sleeps == [0.5],
   "traffic-source query retries a 5xx too")

# ---------------------------------------------------------------------------
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
s = Session(engine)
ch = Channel(slug="ana", name="Ana", oauth_status=OAuthStatus.CONNECTED,
             yt_channel_id="UC1", daily_publish_budget=0)
s.add(ch)
s.commit()
s.add(Topic(channel_id=ch.id, name="t", content_format="short"))
s.commit()
now = datetime.now(timezone.utc)


def pub_video(hours: float, yt: str) -> Video:
    v = Video(channel_id=ch.id, topic_id=1, subject=yt, status=VideoStatus.PUBLISHED,
              yt_video_id=yt, published_at=now - timedelta(hours=hours))
    s.add(v)
    s.commit()
    return v


print("record_video_snapshot: NULL inside the 72h lag")
v30 = pub_video(30, "yt30")
ana = FakeAnalytics([EMPTY])                      # no traffic query expected
m = analytics_loop.record_video_snapshot(s, ana, ch, v30, now)
s.commit()
ok(m is not None and len(ana.queries) == 1, "empty young answer stores a snapshot, traffic not queried")
row = s.exec(select(VideoMetric).where(VideoMetric.video_id == v30.id)).one()
ok(all(getattr(row, f) is None for f in analytics_loop._METRIC_FIELDS),
   "30h + empty → every metric column is NULL (not 0)")
ok(row.traffic_json is None, "no traffic_json for a NULL snapshot")
v80 = pub_video(80, "yt80")
analytics_loop.record_video_snapshot(s, FakeAnalytics([EMPTY]), ch, v80, now)
s.commit()
row = s.exec(select(VideoMetric).where(VideoMetric.video_id == v80.id)).one()
ok(row.views == 0 and row.avg_view_pct == 0.0, "80h + empty → real zeros (past the lag)")
v40 = pub_video(40, "yt40")
analytics_loop.record_video_snapshot(s, FakeAnalytics([FULL, TRAFFIC]), ch, v40, now)
s.commit()
row = s.exec(select(VideoMetric).where(VideoMetric.video_id == v40.id)).one()
ok(row.views == 9 and row.avg_view_pct == 40.0 and row.traffic_json,
   "40h + real row → real values + traffic")

print("consumers treat NULL as no data")
# v40 also gets an older measured row then a newer NULL row: latest measured wins.
s.add(VideoMetric(video_id=v40.id, channel_id=ch.id, views=None, impressions=None, ctr=None,
                  avg_view_pct=None, watch_time_minutes=None, average_view_duration=None,
                  likes=None, comments=None, subscribers_gained=None,
                  captured_at=now + timedelta(minutes=5)))
s.commit()
latest = youtube_admin._latest_metrics(s, ch.id)
ok(v30.id not in latest and latest[v40.id].views == 9 and latest[v80.id].views == 0,
   "_latest_metrics skips NULL snapshots (unmeasured), keeps the latest measured row")
lb = youtube_admin.video_analytics(ch.id, "avg_view_pct", s)
by = {it["video_id"]: it for it in lb["items"]}
ok(by[v30.id]["has_data"] is False and lb["measured"] == 2,
   "leaderboard: NULL-only video is unmeasured, measured count 2")
agg = youtube_admin.video_analytics_by_topic(ch.id, s)
t = agg["by_topic"][0]
ok(t["measured"] == 2 and t["avg_views"] == 4.5 and t["avg_view_pct"] == 20.0,
   "by-topic averages over measured videos only (NULL is not a zero)")
mon = youtube_admin._compute_monetization(s, ch.id)
ok(isinstance(mon, dict), "monetization summary tolerates NULL rows (no TypeError)")
_bf = Path(__file__).resolve().parents[1] / "run" / "backfill_ch2_metadata.py"
ok("if m.views is None" in _bf.read_text(), "backfill helper skips NULL views before sorting")

print("_snapshot_channel: one failure does not end the pass")
s2 = Session(engine)
ch2 = Channel(slug="ana2", name="Ana2", oauth_status=OAuthStatus.CONNECTED,
              yt_channel_id="UC2", daily_publish_budget=0)
s2.add(ch2)
s2.commit()
for i, h in enumerate((30, 40, 50, 60)):
    s2.add(Video(channel_id=ch2.id, topic_id=1, subject=f"v{i}", status=VideoStatus.PUBLISHED,
                 yt_video_id=f"c2_{i}", published_at=now - timedelta(hours=h)))
s2.commit()
attempts = []


def fetch_first_fails(analytics, ch_yt, vid, start, end):
    attempts.append(vid)
    if vid == "c2_0":
        raise http_error(503)
    return {"empty": False, "views": 1, "impressions": 0, "ctr": 0.0, "avg_view_pct": 1.0,
            "average_view_duration": 1.0, "watch_time_minutes": 1, "likes": 0,
            "comments": 0, "subscribers_gained": 0}


with patch.object(youtube, "get_analytics_service", lambda slug: "ana"), \
        patch.object(youtube, "fetch_video_analytics", fetch_first_fails), \
        patch.object(youtube, "fetch_traffic_sources", lambda *a: {"sources": {}}):
    n = analytics_loop._snapshot_channel(s2, ch2, now)
ok(attempts == ["c2_0", "c2_1", "c2_2", "c2_3"] and n == 3,
   f"first video fails (5xx after backoff) → the other 3 still recorded ({attempts}, n={n})")

print("tick isolates a crashing channel")
calls = []


def snap(session, channel, now_):
    calls.append(channel.slug)
    if channel.slug == "ana":
        raise RuntimeError("boom")
    return 0


@contextlib.contextmanager
def _scope():
    ss = Session(engine)
    try:
        yield ss
        ss.commit()
    finally:
        ss.close()


with patch.object(analytics_loop, "session_scope", _scope), \
        patch.object(analytics_loop, "_snapshot_channel", snap):
    analytics_loop.tick()
ok(calls == ["ana", "ana2"], f"channel 1 raised, channel 2 still processed ({calls})")

# ---------------------------------------------------------------------------
print("migration: pre-P1 NOT NULL videometric is rebuilt once")
db = Path(tempfile.mkdtemp(prefix="p1-mig-")) / "old.db"
old = create_engine(f"sqlite:///{db}")
with old.begin() as c:
    c.execute(text("CREATE TABLE video (id INTEGER PRIMARY KEY)"))
    c.execute(text("CREATE TABLE channel (id INTEGER PRIMARY KEY)"))
    c.execute(text("""CREATE TABLE videometric (
        id INTEGER NOT NULL, video_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        views INTEGER NOT NULL, impressions INTEGER NOT NULL, ctr FLOAT NOT NULL,
        avg_view_pct FLOAT NOT NULL, watch_time_minutes INTEGER NOT NULL,
        likes INTEGER NOT NULL, comments INTEGER NOT NULL,
        subscribers_gained INTEGER NOT NULL, captured_at DATETIME NOT NULL,
        average_view_duration FLOAT DEFAULT 0, traffic_json VARCHAR,
        PRIMARY KEY (id), FOREIGN KEY(video_id) REFERENCES video (id),
        FOREIGN KEY(channel_id) REFERENCES channel (id))"""))
    for ix in ("video_id", "channel_id", "captured_at"):
        c.execute(text(f"CREATE INDEX ix_videometric_{ix} ON videometric ({ix})"))
    c.execute(text("INSERT INTO video (id) VALUES (7)"))
    c.execute(text("INSERT INTO channel (id) VALUES (1)"))
    for i in (1, 2, 5):
        c.execute(text(
            "INSERT INTO videometric (id, video_id, channel_id, views, impressions, ctr, "
            "avg_view_pct, watch_time_minutes, likes, comments, subscribers_gained, "
            "captured_at, average_view_duration, traffic_json) VALUES "
            f"({i}, 7, 1, {i * 10}, 0, 0, 33.3, 4, 1, 0, 0, '2026-09-2{i} 12:00:00', 7.5, "
            "'{\"sources\": {}}')"))
with patch.object(app_db, "engine", old):
    rebuilt = app_db._relax_videometric_notnull()
    again = app_db._relax_videometric_notnull()
ok(rebuilt is True and again is False, "rebuilt once, then a no-op (idempotent)")
with old.begin() as c:
    info = {r[1]: r[3] for r in c.execute(text("PRAGMA table_info(videometric)"))}
    rows = list(c.execute(text("SELECT id, views, avg_view_pct, average_view_duration, "
                               "traffic_json, captured_at FROM videometric ORDER BY id")))
    idx = {r[1] for r in c.execute(text("PRAGMA index_list(videometric)"))}
    leftovers = list(c.execute(text(
        "SELECT name FROM sqlite_master WHERE name LIKE '%pre_null%'")))
ok(all(info[f] == 0 for f in app_db._VIDEOMETRIC_NULLABLE), "metric columns now nullable")
ok(info["video_id"] == 1 and info["channel_id"] == 1, "video_id / channel_id stay NOT NULL")
ok([(r[0], r[1], r[2], r[3]) for r in rows] == [(1, 10, 33.3, 7.5), (2, 20, 33.3, 7.5),
                                                (5, 50, 33.3, 7.5)]
   and all(r[4] == '{"sources": {}}' for r in rows) and rows[0][5].startswith("2026-09-21"),
   "every row, id and value preserved")
ok({"ix_videometric_video_id", "ix_videometric_channel_id",
    "ix_videometric_captured_at"} <= idx, "indexes recreated")
ok(leftovers == [], "temporary table dropped")
with Session(old) as so:
    so.add(VideoMetric(video_id=7, channel_id=1, views=None, avg_view_pct=None))
    so.commit()
    ok(so.exec(select(VideoMetric).where(VideoMetric.views.is_(None))).one().video_id == 7,
       "a NULL snapshot can be inserted after the rebuild")
fresh = create_engine("sqlite://", poolclass=StaticPool)
SQLModel.metadata.create_all(fresh)
with patch.object(app_db, "engine", fresh):
    ok(app_db._relax_videometric_notnull() is False, "fresh (model-created) table: no rebuild")

print(f"all {_checks} checks passed")
