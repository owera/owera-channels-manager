"""Regression checks for youtube_admin monetization (YPP shorts_views gate).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_youtube_admin.py

Backlog #25: `_compute_monetization` summed shorts_views only for topics with
``content_format == "short"``. Render / issues / publish / autofill all treat
empty-format and ``"LONG"`` leftovers as shorts via ``!= "long"``, so a PATCH
leftover omitted those published views from the YPP short-views milestone.
Live topics are canonical; this is latent until a bad PATCH.

Pins:
  - empty / ``"LONG"`` / ``"medium"`` published views count toward shorts_views
  - a real ``"long"`` is excluded
  - canonical ``"short"`` still counts
  - no-metric published video adds nothing; unpublished metrics do not count
  - missing channel is 404; unauthenticated is 401

Uses an in-memory SQLite DB and FastAPI's TestClient (no real manager.db, no
network, no live YouTube). Exits non-zero on the first failed assertion.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, ChannelMetric, OAuthStatus, Topic, Video,
                        VideoMetric, VideoStatus)
from app.routers import youtube_admin as yt_admin

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# Isolated-copy batteries pin this so a stale pyc / wrong PYTHONPATH cannot
# silently test a different checkout (08-01 lesson).
ok(Path(yt_admin.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "youtube_admin module loaded from this tree")


def fresh_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-test"), name=kw.pop("name", "Test"),
                 oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED), **kw)
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


def make_published(session, channel, topic, subject, views, watch_min=0,
                   captured_at=None, status=VideoStatus.PUBLISHED):
    v = Video(channel_id=channel.id, topic_id=topic.id, subject=subject,
              status=status)
    session.add(v)
    session.commit()
    session.refresh(v)
    m = VideoMetric(video_id=v.id, channel_id=channel.id, views=views,
                    watch_time_minutes=watch_min,
                    captured_at=captured_at or datetime(2026, 8, 1, tzinfo=timezone.utc))
    session.add(m)
    session.commit()
    return v, m


# ---------------------------------------------------------------------------
# Helper: shorts_views uses != "long" (empty / LONG / medium count; long does not)
# ---------------------------------------------------------------------------
print("youtube_admin._compute_monetization: leftover formats count as shorts")
engine = fresh_engine()
with Session(engine) as s:
    ch = make_channel(s)
    t_short = make_topic(s, ch, name="Canonical short", content_format="short")
    t_empty = make_topic(s, ch, name="Empty leftover", content_format="")
    t_LONG = make_topic(s, ch, name="LONG leftover", content_format="LONG")
    t_medium = make_topic(s, ch, name="medium leftover", content_format="medium")
    t_long = make_topic(s, ch, name="Canonical long", content_format="long")
    make_published(s, ch, t_short, "short-vid", views=100)
    make_published(s, ch, t_empty, "empty-vid", views=10)
    make_published(s, ch, t_LONG, "LONG-vid", views=20)
    make_published(s, ch, t_medium, "medium-vid", views=40)
    make_published(s, ch, t_long, "long-vid", views=9999, watch_min=120)
    # Unpublished short with metrics must not count (status gate).
    make_published(s, ch, t_short, "draft-vid", views=777,
                   status=VideoStatus.DRAFT)
    # Published short with no VideoMetric adds nothing.
    v_bare = Video(channel_id=ch.id, topic_id=t_short.id, subject="bare-pub",
                   status=VideoStatus.PUBLISHED)
    s.add(v_bare)
    s.commit()

    body = yt_admin._compute_monetization(s, ch.id)

ok(body["shorts_views"] == 170,
   "empty+LONG+medium leftovers join canonical short in shorts_views "
   "(== 'short' would be 100; allowlist without medium would be 130; "
   "always-short would be 10169)")
ok(body["shorts_views"] != 100,
   "empty-format leftover views are not dropped (pre-fix == 'short' pin)")
ok(body["shorts_views"] != 130,
   "medium leftover views are not dropped (allowlist short+empty+LONG mutant)")
ok(9999 not in (body["shorts_views"],),
   "canonical long views are excluded from shorts_views")
ok(body["total_watch_hours"] == 2.0,
   "watch hours still sum ALL latest metrics (long 120min = 2.0); "
   "a shorts-only filter would drop this")
ok(body["subscriber_count"] == 0,
   "no ChannelMetric -> subscriber_count 0")
ok(body["channel_id"] == ch.id, "monetization names the requested channel")
ok(body["lower_tier"]["shorts_views"]["current"] == 170,
   "lower_tier.shorts_views.current mirrors the leftover-inclusive total")
ok(body["full_tier"]["shorts_views"]["current"] == 170,
   "full_tier.shorts_views.current mirrors the leftover-inclusive total")
ok(body["lower_tier"]["shorts_views"]["achieved"] is False,
   "170 << 3_000_000 is not the lower shorts milestone")

# Latest VideoMetric wins; older snapshot is ignored.
print("youtube_admin._compute_monetization: latest metric + isolation")
engine = fresh_engine()
with Session(engine) as s:
    ch = make_channel(s, slug="iso-a")
    other = make_channel(s, slug="iso-b")
    t = make_topic(s, ch, name="Short A", content_format="short")
    t_other = make_topic(s, other, name="Short B", content_format="short")
    t0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=1)
    v, _old = make_published(s, ch, t, "snap-vid", views=5, captured_at=t0)
    s.add(VideoMetric(video_id=v.id, channel_id=ch.id, views=50, captured_at=t1))
    s.commit()
    make_published(s, other, t_other, "other-vid", views=8000)
    s.add(ChannelMetric(channel_id=ch.id, subscriber_count=10, captured_at=t0))
    s.add(ChannelMetric(channel_id=ch.id, subscriber_count=42, captured_at=t1))
    s.commit()

    a = yt_admin._compute_monetization(s, ch.id)
    b = yt_admin._compute_monetization(s, other.id)

ok(a["shorts_views"] == 50,
   "latest VideoMetric wins (50 not the older 5)")
ok(b["shorts_views"] == 8000,
   "per-channel isolation: sibling channel shorts_views stay on their own rows")
ok(a["subscriber_count"] == 42,
   "latest ChannelMetric wins for subscriber_count (42 not 10)")
ok(a["shorts_views"] != b["shorts_views"],
   "two channels do not share a shorts_views total")

# Missing channel 404 + HTTP wiring + auth.
print("GET /api/channels/{id}/monetization: HTTP contract")
http_engine = fresh_engine()
with Session(http_engine) as s:
    http_ch = make_channel(s, slug="http-ch")
    t_http = make_topic(s, http_ch, name="Empty HTTP", content_format="")
    t_long_http = make_topic(s, http_ch, name="Long HTTP", content_format="long")
    make_published(s, http_ch, t_http, "http-empty", views=15)
    make_published(s, http_ch, t_long_http, "http-long", views=4000)
    http_ch_id = http_ch.id


def _override_session():
    with Session(http_engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

try:
    r = client.get(f"/api/channels/{http_ch_id}/monetization", auth=auth)
    ok(r.status_code == 200, "existing channel monetization is 200")
    payload = r.json()
    ok(payload.get("shorts_views") == 15,
       "HTTP shorts_views counts empty-format leftover and excludes the long "
       "(== 'short' would be 0)")
    ok(payload.get("channel_id") == http_ch_id,
       "HTTP payload names the requested channel")
    ok("lower_tier" in payload and "full_tier" in payload,
       "HTTP payload carries both YPP tiers")
    ok(payload["lower_tier"]["shorts_views"]["current"] == 15,
       "HTTP lower_tier current matches leftover-inclusive shorts_views")

    r = client.get("/api/channels/99999/monetization", auth=auth)
    ok(r.status_code == 404, "missing channel is 404")
    ok("not found" in r.text.lower(), "missing-channel 404 names the miss")

    r = client.get(f"/api/channels/{http_ch_id}/monetization")
    ok(r.status_code == 401, "monetization still requires auth")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
