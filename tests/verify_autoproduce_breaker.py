"""Auto-produce circuit breaker: a failed render must not pull a fresh draft.

ch1 2026-10-11 21:05 → 10-12 04:15 BRT: every FAILED render freed its slot
(rendered_today counts successes only) and _auto_produce queued the next draft
into the same broken path — 7 drafts burned (4× edge-tts 403, 3× timeout).

Also pins the edge-tts endpoint override (legacy host speech.platform.bing.com
returns 403; api.msedgeservices.com accepts the same client token), and that a
provided-script hook block (one video, no slot) does not pause the channel.

Run: PYTHONPATH=. .venv/bin/python tests/verify_autoproduce_breaker.py
"""
import json
import sys
from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import os as _os; _os.environ["MANAGER_TOPIC_FLAGS"] = '{"vm_pass_required": []}'
from app.config import settings
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus, utcnow
from app.services import episode_guard as _eg
_eg.duplicate_episode_reason = lambda session, video: None
from app.services import quota, render_loop

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def setup(budget=1, drafts=3):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    s = Session(eng)
    ch = Channel(name="OS", slug="os", oauth_status=OAuthStatus.CONNECTED,
                 daily_render_budget=budget, daily_publish_budget=2)
    s.add(ch); s.commit(); s.refresh(ch)
    t = Topic(channel_id=ch.id, name="t", weight=1, active=True)
    s.add(t); s.commit(); s.refresh(t)
    ids = []
    for i in range(drafts):
        v = Video(channel_id=ch.id, topic_id=t.id, subject=f"Agent trap {i} · Agent traps {i+1}",
                  status=VideoStatus.DRAFT)
        s.add(v); s.commit(); s.refresh(v); ids.append(v.id)
    return s, ch, ids


def fail_render(s, v, err):
    v.status = VideoStatus.FAILED
    v.error = err
    s.add(v)
    quota.log(s, kind="render", status="error", video_id=v.id, channel_id=v.channel_id, detail=err)
    s.commit()


def queued(s):
    return [v.id for v in s.exec(select(Video).where(Video.status == VideoStatus.QUEUED)).all()]


print("breaker: no failure → auto_produce fills headroom as before")
s, ch, ids = setup()
render_loop._auto_produce(s); s.commit()
ok(len(queued(s)) == 1, "one draft queued with budget 1")

print("breaker: final render failure → next draft NOT consumed")
v = s.get(Video, queued(s)[0])
fail_render(s, v, "WSServerHandshakeError: 403, message='Invalid response status'")
render_loop._auto_produce(s); s.commit()
ok(queued(s) == [], "no new draft queued after a failed render")
held = s.exec(select(JobRun).where(JobRun.kind == "produce", JobRun.status == "held")).all()
ok(len(held) == 1 and "403" in held[0].detail, "trip recorded once with the cause")
for _ in range(5):
    render_loop._auto_produce(s); s.commit()
held = s.exec(select(JobRun).where(JobRun.kind == "produce", JobRun.status == "held")).all()
ok(len(held) == 1, "no JobRun spam across ticks")
drafts = s.exec(select(Video).where(Video.status == VideoStatus.DRAFT)).all()
ok(len(drafts) == 2, "remaining drafts untouched")

print("breaker: transient retry does not trip (same video, no draft consumed)")
s2, ch2, _ = setup()
quota.log(s2, kind="render", status="error", channel_id=ch2.id, detail="transient error (retry 1/2): render timed out")
s2.commit()
render_loop._auto_produce(s2); s2.commit()
ok(len(queued(s2)) == 1, "transient error alone does not block")

print("breaker: a successful render resets it")
quota.log(s, kind="render", status="success", video_id=v.id, channel_id=ch.id)
ch.daily_render_budget = 2  # the success counts in rendered_today; leave 1 slot
s.add(ch); s.commit()
render_loop._auto_produce(s); s.commit()
ok(len(queued(s)) == 1, "draft queued again after a success")

print("breaker: cool-down expiry resets it; 0 disables")
s3, ch3, _ = setup()
s3.add(JobRun(kind="render", status="error", channel_id=ch3.id, detail="render timed out",
              created_at=utcnow() - timedelta(hours=settings.auto_produce_fail_cooldown_hours + 1)))
s3.commit()
render_loop._auto_produce(s3); s3.commit()
ok(len(queued(s3)) == 1, "old failure outside the window does not block")
s4, ch4, _ = setup()
quota.log(s4, kind="render", status="error", channel_id=ch4.id, detail="render timed out"); s4.commit()
_orig = settings.auto_produce_fail_cooldown_hours
settings.auto_produce_fail_cooldown_hours = 0
render_loop._auto_produce(s4); s4.commit()
settings.auto_produce_fail_cooldown_hours = _orig
ok(len(queued(s4)) == 1, "cooldown 0 disables the breaker")

print("breaker: other channels unaffected")
s5, ch5, _ = setup()
other = Channel(name="RR", slug="rr", oauth_status=OAuthStatus.CONNECTED, daily_render_budget=1, daily_publish_budget=1)
s5.add(other); s5.commit(); s5.refresh(other)
quota.log(s5, kind="render", status="error", channel_id=other.id, detail="render timed out"); s5.commit()
render_loop._auto_produce(s5); s5.commit()
ok(len(queued(s5)) == 1, "failure on channel B does not hold channel A")

print("breaker: provided-script content block does not pause the channel")
s6, ch6, _ = setup()
prefix = render_loop._SCRIPT_BLOCK_DETAIL_PREFIX
quota.log(s6, kind="render", status="error", channel_id=ch6.id,
          detail=f"{prefix}: hook mismatch")
s6.commit()
render_loop._auto_produce(s6); s6.commit()
ok(len(queued(s6)) == 1, "provided-script block alone does not pause auto-produce")

print("breaker: a newer script-block does not hide an earlier terminal failure")
s7, ch7, _ = setup()
quota.log(s7, kind="render", status="error", channel_id=ch7.id,
          detail="WSServerHandshakeError: 403, message='Invalid response status'")
s7.commit()
quota.log(s7, kind="render", status="error", channel_id=ch7.id,
          detail=f"{prefix}: hook mismatch")
s7.commit()
render_loop._auto_produce(s7); s7.commit()
ok(len(queued(s7)) == 0, "terminal 403 still blocks when a script-block row is newer")

print("breaker: empty-detail success still resets under the script-block filter")
s8, ch8, _ = setup()
quota.log(s8, kind="render", status="error", channel_id=ch8.id, detail="render timed out")
s8.commit()
quota.log(s8, kind="render", status="success", channel_id=ch8.id, detail="")
ch8.daily_render_budget = 2  # the success counts in rendered_today; leave 1 slot
s8.add(ch8); s8.commit()
render_loop._auto_produce(s8); s8.commit()
ok(len(queued(s8)) == 1, "empty-detail success still resets the breaker")

print("breaker: the real provided-script gate logs the prefix and does not pause")
s9, ch9, ids9 = setup()
bad = s9.get(Video, ids9[0])
bad.status = VideoStatus.QUEUED
bad.title = "The cache miss costs the conversion"
bad.subject = "The cache miss costs the conversion"
bad.script = "Totally unrelated opening sentence about something else."
bad.creation_config = json.dumps({"script_source": "provided"})
s9.add(bad); s9.commit()
got = render_loop._prepare_provided_script(s9, bad, ch9, "short", None)
s9.commit()
ok(got is None, "misaligned provided script returns None (no render)")
ok(bad.status == VideoStatus.FAILED, "that video is parked FAILED")
row = s9.exec(select(JobRun).where(JobRun.video_id == bad.id, JobRun.kind == "render",
                                   JobRun.status == "error")).first()
ok(row is not None and row.detail.startswith(prefix),
   "the gate logs the breaker prefix (wording cannot drift)")
render_loop._auto_produce(s9); s9.commit()
ok(len(queued(s9)) == 1, "a sibling draft is still promoted after the script block")
ok(s9.get(Video, bad.id).status == VideoStatus.FAILED, "the blocked video stays FAILED")

print("edge-tts endpoint override")
import edge_tts.communicate as comm
from app.services.engines import worker
worker._apply_edge_tts_endpoint()
ok(comm.WSS_URL.startswith("wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1?Ocp-Apim-Subscription-Key=")
   and "{token}" not in comm.WSS_URL, "WSS_URL points at api.msedgeservices.com with the client token")

print(f"\nALL {_checks} CHECKS PASSED")
