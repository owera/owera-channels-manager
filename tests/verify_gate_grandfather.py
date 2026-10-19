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
        ok(craft.trust_stored_gate(
            {"composition_version": "storyboard", "beat_count": 1,
             "brand": "os", **vp, "craft_gate": {"result": "PASS"}},
            video_id=1452, brand="os",
            gate_context=craft.GATE_CONTEXT_PUBLISH),
           "publish + listed + vm_pass on the snapshot → stored gate")
        ok(not craft.trust_stored_gate(
            {"composition_version": "storyboard", "beat_count": 1,
             "brand": "os", **vp},
            video_id=1452, brand="os",
            gate_context=craft.GATE_CONTEXT_APPROVE),
           "OS approve never uses the stored gate")
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
ok((REPO / "docs" / "deploy-os-cards.md").is_file(),
   "deploy/rollback doc is committed under docs/")
ok((REPO / "app" / "data" / "gate_grandfather.json").is_file(),
   "closed list file exists")


print(f"\nverify_gate_grandfather: {_checks}/{_checks} passed")
