"""Render rethink phase 1: scene/v1, 4:5 aspect, per-aspect safe areas, CTA
legibility (px floor + contrast, gate check ``cta_legibility``) and the
``terminal`` scene (asciinema .cast → asciinema-agg, strict CLI allowlist,
cast sha256 in the gate snapshot).

    PYTHONPATH=. .venv/bin/python tests/verify_scene.py

The real-agg render check runs only when ``agg`` and ``ffmpeg`` are on PATH
(skipped, never faked, otherwise).
"""
import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.services import craft, scene
from app.services.engines import storyboard, theme, worker

FAILS = []
N = [0]


def ok(cond, msg):
    N[0] += 1
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


print("scene/v1 schema: existing card types round-trip unchanged")
fixture_boards = []
for f in sorted(glob.glob("tests/fixtures/**/*.json", recursive=True)):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    cc0 = d.get("creation_config") if isinstance(d, dict) else None
    if isinstance(cc0, str):
        try:
            cc0 = json.loads(cc0)
        except ValueError:
            cc0 = None
    for cand in (d, cc0):
        if isinstance(cand, dict) and isinstance(cand.get("beats"), list):
            fixture_boards.append((f, cand["beats"]))
ok(len(fixture_boards) >= 1, f"found {len(fixture_boards)} fixture boards")
for f, beats in fixture_boards:
    sc = scene.board_to_scenes(beats)
    ok(all(s["schema"] == "scene/v1" and s["type"] == "card" for s in sc),
       f"{Path(f).name}: every beat maps to a scene/v1 card")
    back = scene.scenes_to_board(sc)
    ok(json.dumps(back, sort_keys=True) == json.dumps(beats, sort_keys=True), f"{Path(f).name}: round-trip lossless")
    ok(craft.video_maker_gate(back) == craft.video_maker_gate(beats), f"{Path(f).name}: same gate verdict after round-trip")
for k in storyboard._RENDERERS:
    if k != "terminal":
        ok(k in scene.CARD_KINDS, f"renderer {k} is a scene/v1 card kind")
for t in ("animation", "media", "caption"):
    ok(t in scene.SCENE_TYPES, f"{t} declared in schema")
    ok(raises(lambda t=t: scene.validate_scene({"schema": "scene/v1", "type": t}), scene.SceneError),
       f"{t} not implemented → fails closed")
ok(raises(lambda: scene.validate_scene({"schema": "scene/v2", "type": "card", "kind": "hook"}),
          scene.SceneError), "unknown schema version fails closed")
ok(raises(lambda: scene.beat_to_scene({"type": "bogus"}), scene.SceneError),
   "unknown card kind fails closed")

print("aspects: 4:5 added, unknown fails closed")
ok(scene.resolve_aspect("4:5") == ("custom", 1080, 1350), "4:5 → 1080x1350")
ok(scene.resolve_aspect(None) == ("portrait", 1080, 1920), "missing → 9:16 default")
ok(raises(lambda: scene.resolve_aspect("4:3"), scene.UnknownAspectError), "4:3 → UnknownAspectError")
ok(worker._ASPECTS is scene.ASPECTS, "worker reads the single ASPECTS table")

print("safe areas: 9:16 split box identical, other aspects own table")
for (w, h) in ((1080, 1920), (720, 1280)):
    b = craft.split_box(w, h)
    old = {"top": h * 0.09, "height": h * 0.72 - h * 0.09, "left": w * 0.08,
           "width": w * (1 - 0.08 - 0.11)}
    ok(all(abs(b[k] - old[k]) < 1e-9 for k in old), f"9:16 {w}x{h} split box unchanged")
ok(craft.split_box(1080, 1350)["height"] > craft.split_box(1080, 1350, None)["height"] - 1e-9,
   "4:5 box computed")
b45 = craft.split_box(1080, 1350)
ok(abs(b45["top"] - 1350 * 0.08) < 1e-9 and abs(b45["width"] - 1080 * 0.84) < 1e-9,
   "4:5 uses its own fractions")
ov = {"top": 0.1, "bottom": 0.8, "left": 0.05, "right": 0.05}
ok(abs(craft.split_box(1080, 1920, ov)["top"] - 192) < 1e-9, "per-profile safe_area override")
ok(raises(lambda: scene.split_box_fracs(1080, 1920, {"top": 0.9, "bottom": 0.1,
                                                     "left": 0, "right": 0}), scene.SceneError),
   "invalid override fails closed")
ok(scene.safe_area("9:16")["right"] == 0.11, "YouTube Shorts safe area right 11%")
ok(scene.safe_area("4:5")["left"] == 0.06, "LinkedIn 4:5 safe area 6%")
ok(raises(lambda: scene.safe_area("4:5", network="tiktok"), scene.SceneError),
   "unknown network/aspect pair fails closed")

print("CTA legibility: floors + contrast in renderer and gate")
ok(abs(scene.contrast("#ffffff", "#000000") - 21) < 1e-6, "white/black = 21:1")
ok(scene.contrast("#6b6b6b", "#000000") < 4.5, "OS/RR fg_dim on black < 4.5:1")
for brand in ("os", "rr", None):
    th = theme.resolve(1, "subject", brand=brand)
    for a, (_r, w, h) in scene.ASPECTS.items():
        css = storyboard._base_css(w, h, th)
        sz = scene.cta_sizes(w, h, max(32, int(w * .065)), max(28, int(w * .045)))
        fl = scene.CTA_MIN_PX[a]
        ok(sz["main"] >= fl["main"] and sz["sub"] >= fl["sub"], f"{brand}/{a}: sizes ≥ floors")
        ok(f'font-size:{sz["main"]}px' in css and f'font-size:{sz["sub"]}px' in css,
           f"{brand}/{a}: CSS carries the floored px")
        ok(".cta .cta-micro{font-size:" in css and "cta-micro{font-size:" + str(sz["sub"]) + "px;color:var(--fg-dim)" not in css,
           f"{brand}/{a}: no var(--fg-dim) on cta-micro")
        m = scene.cta_legibility_marker([{"type": "cta", "text": "x"}], th, w, h)
        ok(scene.cta_legibility_hits(m) == [], f"{brand}/{a}: marker passes")
ok(scene.cta_legibility_marker([{"type": "hook", "text": "x"}], theme.resolve(1, "s"), 1080, 1920) is None,
   "no CTA → no marker")
bad = {"v": 1, "aspect": "9:16", "main_px": 50, "sub_px": 38, "main_contrast": 21, "sub_contrast": 3.9}
hits = scene.cta_legibility_hits(bad)
ok(len(hits) == 3 and all(h["check"] == "cta_legibility" for h in hits), "under floor / low contrast → hits")
ok(scene.cta_legibility_hits({"aspect": "4:3"}) != [], "unknown aspect marker fails closed")
ok(scene.cta_legibility_hits("junk") != [], "malformed marker fails closed")
board = [{"type": "statement", "text": "a", "start": 0, "dur": 2, "cue": "a"},
         {"type": "cta", "text": "Follow", "start": 2, "dur": 2, "cue": "b"}]
g = craft.video_maker_gate(board, cta_legibility=bad)
ok(g["result"] == "FAIL" and any("cta_legibility" in r for r in g["reasons"])
   and any(k["check"] == "cta_legibility" for k in g.get("kinds") or []),
   "gate: cta_legibility FAIL surfaces as a named check")
g0 = craft.video_maker_gate(board)
g1 = craft.video_maker_gate(board, cta_legibility=scene.cta_legibility_marker(
    board, theme.resolve(1, "s", brand="os"), 1080, 1920))
ok(g0 == g1, "gate: passing marker does not change the verdict")
cc = {"beats": board, "beat_timing": craft.BEAT_TIMING_CURRENT, "cta_legibility": bad}
ok(craft._recompute_video_maker_gate(cc, "short")["result"] == "FAIL",
   "publish-time recompute reads the stored marker")

print("terminal scene: .cast strict literal + sha256 in gate snapshot")
td = Path(tempfile.mkdtemp())
cast = td / "demo.cast"
hdr = {"version": 2, "width": 60, "height": 10}
ev = [[0.1, "o", "$ "], [0.5, "o", "npx owera init --yes\r\n"],
      [1.0, "o", "\x1b[32mcreated owera.json\x1b[0m\r\n"], [1.5, "o", "$ "]]
cast.write_text(json.dumps(hdr) + "\n" + "\n".join(json.dumps(e) for e in ev) + "\n")
allow = [{"kind": "command", "text": "npx owera init --yes"},
         {"kind": "log", "text": "created owera.json"}]
tb = {"type": "terminal", "cue": "x", "cast": "demo.cast", "start": 0, "dur": 3}
ok(scene.cast_transcript(cast) == [{"command": "npx owera init --yes",
                                    "output": ["created owera.json"]}],
   "transcript strips ANSI/CR and splits by prompt")
r = scene.terminal_check(tb, allow, td)
ok(r["hits"] == [] and r["sha256"] == scene.sha256_file(cast), "allowlisted cast passes, sha recorded")
r2 = scene.terminal_check(tb, [{"kind": "command", "text": "npx owera init"}], td)
ok(any(h["check"] == "terminal_not_literal" for h in r2["hits"]), "non-byte-exact command fails")
r3 = scene.terminal_check(tb, [allow[0], {"kind": "log", "text": "created owera.json "}], td)
ok(r3["hits"] != [], "output differing by a trailing space fails (raw rules)")
ok(scene.terminal_check(tb, None, td)["hits"] != [], "no allowlist → fail closed")
ok(scene.terminal_check({**tb, "cast_sha256": "0" * 64}, allow, td)["hits"][0]["check"]
   == "terminal_cast_sha_mismatch", "pinned sha mismatch fails")
ok(scene.terminal_check({**tb, "cast": "nope.cast"}, allow, td)["hits"][0]["check"]
   == "terminal_cast_missing", "missing cast fails")
ok(scene.terminal_check({**tb, "cast": "../x.cast"}, allow, td)["hits"] != [],
   "cast path escaping cast_root fails")
tboard = [{"type": "statement", "text": "a", "start": 0, "dur": 2, "cue": "a"}, tb | {"start": 2}]
mk = scene.terminal_check_marker(tboard, allow, td)
gt = craft.video_maker_gate(tboard, terminal_check=mk)
ok(gt.get("terminal_casts") == [{"i": 1, "cast": "demo.cast", "sha256": scene.sha256_file(cast)}],
   "gate snapshot stores the cast sha256")
ok(not any("Terminal cast" in x for x in gt["reasons"]), "clean terminal → no terminal reason")
gbad = craft.video_maker_gate(tboard, terminal_check=scene.terminal_check_marker(tboard, [], td))
ok(gbad["result"] == "FAIL" and any("Terminal cast" in x for x in gbad["reasons"]),
   "un-allowlisted terminal → Gate B FAIL")
ok(craft.video_maker_gate(tboard, terminal_check="junk")["result"] == "FAIL",
   "malformed terminal marker fails closed")
ok(scene.terminal_check_marker(board, allow, td) is None, "board without terminal → no marker")

print("terminal render: agg missing fails closed; real agg when present")
with patch.object(shutil, "which", return_value=None):
    ok(raises(lambda: scene.render_terminal_clip(tb, td, td, 1, width=1080), scene.AggMissingError),
       "agg missing → AggMissingError (no fake typing)")
with patch.object(craft, "beats_from_html",
                  return_value=tboard), patch.object(shutil, "which", return_value=None):
    ok(raises(lambda: worker._prerender_terminals("x", td, {"cast_root": str(td)}, 1080),
              scene.AggMissingError), "worker prerender propagates AggMissingError")
with patch.object(craft, "beats_from_html", return_value=board):
    worker._prerender_terminals("x", td, {}, 1080)
    ok(True, "no terminal beats → prerender is a no-op (no agg needed)")
html, _tw = storyboard.render_terminal(tb | {"start": 2}, {"i": 1, "start": 2, "dur": 3,
                                                          "is_last": False})
ok('src="term_1.mp4"' in html and 'class="beat terminal"' in html, "renderer references the agg clip")
if shutil.which("agg") and shutil.which("ffmpeg"):
    out = scene.render_terminal_clip(tb, td, td, 1, width=1080)
    ok(out.exists() and out.stat().st_size > 0, f"real agg+ffmpeg render → {out.name}")
else:
    print("  skip: agg/ffmpeg not on PATH (real render not exercised)")

print(f"\n{'ALL ' + str(N[0]) + ' CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
