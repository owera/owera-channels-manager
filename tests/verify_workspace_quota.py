"""WS4 PR-G: per-workspace YouTube quota cap."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Channel, JobRun, Workspace
from app.services import quota

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
now = datetime.now(timezone.utc)

with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera", daily_youtube_quota_cap=4000))
    s.add(Channel(id=1, workspace_id=1, slug="a", name="A"))
    s.add(Channel(id=2, workspace_id=1, slug="b", name="B"))
    s.commit()

with Session(engine) as s:
    ok(quota.youtube_quota_cap_for_workspace(s, 1) == 4000, "workspace cap from row")
    quota.log(s, kind="publish", status="success", channel_id=1, quota_cost=1000)
    quota.log(s, kind="publish", status="success", channel_id=2, quota_cost=500)
    s.commit()
    ok(quota.quota_spent_workspace_today(s, 1) == 1500, "workspace spend sums channels")
    ok(quota.workspace_quota_would_exceed(s, 1, 2500) is False, "2500 more fits in 4000")
    ok(quota.workspace_quota_would_exceed(s, 1, 2501) is True, "2501 more exceeds 4000")

# publish_plan / dashboard ETA used the global cap // 1600. tick() skips when
# the next upload (1600+50+50) does not fit the workspace's REMAINING units,
# so a budget of 6 and 4000 units left still showed a full day's ETAs.
from collections import Counter

from app.db import app_settings
from app.models import Topic, Video, VideoStatus
from app.routers.queue import PUBLISH_UNITS, _next_publish_eta, _publish_hold
from app.routers.videos import _pt_date, publish_plan
from app.services import publish_loop
from app.services.youtube import QUOTA_COMMENT_INSERT, QUOTA_THUMBNAIL_SET, QUOTA_UPLOAD

ok(PUBLISH_UNITS == QUOTA_UPLOAD + QUOTA_THUMBNAIL_SET + QUOTA_COMMENT_INSERT,
   "ETA unit sum is upload + thumbnail + comment (tick's pre-check)")
_tick_src = open(publish_loop.__file__, encoding="utf-8").read()
ok("QUOTA_UPLOAD + QUOTA_THUMBNAIL_SET + QUOTA_COMMENT_INSERT" in _tick_src,
   "tick() still reserves that same sum (a drifted pre-check would lie again)")


def _fresh(cap: int, spend: int = 0, budget: int = 6, n: int = 6, cap2: int = 9000):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    s = Session(eng)
    app_settings(s)
    ws = Workspace(id=1, slug="owera", name="Owera", daily_youtube_quota_cap=cap)
    ws2 = Workspace(id=2, slug="other", name="Other", daily_youtube_quota_cap=cap2)
    s.add(ws)
    s.add(ws2)
    ch = Channel(id=1, workspace_id=1, slug="a", name="A", oauth_status="connected",
                 daily_publish_budget=budget)
    sib = Channel(id=2, workspace_id=1, slug="b", name="B", oauth_status="connected",
                  daily_publish_budget=budget)
    other = Channel(id=3, workspace_id=2, slug="c", name="C", oauth_status="connected",
                    daily_publish_budget=budget)
    s.add(ch)
    s.add(sib)
    s.add(other)
    s.commit()
    topic = Topic(channel_id=ch.id, name="T", content_format="short", weight=1)
    topic_o = Topic(channel_id=other.id, name="O", content_format="short", weight=1)
    s.add(topic)
    s.add(topic_o)
    s.commit()
    s.refresh(topic)
    s.refresh(topic_o)
    vids = []
    for i in range(n):
        v = Video(channel_id=ch.id, topic_id=topic.id, subject=f"s{i}",
                  status=VideoStatus.APPROVED, craft_review="pass",
                  title="Title", video_path="/tmp/x.mp4")
        s.add(v)
        vids.append(v)
    if spend:
        quota.log(s, kind="analytics", status="success", channel_id=sib.id, quota_cost=spend)
    s.commit()
    for v in vids:
        s.refresh(v)
    s.refresh(ch)
    s.refresh(ws)
    return s, ws, ws2, ch, other, vids


def _today_count(plan) -> int:
    today = _pt_date(datetime.now(timezone.utc))
    days = Counter(_pt_date(datetime.fromisoformat(iso)) for iso in plan.values())
    return days[today]


print("\npublish-plan sizes the day from workspace units left, not the global cap")

s, ws, ws2, ch, other, vids = _fresh(4000, spend=0, budget=6, n=6)
plan = publish_plan(ch.id, s, ws)
ok(len(plan) == 6, "budget 6 still schedules every approved video")
ok(_today_count(plan) == 4000 // PUBLISH_UNITS,
   "4000 units left and budget 6: only the uploads that fit land today (not six)")
ok(_today_count(plan) == 2, "4000 // 1700 is 2 (4000 // 1600 is also 2; see 5000 and 3400)")
earliest = min(datetime.fromisoformat(iso) for iso in plan.values())
ok((earliest - datetime.now(timezone.utc)).total_seconds() < 120,
   "the first ETA is now (tick will take a slot that fits)")

s, ws, _, ch, _, vids = _fresh(5000, spend=0, budget=6, n=6)
plan = publish_plan(ch.id, s, ws)
ok(_today_count(plan) == 2,
   "5000 left schedules 2 today (cap // 1600 would schedule 3)")
ok(max(Counter(_pt_date(datetime.fromisoformat(iso)) for iso in plan.values()).values()) == 2,
   "a later quota day is also capped at 2 (the gate is not today-only)")

s, ws, _, ch, _, _ = _fresh(3400, spend=0, budget=6, n=4)
plan = publish_plan(ch.id, s, ws)
ok(_today_count(plan) == 2,
   "3400 left schedules 2 today (1750-unit playlist sum would schedule 1)")

s, ws, _, ch, _, _ = _fresh(9000, spend=5000, budget=6, n=6)
plan = publish_plan(ch.id, s, ws)
ok(quota.quota_spent_workspace_today(s, 1) == 5000, "precondition: 4000 units left on a 9000 cap")
ok(_today_count(plan) == 2,
   "4000 units LEFT on the global cap schedules 2 today (cap // 1700 would still schedule 5)")

s, ws, ws2, ch, other, _ = _fresh(5000, spend=0, budget=6, n=6)
topic_o = s.exec(select(Topic).where(Topic.channel_id == other.id)).one()
ov = []
for i in range(4):
    v = Video(channel_id=other.id, topic_id=topic_o.id, subject=f"o{i}",
              status=VideoStatus.APPROVED, craft_review="pass",
              title="Title", video_path="/tmp/x.mp4")
    s.add(v)
    ov.append(v)
s.commit()
for v in ov:
    s.refresh(v)
plan_tight = publish_plan(ch.id, s, ws)
plan_wide = publish_plan(other.id, s, ws2)
ok(_today_count(plan_tight) == 2, "tight workspace stays at 2 today")
ok(_today_count(plan_wide) == 4, "sibling workspace on the 9000 cap still schedules all 4 today")

s, ws, _, ch, _, vids = _fresh(9000, spend=0, budget=6, n=4)
plan = publish_plan(ch.id, s, ws)
ok(_today_count(plan) == 4 and len(plan) == 4,
   "default 9000 cap with nothing spent still schedules a short bench today")

s, ws, _, ch, _, _ = _fresh(9000, spend=0, budget=0, n=2)
ok(publish_plan(ch.id, s, ws) == {}, "budget 0 is still an empty plan")
ok(_publish_hold(ch, 1, s) == "budget", "budget 0 hold stays budget, not quota")
ok(_next_publish_eta(s, ch, app_settings(s)) is None, "budget 0 dashboard ETA stays None")

s, ws, _, ch, _, vids = _fresh(1600, spend=0, budget=6, n=2)
ok(publish_plan(ch.id, s, ws) == {},
   "cap below one upload (1600 < 1700) has no ETA (// 1600 would still promise one)")
ok(_next_publish_eta(s, ch, app_settings(s)) is None, "dashboard ETA is None when one upload cannot fit")
ok(_publish_hold(ch, 1, s) == "quota", "dashboard hold is quota, not budget 0")
ok(_publish_hold(ch, 1) is None,
   "hold without a session keeps the global ceiling (budget 6 is not quota)")

s, ws, _, ch, _, vids = _fresh(4000, spend=2500, budget=6, n=1)
cfg = app_settings(s)
eta = datetime.fromisoformat(_next_publish_eta(s, ch, cfg))
reset = quota._next_pt_midnight_utc(datetime.now(timezone.utc))
ok(eta == reset,
   "1500 units left: dashboard ETA is the next Pacific midnight (not now, not UTC midnight)")
plan = publish_plan(ch.id, s, ws)
ok(_today_count(plan) == 0, "1500 units left: publish-plan puts nothing on today")
ok(_pt_date(datetime.fromisoformat(plan[str(vids[0].id)])) == _pt_date(reset),
   "that video's ETA is the next quota day")

print(f"\nALL {_checks} CHECKS PASSED")
