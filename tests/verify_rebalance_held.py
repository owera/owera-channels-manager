"""Rebalance dual (0 approved longs → demote one queued short) must only fire when
the freed slot really goes to an eligible long THIS tick (#1474, #1328).

Run: PYTHONPATH=. .venv/bin/python tests/verify_rebalance_held.py

Live ch1 2026-11-11 09:26 / 09:45 BRT: budget 1, rendered_today 0, queued =
#1473 (held) + #1474 (unheld, just unheld/produced), long draft #1476. The
rebalance (which ignores held rows) saw headroom 0 and demoted #1474 ("rebalance:
demoted queued short → draft so a long can fill the 1L mix"); _auto_produce
(which counts the held #1473) then had headroom 0 too, so the long never queued
and the slot idled. Guard: demote only when an unheld, subject-valid long draft
exists, the auto-produce breaker is closed, and _auto_produce's own headroom is
≥1 after the demote. Otherwise the short stays QUEUED.
"""
import sys
from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import os as _os
_os.environ.setdefault("MANAGER_TOPIC_FLAGS", '{"vm_pass_required": []}')
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus  # noqa: E402
from app.services import episode_guard as _eg  # noqa: E402
_eg.duplicate_episode_reason = lambda session, video: None
from app.services import render_loop  # noqa: E402

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    return Session(e)


def chan(s, slug="owera-software", name="Owera Software", budget=1):
    c = Channel(slug=slug, name=name, oauth_status=OAuthStatus.CONNECTED, daily_render_budget=budget)
    s.add(c); s.commit(); s.refresh(c)
    return c


def topic(s, c, fmt, weight=2, name=None):
    t = Topic(channel_id=c.id, name=name or fmt, theme_prompt="x", content_format=fmt, weight=weight)
    s.add(t); s.commit(); s.refresh(t)
    return t


def vid(s, c, t, status, held=False, subject="Test subject"):
    v = Video(channel_id=c.id, topic_id=t.id, subject=subject, status=status, held=held)
    s.add(v); s.commit(); s.refresh(v)
    return v


def st(s, v):
    s.expire_all()
    return s.get(Video, v.id).status


def tick(s, n=1):
    for _ in range(n):
        render_loop._rebalance_queued_mix(s)
        render_loop._auto_produce(s)
        s.commit()


print("#1474 shape: held queued short + unheld queued short + long draft, budget 1")
s = fresh(); c = chan(s); ts = topic(s, c, "short", name="Agent traps"); tl = topic(s, c, "long", weight=3)
v1473 = vid(s, c, ts, VideoStatus.QUEUED, held=True)
v1474 = vid(s, c, ts, VideoStatus.QUEUED)
v1476 = vid(s, c, tl, VideoStatus.DRAFT)
tick(s, 3)
ok(st(s, v1474) == VideoStatus.QUEUED, "#1474 stays QUEUED (no snap-back to draft) over 3 ticks")
ok(st(s, v1473) == VideoStatus.QUEUED and s.get(Video, v1473.id).held, "#1473 untouched (queued, held)")
ok(st(s, v1476) == VideoStatus.DRAFT, "long draft not queued (auto_produce has no slot: held row counts)")
ok(not [j for j in s.exec(select(JobRun)).all() if "rebalance" in (j.detail or "")],
   "no 'rebalance: demoted' JobRun written")

print("held long only: short stays queued")
s = fresh(); c = chan(s); ts = topic(s, c, "short"); tl = topic(s, c, "long")
q = vid(s, c, ts, VideoStatus.QUEUED)
vid(s, c, tl, VideoStatus.APPROVED, held=True)
tick(s)
ok(st(s, q) == VideoStatus.QUEUED, "held APPROVED long (no draft) → short stays")
s = fresh(); c = chan(s, budget=1); ts = topic(s, c, "short"); tl = topic(s, c, "long")
q = vid(s, c, ts, VideoStatus.QUEUED)
hl = vid(s, c, tl, VideoStatus.QUEUED, held=True)
dl = vid(s, c, tl, VideoStatus.DRAFT)
tick(s)
ok(st(s, q) == VideoStatus.QUEUED and st(s, dl) == VideoStatus.DRAFT and st(s, hl) == VideoStatus.QUEUED,
   "held QUEUED long + long draft (budget 1) → short stays; held long never counts as a taker")

print("no long: short stays")
s = fresh(); c = chan(s); ts = topic(s, c, "short")
q = vid(s, c, ts, VideoStatus.QUEUED)
tick(s)
ok(st(s, q) == VideoStatus.QUEUED, "no long at all → short stays")

print("ineligible long draft: short stays")
s = fresh(); c = chan(s); ts = topic(s, c, "short"); tl = topic(s, c, "long")
q = vid(s, c, ts, VideoStatus.QUEUED)
bad = vid(s, c, tl, VideoStatus.DRAFT, subject="Why your prompt costs $79 a day")
render_loop._rebalance_queued_mix(s); s.commit()
ok(st(s, q) == VideoStatus.QUEUED, "long draft failing the subject guard is not a taker → short stays")
s = fresh(); c = chan(s); ts = topic(s, c, "short"); tl = topic(s, c, "long")
q = vid(s, c, ts, VideoStatus.QUEUED); dl = vid(s, c, tl, VideoStatus.DRAFT)
s.add(JobRun(kind="render", status="error", channel_id=c.id, video_id=q.id, detail="boom",
             created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
s.commit()
tick(s)
ok(st(s, q) == VideoStatus.QUEUED and st(s, dl) == VideoStatus.DRAFT,
   "auto-produce breaker open (last render failed) → short stays, long not queued")

print("eligible unheld long draft: demote exactly as before (OS and RR)")
for slug, name in (("owera-software", "Owera Software"), ("rodrigo-recio", "Rodrigo Recio")):
    s = fresh(); c = chan(s, slug=slug, name=name); ts = topic(s, c, "short"); tl = topic(s, c, "long")
    q = vid(s, c, ts, VideoStatus.QUEUED); dl = vid(s, c, tl, VideoStatus.DRAFT)
    render_loop._rebalance_queued_mix(s); s.commit()
    ok(st(s, q) == VideoStatus.DRAFT, f"{slug}: budget 1, one queued short + long draft → short demoted")
    render_loop._auto_produce(s); s.commit()
    ok(st(s, dl) == VideoStatus.QUEUED, f"{slug}: same tick auto_produce queues the long")
s = fresh(); c = chan(s, budget=5); ts = topic(s, c, "short", weight=4); tl = topic(s, c, "long")
qs = [vid(s, c, ts, VideoStatus.QUEUED) for _ in range(5)]; dl = vid(s, c, tl, VideoStatus.DRAFT)
tick(s)
ok([st(s, v) for v in qs].count(VideoStatus.DRAFT) == 1 and st(s, qs[-1]) == VideoStatus.DRAFT
   and st(s, dl) == VideoStatus.QUEUED, "ch2 2026-08-22 shape (5 shorts, budget 5): newest short demoted, long queued (1L+4S)")
s = fresh(); c = chan(s, budget=5); ts = topic(s, c, "short", weight=4); tl = topic(s, c, "long")
qs = [vid(s, c, ts, VideoStatus.QUEUED) for _ in range(4)]
h = vid(s, c, ts, VideoStatus.QUEUED, held=True); dl = vid(s, c, tl, VideoStatus.DRAFT)
tick(s)
ok(all(st(s, v) == VideoStatus.QUEUED for v in qs) and st(s, dl) == VideoStatus.DRAFT,
   "4 unheld + 1 held queued short (budget 5): no demote — the held row would eat the freed slot")

print(f"ALL {_checks} CHECKS PASSED")
