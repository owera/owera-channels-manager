"""Gate B preflight before render submit (CoS 2026-11-01).

Run: PYTHONPATH=. .venv/bin/python tests/verify_gate_b_preflight.py
"""
from __future__ import annotations

import json
import os
import sys

os.environ["MANAGER_TOPIC_FLAGS"] = (
    '{"vm_pass_required": [], "gate_b_preflight": [1], "literal_cards": [1]}'
)

from sqlmodel import Session, select

from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, gate_b_preflight, topic_flags
from app.services import render_loop

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("topic flag (env override for this suite)")
ok(topic_flags.has(topic_flags.GATE_B_PREFLIGHT, 1), "topic 1 carries gate_b_preflight")
ok(not topic_flags.has(topic_flags.GATE_B_PREFLIGHT, 99), "topic 99 off")

print("preflight_hits: cut / hook / card_sync")
_board = [
    {"type": "hook", "start": 0.0, "dur": 2.6, "text": "One folder broke Copilot CLI."},
    {"type": "statement", "start": 3.0, "dur": 2.0, "text": "Add a directory."},
]
_sync = {
    "version": craft.SYNC_V1,
    "notes": [{
        "i": 1, "type": "statement", "start": 3.0, "speech_start": 2.5,
        "lead": -0.5, "status": "late", "kind": "real",
    }],
}
_hits = gate_b_preflight.preflight_hits(
    _board, brand="os", topic_id=1, hook_pace=None, card_sync=_sync,
    words=[{"text": "Add", "start": 2.0, "dur": 0.2}], script="Add a directory.",
    duration=12.0, sources=None,
)
ok(any(h["area"] == "cut" for h in _hits),
   "literal_cards topic: first cut >2.5s is a cut hit")
ok(any(h["area"] == "card_sync" for h in _hits), "early card_sync note is a hit")
ok(not any(h["area"] == "hook" and "claim 9 words" in h["detail"] for h in _hits),
   "short claim on hook not flagged")

_pace = {"version": craft.HOOK_PACE_V1, "claim_words": 9,
         "claim_spoken_end": 3.4, "claim_match": "exact"}
_rr = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Nine word hook claim here now x."},
    {"type": "statement", "start": 2.6, "dur": 2.0, "text": "Body."},
]
_hrr = gate_b_preflight.preflight_hits(
    _rr, brand="rr", topic_id=1, hook_pace=_pace,
    card_sync={"version": craft.SYNC_V1, "notes": []},
    words=[], script="", duration=10.0, sources=None,
)
ok(any(h["area"] == "hook" for h in _hrr), "RR hook_pace claim spoken / words → hook hit")

print("format_reason + stamp")
reason = gate_b_preflight.format_reason([{"area": "card_sync", "detail": "late card"}])
ok(reason.startswith(gate_b_preflight.PREFIX), "reason is prefixed")
cc = json.loads(gate_b_preflight.stamp_creation_config("{}", hits=[{"area": "cut", "detail": "x"}]))
ok(cc["gate_b_preflight"]["result"] == "FAIL", "creation_config records FAIL")

print("render_loop: preflight FAIL → FAILED, skips submit (no budget burn)")
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

def fresh_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-test"), name=kw.pop("name", "Test"),
                 oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED),
                 daily_render_budget=kw.pop("daily_render_budget", 5), **kw)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_topic(session, channel, **kw):
    t = Topic(channel_id=channel.id, name=kw.pop("name", "Topic"),
              theme_prompt="x", **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def make_video(session, channel, topic, **kw):
    v = Video(channel_id=channel.id, topic_id=topic.id,
              subject=kw.pop("subject", "Test subject"),
              status=kw.pop("status", VideoStatus.DRAFT), **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def set_concurrency(session, n):
    from app.db import app_settings
    cfg = app_settings(session)
    cfg.render_concurrency = n
    session.add(cfg)
    session.commit()


class FakeEngine:
    def submit(self, video, params):
        return "task-preflight"


render_loop.get_engine = lambda name: FakeEngine()
_real_eval = gate_b_preflight.evaluate


def _fail_eval(**kw):
    return [{"area": "card_sync", "detail": "card 2 late"}]


gate_b_preflight.evaluate = _fail_eval
s = fresh_session()
set_concurrency(s, 1)
ch = make_channel(s, slug="ch1", name="Owera Software", daily_render_budget=1)
t = make_topic(s, ch, content_format="short")
v = make_video(s, ch, t, status=VideoStatus.QUEUED)
from app.services import craft as _craft

cc = {"script_source": _craft.SCRIPT_SOURCE_PROVIDED}
v.creation_config = json.dumps(cc)
v.script = "One folder broke Copilot CLI. Add a directory with the add-dir flag."
v.title = "One folder broke Copilot CLI."
s.add(v)
s.commit()
render_loop._submit_new(s)
s.commit()
s.refresh(v)
ok(v.status == VideoStatus.FAILED and not v.held,
   "preflight FAIL → FAILED, not held (no RENDERING)")
ok(v.error and v.error.startswith(gate_b_preflight.PREFIX), "error carries preflight reason")
ok(not s.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all(),
   "no render JobRun / slot consumed")

v_held = make_video(s, ch, t, status=VideoStatus.QUEUED, held=True)
v_held.creation_config = json.dumps(cc)
v_held.script = v.script
v_held.title = v.title
s.add(v_held)
s.commit()
gate_b_preflight.evaluate = _fail_eval
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v_held.id).status == VideoStatus.QUEUED,
   "operator held QUEUED still skipped by submit (#153)")

gate_b_preflight.evaluate = lambda **kw: None
v2 = make_video(s, ch, t, status=VideoStatus.QUEUED)
v2.creation_config = json.dumps(cc)
v2.script = v.script
v2.title = v.title
s.add(v2)
s.commit()
render_loop._submit_new(s)
s.commit()
ok(s.get(Video, v2.id).status == VideoStatus.RENDERING,
   "preflight PASS → submit proceeds as today")

gate_b_preflight.evaluate = _real_eval

print(f"\nverify_gate_b_preflight: {_checks} checks passed")
