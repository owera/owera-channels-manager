"""Dependency-free regression checks for app/services/metrics_loop.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_metrics.py

``metrics_loop`` is the daily public-stats probe (one ChannelMetric per channel
per UTC day) and the silent-death alert path during a publishing lull: when a
token dies with nothing queued, the publish loop never runs, so this tick is
what flips the channel and pages. ``verify_notify.py`` already pins the
NeedsConnect → mark_dead_committed wiring; this suite owns the due-gate, the
happy-path write + quota log, the stats defaults, and ``tick()``'s CONNECTED
filter + due-only scheduling — all previously untested.

Covers, dependency-free (in-memory SQLite, no network/creds):
  - ``_snapshot_due``: no row → due; today's capture (aware + SQLite-naive) →
    not due; yesterday's → due; other channel's snapshot does not satisfy
  - ``record_snapshot`` happy path: ChannelMetric fields, 1-unit success
    JobRun, get_service called with the channel slug
  - missing/empty/None statistics → zeros (never KeyError / never None stored)
  - NeedsConnect → None + durable EXPIRED flip (contract pin; full alert
    semantics live in verify_notify)
  - transient exception → None, status untouched, no metric, no success log
  - ``tick()``: only CONNECTED + due channels are probed; not-due and
    non-CONNECTED channels never call YouTube

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Channel, ChannelMetric, JobRun, OAuthStatus
from app.services import metrics_loop, notify, youtube
from app.services.quota import _day_start

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
        slug=kw.pop("slug", "ch-metrics"),
        name=kw.pop("name", "Metrics Test"),
        oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED),
        **kw,
    )
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def add_metric(session, channel_id: int, captured_at: datetime, **counts) -> ChannelMetric:
    m = ChannelMetric(
        channel_id=channel_id,
        subscriber_count=counts.get("subscriber_count", 0),
        view_count=counts.get("view_count", 0),
        video_count=counts.get("video_count", 0),
        captured_at=captured_at,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def metrics_for(session, channel_id: int):
    return session.exec(
        select(ChannelMetric).where(ChannelMetric.channel_id == channel_id)
    ).all()


def jobruns(session, *, kind=None, status=None):
    rows = session.exec(select(JobRun)).all()
    if kind is not None:
        rows = [r for r in rows if r.kind == kind]
    if status is not None:
        rows = [r for r in rows if r.status == status]
    return rows


# ---------------------------------------------------------------------------
# _snapshot_due
# ---------------------------------------------------------------------------
print("_snapshot_due: UTC-day gate (aware + SQLite-naive)")

s = fresh_session()
ch = make_channel(s, slug="due-1")
ok(metrics_loop._snapshot_due(s, ch.id) is True,
   "no snapshot yet → due")

today_aware = _day_start() + timedelta(hours=3)
add_metric(s, ch.id, today_aware, subscriber_count=10)
ok(metrics_loop._snapshot_due(s, ch.id) is False,
   "aware capture today → not due")

# Other channel with a fresh snapshot must not satisfy this channel.
ch2 = make_channel(s, slug="due-2")
ok(metrics_loop._snapshot_due(s, ch2.id) is True,
   "other channel's today-snapshot does not satisfy this channel")

# Yesterday (aware) → due again.
s2 = fresh_session()
ch_y = make_channel(s2, slug="due-y")
yesterday = _day_start() - timedelta(hours=1)
add_metric(s2, ch_y.id, yesterday, subscriber_count=1)
ok(metrics_loop._snapshot_due(s2, ch_y.id) is True,
   "yesterday's capture → due again at the next UTC day")

# SQLite stores naive datetimes; the gate must not TypeError on naive vs aware.
s3 = fresh_session()
ch_n = make_channel(s3, slug="due-naive")
# Store as naive UTC wall time for "today"
naive_today = (_day_start() + timedelta(hours=1)).replace(tzinfo=None)
add_metric(s3, ch_n.id, naive_today)
ok(metrics_loop._snapshot_due(s3, ch_n.id) is False,
   "SQLite-naive capture today → not due (tzinfo attached before compare)")

s3b = fresh_session()
ch_ny = make_channel(s3b, slug="due-naive-y")
naive_yest = (_day_start() - timedelta(hours=2)).replace(tzinfo=None)
add_metric(s3b, ch_ny.id, naive_yest)
ok(metrics_loop._snapshot_due(s3b, ch_ny.id) is True,
   "SQLite-naive capture yesterday → due")

# Boundary: capture exactly at _day_start is "today" (not due). A mutant using
# `<=` for the due check would still pass here; the discriminating mutant is
# `>` (flipped comparison) which makes today's capture look due.
s4 = fresh_session()
ch_b = make_channel(s4, slug="due-boundary")
add_metric(s4, ch_b.id, _day_start())
ok(metrics_loop._snapshot_due(s4, ch_b.id) is False,
   "capture exactly at UTC midnight → not due (cap < day_start is False)")


# ---------------------------------------------------------------------------
# record_snapshot happy path
# ---------------------------------------------------------------------------
print("\nrecord_snapshot: happy path writes metric + 1-unit success JobRun")

_ORIG_GET = youtube.get_service
_ORIG_FETCH = youtube.fetch_channel
_calls = {"get_service": [], "fetch_channel": []}


def _fake_get(slug):
    _calls["get_service"].append(slug)
    return {"service": slug}


def _fake_fetch(service):
    _calls["fetch_channel"].append(service)
    return {
        "id": "UC_test",
        "title": "T",
        "statistics": {
            "subscriber_count": 1234,
            "view_count": 56789,
            "video_count": 42,
        },
    }


youtube.get_service = _fake_get
youtube.fetch_channel = _fake_fetch
try:
    s = fresh_session()
    ch = make_channel(s, slug="happy-ch")
    before_runs = len(jobruns(s))
    m = metrics_loop.record_snapshot(s, ch)
    s.commit()
    ok(m is not None, "happy path returns a ChannelMetric")
    ok(m.channel_id == ch.id, "metric.channel_id matches the channel")
    ok(m.subscriber_count == 1234, "subscriber_count taken from statistics")
    ok(m.view_count == 56789, "view_count taken from statistics")
    ok(m.video_count == 42, "video_count taken from statistics")
    # Persist + re-read so we prove the session was dirtied, not just a detached obj
    rows = metrics_for(s, ch.id)
    ok(len(rows) == 1, "exactly one ChannelMetric row written")
    ok(rows[0].subscriber_count == 1234 and rows[0].view_count == 56789
       and rows[0].video_count == 42,
       "persisted row carries the fetched counts")
    runs = jobruns(s, kind="metrics", status="success")
    ok(len(runs) == before_runs + 1, "exactly one metrics success JobRun")
    ok(runs[-1].channel_id == ch.id, "JobRun.channel_id matches")
    ok(runs[-1].quota_cost == 1, "channels.list costs 1 quota unit")
    ok(_calls["get_service"] == ["happy-ch"],
       "get_service called with the channel slug (not id/name)")
    ok(len(_calls["fetch_channel"]) == 1
       and _calls["fetch_channel"][0] == {"service": "happy-ch"},
       "fetch_channel received the service handle from get_service")
finally:
    youtube.get_service = _ORIG_GET
    youtube.fetch_channel = _ORIG_FETCH
    _calls["get_service"].clear()
    _calls["fetch_channel"].clear()


# ---------------------------------------------------------------------------
# stats defaults
# ---------------------------------------------------------------------------
print("\nrecord_snapshot: missing/empty statistics → zeros")

youtube.get_service = lambda slug: object()
try:
    for label, payload in (
        ("no statistics key", {"id": "x", "title": "t"}),
        ("statistics is None", {"id": "x", "statistics": None}),
        ("statistics is empty dict", {"id": "x", "statistics": {}}),
    ):
        youtube.fetch_channel = lambda service, p=payload: p
        s = fresh_session()
        ch = make_channel(s, slug=f"zeros-{label[:8].strip()}")
        m = metrics_loop.record_snapshot(s, ch)
        s.commit()
        ok(m is not None, f"{label}: still returns a metric")
        ok((m.subscriber_count, m.view_count, m.video_count) == (0, 0, 0),
           f"{label}: all counts default to 0")
finally:
    youtube.get_service = _ORIG_GET
    youtube.fetch_channel = _ORIG_FETCH


# ---------------------------------------------------------------------------
# NeedsConnect + transient (contract pins; full alert semantics in verify_notify)
# ---------------------------------------------------------------------------
print("\nrecord_snapshot: NeedsConnect flips dead; transient skips cleanly")

_ORIG_HAS = youtube.has_token
youtube.has_token = lambda slug: True  # dead_status_for → EXPIRED (file present)


def _raise_needs(slug):
    raise youtube.NeedsConnect(f"token dead for {slug}")


def _raise_transient(slug):
    raise RuntimeError("network blip")


# Silence the mark_dead alert log noise (webhook may be unset anyway).
youtube.get_service = _raise_needs
try:
    s = fresh_session()
    ch = make_channel(s, slug="dead-tok")
    with patch.object(notify, "alert_dead", lambda *a, **k: None):
        m = metrics_loop.record_snapshot(s, ch)
    s.refresh(ch)
    ok(m is None, "NeedsConnect yields None (no snapshot)")
    ok(ch.oauth_status == OAuthStatus.EXPIRED,
       "NeedsConnect flips the channel to EXPIRED durably")
    ok(len(metrics_for(s, ch.id)) == 0, "NeedsConnect writes no ChannelMetric")
    ok(len(jobruns(s, kind="metrics", status="success")) == 0,
       "NeedsConnect writes no metrics success JobRun")
finally:
    youtube.get_service = _ORIG_GET

youtube.get_service = _raise_transient
try:
    s = fresh_session()
    ch = make_channel(s, slug="blip-tok")
    m = metrics_loop.record_snapshot(s, ch)
    s.refresh(ch)
    ok(m is None, "transient failure yields None")
    ok(ch.oauth_status == OAuthStatus.CONNECTED,
       "transient failure leaves status CONNECTED")
    ok(len(metrics_for(s, ch.id)) == 0, "transient failure writes no metric")
    ok(len(jobruns(s, kind="metrics", status="success")) == 0,
       "transient failure writes no success JobRun")
finally:
    youtube.get_service = _ORIG_GET
    youtube.has_token = _ORIG_HAS


# ---------------------------------------------------------------------------
# tick(): CONNECTED filter + due-only
# ---------------------------------------------------------------------------
print("\ntick(): only CONNECTED + due channels are probed")

# session_scope for tick() points at our in-memory session.
_ORIG_SCOPE = metrics_loop.session_scope
_probe_slugs: list[str] = []


@contextmanager
def _scoped(session):
    yield session
    session.commit()


def _probe_get(slug):
    _probe_slugs.append(slug)
    return {"service": slug}


def _probe_fetch(service):
    return {
        "statistics": {
            "subscriber_count": 1,
            "view_count": 2,
            "video_count": 3,
        },
    }


youtube.get_service = _probe_get
youtube.fetch_channel = _probe_fetch
try:
    s = fresh_session()
    ch_due = make_channel(s, slug="tick-due", oauth_status=OAuthStatus.CONNECTED)
    ch_fresh = make_channel(s, slug="tick-fresh", oauth_status=OAuthStatus.CONNECTED)
    ch_exp = make_channel(s, slug="tick-exp", oauth_status=OAuthStatus.EXPIRED)
    ch_dis = make_channel(s, slug="tick-dis", oauth_status=OAuthStatus.DISCONNECTED)
    ch_err = make_channel(s, slug="tick-err", oauth_status=OAuthStatus.ERROR)
    # ch_fresh already has today's snapshot → not due
    add_metric(s, ch_fresh.id, _day_start() + timedelta(hours=2), subscriber_count=9)

    metrics_loop.session_scope = lambda: _scoped(s)
    _probe_slugs.clear()
    metrics_loop.tick()

    ok(_probe_slugs == ["tick-due"],
       f"tick probes only the due CONNECTED channel (got {_probe_slugs!r})")
    due_rows = metrics_for(s, ch_due.id)
    ok(len(due_rows) == 1 and due_rows[0].subscriber_count == 1,
       "tick wrote the due channel's snapshot")
    fresh_rows = metrics_for(s, ch_fresh.id)
    ok(len(fresh_rows) == 1 and fresh_rows[0].subscriber_count == 9,
       "already-snapshotted CONNECTED channel left untouched")
    ok(len(metrics_for(s, ch_exp.id)) == 0
       and len(metrics_for(s, ch_dis.id)) == 0
       and len(metrics_for(s, ch_err.id)) == 0,
       "EXPIRED / DISCONNECTED / ERROR channels never snapshotted")
    ok(len(jobruns(s, kind="metrics", status="success")) == 1,
       "tick logs exactly one metrics success (the due channel)")

    # Second tick same day: nothing due → zero probes.
    _probe_slugs.clear()
    metrics_loop.tick()
    ok(_probe_slugs == [], "second tick same day probes nothing")
    ok(len(jobruns(s, kind="metrics", status="success")) == 1,
       "second tick writes no extra success JobRun")
finally:
    metrics_loop.session_scope = _ORIG_SCOPE
    youtube.get_service = _ORIG_GET
    youtube.fetch_channel = _ORIG_FETCH
    _probe_slugs.clear()


print()
print(f"ALL {_checks} CHECKS PASSED")
