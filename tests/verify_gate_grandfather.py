"""Closed-list OS grandfather + held / RR scope / vm_pass.

Run: PYTHONPATH=. .venv/bin/python tests/verify_gate_grandfather.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

from app.services import craft, gate_grandfather, publish_loop, render_loop
from app.services.engines import worker
from app.routers import videos as videos_router

_checks = 0
REPO = Path(__file__).resolve().parent.parent


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("closed list (expected empty) + #1328 excluded")
ok(gate_grandfather.ALWAYS_EXCLUDE == frozenset({1328}), "#1328 always excluded")
ok(gate_grandfather.grandfather_ids() == set(),
   "committed list is empty (expected grandfather = [])")
ok(not gate_grandfather.is_grandfathered(1328), "#1328 is not grandfathered")
ok(not gate_grandfather.is_grandfathered(1452), "1452 not on the list")
ok(not craft.trust_stored_gate(
        {"composition_version": "storyboard", "beat_count": 1,
         "craft_gate": {"result": "PASS"}},
        video_id=1452, brand="os"),
   "OS missing marker is not the old gate")

with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "gf.json"
    p.write_text(json.dumps({"ids": [1452, 1328], "excluded": [1328]}))
    os.environ["GATE_GRANDFATHER_PATH"] = str(p)
    try:
        ok(1328 not in gate_grandfather.grandfather_ids(),
           "list loader drops #1328 even if pasted")
        ok(1452 in gate_grandfather.grandfather_ids(), "1452 on the temp list")
        ok(not gate_grandfather.is_grandfathered(
            1452, creation_config={"craft_gate": {"result": "PASS"}}),
           "listed id without vm_pass is not grandfathered")
        vp = {"vm_pass": {"result": "PASS", "video_path": "/x/video.mp4"}}
        ok(gate_grandfather.is_grandfathered(
            1452, creation_config=vp, video_path="/x/video.mp4"),
           "listed id + vm_pass matching path is grandfathered")
        ok(not gate_grandfather.is_grandfathered(
            1452, creation_config=vp, video_path="/x/other.mp4"),
           "listed id + vm_pass for another master is excluded")
        from types import SimpleNamespace
        # Video.creation_config is a JSON string; vm_pass_of/_cc parse that.
        listed = SimpleNamespace(id=1452, video_path="/x/video.mp4",
                                 creation_config=json.dumps({**vp}))
        ok(craft.trust_stored_gate(
            {"composition_version": "storyboard", "beat_count": 1,
             "brand": "os", **vp, "craft_gate": {"result": "PASS"}},
            video_id=1452, video=listed, brand="os",
            gate_context=craft.GATE_CONTEXT_PUBLISH),
           "publish + listed + vm_pass on the snapshot → stored gate")
        ok(not craft.trust_stored_gate(
            {"composition_version": "storyboard", "beat_count": 1,
             "brand": "os", **vp},
            video_id=1452, brand="os",
            gate_context=craft.GATE_CONTEXT_APPROVE),
           "OS approve never uses the stored gate")
        ok(not gate_grandfather.is_grandfathered(1452, creation_config=vp),
           "listed id + vm_pass without current video_path is not grandfathered")
        ok(not gate_grandfather.is_grandfathered(
            1328, creation_config=vp, video_path="/x/video.mp4"),
           "#1328 excluded even with a matching vm_pass")
    finally:
        os.environ.pop("GATE_GRANDFATHER_PATH", None)


print("RR pre-deploy unchanged; new OS checks do not apply")
rr_quote = [
    {"type": "hook", "start": 0.0, "dur": 1.5, "text": "Hook", "object": "bill"},
    {"type": "quote", "start": 1.62, "dur": 3.40,
     "text": "A maximum of 4 blocks with cache_control may be provided.",
     "literal_quote": True},
    {"type": "cta", "start": 6.0, "dur": 2.0, "text": "Subscribe · IA"},
]
words = []
t = 1.62
for tok in rr_quote[1]["text"].split():
    words.append({"text": tok.strip(".,"), "start": round(t, 3), "dur": 0.28})
    t += 0.28
words[-1]["start"] = 4.72
g_rr = craft.video_maker_gate(rr_quote, brand="rr", words=words,
                              beat_timing=craft.BEAT_TIMING_CURRENT)
ok(not any("mid card" in r and "beat[1]" in r for r in (g_rr.get("reasons") or [])),
   "RR quote hold exemption still applies")
ok(craft.card_timing_marker(rr_quote, None, fail_closed=False) is None,
   "RR missing words stay fail-open")
ok(not craft.description_missing_source_hits("no public source", brand="rr"),
   "RR description without URL is not description_missing_source")
ok(not any("description_missing_source" in r or "Description source" in r
           for r in (craft.video_maker_gate(
               rr_quote, brand="rr", words=words, description="no public source",
               beat_timing=craft.BEAT_TIMING_CURRENT).get("reasons") or [])),
   "RR live gate does not score description source")
rr_pre = {"composition_version": "storyboard", "beat_count": 1,
          "brand": "rr", "craft_gate": {"result": "PASS"}}
ok(craft.trust_stored_gate(rr_pre, brand="rr",
                           gate_context=craft.GATE_CONTEXT_PUBLISH)
   and craft.trust_stored_gate(rr_pre, brand="rr",
                               gate_context=craft.GATE_CONTEXT_APPROVE),
   "RR pre-deploy snapshot trusts stored gate at publish and approve")
ok(any("Blank gap" in r or "blank" in r.lower()
       for r in (craft.video_maker_gate(
           [{"type": "hook", "start": 0.0, "dur": 1.0, "text": "H",
             "object": "bill"},
            {"type": "cta", "start": 8.0, "dur": 2.0, "text": "Subscribe · IA"}],
           brand="rr", duration=12.0,
           beat_timing=craft.BEAT_TIMING_CURRENT).get("reasons") or [])),
   "blank_gap still applies to RR on a live board")
g_os = craft.video_maker_gate(rr_quote, brand="os", words=words,
                              beat_timing=craft.BEAT_TIMING_CURRENT)
ok(g_os["checks"]["B"] == "FAIL"
   and any("mid card" in r for r in (g_os.get("reasons") or [])),
   "OS quote >3.0s incl fade FAILs beat_hold")


print("held never auto-promotes")
pl_src = inspect.getsource(publish_loop)
rl_src = inspect.getsource(render_loop)
wk_src = inspect.getsource(worker)
ok("held.is_not(True)" in pl_src, "publish selection skips Video.held")
ok("status = VideoStatus.REVIEW" in pl_src and "held:" in pl_src,
   "OS publish craft FAIL parks review/held, not failed")
ok("VideoStatus.FAILED" not in inspect.getsource(videos_router.approve),
   "approve path never writes status=failed")
ok("held=False" not in rl_src and "held = False" not in rl_src,
   "render_loop never clears Video.held")
ok("held=False" not in wk_src and "held = False" not in wk_src,
   "worker never clears Video.held")
ok("held, v.held_at = False, None" in inspect.getsource(videos_router.approve),
   "held leaves only via explicit approve (actor logged)")
ok("held, v.held_at = False, None" in inspect.getsource(videos_router.unhold),
   "or via the audited unhold API")
ok("park_os_held" in inspect.getsource(videos_router.approve)
   and 'if brand == "os"' in inspect.getsource(videos_router.approve),
   "approve fail-closed parks Video.held for OS only")
ok("park_os_held" in pl_src and 'if brand == "os"' in pl_src,
   "publish OS FAIL parks Video.held; RR stays REJECTED")
ok("elif video.held and ch_brand == \"os\"" in rl_src and "skip_gate" in rl_src,
   "finalize skip_gate does not auto-approve OS Video.held")
ok('fmt != "long"' in rl_src and "stamp_description_on_config" in rl_src,
   "post-render description stamp is shorts-only")


print("persist + re-gate (script / words / description)")
from tests.os_gate_b_real_vo import snapshot_script, whisper_words
S1452 = snapshot_script(1452)
w1452 = whisper_words(1452)
board = [
    {"type": "hook", "start": 0.0, "dur": 2.3, "text": "One folder broke Copilot CLI.",
     "object": "terminal"},
    {"type": "quote", "start": 19.82, "dur": 2.44,
     "text": "and Anthropic subagents keep working with extra directories."},
    {"type": "cta", "start": 24.0, "dur": 2.0, "text": "Subscribe · Agent traps"},
]
cc = {
    "beats": board,
    "script": S1452,
    "narration_words": w1452,
    "brand": "os",
    "description": "no public source",
    "beat_timing": craft.BEAT_TIMING_CURRENT,
    "duration": 30.0,
}
reason = craft.video_maker_gate_reason(
    cc, "short", brand="os", gate_context=craft.GATE_CONTEXT_APPROVE,
    description_required=True)
ok(reason and ("sentence_too_long" in reason or "Description" in reason
               or "mid card" in reason),
   f"approve re-gate sees persisted words/description ({reason})")
empty = {"beats": board, "brand": "os", "beat_timing": craft.BEAT_TIMING_CURRENT,
         "duration": 30.0}
miss = craft.video_maker_gate_reason(
    empty, "short", brand="os", gate_context=craft.GATE_CONTEXT_APPROVE)
ok(miss and "missing_word_timings" in miss,
   f"OS approve without words fail-closed ({miss})")
stamped = json.loads(craft.stamp_description_on_config(
    cc, "no public source", brand="os"))
ok(stamped.get("description") == "no public source"
   and stamped.get("craft_gate", {}).get("result") == "FAIL"
   and any("description_missing_source" in r
           for r in (stamped.get("craft_gate") or {}).get("reasons") or []),
   "post-render stamp persists description and FAILs OS without a source URL")
ok(json.loads(craft.stamp_description_on_config(
    {"beat": 7}, "gen-desc", brand="os"))["description"] == "gen-desc",
   "stamp without beats still persists description")


print("snapshot script")
src = (REPO / "scripts" / "gate_grandfather_snapshot.py").read_text()
ok("--exclude" in src and "1328" in src and "vm_pass_matches_master" in src,
   "snapshot script supports --exclude and drops vm_pass mismatches")
ok('print(f"count: {len(kept)}")' in src or 'print(f"count:' in src,
   "snapshot prints count")
ok("APPROVED" in src and "PUBLISHING" in src,
   "snapshot selects approved + publishing (no separate scheduled status)")
ok((REPO / "docs" / "deploy-os-cards.md").is_file(),
   "deploy/rollback doc is committed under docs/")
ok((REPO / "app" / "data" / "gate_grandfather.json").is_file(),
   "closed list file exists")
doc = (REPO / "docs" / "deploy-os-cards.md").read_text()
ok("artifact_fingerprint" in doc or "size:mtime" in doc,
   "docs say vm_pass is bound to the current render file, not a git SHA")
ok("closed list" in doc.lower() and "missing" in doc.lower(),
   "docs say grandfather is a closed list, not missing-marker")
ok("expected grandfather" in doc.lower() or "expected list" in doc.lower(),
   "docs say expected grandfather is empty")
ok("#1328" in doc and "held" in doc.lower(),
   "docs say #1328 is held / excluded")
ok("review" in doc.lower() and "approve" in doc.lower(),
   "docs say OS review goes through the new gate on approve")
ok("blank_gap" in doc and ("OS+RR" in doc or "OS and RR" in doc),
   "docs say blank_gap stays OS+RR")


print("behavioral: held never auto-promotes; snapshot drops vm_pass mismatch")
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, CraftReview, OAuthStatus, Topic, Video, VideoStatus, utcnow

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
s = Session(engine)
ch = Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED)
s.add(ch)
s.commit()
s.refresh(ch)
t = Topic(channel_id=ch.id, name="Agent traps", theme_prompt="x")
s.add(t)
s.commit()
s.refresh(t)
held_v = Video(
    channel_id=ch.id, topic_id=t.id, subject="held drip",
    status=VideoStatus.APPROVED, craft_review=CraftReview.PASS,
    title="Cache miss re-bills the whole receipt · Copilot Credits 1",
    script="Cache miss costs $79. Subscribe — next Copilot Credits trap.",
    held=True, held_at=utcnow(),
    creation_config=json.dumps({"composition_version": "storyboard", "beat_count": 1,
                                "craft_gate": {"result": "PASS"}}),
)
s.add(held_v)
s.commit()
s.refresh(held_v)
ok(publish_loop._next_approved(s, ch.id) is None,
   "_next_approved skips Video.held")
uploaded = []
from app.services import youtube
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: uploaded.append(a) or "ytX"
publish_loop._publish_one(s, ch, held_v)
s.commit()
s.refresh(held_v)
ok(not uploaded, "_publish_one does not upload a held video")
ok(held_v.held is True and held_v.status == VideoStatus.REVIEW,
   "held publish attempt stays held/review, never approved/published")

rr_ch = Channel(slug="recio", name="Rodrigo Recio", oauth_status=OAuthStatus.CONNECTED)
s.add(rr_ch)
s.commit()
s.refresh(rr_ch)
rr_t = Topic(channel_id=rr_ch.id, name="IA", theme_prompt="x")
s.add(rr_t)
s.commit()
s.refresh(rr_t)
rr_held = Video(
    channel_id=rr_ch.id, topic_id=rr_t.id, subject="rr held",
    status=VideoStatus.APPROVED, craft_review=CraftReview.PASS,
    title="Cache miss re-bills the whole receipt · Copilot Credits 1",
    script="Cache miss costs $79. Subscribe — next Copilot Credits trap.",
    held=True, held_at=utcnow(),
    creation_config=json.dumps({"composition_version": "storyboard", "beat_count": 1,
                                "craft_gate": {"result": "PASS"}}),
)
s.add(rr_held)
s.commit()
s.refresh(rr_held)
uploaded_rr = []
youtube.upload_video = lambda *a, **k: uploaded_rr.append(a) or "ytRR"
publish_loop._publish_one(s, rr_ch, rr_held)
s.commit()
s.refresh(rr_held)
ok(not uploaded_rr, "RR held video is not uploaded")
ok(rr_held.held is True and rr_held.status == VideoStatus.APPROVED,
   "RR operator-hold is not park_os_held (status stays approved)")

v1328 = Video(
    id=1328, channel_id=ch.id, topic_id=t.id, subject="old master",
    status=VideoStatus.APPROVED, craft_review=CraftReview.PASS,
    title="Old master · Agent traps 1",
    script="One folder broke Copilot CLI. Subscribe — next agent trap.",
    video_path="/tmp/old-master.mp4",
    creation_config=json.dumps({
        "vm_pass": {"result": "PASS", "video_path": "/tmp/other.mp4"},
        "craft_gate": {"result": "PASS"},
    }),
)
ok(not gate_grandfather.vm_pass_matches_master(v1328),
   "#1328-style vm_pass for another path is not the current master")
park = Video(channel_id=ch.id, topic_id=t.id, subject="park",
             status=VideoStatus.APPROVED)
gate_grandfather.park_os_held(park, "Video Maker craft gate FAIL: mid card")
ok(park.held is True and park.status == VideoStatus.REVIEW
   and str(park.error).startswith("held:"),
   "park_os_held sets held + review, never failed")

# Snapshot: OS approved without current-master vm_pass is skipped.
db = Path(tempfile.mkdtemp()) / "gf.db"
eng2 = create_engine(f"sqlite:///{db}")
SQLModel.metadata.create_all(eng2)
s2 = Session(eng2)
ch2 = Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED)
s2.add(ch2)
s2.commit()
s2.refresh(ch2)
t2 = Topic(channel_id=ch2.id, name="Agent traps", theme_prompt="x")
s2.add(t2)
s2.commit()
s2.refresh(t2)
s2.add(Video(
    id=1328, channel_id=ch2.id, topic_id=t2.id, subject="1328",
    status=VideoStatus.APPROVED, craft_review=CraftReview.PASS,
    video_path="/tmp/1328.mp4",
    creation_config=json.dumps({"vm_pass": {"result": "PASS", "video_path": "/tmp/1328.mp4"}}),
))
s2.add(Video(
    id=1452, channel_id=ch2.id, topic_id=t2.id, subject="no vp",
    status=VideoStatus.APPROVED, craft_review=CraftReview.PASS,
    creation_config=json.dumps({"craft_gate": {"result": "PASS"}}),
))
s2.commit()
s2.close()
import subprocess
proc = subprocess.run(
    [sys.executable, str(REPO / "scripts" / "gate_grandfather_snapshot.py"),
     "--db", str(db), "--dry-run"],
    cwd=str(REPO), capture_output=True, text=True,
    env={**os.environ, "PYTHONPATH": str(REPO)},
)
combo = (proc.stdout or "") + (proc.stderr or "")
ok(proc.returncode == 0, f"snapshot --dry-run exits 0 ({combo})")
ok("grandfather ids (0): []" in proc.stdout and "count: 0" in proc.stdout,
   f"snapshot drops #1328 and the row without current-master vm_pass ({proc.stdout})")
exc = subprocess.run(
    [sys.executable, str(REPO / "scripts" / "gate_grandfather_snapshot.py"),
     "--db", str(db), "--dry-run", "--exclude", "1328,1452"],
    cwd=str(REPO), capture_output=True, text=True,
    env={**os.environ, "PYTHONPATH": str(REPO)},
)
ok(exc.returncode == 0 and "grandfather ids (0): []" in exc.stdout
   and "count: 0" in exc.stdout,
   f"snapshot --exclude 1328,1452 prints empty ids + count ({exc.stdout})")


print(f"\nverify_gate_grandfather: {_checks}/{_checks} passed")
