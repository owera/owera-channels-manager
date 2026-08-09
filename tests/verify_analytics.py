"""Dependency-free regression checks for app/services/analytics_loop.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_analytics.py

``analytics_loop`` is the daily per-video YouTube Analytics snapshot pass —
the measurement foundation the growth agent steers by. ``verify_analytics_reserve.py``
already pins ``_publish_reserve``; ``verify_notify.py`` pins the dead-token vs
missing-scope split. This suite owns the rest: the due/maturity gates,
``record_video_snapshot`` happy + error paths (incl. traffic-source gating and
quota propagation), ``_snapshot_channel``'s filter/order/quota-cap/first-fail
stop, and ``tick()``'s pause + CONNECTED + yt_channel_id filter — all previously
untested body.

Covers, dependency-free (in-memory SQLite, no network/creds):
  - ``_snapshot_due``: no row → due; today's capture (aware + SQLite-naive) →
    not due; yesterday's → due; cross-video isolation; midnight boundary
  - ``_mature``: None published_at → False; young → False; ≥24h → True;
    naive tz handled; exactly-24h boundary
  - ``record_video_snapshot`` happy path: VideoMetric fields, 2-unit success
    JobRun, date range from published_at, traffic fetched only when views>0
  - views==0 → traffic never called, traffic_json None
  - traffic exception → metric still saved, traffic_json None
  - generic analytics exception → None + error JobRun (quota_cost=0)
  - QuotaExceeded propagates (caller stops the channel pass)
  - ``_dead_token_error``: NeedsConnect returns message; healthy / other → None
  - ``_snapshot_channel``: immature / not-due / no yt_video_id / non-PUBLISHED
    skipped; newest-first under quota; first hard-fail aborts the rest;
    force=True bypasses due gate; QuotaExceeded mid-pass breaks cleanly
  - ``tick()``: scheduler_paused no-op; only CONNECTED with yt_channel_id;
    non-CONNECTED never probed

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    Channel, JobRun, OAuthStatus, Settings as AppSettingsRow,
    Video, VideoMetric, VideoStatus,
)
from app.services import analytics_loop, notify, youtube
from app.services.quota import _day_start, _quota_day_start

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
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(session, **kw) -> Channel:
    ch = Channel(
        slug=kw.pop("slug", "ch-ana"),
        name=kw.pop("name", "Analytics Test"),
        oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED),
        yt_channel_id=kw.pop("yt_channel_id", "UC_test_channel"),
        **kw,
    )
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_video(session, channel, **kw) -> Video:
    now = datetime.now(timezone.utc)
    v = Video(
        channel_id=channel.id,
        topic_id=kw.pop("topic_id", 1),
        subject=kw.pop("subject", "Test subject"),
        status=kw.pop("status", VideoStatus.PUBLISHED),
        yt_video_id=kw.pop("yt_video_id", "yt_vid_1"),
        published_at=kw.pop("published_at", now - timedelta(hours=48)),
        **kw,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def add_vmetric(session, video_id: int, channel_id: int, captured_at: datetime,
                **counts) -> VideoMetric:
    m = VideoMetric(
        video_id=video_id,
        channel_id=channel_id,
        views=counts.get("views", 0),
        impressions=counts.get("impressions", 0),
        captured_at=captured_at,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def vmetrics_for(session, video_id: int):
    return session.exec(
        select(VideoMetric).where(VideoMetric.video_id == video_id)
    ).all()


def jobruns(session, *, kind=None, status=None, video_id=None):
    rows = session.exec(select(JobRun)).all()
    if kind is not None:
        rows = [r for r in rows if r.kind == kind]
    if status is not None:
        rows = [r for r in rows if r.status == status]
    if video_id is not None:
        rows = [r for r in rows if r.video_id == video_id]
    return rows


def _ana_payload(**overrides):
    base = {
        "views": 100,
        "impressions": 0,
        "ctr": 0.0,
        "avg_view_pct": 42.5,
        "average_view_duration": 33.0,
        "watch_time_minutes": 55,
        "likes": 7,
        "comments": 2,
        "subscribers_gained": 1,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _snapshot_due
# ---------------------------------------------------------------------------
print("_snapshot_due: UTC-day gate (aware + SQLite-naive)")

s = fresh_session()
ch = make_channel(s, slug="due-1")
v = make_video(s, ch, yt_video_id="d1")
ok(analytics_loop._snapshot_due(s, v.id) is True, "no snapshot yet → due")

today_aware = _day_start() + timedelta(hours=3)
add_vmetric(s, v.id, ch.id, today_aware, views=10)
ok(analytics_loop._snapshot_due(s, v.id) is False, "aware capture today → not due")

# Other video with a fresh snapshot must not satisfy this video.
v2 = make_video(s, ch, yt_video_id="d2", subject="other")
ok(analytics_loop._snapshot_due(s, v2.id) is True,
   "other video's today-snapshot does not satisfy this video")

# Yesterday (aware) → due again.
s2 = fresh_session()
ch_y = make_channel(s2, slug="due-y")
v_y = make_video(s2, ch_y, yt_video_id="dy")
yesterday = _day_start() - timedelta(hours=1)
add_vmetric(s2, v_y.id, ch_y.id, yesterday, views=1)
ok(analytics_loop._snapshot_due(s2, v_y.id) is True,
   "yesterday's capture → due again at the next UTC day")

# SQLite stores naive datetimes; the gate must not TypeError on naive vs aware.
s3 = fresh_session()
ch_n = make_channel(s3, slug="due-naive")
v_n = make_video(s3, ch_n, yt_video_id="dn")
naive_today = (_day_start() + timedelta(hours=1)).replace(tzinfo=None)
add_vmetric(s3, v_n.id, ch_n.id, naive_today)
ok(analytics_loop._snapshot_due(s3, v_n.id) is False,
   "SQLite-naive capture today → not due (tzinfo attached before compare)")

s3b = fresh_session()
ch_ny = make_channel(s3b, slug="due-naive-y")
v_ny = make_video(s3b, ch_ny, yt_video_id="dny")
naive_yest = (_day_start() - timedelta(hours=2)).replace(tzinfo=None)
add_vmetric(s3b, v_ny.id, ch_ny.id, naive_yest)
ok(analytics_loop._snapshot_due(s3b, v_ny.id) is True,
   "SQLite-naive capture yesterday → due")

# Boundary: capture exactly at _day_start is "today" (not due).
s4 = fresh_session()
ch_b = make_channel(s4, slug="due-boundary")
v_b = make_video(s4, ch_b, yt_video_id="db")
add_vmetric(s4, v_b.id, ch_b.id, _day_start())
ok(analytics_loop._snapshot_due(s4, v_b.id) is False,
   "capture exactly at UTC midnight → not due (cap < day_start is False)")


# ---------------------------------------------------------------------------
# _mature
# ---------------------------------------------------------------------------
print("\n_mature: 24h analytics-latency gate")

now = datetime.now(timezone.utc)
v_none = Video(channel_id=1, topic_id=1, subject="x", published_at=None)
ok(analytics_loop._mature(v_none, now) is False, "published_at=None → not mature")

v_young = Video(channel_id=1, topic_id=1, subject="x",
                published_at=now - timedelta(hours=12))
ok(analytics_loop._mature(v_young, now) is False, "12h old → not mature")

v_edge = Video(channel_id=1, topic_id=1, subject="x",
               published_at=now - timedelta(hours=24))
ok(analytics_loop._mature(v_edge, now) is True,
   "exactly 24h old → mature (>= boundary)")

v_old = Video(channel_id=1, topic_id=1, subject="x",
              published_at=now - timedelta(hours=48))
ok(analytics_loop._mature(v_old, now) is True, "48h old → mature")

# Naive published_at (SQLite) must not TypeError against aware now.
v_naive = Video(channel_id=1, topic_id=1, subject="x",
                published_at=(now - timedelta(hours=30)).replace(tzinfo=None))
ok(analytics_loop._mature(v_naive, now) is True,
   "SQLite-naive published_at 30h ago → mature")

v_naive_young = Video(channel_id=1, topic_id=1, subject="x",
                      published_at=(now - timedelta(hours=1)).replace(tzinfo=None))
ok(analytics_loop._mature(v_naive_young, now) is False,
   "SQLite-naive published_at 1h ago → not mature")

ok(analytics_loop._MIN_MATURITY_HOURS == 24,
   "maturity window pinned at 24h (API latency)")


# ---------------------------------------------------------------------------
# record_video_snapshot happy path + traffic gating
# ---------------------------------------------------------------------------
print("\nrecord_video_snapshot: happy path + traffic-source gating")

_ORIG_FETCH = youtube.fetch_video_analytics
_ORIG_TRAFFIC = youtube.fetch_traffic_sources
_calls = {"fetch": [], "traffic": []}


def _fake_fetch(analytics, ch_yt, vid, start, end):
    _calls["fetch"].append({
        "analytics": analytics, "ch_yt": ch_yt, "vid": vid,
        "start": start, "end": end,
    })
    return _ana_payload(views=100, likes=7, comments=2, subscribers_gained=1,
                        avg_view_pct=42.5, average_view_duration=33.0,
                        watch_time_minutes=55)


def _fake_traffic(analytics, ch_yt, vid, start, end):
    _calls["traffic"].append(vid)
    return {"sources": {"YT_SEARCH": {"views": 40, "watch_min": 10}},
            "search_terms": {"rag": 12}}


youtube.fetch_video_analytics = _fake_fetch
youtube.fetch_traffic_sources = _fake_traffic
try:
    s = fresh_session()
    ch = make_channel(s, slug="happy", yt_channel_id="UC_happy")
    pub = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    v = make_video(s, ch, yt_video_id="yt_happy", published_at=pub)
    now = datetime(2026, 8, 9, 15, 30, tzinfo=timezone.utc)
    before = len(jobruns(s))
    m = analytics_loop.record_video_snapshot(s, "ana-svc", ch, v, now)
    s.commit()
    ok(m is not None, "happy path returns a VideoMetric")
    ok(m.video_id == v.id and m.channel_id == ch.id, "metric ids match video/channel")
    ok(m.views == 100 and m.likes == 7 and m.comments == 2
       and m.subscribers_gained == 1,
       "core counts taken from fetch_video_analytics")
    ok(m.avg_view_pct == 42.5 and m.average_view_duration == 33.0
       and m.watch_time_minutes == 55,
       "watch metrics taken from fetch_video_analytics")
    ok(m.traffic_json is not None, "views>0 → traffic_json populated")
    traffic = json.loads(m.traffic_json)
    ok(traffic["sources"]["YT_SEARCH"]["views"] == 40
       and traffic["search_terms"]["rag"] == 12,
       "traffic_json is the dumps of fetch_traffic_sources")
    rows = vmetrics_for(s, v.id)
    ok(len(rows) == 1 and rows[0].views == 100, "exactly one persisted VideoMetric")
    runs = jobruns(s, kind="analytics", status="success")
    ok(len(runs) == before + 1, "exactly one analytics success JobRun")
    ok(runs[-1].channel_id == ch.id and runs[-1].video_id == v.id,
       "JobRun ids match channel + video")
    ok(runs[-1].quota_cost == analytics_loop._QUOTA_PER_VIDEO == 2,
       "quota_cost is 2 (core + traffic query units)")
    ok(len(_calls["fetch"]) == 1, "fetch_video_analytics called once")
    c0 = _calls["fetch"][0]
    ok(c0["ch_yt"] == "UC_happy" and c0["vid"] == "yt_happy"
       and c0["analytics"] == "ana-svc",
       "fetch called with analytics handle, channel yt id, video yt id")
    ok(c0["start"] == "2026-07-01" and c0["end"] == "2026-08-09",
       "date range is published_at.date .. now.date (iso)")
    ok(_calls["traffic"] == ["yt_happy"],
       "traffic fetched once for the viewed video")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _calls["fetch"].clear()
    _calls["traffic"].clear()


# views==0 → traffic never called
print("\nrecord_video_snapshot: views==0 skips traffic query")

youtube.fetch_video_analytics = lambda *a, **k: _ana_payload(views=0)
youtube.fetch_traffic_sources = lambda *a, **k: (_calls["traffic"].append("HIT")
                                                  or {"sources": {}})
try:
    s = fresh_session()
    ch = make_channel(s, slug="zero-views")
    v = make_video(s, ch, yt_video_id="yt_zero")
    _calls["traffic"].clear()
    m = analytics_loop.record_video_snapshot(
        s, "ana", ch, v, datetime.now(timezone.utc))
    s.commit()
    ok(m is not None and m.views == 0, "zero-views still returns a metric")
    ok(m.traffic_json is None, "zero-views → traffic_json is None")
    ok(_calls["traffic"] == [], "zero-views never calls fetch_traffic_sources")
    ok(len(jobruns(s, kind="analytics", status="success")) == 1
       and jobruns(s, kind="analytics", status="success")[0].quota_cost == 2,
       "zero-views still bills the full _QUOTA_PER_VIDEO (reservation is fixed)")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _calls["traffic"].clear()


# traffic exception → metric still saved
# views>0 but empty sources → traffic_json stays None (not dumps of empty)
print("\nrecord_video_snapshot: views>0 empty sources → traffic_json None")

youtube.fetch_video_analytics = lambda *a, **k: _ana_payload(views=10)
youtube.fetch_traffic_sources = lambda *a, **k: {"sources": {}, "search_terms": {}}
try:
    s = fresh_session()
    ch = make_channel(s, slug="empty-src")
    v = make_video(s, ch, yt_video_id="yt_es")
    m = analytics_loop.record_video_snapshot(
        s, "ana", ch, v, datetime.now(timezone.utc))
    s.commit()
    ok(m is not None and m.views == 10, "empty-sources still returns the metric")
    ok(m.traffic_json is None,
       "empty sources → traffic_json None (only non-empty sources are dumped)")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC


print("\nrecord_video_snapshot: traffic failure is best-effort")

youtube.fetch_video_analytics = lambda *a, **k: _ana_payload(views=50)


def _raise_traffic(*a, **k):
    raise RuntimeError("traffic blip")


youtube.fetch_traffic_sources = _raise_traffic
try:
    s = fresh_session()
    ch = make_channel(s, slug="traf-fail")
    v = make_video(s, ch, yt_video_id="yt_tf")
    m = analytics_loop.record_video_snapshot(
        s, "ana", ch, v, datetime.now(timezone.utc))
    s.commit()
    ok(m is not None and m.views == 50, "traffic failure still returns the metric")
    ok(m.traffic_json is None, "traffic failure → traffic_json None (not raised)")
    ok(len(jobruns(s, kind="analytics", status="success")) == 1,
       "traffic failure still logs analytics success (core query billed)")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC


# generic exception → None + error JobRun
print("\nrecord_video_snapshot: generic fetch failure → None + error JobRun")


def _raise_fetch(*a, **k):
    raise RuntimeError("analytics API disabled")


youtube.fetch_video_analytics = _raise_fetch
youtube.fetch_traffic_sources = lambda *a, **k: {"sources": {}}
try:
    s = fresh_session()
    ch = make_channel(s, slug="fetch-fail")
    v = make_video(s, ch, yt_video_id="yt_ff")
    m = analytics_loop.record_video_snapshot(
        s, "ana", ch, v, datetime.now(timezone.utc))
    s.commit()
    ok(m is None, "generic failure yields None")
    ok(len(vmetrics_for(s, v.id)) == 0, "generic failure writes no VideoMetric")
    errs = jobruns(s, kind="analytics", status="error")
    ok(len(errs) == 1, "generic failure logs one analytics error JobRun")
    ok(errs[0].quota_cost == 0, "failed call bills 0 quota (only successes do)")
    ok(errs[0].video_id == v.id and errs[0].channel_id == ch.id,
       "error JobRun carries video + channel ids")
    ok("analytics API disabled" in (errs[0].detail or ""),
       "error JobRun detail carries the exception message")
    ok(len(jobruns(s, kind="analytics", status="success")) == 0,
       "generic failure writes no success JobRun")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC


# QuotaExceeded propagates
print("\nrecord_video_snapshot: QuotaExceeded propagates to the caller")


def _raise_quota(*a, **k):
    raise youtube.QuotaExceeded("daily quota")


youtube.fetch_video_analytics = _raise_quota
try:
    s = fresh_session()
    ch = make_channel(s, slug="quota-raise")
    v = make_video(s, ch, yt_video_id="yt_q")
    raised = False
    try:
        analytics_loop.record_video_snapshot(
            s, "ana", ch, v, datetime.now(timezone.utc))
    except youtube.QuotaExceeded:
        raised = True
    ok(raised, "QuotaExceeded propagates (not swallowed)")
    ok(len(vmetrics_for(s, v.id)) == 0, "QuotaExceeded writes no metric")
    ok(len(jobruns(s, kind="analytics")) == 0,
       "QuotaExceeded writes no analytics JobRun (caller stops the pass)")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH


# naive published_at for date-range start (falls back path uses created_at too)
print("\nrecord_video_snapshot: naive published_at date range + created_at fallback")

_calls["fetch"].clear()
youtube.fetch_video_analytics = _fake_fetch
youtube.fetch_traffic_sources = lambda *a, **k: {"sources": {}}
try:
    s = fresh_session()
    ch = make_channel(s, slug="naive-pub")
    # published_at naive UTC wall time
    pub_naive = datetime(2026, 6, 15, 10, 0)  # no tzinfo
    v = make_video(s, ch, yt_video_id="yt_np", published_at=pub_naive)
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    m = analytics_loop.record_video_snapshot(s, "ana", ch, v, now)
    s.commit()
    ok(m is not None, "naive published_at still snapshots")
    ok(_calls["fetch"][-1]["start"] == "2026-06-15",
       "naive published_at.date() used as start_date")

    # published_at=None falls back to created_at
    _calls["fetch"].clear()
    v2 = make_video(s, ch, yt_video_id="yt_np2", published_at=None,
                    subject="no pub")
    # force created_at to a known date by re-assigning on the identity map
    v2.created_at = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    s.add(v2)
    s.commit()
    s.refresh(v2)
    m2 = analytics_loop.record_video_snapshot(s, "ana", ch, v2, now)
    s.commit()
    ok(m2 is not None, "published_at=None falls back to created_at")
    ok(_calls["fetch"][-1]["start"] == "2026-05-01",
       "created_at.date() used when published_at is None")
finally:
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _calls["fetch"].clear()


# ---------------------------------------------------------------------------
# _dead_token_error
# ---------------------------------------------------------------------------
print("\n_dead_token_error: narrow-scope probe classification")

_ORIG_GET = youtube.get_service


def _raise_needs(slug):
    raise youtube.NeedsConnect(f"token dead for {slug}")


youtube.get_service = _raise_needs
try:
    msg = analytics_loop._dead_token_error("dead-ch")
    ok(msg == "token dead for dead-ch",
       "NeedsConnect → returns the exception message")
finally:
    youtube.get_service = _ORIG_GET

youtube.get_service = lambda slug: object()
try:
    ok(analytics_loop._dead_token_error("ok-ch") is None,
       "healthy narrow-scope token → None (scope-only failure path)")
finally:
    youtube.get_service = _ORIG_GET

youtube.get_service = lambda slug: (_ for _ in ()).throw(RuntimeError("blip"))
try:
    ok(analytics_loop._dead_token_error("blip-ch") is None,
       "transient narrow-scope error → None (must not kill the channel)")
finally:
    youtube.get_service = _ORIG_GET


# ---------------------------------------------------------------------------
# _snapshot_channel: filters, order, quota, first-fail, force
# ---------------------------------------------------------------------------
print("\n_snapshot_channel: filters + newest-first + first-fail + force")

_ORIG_ANA = youtube.get_analytics_service
_snap_order: list[str] = []


def _ana_ok(slug):
    return f"ana-{slug}"


def _fetch_track(analytics, ch_yt, vid, start, end):
    _snap_order.append(vid)
    return _ana_payload(views=10)


def _traffic_empty(*a, **k):
    return {"sources": {}}


youtube.get_analytics_service = _ana_ok
youtube.fetch_video_analytics = _fetch_track
youtube.fetch_traffic_sources = _traffic_empty
try:
    s = fresh_session()
    ch = make_channel(s, slug="snap-filters", yt_channel_id="UC_f",
                      daily_publish_budget=0)  # reserve 0 → full analytics cap
    now = datetime.now(timezone.utc)
    # mature + due (newest)
    v_new = make_video(s, ch, yt_video_id="yt_new", subject="new",
                       published_at=now - timedelta(hours=30))
    # mature + due (older)
    v_old = make_video(s, ch, yt_video_id="yt_old", subject="old",
                       published_at=now - timedelta(hours=72))
    # immature — must skip
    v_young = make_video(s, ch, yt_video_id="yt_young", subject="young",
                         published_at=now - timedelta(hours=6))
    # already snapshotted today — must skip unless force
    v_done = make_video(s, ch, yt_video_id="yt_done", subject="done",
                        published_at=now - timedelta(hours=50))
    add_vmetric(s, v_done.id, ch.id, _day_start() + timedelta(hours=1), views=1)
    # no yt_video_id — excluded by the query
    make_video(s, ch, yt_video_id=None, subject="no-yt",
               published_at=now - timedelta(hours=40))
    # not PUBLISHED — excluded
    make_video(s, ch, yt_video_id="yt_draft", subject="draft",
               status=VideoStatus.DRAFT,
               published_at=now - timedelta(hours=40))

    _snap_order.clear()
    n = analytics_loop._snapshot_channel(s, ch, now)
    ok(n == 2, f"records exactly the 2 mature+due videos (got {n})")
    ok(_snap_order == ["yt_new", "yt_old"],
       f"newest-first order under the pass (got {_snap_order!r})")
    ok(len(vmetrics_for(s, v_new.id)) == 1
       and len(vmetrics_for(s, v_old.id)) == 1,
       "both mature+due videos have a VideoMetric")
    ok(len(vmetrics_for(s, v_young.id)) == 0, "immature video not snapshotted")
    ok(len(vmetrics_for(s, v_done.id)) == 1,
       "already-due video kept its single pre-existing metric (not re-fetched)")
    ok(len(jobruns(s, kind="analytics", status="success")) == 2,
       "two analytics success JobRuns for the two recorded")

    # force=True re-snapshots the already-done video too
    _snap_order.clear()
    n2 = analytics_loop._snapshot_channel(s, ch, now, force=True)
    ok(n2 == 3, f"force=True records 3 mature videos incl. already-done (got {n2})")
    ok("yt_done" in _snap_order, "force bypasses the once-per-day due gate")
    ok(len(vmetrics_for(s, v_done.id)) == 2,
       "force wrote a second metric row for the previously-done video")
    ok("yt_young" not in _snap_order, "force still respects the maturity gate")
finally:
    youtube.get_analytics_service = _ORIG_ANA
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _snap_order.clear()


# first hard-fail aborts the rest of the channel pass
print("\n_snapshot_channel: first hard-fail aborts the rest")

_fail_count = {"n": 0}


def _fetch_always_fail(*a, **k):
    _fail_count["n"] += 1
    raise RuntimeError("channel not analytics-ready")


youtube.get_analytics_service = _ana_ok
youtube.fetch_video_analytics = _fetch_always_fail
try:
    s = fresh_session()
    ch = make_channel(s, slug="first-fail", daily_publish_budget=0)
    now = datetime.now(timezone.utc)
    make_video(s, ch, yt_video_id="yt_a", subject="a",
               published_at=now - timedelta(hours=30))
    make_video(s, ch, yt_video_id="yt_b", subject="b",
               published_at=now - timedelta(hours=40))
    make_video(s, ch, yt_video_id="yt_c", subject="c",
               published_at=now - timedelta(hours=50))
    _fail_count["n"] = 0
    n = analytics_loop._snapshot_channel(s, ch, now)
    ok(n == 0, "first-fail records nothing")
    ok(_fail_count["n"] == 1,
       f"first hard-fail aborts — only 1 fetch attempted (got {_fail_count['n']})")
    ok(len(jobruns(s, kind="analytics", status="error")) == 1,
       "one error JobRun for the failed first attempt")
    ok(len(jobruns(s, kind="analytics", status="success")) == 0,
       "no success JobRuns when the channel isn't analytics-ready")
finally:
    youtube.get_analytics_service = _ORIG_ANA
    youtube.fetch_video_analytics = _ORIG_FETCH
    _fail_count["n"] = 0


# first-fail only triggers when recorded==0; a later fail after successes continues
print("\n_snapshot_channel: mid-pass soft-fail does not abort (recorded>0)")

_mid = {"n": 0, "ids": []}


def _fetch_mid(analytics, ch_yt, vid, start, end):
    _mid["n"] += 1
    _mid["ids"].append(vid)
    if vid == "yt_mid":
        raise RuntimeError("transient per-video blip")
    return _ana_payload(views=5)


youtube.get_analytics_service = _ana_ok
youtube.fetch_video_analytics = _fetch_mid
youtube.fetch_traffic_sources = _traffic_empty
try:
    s = fresh_session()
    ch = make_channel(s, slug="mid-fail", daily_publish_budget=0)
    now = datetime.now(timezone.utc)
    # newest first: yt_new succeeds, yt_mid soft-fails, yt_old continues
    make_video(s, ch, yt_video_id="yt_new", subject="new",
               published_at=now - timedelta(hours=30))
    make_video(s, ch, yt_video_id="yt_mid", subject="mid",
               published_at=now - timedelta(hours=40))
    make_video(s, ch, yt_video_id="yt_old", subject="old",
               published_at=now - timedelta(hours=50))
    _mid["n"] = 0
    _mid["ids"].clear()
    n = analytics_loop._snapshot_channel(s, ch, now)
    ok(n == 2, f"two successes around a mid soft-fail (got {n})")
    ok(_mid["ids"] == ["yt_new", "yt_mid", "yt_old"],
       f"all three mature videos attempted (got {_mid['ids']!r})")
    ok(len(jobruns(s, kind="analytics", status="success")) == 2, "two successes")
    ok(len(jobruns(s, kind="analytics", status="error")) == 1, "one soft-fail error")
finally:
    youtube.get_analytics_service = _ORIG_ANA
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _mid["n"] = 0
    _mid["ids"].clear()


# QuotaExceeded mid-pass stops cleanly (no further fetches)
print("\n_snapshot_channel: QuotaExceeded mid-pass breaks")

_q = {"n": 0, "ids": []}


def _fetch_quota(analytics, ch_yt, vid, start, end):
    _q["n"] += 1
    _q["ids"].append(vid)
    if vid == "yt_q2":
        raise youtube.QuotaExceeded("hit the wall")
    return _ana_payload(views=3)


youtube.get_analytics_service = _ana_ok
youtube.fetch_video_analytics = _fetch_quota
youtube.fetch_traffic_sources = _traffic_empty
try:
    s = fresh_session()
    ch = make_channel(s, slug="q-mid", daily_publish_budget=0)
    now = datetime.now(timezone.utc)
    make_video(s, ch, yt_video_id="yt_q1", subject="q1",
               published_at=now - timedelta(hours=30))
    make_video(s, ch, yt_video_id="yt_q2", subject="q2",
               published_at=now - timedelta(hours=40))
    make_video(s, ch, yt_video_id="yt_q3", subject="q3",
               published_at=now - timedelta(hours=50))
    _q["n"] = 0
    _q["ids"].clear()
    n = analytics_loop._snapshot_channel(s, ch, now)
    ok(n == 1, f"records the pre-quota success only (got {n})")
    ok(_q["ids"] == ["yt_q1", "yt_q2"],
       f"stops at QuotaExceeded — never reaches yt_q3 (got {_q['ids']!r})")
    ok(len(jobruns(s, kind="analytics", status="success")) == 1,
       "only the pre-quota success JobRun")
finally:
    youtube.get_analytics_service = _ORIG_ANA
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _q["n"] = 0
    _q["ids"].clear()


# pre-emptive quota-cap gate (spent + per-video > cap-reserve)
print("\n_snapshot_channel: pre-emptive quota cap stops before overspend")

_cap_ids: list[str] = []


def _fetch_cap(analytics, ch_yt, vid, start, end):
    _cap_ids.append(vid)
    return _ana_payload(views=1)


youtube.get_analytics_service = _ana_ok
youtube.fetch_video_analytics = _fetch_cap
youtube.fetch_traffic_sources = _traffic_empty
try:
    s = fresh_session()
    # budget 5 → reserve 8750 → analytics cap = 9000-8750 = 250 → only 125 videos
    # of cost-2 would fit; we seed spent so only ONE more fits.
    ch = make_channel(s, slug="cap-gate", daily_publish_budget=5)
    now = datetime.now(timezone.utc)
    # Seed 248 units already spent → +2 = 250 would equal cap, next would exceed.
    # Gate: spent + _QUOTA_PER_VIDEO > cap  →  248+2=250 is NOT > 250, so one more
    # fits; after that success spends 2 more → 250, next 250+2>250 stops.
    # Wait: the gate is `spent + per_video > cap` BEFORE the call. After first
    # success spent becomes 250; 250+2 > 250 → stop. So exactly one call.
    # Actually we need spent such that first fits: spent + 2 <= cap, second doesn't.
    # cap = 9000 - 8750 = 250. spent=248 → 248+2=250 not > 250 → first OK.
    # After commit spent=250 → 250+2=252 > 250 → second blocked. Perfect.
    from app.models import JobRun as _JR
    s.add(_JR(channel_id=ch.id, kind="analytics", status="success",
              quota_cost=248, created_at=_quota_day_start() + timedelta(hours=1)))
    s.commit()
    make_video(s, ch, yt_video_id="yt_cap1", subject="c1",
               published_at=now - timedelta(hours=30))
    make_video(s, ch, yt_video_id="yt_cap2", subject="c2",
               published_at=now - timedelta(hours=40))
    make_video(s, ch, yt_video_id="yt_cap3", subject="c3",
               published_at=now - timedelta(hours=50))
    _cap_ids.clear()
    n = analytics_loop._snapshot_channel(s, ch, now)
    ok(n == 1, f"quota cap allows exactly one more snapshot (got {n})")
    ok(_cap_ids == ["yt_cap1"],
       f"only the newest video attempted under the cap (got {_cap_ids!r})")
finally:
    youtube.get_analytics_service = _ORIG_ANA
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _cap_ids.clear()


# get_analytics_service fails: dead token vs scope-only (contract pins)
print("\n_snapshot_channel: dead token flips; missing scope only skips (contract)")

_ORIG_HAS = youtube.has_token
youtube.has_token = lambda slug: True  # dead_status_for → EXPIRED


def _raise_ana(slug):
    raise youtube.NeedsConnect(f"no analytics for {slug}")


youtube.get_analytics_service = _raise_ana
try:
    # genuinely dead: narrow scope also NeedsConnect
    youtube.get_service = _raise_needs
    s = fresh_session()
    ch = make_channel(s, slug="dead-ana")
    with patch.object(notify, "alert_dead", lambda *a, **k: None):
        n = analytics_loop._snapshot_channel(s, ch, datetime.now(timezone.utc))
    s.refresh(ch)
    ok(n == 0, "dead token records nothing")
    ok(ch.oauth_status == OAuthStatus.EXPIRED,
       "genuinely dead token flips the channel to EXPIRED")

    # scope-only: narrow scope still loads
    youtube.get_service = lambda slug: object()
    s2 = fresh_session()
    ch2 = make_channel(s2, slug="scope-only")
    with patch.object(notify, "alert_dead", lambda *a, **k: None):
        n2 = analytics_loop._snapshot_channel(s2, ch2, datetime.now(timezone.utc))
    s2.refresh(ch2)
    ok(n2 == 0, "missing analytics scope records nothing")
    ok(ch2.oauth_status == OAuthStatus.CONNECTED,
       "missing analytics scope leaves status CONNECTED (publishing is fine)")
finally:
    youtube.get_analytics_service = _ORIG_ANA
    youtube.get_service = _ORIG_GET
    youtube.has_token = _ORIG_HAS


# ---------------------------------------------------------------------------
# tick(): pause + CONNECTED filter + yt_channel_id
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# tick(): pause + CONNECTED filter + yt_channel_id
# ---------------------------------------------------------------------------
print("\ntick(): scheduler_paused + CONNECTED + yt_channel_id filter")

_ORIG_SCOPE = analytics_loop.session_scope
_probe_slugs: list[str] = []


@contextmanager
def _scoped(session):
    yield session
    session.commit()


def _probe_ana(slug):
    _probe_slugs.append(slug)
    return f"ana-{slug}"


youtube.get_analytics_service = _probe_ana
youtube.fetch_video_analytics = lambda *a, **k: _ana_payload(views=1)
youtube.fetch_traffic_sources = _traffic_empty
try:
    s = fresh_session()
    s.add(AppSettingsRow(id=1, scheduler_paused=False))
    s.commit()

    ch_due = make_channel(s, slug="tick-due", yt_channel_id="UC_due")
    make_channel(s, slug="tick-noyt", yt_channel_id=None)
    ch_exp = make_channel(s, slug="tick-exp", oauth_status=OAuthStatus.EXPIRED,
                          yt_channel_id="UC_exp")
    make_channel(s, slug="tick-dis", oauth_status=OAuthStatus.DISCONNECTED,
                 yt_channel_id="UC_dis")
    make_channel(s, slug="tick-err", oauth_status=OAuthStatus.ERROR,
                 yt_channel_id="UC_err")
    now = datetime.now(timezone.utc)
    v_due = make_video(s, ch_due, yt_video_id="yt_tick",
                       published_at=now - timedelta(hours=48))
    # EXPIRED also has a mature video — must never be probed
    make_video(s, ch_exp, yt_video_id="yt_exp",
               published_at=now - timedelta(hours=48))

    analytics_loop.session_scope = lambda: _scoped(s)
    _probe_slugs.clear()
    analytics_loop.tick()
    ok(_probe_slugs == ["tick-due"],
       f"tick probes only CONNECTED-with-yt_channel_id (got {_probe_slugs!r})")
    due_rows = vmetrics_for(s, v_due.id)
    ok(len(due_rows) == 1 and due_rows[0].views == 1,
       "tick wrote the due CONNECTED channel's video snapshot")
    ok(len(jobruns(s, kind="analytics", status="success")) == 1,
       "tick logs exactly one analytics success")

    # Second tick same day: due gate stops re-probe of the video; channel still
    # probed (get_analytics_service) but records 0. Pin: no extra success JobRun.
    _probe_slugs.clear()
    analytics_loop.tick()
    ok(_probe_slugs == ["tick-due"],
       "second tick still opens the CONNECTED channel (due-gate is per-video)")
    ok(len(jobruns(s, kind="analytics", status="success")) == 1,
       "second tick writes no extra success JobRun (video not due)")
    ok(len(vmetrics_for(s, v_due.id)) == 1,
       "second tick does not double-snapshot the same video")

    # scheduler_paused → no probes at all
    cfg = s.get(AppSettingsRow, 1)
    cfg.scheduler_paused = True
    s.add(cfg)
    s.commit()
    _probe_slugs.clear()
    analytics_loop.tick()
    ok(_probe_slugs == [],
       "scheduler_paused → tick probes nothing")
    ok(len(jobruns(s, kind="analytics", status="success")) == 1,
       "paused tick writes no analytics JobRun")
finally:
    analytics_loop.session_scope = _ORIG_SCOPE
    youtube.get_analytics_service = _ORIG_ANA
    youtube.fetch_video_analytics = _ORIG_FETCH
    youtube.fetch_traffic_sources = _ORIG_TRAFFIC
    _probe_slugs.clear()


print()
print(f"ALL {_checks} CHECKS PASSED")
