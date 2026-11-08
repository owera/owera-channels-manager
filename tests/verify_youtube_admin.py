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
  - a foreign workspace 404s LIST/mutate routes before get_service
    (PR #196 residual: youtube_admin still session.gets Channel)

Uses an in-memory SQLite DB and FastAPI's TestClient (no real manager.db, no
network, no live YouTube). Exits non-zero on the first failed assertion.
"""
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, ChannelMetric, OAuthStatus, Topic, Video,
                        VideoMetric, VideoStatus, Workspace)
from app.routers import youtube_admin as yt_admin
from app.services import youtube

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
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(slug="tenant-b", name="Tenant B"))
    s.commit()
    ws_b = s.exec(select(Workspace).where(Workspace.slug == "tenant-b")).one()
    http_ch = make_channel(s, slug="http-ch")
    t_http = make_topic(s, http_ch, name="Empty HTTP", content_format="")
    t_long_http = make_topic(s, http_ch, name="Long HTTP", content_format="long")
    make_published(s, http_ch, t_http, "http-empty", views=15)
    make_published(s, http_ch, t_long_http, "http-long", views=4000)
    http_ch_id = http_ch.id
    ch_b = make_channel(s, slug="b-ch", workspace_id=ws_b.id)
    CHANNEL_B_ID = ch_b.id


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

    print("workspace isolation: youtube_admin routes follow require_channel")
    _orig_mw = settings.multi_workspace
    settings.multi_workspace = True
    auth_ow = ("owera", "testpw")
    auth_b = ("tenant-b", "testpw")
    yt_calls = []
    _orig_get = youtube.get_service
    _orig_fetch = youtube.fetch_channel
    _orig_brand = youtube.update_branding
    _orig_resolve = youtube.resolve_channel_id
    _orig_sub = youtube.subscribe
    _orig_snap = yt_admin.analytics_loop._snapshot_channel

    def fake_get_service(slug, channel=None, session=None):
        yt_calls.append(("get_service", slug))
        return object()

    def fake_fetch(service):
        yt_calls.append(("fetch_channel",))
        return {"id": "UC-owera"}

    def fake_branding(service, yt_id, **kw):
        yt_calls.append(("update_branding", yt_id))
        return {"ok": True}

    def fake_resolve(service, channel):
        yt_calls.append(("resolve", channel))
        return "UC-target"

    def fake_subscribe(service, target):
        yt_calls.append(("subscribe", target))
        return {"id": "sub-1"}

    def fake_snapshot(session, ch, now, force=False):
        yt_calls.append(("snapshot", ch.slug, force))
        return 0

    youtube.get_service = fake_get_service
    youtube.fetch_channel = fake_fetch
    youtube.update_branding = fake_branding
    youtube.resolve_channel_id = fake_resolve
    youtube.subscribe = fake_subscribe
    yt_admin.analytics_loop._snapshot_channel = fake_snapshot
    try:
        n_calls = len(yt_calls)
        r = client.get(f"/api/channels/{http_ch_id}/monetization", auth=auth_b)
        ok(r.status_code == 404 and "channel not found" in r.text,
           "tenant-b GET owera monetization → 404 channel not found "
           f"({r.status_code} {r.text[:80]})")
        ok("shorts_views" not in r.text,
           "tenant-b monetization does not leak owera YPP numbers")

        r = client.get(f"/api/channels/{http_ch_id}/metrics", auth=auth_b)
        ok(r.status_code == 404 and "channel not found" in r.text,
           "tenant-b GET owera metrics → 404")

        r = client.get(f"/api/channels/{http_ch_id}/youtube", auth=auth_b)
        ok(r.status_code == 404 and "channel not found" in r.text,
           "tenant-b GET owera /youtube → 404")
        ok(len(yt_calls) == n_calls,
           "tenant-b /youtube never called get_service")

        r = client.put(f"/api/channels/{http_ch_id}/branding", auth=auth_b,
                       json={"title": "Hijacked"})
        ok(r.status_code == 404 and "channel not found" in r.text,
           "tenant-b PUT owera branding → 404")
        ok(len(yt_calls) == n_calls,
           "tenant-b branding never called get_service / update_branding")

        r = client.post(f"/api/channels/{http_ch_id}/subscriptions", auth=auth_b,
                        json={"channel": "@victim"})
        ok(r.status_code == 404 and "channel not found" in r.text,
           "tenant-b POST owera subscribe → 404")
        ok(len(yt_calls) == n_calls,
           "tenant-b subscribe never called get_service")

        r = client.post(f"/api/channels/{http_ch_id}/video-analytics/refresh",
                        auth=auth_b)
        ok(r.status_code == 404 and "channel not found" in r.text,
           "tenant-b refresh owera video-analytics → 404")
        ok(len(yt_calls) == n_calls,
           "tenant-b analytics refresh never snapshot the foreign channel")

        r = client.get(f"/api/channels/{http_ch_id}/monetization", auth=auth_ow)
        ok(r.status_code == 200 and r.json().get("shorts_views") == 15,
           "owera GET own monetization still 200")

        r = client.get(f"/api/channels/{CHANNEL_B_ID}/monetization", auth=auth_b)
        ok(r.status_code == 200,
           f"tenant-b GET own monetization still 200 ({r.status_code})")

        src_conn = inspect.getsource(yt_admin._connected)
        src_overview = inspect.getsource(yt_admin.youtube_overview)
        src_brand = inspect.getsource(yt_admin.update_branding)
        src_metrics = inspect.getsource(yt_admin.get_metrics)
        src_monet = inspect.getsource(yt_admin.get_monetization)
        src_refresh = inspect.getsource(yt_admin.refresh_video_analytics)
        ok("require_channel" in src_conn and "current_workspace" in src_overview,
           "_connected uses require_channel; overview injects current_workspace")
        ok("require_channel" in src_brand or " _connected(" in src_brand,
           "update_branding goes through _connected (live YouTube mutate)")
        ok("require_channel" in src_metrics and "current_workspace" in src_metrics,
           "get_metrics gates through require_channel + current_workspace "
           "(session.get alone is the cross-tenant leak)")
        ok("require_channel" in src_monet and "current_workspace" in src_monet,
           "get_monetization gates through require_channel + current_workspace")
        ok("require_channel" in src_refresh and "current_workspace" in src_refresh,
           "refresh_video_analytics gates through require_channel "
           "(session.get then _snapshot_channel is the leak)")
        ok("session.get(Channel, channel_id)" not in inspect.getsource(yt_admin),
           "youtube_admin has no remaining bare session.get(Channel, channel_id)")
        for fn in (yt_admin.youtube_overview, yt_admin.update_branding,
                   yt_admin.refresh_metrics, yt_admin.list_subscriptions,
                   yt_admin.add_subscription, yt_admin.remove_subscription,
                   yt_admin.list_subscribers):
            ok("_connected(session, channel_id, ws)" in inspect.getsource(fn),
               f"{fn.__name__} passes ws into _connected "
               "(two-arg _connected is the pre-fix leak)")
        for fn in (yt_admin.video_analytics, yt_admin.video_analytics_by_topic):
            ok("require_channel" in inspect.getsource(fn)
               and "current_workspace" in inspect.getsource(fn),
               f"{fn.__name__} gates through require_channel + current_workspace")
    finally:
        youtube.get_service = _orig_get
        youtube.fetch_channel = _orig_fetch
        youtube.update_branding = _orig_brand
        youtube.resolve_channel_id = _orig_resolve
        youtube.subscribe = _orig_sub
        yt_admin.analytics_loop._snapshot_channel = _orig_snap
        settings.multi_workspace = _orig_mw
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
