"""S8a / LegalOps L4: raw YouTube metrics only — no derived or fabricated numbers.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_raw_metrics.py

PO CM cut S8a (14 Nov 2026, before the Google verification submission — the
reviewer sees the UI). L4 audit items A3–A6 / C2–C4:
  - no YPP milestone widget data (pct / needed / achieved / tier_achieved,
    lower_tier / full_tier), no summed total_watch_hours / shorts_views;
    GET /monetization is 410 Gone (not a SPA-fallback 200)
  - no subDelta in the SPA
  - /api/agent/state: no monetization_by_channel, no avg_views / avg_ctr (or any
    avg_*), no impressions / ctr; raw channels.list counts instead
  - impressions / ctr are never stored or served as an invented 0:
    fetch_video_analytics -> None, VideoMetric default NULL, leaderboard omits
    the keys, the init_db migration turns legacy fabricated zeros into NULL
  - an unmeasured video reports None, not 0, in the leaderboard
  - static grep of the SPA source and the state/analytics router code

In-memory / temp-file SQLite, no network, no tokens, never the real manager.db.
Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import base64
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.db as app_db
from app.config import settings
from app.db import get_session
from app.models import (Channel, ChannelMetric, OAuthStatus, Topic, Video,
                        VideoMetric, VideoStatus, Workspace)
from app.routers import queue, youtube_admin
from app.services import youtube

ROOT = Path(__file__).resolve().parents[1]
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(youtube_admin.__file__).resolve().parents[2] == ROOT,
   "youtube_admin module loaded from this tree")

FORBIDDEN_KEYS = {"pct", "needed", "achieved", "tier_achieved", "lower_tier", "full_tier",
                  "total_watch_hours", "shorts_views", "avg_views", "avg_ctr",
                  "avg_view_duration", "monetization_by_channel", "impressions", "ctr",
                  "subDelta"}


def all_keys(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            all_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            all_keys(v, acc)
    return acc


# ---------------------------------------------------------------------------
print("fetch_video_analytics / VideoMetric: impressions & ctr are None, not 0")


class _Exec:
    def __init__(self, resp):
        self.resp = resp

    def execute(self):
        return self.resp


class FakeAnalytics:
    def __init__(self, resp):
        self.resp = resp

    def reports(self):
        return self

    def query(self, **kw):
        return _Exec(self.resp)


got = youtube.fetch_video_analytics(FakeAnalytics({
    "rows": [[9, 3, 40.0, 12.5, 2, 1, 0]],
    "columnHeaders": [{"name": n} for n in (
        "views", "estimatedMinutesWatched", "averageViewPercentage",
        "averageViewDuration", "likes", "comments", "subscribersGained")],
}), "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["impressions"] is None and got["ctr"] is None,
   "fetch_video_analytics: impressions/ctr None (API does not expose them)")
ok(got["views"] == 9 and got["avg_view_pct"] == 40.0, "raw API fields still mapped")
vm = VideoMetric(video_id=1, channel_id=1, views=5)
ok(vm.impressions is None and vm.ctr is None, "VideoMetric default impressions/ctr is None")

# ---------------------------------------------------------------------------
print("fixture: one workspace, a connected channel, snapshots, measured + unmeasured videos")
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
now = datetime.now(timezone.utc)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(id=1, workspace_id=1, slug="raw-ch", name="Raw CH",
                  oauth_status=OAuthStatus.CONNECTED, yt_channel_id="UCraw"))
    s.add(Topic(id=1, channel_id=1, name="Shorts T", content_format="short", position=1))
    s.add(Topic(id=2, channel_id=1, name="Long T", content_format="long", position=2))
    s.add(ChannelMetric(channel_id=1, subscriber_count=40, view_count=900, video_count=10,
                        captured_at=now - timedelta(days=3)))
    s.add(ChannelMetric(channel_id=1, subscriber_count=57, view_count=1234, video_count=12,
                        captured_at=now - timedelta(hours=1)))
    s.add(Video(id=10, channel_id=1, topic_id=1, subject="short a", status=VideoStatus.PUBLISHED,
                yt_video_id="ya", published_at=now - timedelta(days=5)))
    s.add(Video(id=11, channel_id=1, topic_id=2, subject="long b", status=VideoStatus.PUBLISHED,
                yt_video_id="yb", published_at=now - timedelta(days=5)))
    s.add(Video(id=12, channel_id=1, topic_id=1, subject="short c (no data)",
                status=VideoStatus.PUBLISHED, yt_video_id="yc",
                published_at=now - timedelta(hours=2)))
    s.add(VideoMetric(video_id=10, channel_id=1, views=300, avg_view_pct=80.0,
                      watch_time_minutes=120, likes=3, comments=1, subscribers_gained=2))
    s.add(VideoMetric(video_id=11, channel_id=1, views=50, avg_view_pct=30.0,
                      watch_time_minutes=600, likes=1, comments=0, subscribers_gained=4))
    s.commit()


def _sess():
    with Session(engine) as session:
        yield session


app = FastAPI()
app.include_router(queue.router)
app.include_router(youtube_admin.router)
app.dependency_overrides[get_session] = _sess
client = TestClient(app)
auth = {"Authorization": "Basic " + base64.b64encode(b"owera:secret").decode()}

# ---------------------------------------------------------------------------
print("GET /api/agent/state: raw channel stats only")
r = client.get("/api/agent/state", headers=auth)
ok(r.status_code == 200, "agent/state 200")
state = r.json()
leak = all_keys(state) & FORBIDDEN_KEYS
ok(not leak, f"agent/state carries no derived/fabricated keys (found: {sorted(leak)})")
ok("monetization_by_channel" not in state, "monetization_by_channel removed")
cs = state.get("channel_stats_by_channel", {}).get("1")
ok(cs is not None and cs["subscriber_count"] == 57 and cs["view_count"] == 1234
   and cs["video_count"] == 12, "channel_stats_by_channel = latest raw channels.list snapshot")
ok(set(cs) == {"subscriber_count", "view_count", "video_count", "captured_at", "source"},
   "raw channel stats carry no extra computed field")
bt = state["analytics_by_topic"]["1"]["by_topic"]
ok([row["topic_id"] for row in bt] == [1, 2],
   "by-topic rows in neutral topic-id order (no avg_views ranking)")
ok(bt[0]["views"] == 300 and bt[0]["video_count"] == 2 and bt[0]["measured"] == 1,
   "by-topic: raw sums of measured videos; the unmeasured one adds nothing")

# ---------------------------------------------------------------------------
print("GET /video-analytics: impressions/ctr omitted, unmeasured is None")
r = client.get("/api/channels/1/video-analytics?sort=views", headers=auth)
lb = r.json()
items = {it["video_id"]: it for it in lb["items"]}
ok(all("impressions" not in it and "ctr" not in it for it in lb["items"]),
   "leaderboard items omit impressions/ctr")
ok(items[12]["has_data"] is False and items[12]["views"] is None
   and items[12]["avg_view_pct"] is None and items[12]["subscribers_gained"] is None,
   "unmeasured video: metrics None, never 0")
ok(items[10]["views"] == 300 and items[10]["avg_view_pct"] == 80.0,
   "measured video keeps raw API values")
r = client.get("/api/channels/1/video-analytics?sort=ctr", headers=auth)
ok(r.json()["sort"] == "views", "sort=ctr no longer accepted (falls back to views)")

r = client.get("/api/channels/1/video-analytics/by-topic", headers=auth)
leak = all_keys(r.json()) & FORBIDDEN_KEYS
ok(not leak and not any(k.startswith("avg_") for k in all_keys(r.json())),
   f"by-topic has no avg_* / impressions / ctr (found: {sorted(leak)})")

# ---------------------------------------------------------------------------
print("GET /monetization: 410 Gone")
r = client.get("/api/channels/1/monetization", headers=auth)
ok(r.status_code == 410, "monetization endpoint is 410 (removed, not a SPA-fallback 200)")
ok("pct" not in r.text and "tier" not in r.text, "410 body carries no milestone data")
ok(client.get("/api/channels/999/monetization", headers=auth).status_code == 404,
   "missing channel stays 404 (S4 merge swaps in the workspace-scoped check)")
ok(not hasattr(youtube_admin, "_compute_monetization"), "_compute_monetization removed")

import app.main as main  # noqa: E402  (full app: auth middleware)

main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "testpw"
try:
    mc = TestClient(main.app)
    ok(mc.get("/api/channels/1/monetization").status_code == 401,
       "removed endpoint still behind auth (401 without credentials)")
    ok(mc.get("/api/channels/1/monetization", auth=("owera", "testpw")).status_code == 410,
       "full app: authenticated GET /monetization is 410")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _pw

# ---------------------------------------------------------------------------
print("migration: legacy fabricated impressions/ctr zeros become NULL")
db = Path(tempfile.mkdtemp(prefix="s8a-mig-")) / "m.db"
mig = create_engine(f"sqlite:///{db}")
SQLModel.metadata.create_all(mig)
with mig.begin() as c:
    for i, (imp, ctr) in enumerate(((0, 0.0), (0, 0.0), (77, 0.05), (None, None)), start=1):
        c.execute(text(
            "INSERT INTO videometric (id, video_id, channel_id, views, impressions, ctr, "
            "captured_at) VALUES (:i, 1, 1, 5, :imp, :ctr, '2026-09-01 00:00:00')"),
            {"i": i, "imp": imp, "ctr": ctr})
with patch.object(app_db, "engine", mig):
    changed = app_db._null_fabricated_impressions_ctr()
    again = app_db._null_fabricated_impressions_ctr()
with mig.begin() as c:
    rows = list(c.execute(text("SELECT id, impressions, ctr, views FROM videometric ORDER BY id")))
ok(changed == 4 and again == 0, "zeros nulled once (2 rows x 2 cols), then a no-op")
ok([(r[1], r[2]) for r in rows] == [(None, None), (None, None), (77, 0.05), (None, None)],
   "only exact zeros nulled; a non-zero value is never touched")
ok(all(r[3] == 5 for r in rows), "other metric columns untouched")

# ---------------------------------------------------------------------------
print("static grep: SPA source + state/analytics router code")
spa_pat = re.compile(r"\b(subDelta|tier_achieved|total_watch_hours|shorts_views|avg_views|"
                     r"avg_ctr|useMonetization|MonetizationTier|MonetizationMetric|"
                     r"impressions|ctr)\b")
spa_hits = []
for f in sorted((ROOT / "frontend" / "src").rglob("*.ts*")):
    for n, line in enumerate(f.read_text().splitlines(), 1):
        if spa_pat.search(line):
            spa_hits.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
ok(not spa_hits, "SPA source has none of the removed fields: " + "; ".join(spa_hits))
cy = (ROOT / "frontend" / "src" / "pages" / "ChannelYoutube.tsx").read_text()
ok(not re.search(r"\b(pct|needed|achieved|delta)\b", cy, re.I),
   "ChannelYoutube.tsx has no pct / needed / achieved / delta")
be_pat = re.compile(r"total_watch_hours|shorts_views|avg_views|avg_ctr|tier_achieved|"
                    r"_compute_monetization|monetization_by_channel|"
                    r"[\"']impressions[\"']\s*:\s*0|[\"']ctr[\"']\s*:\s*0")
for rel in ("app/routers/queue.py", "app/routers/youtube_admin.py", "app/services/youtube.py"):
    hits = [f"{rel}:{n}" for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1)
            if be_pat.search(line)]
    ok(not hits, f"{rel}: no derived/fabricated metric fields ({hits})")

print(f"ALL {_checks} CHECKS PASSED")
