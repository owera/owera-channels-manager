"""Dependency-free regression checks for the storyboard composition path.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_storyboard.py

Covers: storyboard parse/validate/align (word-sync + graceful degradation), the
brand-accent single source, and the post-render blank-frame detector (synthetic
ffmpeg clips). Exits non-zero on the first failed assertion.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from app.services.engines import storyboard, theme, worker
from app.services.thumbnail import _THUMB_PALETTE

PHASE_A = ["hook", "statement", "stat", "compare", "list", "term_define", "quote", "cta"]
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# --- parse -------------------------------------------------------------------
print("parse_storyboard")
good = ('{"beats":[{"type":"hook","cue":"a b","text":"Hi there"},'
        '{"type":"stat","cue":"c d","value":"42","label":"ok"},'
        '{"type":"list","cue":"e f","items":["one","two"]},'
        '{"type":"cta","cue":"g h","text":"Sub"}]}')
beats = storyboard.parse_storyboard(good, PHASE_A)
ok(beats and [b["type"] for b in beats] == ["hook", "stat", "list", "cta"], "parses a valid storyboard")
ok(storyboard.parse_storyboard("not json", PHASE_A) is None, "rejects non-JSON")
ok(storyboard.parse_storyboard('{"beats":[]}', PHASE_A) is None, "rejects empty beats")
ok(storyboard.parse_storyboard('{"beats":[{"type":"hook","text":"x"}]}', PHASE_A) is None,
   "rejects too-few beats (<4)")
# out-of-allowlist type downgrades to statement (code not in PHASE_A)
downgrade = ('{"beats":[{"type":"hook","cue":"a","text":"Hi"},'
             '{"type":"code","cue":"b","lines":["x=1"]},'
             '{"type":"stat","cue":"c","value":"9"},{"type":"cta","cue":"d","text":"Go"}]}')
db = storyboard.parse_storyboard(downgrade, PHASE_A)
ok(db and db[1]["type"] == "statement", "downgrades out-of-allowlist type to statement")
# code IS accepted when allowed
db2 = storyboard.parse_storyboard(downgrade, PHASE_A + ["code"])
ok(db2 and db2[1]["type"] == "code", "accepts code when allowlisted")

# --- align: word-sync --------------------------------------------------------
print("align_storyboard (word-sync)")
words = [{"text": w, "start": i * 0.5, "dur": 0.5}
         for i, w in enumerate("alpha bravo charlie delta echo foxtrot golf hotel".split())]
b2 = [{"type": "hook", "cue": "alpha bravo", "text": "A"},
      {"type": "stat", "cue": "charlie delta", "value": "1"},
      {"type": "statement", "cue": "echo foxtrot", "text": "B"},
      {"type": "cta", "cue": "golf hotel", "text": "C"}]
storyboard.align_storyboard(b2, words, 4.0)
ok(abs(b2[0]["start"] - 0.0) < 1e-6, "beat 0 lands on 'alpha' (0.0s)")
ok(abs(b2[1]["start"] - 1.0) < 1e-6, "beat 1 lands on 'charlie' (1.0s)")
ok(abs(b2[2]["start"] - 2.0) < 1e-6, "beat 2 lands on 'echo' (2.0s)")
starts = [b["start"] for b in b2]
ok(starts == sorted(starts), "starts are monotonic")
ok(storyboard.validate_storyboard(b2, 4.0), "word-synced storyboard validates")

# --- align: graceful degradation --------------------------------------------
print("align_storyboard (degradation)")
b3 = [dict(b) for b in b2]
storyboard.align_storyboard(b3, [], 4.0)         # no word timings -> even spacing
ok(abs(b3[0]["start"] - 0.0) < 1e-6 and abs(b3[1]["start"] - 1.0) < 1e-6,
   "words=[] degrades to even spacing")
b4 = [{"type": "hook", "cue": "nomatch zzz", "text": "A"},
      {"type": "stat", "cue": "qqq www", "value": "1"},
      {"type": "statement", "cue": "eee rrr", "text": "B"},
      {"type": "cta", "cue": "ttt yyy", "text": "C"}]
storyboard.align_storyboard(b4, words, 4.0)       # cues never match -> even spacing
ok(storyboard.validate_storyboard(b4, 4.0), "no-cue-match degrades and still validates")

# --- validate ----------------------------------------------------------------
print("validate_storyboard")
overlap = [{"start": 0.0, "dur": 3.0}, {"start": 1.0, "dur": 3.0}]
ok(not storyboard.validate_storyboard(overlap, 6.0), "rejects overlapping beats")
ok(not storyboard.validate_storyboard([{"start": 0.0, "dur": 10.0}], 4.0), "rejects out-of-bounds beat")

# --- brand single source -----------------------------------------------------
print("brand accent")
for tid in (1, 3, 7, 8):
    ok(theme.resolve(tid, "x")["accent"] == _THUMB_PALETTE[tid % len(_THUMB_PALETTE)][0],
       f"in-video accent == thumbnail accent for topic_id={tid}")
ok(_THUMB_PALETTE is theme.PALETTE, "thumbnail palette IS theme.PALETTE (single source)")

# --- blank-frame detector ----------------------------------------------------
print("_has_visible_frames")
with tempfile.TemporaryDirectory() as d:
    black = Path(d) / "black.mp4"
    color = Path(d) / "color.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=320x568:d=3", str(black)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=s=320x568:d=3", str(color)], check=True)
    ok(worker._has_visible_frames(black) is False, "detects an all-black clip as blank")
    ok(worker._has_visible_frames(color) is True, "passes a clip with visible content")

print(f"\nALL {_checks} CHECKS PASSED")
