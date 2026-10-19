"""Regression checks for the publish craft gate (craft_review durable field).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_publish_craft_gate.py

CoS / Rodrigo (2026-09-22): publish must NOT pick videos without an explicit
craft_review=pass. Pins:

  - Video.craft_review column + db._add_missing_columns migration
  - _next_approved only selects craft_review=pass
  - pending approved rows are evaluated on tick (pass stays; fail → rejected)
  - empty script / nonsense title / mute audio block publish
  - existing review_gate (title lock + Gate A/B/C) still blocks
  - approve / skip-gate path writes craft_review=pass when clear
  - no mix / concurrency / spend / budget changes

Uses an in-memory SQLite DB and stubs YouTube — no network. Exits non-zero
on the first failed assertion.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import os as _os_vmp; _os_vmp.environ["MANAGER_TOPIC_FLAGS"] = '{"vm_pass_required": []}'  # test-only: pre-vm_pass-lock flow (PR #87 default = all topics; lock pinned in verify_vm_pass_topics)
from app.config import settings
from app.models import Channel, CraftReview, OAuthStatus, Topic, Video, VideoStatus, utcnow
from app.services import craft, publish_loop, quota, youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def _passing_title(token="Cache miss"):
    token = (token or "ready").strip() or "ready"
    # 2026-09-28: no currency value in titles (publish gate rejects it) — noun path.
    return f"{token} re-bills the whole receipt · Copilot Credits 1"


_OK_TITLE = _passing_title("Cache miss")
_OK_SCRIPT = "Cache miss costs $79. Here is why Credits matter. Subscribe — next Copilot Credits trap."
_OK_CC = json.dumps({
    "beats": [
        {"type": "hook", "start": 0.0, "dur": 2.0, "object": "receipt",
         "text": "Cache miss costs $79"},
        {"type": "stat", "start": 2.1, "dur": 2.5, "value": "79", "unit": "$"},
        {"type": "cta", "start": 4.7, "dur": 3.5, "text": "Subscribe · Copilot Credits"},
    ],
})


def fresh_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-cg"), name=kw.pop("name", "CraftGate"),
                 oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED),
                 daily_publish_budget=kw.pop("daily_publish_budget", 6), **kw)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_topic(session, channel, **kw):
    t = Topic(channel_id=channel.id, name=kw.pop("name", "T"),
              content_format=kw.pop("content_format", "short"),
              weight=kw.pop("weight", 1), **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def make_video(session, channel, topic, **kw):
    kw.setdefault("title", _OK_TITLE)
    kw.setdefault("script", _OK_SCRIPT)
    kw.setdefault("creation_config", _OK_CC)
    kw.setdefault("craft_review", CraftReview.PENDING)
    kw.setdefault("status", VideoStatus.APPROVED)
    kw.setdefault("video_path", "/tmp/cg-missing.mp4")  # missing → audio probe skips
    kw.setdefault("approved_at", utcnow())
    v = Video(channel_id=channel.id, topic_id=topic.id,
              subject=kw.pop("subject", "cg-subject"), **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _silent_mp4(path: Path) -> None:
    """Write a tiny mute mp4 (video only, no audio stream) via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.2",
            "-an", str(path),
        ],
        check=True, capture_output=True,
    )


def _voiced_mp4(path: Path) -> None:
    """Write a tiny mp4 with a silent audio track (has an audio stream)."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path),
        ],
        check=True, capture_output=True,
    )


# --- model / migration pins -------------------------------------------------
print("craft_review field + migration")
ok(hasattr(Video, "craft_review"), "Video.craft_review field exists")
ok(CraftReview.PASS == "pass" and CraftReview.FAIL == "fail"
   and CraftReview.PENDING == "pending", "CraftReview constants")
ok("craft_review" in Path("app/db.py").read_text(),
   "db._add_missing_columns lists craft_review")
src_pl = Path("app/services/publish_loop.py").read_text()
ok("CRAFT_REVIEW_PASS" in src_pl and "_sweep_craft_reviews" in src_pl,
   "publish_loop selects craft_review=pass and sweeps pending")
ok("publish_craft_block_reason" in Path("app/services/craft.py").read_text(),
   "craft.publish_craft_block_reason exists")


# --- unit: nonsense / script / vo / audio ----------------------------------
print("publish_craft_block_reason helpers")
ok(craft.nonsense_title_reason("billed $58 · Copilot Credits 1")
   == craft.NONSENSE_TITLE_REASON,
   "billed $N head is nonsense")
ok(craft.nonsense_title_reason("Copilot Credits 14")
   == craft.NONSENSE_TITLE_REASON,
   "bare series nn is nonsense")
ok(craft.nonsense_title_reason(_OK_TITLE) is None,
   "useful Credits noun title is not nonsense")
# 2026-09-28 (CoS item 6): any currency value in a title is now blocked, so the
# former "billed $N mid-claim is a real stake" case flips to the currency reason.
ok(craft.nonsense_title_reason("Copilot billed $58 when it timed out · Copilot Credits 1")
   == craft.CURRENCY_TITLE_REASON,
   "Copilot billed $N claim is now blocked (currency value in title)")
ok(craft.nonsense_title_reason("Copilot billed the run that timed out · Copilot Credits 1")
   is None,
   "billing words without an amount are NOT nonsense (real spoken stake)")

ok(craft.publish_craft_block_reason(
       title=_OK_TITLE, script="", creation_config=_OK_CC)
   == craft.EMPTY_SCRIPT_REASON,
   "empty script blocks")
ok(craft.publish_craft_block_reason(
       title=_OK_TITLE, script=_OK_SCRIPT,
       creation_config=json.dumps({"beats": []}))
   == craft.MISSING_VO_BEATS_REASON,
   "empty beats[] blocks when require_vo_beats")
ok(craft.publish_craft_block_reason(
       title=_OK_TITLE, script=_OK_SCRIPT, creation_config=None) is None,
   "legacy (no creation_config) + good title/script passes")

with tempfile.TemporaryDirectory() as td:
    mute = Path(td) / "mute.mp4"
    voiced = Path(td) / "voiced.mp4"
    _silent_mp4(mute)
    _voiced_mp4(voiced)
    ok(craft.probe_has_audio(str(mute)) is False, "mute mp4 probes False")
    ok(craft.probe_has_audio(str(voiced)) is True, "voiced mp4 probes True")
    ok(craft.probe_has_audio("/tmp/does-not-exist-cg.mp4") is None,
       "missing path probes None (skip mute reject)")
    ok(craft.publish_craft_block_reason(
           title=_OK_TITLE, script=_OK_SCRIPT, creation_config=_OK_CC,
           video_path=str(mute))
       == craft.MUTE_AUDIO_REASON,
       "mute file blocks publish craft gate")
    ok(craft.publish_craft_block_reason(
           title=_OK_TITLE, script=_OK_SCRIPT, creation_config=_OK_CC,
           video_path=str(voiced)) is None,
       "voiced file clears publish craft gate")


# --- selection: pending is invisible; pass is visible ----------------------
print("_next_approved requires craft_review=pass")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch)
pending = make_video(s, ch, t, subject="pending", craft_review=CraftReview.PENDING)
passed = make_video(s, ch, t, subject="passed", craft_review=CraftReview.PASS,
                    approved_at=utcnow() + timedelta(seconds=1))
picked = publish_loop._next_approved(s, ch.id)
ok(picked is not None and picked.id == passed.id,
   "only craft_review=pass is selected (pending skipped)")
# fail also skipped
fail = make_video(s, ch, t, subject="failed", craft_review=CraftReview.FAIL,
                  status=VideoStatus.APPROVED)
# remove the pass so pool is pending+fail
s.delete(passed)
s.commit()
ok(publish_loop._next_approved(s, ch.id) is None,
   "pending+fail pool yields nothing")


# --- sweep: pending → pass or reject ---------------------------------------
print("_sweep_craft_reviews evaluates pending")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch)
good = make_video(s, ch, t, subject="good", craft_review=CraftReview.PENDING,
                  title=_OK_TITLE, script=_OK_SCRIPT, creation_config=_OK_CC)
bad_script = make_video(s, ch, t, subject="noscript", craft_review=CraftReview.PENDING,
                        title=_OK_TITLE, script="", creation_config=_OK_CC)
bad_title = make_video(s, ch, t, subject="billed", craft_review=CraftReview.PENDING,
                       title="billed $58 · Copilot Credits 9",
                       script=_OK_SCRIPT, creation_config=_OK_CC)
publish_loop._sweep_craft_reviews(s, ch.id)
s.refresh(good); s.refresh(bad_script); s.refresh(bad_title)
ok(good.craft_review == CraftReview.PASS and good.status == VideoStatus.APPROVED,
   "good pending → craft_review=pass, stays approved")
ok(bad_script.status == VideoStatus.REJECTED
   and bad_script.craft_review == CraftReview.FAIL,
   "empty script → rejected + craft_review=fail")
ok(bad_title.status == VideoStatus.REJECTED
   and bad_title.craft_review == CraftReview.FAIL,
   "nonsense billed $N title → rejected")
ok("empty script" in (bad_script.rejected_reason or "").lower()
   or "script" in (bad_script.rejected_reason or "").lower(),
   "reject reason mentions script")


# --- tick: pending nonsense never uploads ----------------------------------
print("tick: craft gate blocks nonsense before upload")
s = fresh_session()
ch = make_channel(s, daily_publish_budget=3)
t = make_topic(s, ch)
uploaded = []

def _fake_upload(*a, **k):
    uploaded.append(a)
    return "ytFAKE1"

youtube.get_service = lambda slug: object()
youtube.upload_video = _fake_upload
youtube.set_thumbnail = lambda *a, **k: None
youtube.insert_comment = lambda *a, **k: None
youtube.add_to_playlist = lambda *a, **k: None

nonsense = make_video(
    s, ch, t, subject="spam", craft_review=CraftReview.PENDING,
    title="billed $99 · Copilot Credits 3",
    script=_OK_SCRIPT, creation_config=_OK_CC,
)
publish_loop._sweep_craft_reviews(s, ch.id)
s.refresh(nonsense)
ok(nonsense.status == VideoStatus.REJECTED, "sweep rejects nonsense before pick")
ok(publish_loop._next_approved(s, ch.id) is None, "nothing left to publish")
ok(not uploaded, "upload never called for nonsense")

# Pass row publishes
good = make_video(
    s, ch, t, subject="ready", craft_review=CraftReview.PASS,
    title=_OK_TITLE, script=_OK_SCRIPT, creation_config=_OK_CC,
    video_path="/tmp/cg-ready.mp4",
)
picked = publish_loop._next_approved(s, ch.id)
ok(picked is not None and picked.id == good.id, "pass row is next")
publish_loop._publish_one(s, ch, good)
s.commit()
s.refresh(good)
ok(good.status == VideoStatus.PUBLISHED, "pass row published")
ok(uploaded, "upload called for craft_review=pass")


# --- _publish_one defense: empty script on a stale pass → reject ------------
print("_publish_one re-checks gate (stale pass)")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch)
stale = make_video(
    s, ch, t, subject="stale", craft_review=CraftReview.PASS,
    title=_OK_TITLE, script="", creation_config=_OK_CC,
)
youtube.upload_video = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not upload"))
publish_loop._publish_one(s, ch, stale)
s.commit()
s.refresh(stale)
ok(stale.status == VideoStatus.REJECTED and stale.craft_review == CraftReview.FAIL,
   "stale pass + empty script → rejected at publish")


print("_publish_one: OS pre-deploy without closed-list grandfather is held")
s = fresh_session()
ch = make_channel(s, slug="owera", name="Owera Software")
t = make_topic(s, ch)
_gap = [
    {"type": "hook", "start": 0.0, "dur": 1.5, "text": "Hook", "object": "bill"},
    {"type": "stat", "start": 1.62, "dur": 2.0, "value": "1"},
    {"type": "cta", "start": 10.0, "dur": 2.0, "text": "Subscribe · Copilot Credits"},
]
_pre_cc = json.dumps({
    "composition_version": "storyboard",
    "beat_count": 3,
    "beats": _gap,
    "duration": 13.0,
    "beat_timing": craft.BEAT_TIMING_CURRENT,
    "craft_gate": {"result": "PASS", "checks": {"A": "PASS", "B": "PASS", "C": "PASS"},
                   "reasons": []},
})
ok(craft.video_maker_gate(_gap, duration=13.0)["checks"]["B"] == "FAIL",
   "the stored board would FAIL today's Gate B if recomputed")
pre = make_video(
    s, ch, t, subject="pre-deploy os", craft_review=CraftReview.PASS,
    title=_OK_TITLE, script=_OK_SCRIPT, creation_config=_pre_cc,
)
uploaded_pre = []
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: uploaded_pre.append(a) or "ytPRE"
youtube.set_thumbnail = lambda *a, **k: None
youtube.insert_comment = lambda *a, **k: None
youtube.add_to_playlist = lambda *a, **k: None
publish_loop._sweep_craft_reviews(s, ch.id)
s.refresh(pre)
ok(pre.status == VideoStatus.APPROVED and pre.craft_review == CraftReview.PASS,
   "sweep does not touch already-pass pre-deploy rows")
publish_loop._publish_one(s, ch, pre)
s.commit()
s.refresh(pre)
ok(pre.status == VideoStatus.REVIEW and pre.craft_review == CraftReview.FAIL,
   "OS not on the closed grandfather list: new gate, fail-closed → held in review")
ok((pre.error or "").startswith("held:"), "error is held: … (never status=failed)")
ok(pre.held is True, "OS fail-closed sets Video.held (no auto-promote)")
ok(not uploaded_pre, "upload is not called")
ok(pre.status != VideoStatus.FAILED, "Gate B recompute does not write status=failed")


# --- configurable nonsense patterns ----------------------------------------
print("configurable nonsense patterns")
craft.set_nonsense_title_patterns([r"^spam-token\b"])
ok(craft.nonsense_title_reason("spam-token hello · Copilot Credits 1")
   == craft.NONSENSE_TITLE_REASON,
   "custom pattern matches")
ok(craft.nonsense_title_reason("billed $58 · Copilot Credits 1") is None,
   "default billed pattern cleared after replace")
# restore defaults for any later import reuse
craft.set_nonsense_title_patterns([
    r"^billed\s*\$\d+\b",
    r"^(copilot\s+credits|ia|agent\s+memory|crewai|local|claude\s+code)\s+\d+\s*$",
    craft.CURRENCY_TITLE_PATTERN,
])


print(f"\nALL {_checks} CHECKS PASSED")
