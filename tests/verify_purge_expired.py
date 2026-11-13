"""P3b: EXPIRED channels lose their YouTube metrics + branding after 30 days,
including channels that were already dead before the purge clock existed.

Run: PYTHONPATH=. uv run python tests/verify_purge_expired.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("MANAGER_APP_PASSWORD", "")

from sqlmodel import Session, SQLModel, create_engine, select

import app.db as db_mod
from app.config import settings
from app.models import Channel, ChannelMetric, OAuthStatus, Topic, Video, VideoMetric, Workspace, utcnow
from app.services import notify, youtube_purge

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


settings.credentials_dir = tempfile.mkdtemp()
settings.resend_api_key = ""
d = tempfile.mkdtemp()
eng = create_engine(f"sqlite:///{d}/t.db", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(eng)
p = patch.object(db_mod, "engine", eng)
p.start()

with Session(eng) as s:
    s.add(Workspace(id=2, slug="tenant-b", name="Tenant B"))
    rows = [
        Channel(id=4, workspace_id=2, slug="o3-consent-smoke", name="elen3x", yt_channel_title="elen3x",
                yt_channel_id="UC4", oauth_status=OAuthStatus.EXPIRED),               # dead before deploy
        Channel(id=3, workspace_id=2, slug="o4-dogfood", name="O4", oauth_status=OAuthStatus.DISCONNECTED,
                paused=True),                                                          # never connected, no data
        Channel(id=5, workspace_id=2, slug="old-disc", name="Old", oauth_status=OAuthStatus.DISCONNECTED),
        Channel(id=6, workspace_id=2, slug="err", name="Err", oauth_status=OAuthStatus.ERROR),
        Channel(id=7, workspace_id=2, slug="live", name="Live", oauth_status=OAuthStatus.CONNECTED),
    ]
    for r in rows:
        s.add(r)
    s.commit()
    t = Topic(channel_id=4, name="T"); s.add(t); s.commit(); s.refresh(t)
    v = Video(channel_id=4, topic_id=t.id, subject="s"); s.add(v); s.commit(); s.refresh(v)
    for cid in (4, 5, 7):
        s.add(ChannelMetric(channel_id=cid, subscriber_count=1))
    s.add(VideoMetric(video_id=v.id, channel_id=4, views=1))
    s.commit()

with Session(eng) as s:
    n = youtube_purge.backfill_lost_access(s)
ok(n == 2, "backfill stamps the already-EXPIRED channel and the DISCONNECTED one holding metrics")
with Session(eng) as s:
    ok(s.get(Channel, 4).access_lost_reason == "expired_backfill", "EXPIRED → expired_backfill")
    ok(s.get(Channel, 5).access_lost_reason == "disconnected_backfill", "DISCONNECTED with data → backfilled")
    ok(s.get(Channel, 3).access_lost_at is None, "never-connected channel without data → untouched")
    ok(s.get(Channel, 6).access_lost_at is None, "ERROR channel → untouched")
    ok(s.get(Channel, 7).access_lost_at is None, "CONNECTED channel → untouched")
    stamp = s.get(Channel, 4).access_lost_at
with Session(eng) as s:
    ok(youtube_purge.backfill_lost_access(s) == 0 and s.get(Channel, 4).access_lost_at == stamp,
       "backfill is idempotent")

ok(youtube_purge.tick(now=stamp + timedelta(days=30) - timedelta(hours=1)) == 0,
   "29d23h after backfill: YouTube data still exists")
ok(youtube_purge.tick(now=stamp + timedelta(days=30)) == 2,
   "30d after backfill: both purged")
with Session(eng) as s:
    c4 = s.get(Channel, 4)
    ok(not s.exec(select(ChannelMetric).where(ChannelMetric.channel_id == 4)).all()
       and not s.exec(select(VideoMetric).where(VideoMetric.channel_id == 4)).all(), "EXPIRED channel metrics deleted")
    ok(c4.yt_channel_title is None and c4.name == "o3-consent-smoke", "EXPIRED channel branding (title/name) deleted")
    ok(s.get(Workspace, 2) is not None and s.exec(select(Video).where(Video.channel_id == 4)).first(),
       "workspace and video rows kept")
    ok(s.exec(select(ChannelMetric).where(ChannelMetric.channel_id == 7)).first() is not None,
       "connected channel data kept")

# a live flip to EXPIRED without invalid_grant also starts the clock now
with Session(eng) as s:
    notify.mark_dead_committed(s, s.get(Channel, 7), "token missing/expired — reconnect required",
                               status=OAuthStatus.EXPIRED)
with Session(eng) as s:
    c7 = s.get(Channel, 7)
    ok(c7.access_lost_at is not None and c7.access_lost_reason == "expired", "generic EXPIRED flip stamps the clock")
with Session(eng) as s:
    notify.mark_dead_committed(s, s.get(Channel, 6), "consent failed", status=OAuthStatus.ERROR)
    ok(s.get(Channel, 6).access_lost_at is None, "ERROR flip never stamps")

p.stop()
print(f"\nALL {_checks} CHECKS PASSED")
