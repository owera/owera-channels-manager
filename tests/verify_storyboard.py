"""Dependency-free regression checks for the storyboard composition path.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_storyboard.py

``storyboard`` is the typed-beat composition engine HyperFrames renders: the
LLM emits a schema-clamped JSON storyboard, cues are aligned to edge-tts
word timings, and per-type renderers emit one self-contained index.html.
A silent break here ships a statement-echo card, a CTA with no follow ask
(R7), a bunched <2s payoff (R4), or a code beat that lost its indent
(13be882). Previously only parse/align/validate smoke, palette identity,
and an all-types HTML scrape were covered (~40 checks) against a 1031-line
module.

Covers, dependency-free (no network, no HyperFrames CLI, no live LLM):
  - module contracts: beat-count / gap / floor / drift / row-step pins,
    _RENDERERS keys == _BEAT_SPECS
  - clip helpers: _words_clip / _chars_clip / _code_line_clip (07-09
    indent-preserving clip — a ``return`` under a ``def`` must stay indented)
  - _coerce_beat every type + salvage / None paths (w-clamp, highlight
    ints-only, diagram layout fallback, cta default text, 5-item list cap)
  - parse_storyboard: fenced/prose unwrap, _MAX_BEATS clamp, array-root None
  - theme.fold + _tok + _find_subseq: PT diacritics, empty needle
  - align_storyboard tail/mid floor (07-16 bunched-close incident), empty
    input, unmatched-middle interpolation, tiny-clip infeasible floors
  - validate: empty / missing start|dur / below _MIN_DUR
  - _wrap long-hold drift (R4) + last-beat no fade
  - render_list row-step cap (07-29: last item must not land 7s in)
  - render_stat numeric count-up vs non-numeric escape
  - render_code indent + highlight class
  - _diagram_svg fanout path vs pipeline line + portrait viewBox 760
  - _follow_verb / _variety_ok / _rich_types / prompts (2b + PACING)
  - compose() with stubbed llm: CTA force, language, unparseable→None
    (exactly 2 calls), variety retry, default allowlist drops code,
    validate-fail even-space fallback

The original parse/align/validate/palette/blank-frame/html-scrape pins stay
(n never decreases). Exits non-zero on the first failed assertion.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from app.services import craft
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
# the opening beat must pin to 0 even when its cue matches mid-narration (no dead air)
b_open = [{"type": "hook", "cue": "delta echo", "text": "H"},   # cue is at ~1.5s, not the start
          {"type": "stat", "cue": "golf hotel", "value": "1"},
          {"type": "statement", "cue": "charlie", "text": "B"},
          {"type": "cta", "cue": "hotel", "text": "C"}]
storyboard.align_storyboard(b_open, words, 4.0)
ok(b_open[0]["start"] == 0.0, "first beat pins to 0.0 even when its cue matches later")

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

# --- all beat types build into valid HTML (guards every renderer) ------------
print("build_index_html (all beat types)")
ALL = [
    {"type": "hook", "cue": "", "text": "Hook line", "emoji": "🔥"},
    {"type": "statement", "cue": "", "text": "A statement", "w": 3},
    {"type": "stat", "cue": "", "value": "42", "unit": "ms", "label": "per call"},
    {"type": "compare", "cue": "", "title": "X vs Y",
     "left": {"title": "X", "items": ["a"]}, "right": {"title": "Y", "items": ["b"]}},
    {"type": "list", "cue": "", "title": "Steps", "ordered": True,
     "items": [{"text": "one"}, {"text": "two"}]},
    {"type": "term_define", "cue": "", "term": "Chunking", "definition": "splitting text into pieces"},
    {"type": "quote", "cue": "", "text": "A memorable line", "attribution": "me"},
    {"type": "code", "cue": "", "lang": "python",
     "lines": ["from sentence_transformers import CrossEncoder", "y = rerank(x)"], "highlight": [1]},
    {"type": "command", "cue": "", "prompt": "$", "command": "pip install rerankers", "output": ["done"]},
    {"type": "diagram", "cue": "", "layout": "pipeline",
     "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "edges": [{"from": "a", "to": "b"}]},
    {"type": "cta", "cue": "", "text": "Follow", "sub": "more"},
]
storyboard.align_storyboard(ALL, [], 44.0)
html = storyboard.build_index_html(ALL, theme.resolve(1, "x"), "portrait", 1080, 1920, 44.0)
ok(worker._looks_valid(html), "all-beat-types storyboard passes _looks_valid")
for cls in ("beat hook", "beat stat", "beat cmp", "beat lst", "beat term", "beat quote",
            "beat code", "beat cmd", "beat diagram", "beat cta"):
    ok(cls in html, f"renders {cls!r}")
ok('class="code" style="font-size:' in html, "code beat emits an adaptive font-size (no clip)")
ok('marker-end="url(#ar)"' in html, "diagram emits arrowhead marker")

# OS vs RR stills: same beats, different brand tokens. Mute-scroll (~0.3s) fail
# condition is indistinguishable stills.
os_board = storyboard.build_index_html(
    ALL, theme.resolve(1, "x", brand="os"), "portrait", 1080, 1920, 44.0)
rr_board = storyboard.build_index_html(
    ALL, theme.resolve(1, "x", brand="rr"), "portrait", 1080, 1920, 44.0)
ok('data-brand="os"' in os_board and theme.OS_LOGO_FILE in os_board,
   "OS storyboard still carries the O crop")
ok('data-brand="rr"' in rr_board and theme.OS_LOGO_FILE not in rr_board,
   "RR storyboard still has zero Owera asset")
ok("#c41e5a" in rr_board.lower() and "#c41e5a" not in os_board.lower(),
   "RR stroke #C41E5A is absent from OS")
ok("#4a1528" in rr_board.lower() and "#1a1a1a" in os_board.lower(),
   "glow families diverge (burgundy vs cold gray)")
ok("border:2px solid var(--stroke)" in rr_board,
   "RR uses thin stroke on boxes, not accent fill as identity")
ok(os_board != rr_board, "OS and RR compositions are not the same bytes")

# --- creation_config capture (Phase 2 treatment signal) ----------------------
print("_creation_config")
cc = worker._creation_config("x", {"topic_id": 1, "content_format": "short"}, html,
                             "word " * 50, 44.0, "portrait", None, False)
ok(cc["beat_count"] == len(ALL), "creation_config captures the full beat mix")
ok("code" in cc["beat_types"] and cc["theme"]["accent"] and cc["composition_version"],
   "creation_config records beat_types + theme + version")

# --- variety guard (R2: no all-statement storyboards) ------------------------
print("_variety_ok")
ok(storyboard._variety_ok([{"type": "hook"}, {"type": "stat"}, {"type": "compare"}, {"type": "cta"}]),
   "varied storyboard passes the variety guard")
ok(not storyboard._variety_ok([{"type": "hook"}, {"type": "statement"}, {"type": "statement"},
                               {"type": "statement"}, {"type": "cta"}]),
   "mostly-statement storyboard fails the variety guard")


# ---------------------------------------------------------------------------
# Module contracts (pins the numbers the growth experiments welded in)
# ---------------------------------------------------------------------------
print("module contracts: floors, caps, renderer registry")
ALL_TYPES = list(storyboard._BEAT_SPECS)
ok(storyboard._MIN_BEATS == 4 and storyboard._MAX_BEATS == 14,
   "beat-count window stays 4..14 (parse drops below, clamps above)")
ok(storyboard._GAP == 0.12 and storyboard._MIN_DUR == 0.5,
   "inter-beat gap 0.12s + min duration 0.5s (matches worker clip tolerance)")
ok(storyboard._TAIL_MIN == 2.0 and storyboard._MID_MIN == 1.8,
   "align floors: last two beats 2.0s, others 1.8s (14b1979 R4)")
ok(storyboard._MID_MAX == 7.5,
   "align max-hold: mid-body 7.5s (penultimate dump still lands on the CTA first)")
ok(storyboard._ENDCARD_MAX == 4.0,
   "series endcard / last-cta hold ceiling is 4.0s")
ok(storyboard._ENDCARD_MAX == craft.ENDCARD_MAX_S,
   "storyboard ceiling matches the craft gate")
ok(storyboard._ROW_STEP_MAX == 1.1,
   "list row-step cap is 1.1s (ce46b43: last item must not land 7s in)")
ok(storyboard._ROW_STEP_SHORTS == 0.6,
   "Shorts list stagger cap is 0.6s (Video Maker craft gate C)")
ok(storyboard._DRIFT_MIN == 5.5,
   "long-hold drift kicks in above 5.5s (70f5320 R4 frozen-card)")
ok(set(storyboard._RENDERERS) == set(storyboard._BEAT_SPECS),
   "every schema type has a renderer (unknown types can't silently vanish)")
ok(set(PHASE_A) <= set(storyboard._BEAT_SPECS),
   "Phase A allowlist is a subset of the schema")


# ---------------------------------------------------------------------------
# Clip helpers — 07-09 code-indent incident lives here
# ---------------------------------------------------------------------------
print("clip helpers")
ok(storyboard._words_clip("one two three four", 2) == "one two",
   "_words_clip keeps the first n words")
ok(storyboard._words_clip("  only  ", 8) == "only",
   "_words_clip strips leftover whitespace")
ok(storyboard._words_clip(None, 3) == "None",
   "_words_clip stringifies a missing field (never raises)")
ok(storyboard._chars_clip("  hello  ", 3) == "hel",
   "_chars_clip strips THEN clips THEN strips (leading space is gone)")
ok(storyboard._chars_clip("    return x", 60) == "return x",
   "_chars_clip would destroy code indent — this is WHY _code_line_clip exists")
ok(storyboard._code_line_clip("    return x  ", 60) == "    return x",
   "_code_line_clip keeps leading indent, only rstrip()s the tail (13be882)")
ok(storyboard._code_line_clip("    return x", 6) == "    re",
   "_code_line_clip clips by characters INCLUDING the indent spaces")
ok(storyboard._code_line_clip("\t\treturn", 60) == "\t\treturn",
   "_code_line_clip keeps tab indentation too")


# ---------------------------------------------------------------------------
# _coerce_beat — every type + the salvage / None paths
# ---------------------------------------------------------------------------
print("_coerce_beat")
ok(storyboard._coerce_beat("not-a-dict", set(ALL_TYPES)) is None,
   "non-dict raw beat is unsalvageable")
ok(storyboard._coerce_beat({"type": "nope"}, set(ALL_TYPES)) is None,
   "unknown type with no text/term/title/cue cannot be salvaged")
salv = storyboard._coerce_beat({"type": "nope", "cue": "hello there friend"}, set(ALL_TYPES))
ok(salv == {"type": "statement", "cue": "hello there friend", "text": "hello there friend",
            "w": 2, "emoji": ""},
   "unknown type with a cue salvages to a 8-word statement (arc survives)")
ok(storyboard._coerce_beat({"type": "code", "lines": ["x=1"]}, set(PHASE_A)) is None,
   "out-of-allowlist code with no text/cue cannot be salvaged")
ok(storyboard._coerce_beat({"type": "code", "cue": "shown here", "lines": ["x=1"]},
                           set(PHASE_A))["type"] == "statement",
   "out-of-allowlist code with a cue downgrades to a statement (arc survives)")

ok(storyboard._coerce_beat({"type": "hook", "text": ""}, set(ALL_TYPES)) is None,
   "hook with empty text is dropped (can't salvage an empty punch)")
ok(storyboard._coerce_beat({"type": "hook", "text": "   "}, set(ALL_TYPES)) is None,
   "hook with whitespace-only text is dropped")
hook = storyboard._coerce_beat(
    {"type": "hook", "text": " ".join(f"w{i}" for i in range(12)), "emoji": "🔥x"},
    set(ALL_TYPES))
ok(hook["text"] == "w0 w1 w2 w3 w4 w5 w6 w7" and hook["emoji"] == "🔥x",
   "hook text clipped to 8 words; emoji clipped to 2 chars (flag+variant ok)")
ok(hook.get("object") == "", "hook without object/prop stores empty object")
hook_obj = storyboard._coerce_beat(
    {"type": "hook", "text": "Your RAG reads junk", "object": "API bill"},
    set(ALL_TYPES))
ok(hook_obj["object"] == "API bill", "hook.object is kept (Decolar prop)")
hook_prop = storyboard._coerce_beat(
    {"type": "hook", "text": "Your RAG reads junk", "prop": "terminal"},
    set(ALL_TYPES))
ok(hook_prop["object"] == "terminal", "hook.prop aliases to object")
hook_emo_obj = storyboard._coerce_beat(
    {"type": "hook", "text": "Your RAG reads junk", "object": "💸🔥"},
    set(ALL_TYPES))
ok(hook_emo_obj["object"] == "", "emoji-only hook.object is stripped (not an object)")

stmt = storyboard._coerce_beat({"type": "statement", "text": "hi", "w": 0}, set(ALL_TYPES))
ok(stmt["w"] == 1, "statement w=0 clamps to 1")
ok(storyboard._coerce_beat({"type": "statement", "text": "hi", "w": 5}, set(ALL_TYPES))["w"] == 3,
   "statement w=5 clamps to 3")
ok(storyboard._coerce_beat({"type": "statement", "text": "hi", "w": "nope"}, set(ALL_TYPES))["w"] == 1,
   "statement non-int w falls back to 1 (not a crash)")
ok(storyboard._coerce_beat({"type": "statement", "text": "hi", "w": 2}, set(ALL_TYPES))["w"] == 2,
   "statement w=2 is kept")

qtext = " ".join(f"w{i}" for i in range(20))
quote = storyboard._coerce_beat(
    {"type": "quote", "text": qtext, "attribution": " ".join(f"a{i}" for i in range(10))},
    set(ALL_TYPES))
ok(len(quote["text"].split()) == 16, "quote text clipped to 16 words")
ok(len(quote["attribution"].split()) == 6, "quote attribution clipped to 6 words")

ok(storyboard._coerce_beat({"type": "stat", "value": ""}, set(ALL_TYPES)) is None,
   "stat with empty value is dropped")
stat = storyboard._coerce_beat(
    {"type": "stat", "value": "123456789012345", "unit": "milliseconds",
     "label": " ".join(f"l{i}" for i in range(8))},
    set(ALL_TYPES))
ok(stat["value"] == "123456789012" and stat["unit"] == "millisec"
   and len(stat["label"].split()) == 6,
   "stat value/unit char-clipped, label word-clipped")

ok(storyboard._coerce_beat({"type": "compare", "left": {}, "right": {"title": "R"}},
                           set(ALL_TYPES)) is None,
   "compare with an empty side is dropped")
ok(storyboard._coerce_beat({"type": "compare", "left": {"title": "L"}, "right": {}},
                           set(ALL_TYPES)) is None,
   "compare with empty right is dropped")
cmpb = storyboard._coerce_beat(
    {"type": "compare", "title": " ".join(f"t{i}" for i in range(8)),
     "left": {"title": "L", "items": ["a", "b", "c", "d"]},
     "right": {"title": "R", "items": ["x"]}},
    set(ALL_TYPES))
ok(len(cmpb["title"].split()) == 5 and len(cmpb["left"]["items"]) == 3,
   "compare title clipped to 5 words; items capped at 3 per side")

ok(storyboard._coerce_beat({"type": "list", "items": []}, set(ALL_TYPES)) is None,
   "list with no salvageable items is dropped")
ok(storyboard._coerce_beat({"type": "list", "items": ["", "  "]}, set(ALL_TYPES)) is None,
   "list of blank strings is dropped")
lst = storyboard._coerce_beat(
    {"type": "list", "items": [f"item{i}" for i in range(10)], "ordered": 1},
    set(ALL_TYPES))
ok(len(lst["items"]) == 5 and lst["ordered"] is True,
   "list items capped at 5; truthy ordered becomes bool True")
lst2 = storyboard._coerce_beat(
    {"type": "list", "items": [{"text": "keep", "emoji": "🔥"}, "bare", {"text": ""}]},
    set(ALL_TYPES))
ok(lst2["items"] == [{"text": "keep", "emoji": "🔥"}, {"text": "bare", "emoji": ""}],
   "list accepts dict+string items and drops empty dicts")

ok(storyboard._coerce_beat({"type": "term_define", "term": "X", "definition": ""},
                           set(ALL_TYPES)) is None,
   "term_define without a definition is dropped")
ok(storyboard._coerce_beat({"type": "term_define", "term": "", "definition": "d"},
                           set(ALL_TYPES)) is None,
   "term_define without a term is dropped")
td = storyboard._coerce_beat(
    {"type": "term_define",
     "term": "one two three four five",
     "definition": " ".join(f"d{i}" for i in range(20))},
    set(ALL_TYPES))
ok(len(td["term"].split()) == 4 and len(td["definition"].split()) == 14,
   "term clipped to 4 words, definition to 14")

cta = storyboard._coerce_beat({"type": "cta", "cue": "go"}, set(ALL_TYPES))
ok(cta["text"] == "Build" and cta["sub"] == "",
   "cta with no text defaults to 'Build' (compose overwrites shorts to the series chip)")
cta2 = storyboard._coerce_beat(
    {"type": "cta", "text": qtext, "sub": qtext}, set(ALL_TYPES))
ok(len(cta2["text"].split()) == 6 and len(cta2["sub"].split()) == 6,
   "cta text clipped to 6 words (chip · Series Name), sub to 6")

ok(storyboard._coerce_beat({"type": "code", "lines": ["", "  "]}, set(ALL_TYPES)) is None,
   "code with only blank lines is dropped")
code = storyboard._coerce_beat(
    {"type": "code",
     "lines": ["def f():", "    return x", ""] + [f"ln{i}" for i in range(10)],
     "highlight": [0, "1", 1.5, 2],
     "lang": "python-too-long-name"},
    set(ALL_TYPES))
ok(code["lines"][1] == "    return x",
   "code line indent survives coerce (not routed through _chars_clip)")
ok(len(code["lines"]) == 8, "code lines capped at 8 (blank dropped before the cap)")
ok(code["highlight"] == [0, 2],
   "code highlight keeps ints only (str/float indices dropped)")
ok(code["lang"] == "python-too-long-",
   "code lang clipped to 16 chars")

ok(storyboard._coerce_beat({"type": "command", "command": ""}, set(ALL_TYPES)) is None,
   "command with empty command is dropped")
cmd = storyboard._coerce_beat(
    {"type": "command", "command": "x" * 100, "prompt": ">>>>",
     "output": [f"o{i}" for i in range(8)]},
    set(ALL_TYPES))
ok(len(cmd["command"]) == 80 and cmd["prompt"] == ">>>" and len(cmd["output"]) == 4,
   "command clipped to 80 chars, prompt to 3, output to 4 rows")

ok(storyboard._coerce_beat({"type": "diagram", "nodes": [{"id": "a", "label": "A"}]},
                           set(ALL_TYPES)) is None,
   "diagram with <2 id-bearing nodes is dropped")
ok(storyboard._coerce_beat({"type": "diagram", "nodes": ["bare", {"label": "no-id"}]},
                           set(ALL_TYPES)) is None,
   "diagram nodes without id are skipped (non-dicts too)")
diag = storyboard._coerce_beat(
    {"type": "diagram",
     "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
     "layout": "bogus",
     "edges": [{"from": "a", "to": "b", "label": "one two three four"}]},
    set(ALL_TYPES))
ok(diag["layout"] == "pipeline",
   "unknown diagram layout falls back to pipeline (never a crash later)")
ok(diag["edges"][0]["label"] == "one two three",
   "diagram edge label clipped to 3 words")
ok(storyboard._coerce_beat(
    {"type": "diagram",
     "nodes": [{"id": "h"}, {"id": "a"}, {"id": "b"}],
     "layout": "fanout"},
    set(ALL_TYPES))["layout"] == "fanout",
   "fanout layout is kept when the model names it")


# ---------------------------------------------------------------------------
# parse_storyboard — unwrap + clamp
# ---------------------------------------------------------------------------
print("parse_storyboard unwrap / clamp")
_FOUR = [
    {"type": "hook", "cue": "a", "text": "Hi"},
    {"type": "stat", "cue": "b", "value": "9"},
    {"type": "list", "cue": "c", "items": ["one"]},
    {"type": "cta", "cue": "d", "text": "Go"},
]
fenced = "Here you go:\n```json\n" + json.dumps({"beats": _FOUR}) + "\n```\nThanks"
ok(storyboard.parse_storyboard(fenced, PHASE_A) is not None,
   "parse unwraps fenced JSON + surrounding prose (re.search first {..})")
ok(storyboard.parse_storyboard(json.dumps(_FOUR), PHASE_A) is None,
   "a top-level array (no {beats:}) is a structural failure")
ok(storyboard.parse_storyboard(None, PHASE_A) is None,
   "None raw is unparseable (never raises)")
many = ([{"type": "hook", "cue": "a", "text": "H"}]
        + [{"type": "stat", "cue": "b", "value": "1"}] * 16
        + [{"type": "cta", "cue": "c", "text": "G"}])
parsed = storyboard.parse_storyboard(json.dumps({"beats": many}), PHASE_A)
ok(parsed is not None and len(parsed) == 14,
   "18 valid beats clamp to _MAX_BEATS=14 (the +4 window does not leak extras)")


# ---------------------------------------------------------------------------
# theme.fold / _tok / _find_subseq — PT cue matching
# ---------------------------------------------------------------------------
print("fold / _tok / _find_subseq")
ok(theme.fold("Produção") == "producao",
   "fold strips PT acute accent (cue 'produção' matches spoken 'producao')")
ok(theme.fold("inferência") == "inferencia",
   "fold strips PT circumflex")
ok(theme.fold("") == "" and theme.fold(None) == "",
   "fold on empty/None is '' (never raises)")
ok(theme.esc("<a&b>") == "&lt;a&amp;b&gt;",
   "esc encodes &, <, > in that order (amp first so we don't double-escape)")
ok(storyboard._tok("Produção inferência") == ["producao", "inferencia"],
   "_tok folds then keeps [a-z0-9]+ tokens")
ok(storyboard._tok("PRODUÇÃO") == ["producao"],
   "_tok is case-insensitive via fold")
ok(storyboard._find_subseq(["a", "b", "c"], [], 0) == -1,
   "empty needle is not a match (would otherwise match every position)")
ok(storyboard._find_subseq(["a", "b", "c"], ["b", "c"], 0) == 1,
   "contiguous subsequence found at index 1")
ok(storyboard._find_subseq(["a", "b", "c"], ["b", "c"], 2) == -1,
   "start cursor past the match returns -1")
ok(storyboard._find_subseq(["a", "b", "c"], ["z"], 0) == -1,
   "missing needle returns -1")
# same-topic palette + subject-hash bg_variant (compose keys theme this way)
ok(theme.resolve(1, "alpha")["accent"] == theme.resolve(1, "omega")["accent"],
   "palette is keyed by topic_id — two subjects under topic 1 share the accent")
ok(theme.resolve("12", "x")["accent"] == theme.PALETTE[12 % len(theme.PALETTE)][0],
   "numeric-string topic_id is accepted (int() then palette index)")
ok(theme.resolve(None, "hello")["accent"]
   == theme.PALETTE[theme._subject_hash("hello") % len(theme.PALETTE)][0],
   "missing topic_id falls back to subject-hash palette")
ok(theme.resolve("nope", "hello")["accent"]
   == theme.PALETTE[theme._subject_hash("hello") % len(theme.PALETTE)][0],
   "non-int topic_id falls back to subject-hash palette")
variants = {theme.resolve(1, s)["bg_variant"] for s in list("abcdefghij")}
ok(len(variants) > 1, "same topic_id still varies bg_variant by subject")
ok(variants <= set(theme.BG_VARIANTS), "bg_variant is always one of the five named looks")


# ---------------------------------------------------------------------------
# align_storyboard — tail/mid floor (07-16) + interpolation + tiny clip
# ---------------------------------------------------------------------------
print("align_storyboard tail/mid floor + interpolation")
ok(storyboard.align_storyboard([], [], 10.0) == [],
   "empty beats is a no-op (never divides by zero in _even_space)")

# 5 beats, last three cues bunched in the final 3.4s of a 20s clip — the exact
# class of 07-16 failure (quote 1.04s / cta 1.16s before the floor).
WORDS20 = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "mid", "start": 4.0, "dur": 0.4}]
    + [{"text": w, "start": t, "dur": 0.3}
       for w, t in (("close", 16.6), ("pay", 17.6), ("off", 17.8),
                    ("follow", 18.6), ("now", 18.9))]
)
bunched = [
    {"type": "hook", "cue": "open", "text": "H"},
    {"type": "stat", "cue": "mid", "value": "1"},
    {"type": "quote", "cue": "close pay", "text": "Q"},
    {"type": "statement", "cue": "off", "text": "P"},
    {"type": "cta", "cue": "follow now", "text": "C"},
]
storyboard.align_storyboard(bunched, WORDS20, 20.0)
ok(bunched[0]["start"] == 0.0, "hook stays pinned at 0 after the floor walk")
ok(bunched[-1]["dur"] >= storyboard._TAIL_MIN - 1e-6,
   "last beat (CTA) gets the 2.0s tail floor — was ~1.2s on word-sync alone")
ok(bunched[-2]["dur"] >= storyboard._TAIL_MIN - storyboard._GAP - 1e-6,
   "second-last beat gets the tail floor minus the 0.12s gap (1.88, not mid-floor 1.68)")
ok(all(b["dur"] >= storyboard._MIN_DUR for b in bunched),
   "no beat drops below _MIN_DUR after backward relaxation")
ok(storyboard.validate_storyboard(bunched, 20.0),
   "floor-relaxed bunched close still validates")
# word-sync would have put the CTA at 18.6; the floor must pull it earlier
ok(bunched[-1]["start"] < 18.6 - 0.01,
   "CTA start is pulled EARLIER than its cue (steals slack, never later)")

# Unmatched middle cue interpolates between neighboring anchors.
interp = [
    {"type": "hook", "cue": "open", "text": "A"},
    {"type": "stat", "cue": "nomatchzzz", "value": "1"},
    {"type": "statement", "cue": "mid", "text": "B"},
    {"type": "cta", "cue": "follow now", "text": "C"},
]
storyboard.align_storyboard(interp, WORDS20, 20.0)
ok(interp[0]["start"] == 0.0, "interpolated board still pins hook at 0")
ok(abs(interp[1]["start"] - 2.0) < 1e-6,
   "unmatched mid interpolates halfway between hook@0 and 'mid'@4 (not min-space 0.5)")

# Tiny clip: 6 beats in 4s — prefix floors (~10.8s) cannot fit, so we must
# not invent a layout that fails validate. Word-sync + MIN_DUR only.
tiny_words = [{"text": w, "start": i * 0.6, "dur": 0.4}
              for i, w in enumerate("a b c d e f".split())]
tiny = [{"type": "hook", "cue": "a", "text": "A"},
        {"type": "stat", "cue": "b", "value": "1"},
        {"type": "list", "cue": "c", "items": ["x"]},
        {"type": "quote", "cue": "d", "text": "Q"},
        {"type": "statement", "cue": "e", "text": "S"},
        {"type": "cta", "cue": "f", "text": "C"}]
storyboard.align_storyboard(tiny, tiny_words, 4.0)
ok(tiny[0]["start"] == 0.0, "tiny-clip hook still at 0")
ok(storyboard.validate_storyboard(tiny, 4.0),
   "infeasible floors degrade to MIN_DUR spacing that still validates")

# Max-hold: 08-27 baseline ch2-concept list sat ~9s then a 5.5s cmp (R4 DRAG).
# Word-sync would keep the 9s span; the cap must pull the successor earlier.
DRAG43 = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "term", "start": 4.3, "dur": 0.4},
     {"text": "stat", "start": 8.2, "dur": 0.3},
     {"text": "list", "start": 10.5, "dur": 0.3},
     {"text": "cmp", "start": 19.5, "dur": 0.3},
     {"text": "code", "start": 25.0, "dur": 0.3},
     {"text": "follow", "start": 36.8, "dur": 0.3},
     {"text": "now", "start": 37.1, "dur": 0.3}]
)
draggy = [
    {"type": "hook", "cue": "open", "text": "H"},
    {"type": "term_define", "cue": "term", "term": "T", "definition": "d"},
    {"type": "stat", "cue": "stat", "value": "1"},
    {"type": "list", "cue": "list", "items": [{"text": "a"}]},
    {"type": "compare", "cue": "cmp",
     "left": {"title": "L", "items": ["x"]}, "right": {"title": "R", "items": ["y"]}},
    {"type": "code", "cue": "code", "lang": "python", "lines": ["x=1"]},
    {"type": "cta", "cue": "follow now", "text": "C"},
]
storyboard.align_storyboard(draggy, DRAG43, 43.3)
ok(draggy[0]["start"] == 0.0, "max-hold still pins hook at 0")
ok(draggy[3]["dur"] <= storyboard._MID_MAX + 1e-6,
   "9s list is capped at _MID_MAX (was ~9s on word-sync)")
ok(draggy[4]["dur"] <= storyboard._MID_MAX + 1e-6,
   "successor cmp absorbs the surplus without itself exceeding _MID_MAX")
ok(draggy[3]["start"] < draggy[4]["start"],
   "capped list still precedes the cmp (monotonic)")
ok(storyboard.validate_storyboard(draggy, 43.3),
   "max-hold layout still validates")
# Word-sync would freeze the list ~9s (cmp cue at 19.5). Max-hold + endcard
# walk must keep the list ≤ _MID_MAX; later mids may slide but stay capped.
ok(draggy[3]["start"] + draggy[3]["dur"] <= draggy[4]["start"] + 1e-6,
   "list yields to cmp (no 9s drag into the claim)")

# Successor already at the cap: only shorten as far as the successor can absorb.
tight_succ_words = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "list", "start": 2.0, "dur": 0.3},
     {"text": "cmp", "start": 12.0, "dur": 0.3},
     {"text": "follow", "start": 19.4, "dur": 0.3}]
)
tight_succ = [
    {"type": "hook", "cue": "open", "text": "H"},
    {"type": "list", "cue": "list", "items": [{"text": "a"}]},
    {"type": "compare", "cue": "cmp",
     "left": {"title": "L", "items": ["x"]}, "right": {"title": "R", "items": ["y"]}},
    {"type": "cta", "cue": "follow", "text": "C"},
]
storyboard.align_storyboard(tight_succ, tight_succ_words, 22.0)
ok(tight_succ[2]["dur"] <= storyboard._MID_MAX + 1e-6,
   "already-capped successor is not pushed over _MID_MAX")
ok(storyboard.validate_storyboard(tight_succ, 22.0),
   "partial max-hold (successor at cap) still validates")

# Last beat (CTA/endcard) is capped at 4.0s — surplus stays on the claim.
cta_long_words = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "mid", "start": 5.0, "dur": 0.3},
     {"text": "follow", "start": 12.0, "dur": 0.3}]
)
cta_long = [
    {"type": "hook", "cue": "open", "text": "H"},
    {"type": "statement", "cue": "mid", "text": "S"},
    {"type": "cta", "cue": "follow", "text": "C"},
]
storyboard.align_storyboard(cta_long, cta_long_words, 20.2)
ok(cta_long[-1]["dur"] <= storyboard._ENDCARD_MAX + 1e-6,
   "CTA/endcard hold is capped at 4.0s")
ok(cta_long[-1]["start"] >= 20.2 - storyboard._ENDCARD_MAX - 1e-6,
   "endcard slides to the tail (after the claim, not on frame0)")
ok(cta_long[0]["start"] == 0.0, "endcard cap still pins hook at 0")
ok(storyboard.validate_storyboard(cta_long, 20.2),
   "capped endcard still validates")

# Penultimate 8.72s list still dumps into the CTA first, then the 4.0s chip
# ceiling slides the endcard to the tail and re-caps the list at _MID_MAX
# (surplus goes to the hook, which stays at 0).
penult_words = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "list", "start": 2.0, "dur": 0.3},
     {"text": "follow", "start": 10.72, "dur": 0.3}]
)
penult = [
    {"type": "hook", "cue": "open", "text": "H"},
    {"type": "list", "cue": "list", "items": [{"text": "a"}]},
    {"type": "cta", "cue": "follow", "text": "C"},
]
storyboard.align_storyboard(penult, penult_words, 18.72)
ok(penult[1]["dur"] <= storyboard._MID_MAX + 1e-6,
   "penultimate list stays ≤ _MID_MAX after the endcard cap")
ok(penult[2]["dur"] <= storyboard._ENDCARD_MAX + 1e-6,
   "endcard does not grow past 4.0s to absorb the surplus")
ok(penult[0]["start"] == 0.0, "endcard cap still pins hook at 0")
ok(penult[2]["start"] >= penult[1]["start"] + penult[1]["dur"] - 1e-6,
   "chip starts after the claim beat (no overlap with frame0)")
ok(storyboard.validate_storyboard(penult, 18.72),
   "4.0s endcard + re-capped penultimate still validates")

even = [{"type": "x"}, {"type": "y"}, {"type": "z"}]
storyboard._even_space(even, 10.0)
ok(even[0]["start"] == 0.0 and even[0]["dur"] == round(10 / 3, 3),
   "_even_space first beat starts at 0 with duration/n")
ok(abs((even[-1]["start"] + even[-1]["dur"]) - 10.0) < 1e-9,
   "_even_space last beat absorbs the rounding remainder (ends at duration)")


# ---------------------------------------------------------------------------
# validate — remaining None / short-dur legs
# ---------------------------------------------------------------------------
print("validate_storyboard remaining legs")
ok(not storyboard.validate_storyboard([], 10.0), "empty beat list is invalid")
ok(not storyboard.validate_storyboard([{"start": 0.0}], 10.0),
   "missing dur is invalid")
ok(not storyboard.validate_storyboard([{"dur": 2.0}], 10.0),
   "missing start is invalid")
ok(not storyboard.validate_storyboard([{"start": 0.0, "dur": 0.4}], 10.0),
   "dur below _MIN_DUR (0.5) is invalid")
ok(storyboard.validate_storyboard([{"start": 0.0, "dur": 0.5}], 10.0),
   "dur exactly _MIN_DUR is valid")


# ---------------------------------------------------------------------------
# _wrap long-hold drift + last-beat fade policy
# ---------------------------------------------------------------------------
print("_wrap drift / last-beat fade")
drift = storyboard._wrap(0, {"start": 0.0, "dur": 8.0, "is_last": False}, [])
ok(any("scale:1.045" in t and "ease:'none'" in t for t in drift),
   "beat held 8s (>5.5) gets linear camera-drift zoom")
ok(any("opacity:0" in t for t in drift),
   "non-last beat still fades out")
short = storyboard._wrap(0, {"start": 0.0, "dur": 3.0, "is_last": False}, [])
ok(not any("scale:1.045" in t for t in short),
   "beat held 3s (below 5.5) has no drift tween")
boundary = storyboard._wrap(0, {"start": 0.0, "dur": 5.5, "is_last": False}, [])
ok(not any("scale:1.045" in t for t in boundary),
   "dur == _DRIFT_MIN is NOT drifted (strict >)")
last = storyboard._wrap(0, {"start": 0.0, "dur": 8.0, "is_last": True}, [])
ok(any("scale:1.045" in t for t in last),
   "last beat still drifts when held long")
ok(not any(t.startswith("tl.to('#b0'") and "opacity:0" in t for t in last),
   "last beat does not fade out (it holds to the end)")


# ---------------------------------------------------------------------------
# render_list row-step cap (07-29) + ordered/emoji bullets
# ---------------------------------------------------------------------------
print("render_list step cap + bullets")
_CTX = {"i": 0, "start": 0.0, "dur": 11.29, "is_last": True,
        "width": 1080, "height": 1920, "duration": 11.29}
_, ltw = storyboard.render_list(
    {"title": "Steps", "items": [{"text": "one"}, {"text": "two"}, {"text": "three"}],
     "start": 0.0, "dur": 11.29},
    _CTX)
row2 = [t for t in ltw if "#b0r2" in t]
ok(row2, "third list row emits a tween targeting #b0r2")
ok(any(t.endswith(",2.45);") for t in row2),
   "long 11.29s / 3-item list reveals last row at 0.25+2*1.1=2.45s "
   "(uncapped would be ~7.24s — the 07-29 incomplete-card bug)")
tight_ctx = dict(_CTX, dur=2.0, duration=2.0)
_, ttw = storyboard.render_list(
    {"items": [{"text": "one"}, {"text": "two"}, {"text": "three"}],
     "start": 0.0, "dur": 2.0},
    tight_ctx)
row2t = [t for t in ttw if "#b0r2" in t]
ok(any(t.endswith(",1.05);") for t in row2t),
   "tight 2s / 3-item list uses win/n=0.4 (cap does not bind); last row at 1.05s")
_, stw = storyboard.render_list(
    {"title": "Steps", "items": [{"text": "one"}, {"text": "two"}, {"text": "three"}],
     "start": 0.0, "dur": 11.29},
    dict(_CTX, content_format="short"))
row2s = [t for t in stw if "#b0r2" in t]
ok(any(t.endswith(",1.45);") for t in row2s),
   "Shorts 11.29s / 3-item list reveals last row at 0.25+2*0.6=1.45s (stagger ≤0.6)")
ol_html, _ = storyboard.render_list(
    {"items": [{"text": "a"}, {"text": "b"}], "ordered": True, "start": 0.0, "dur": 3},
    dict(_CTX, dur=3.0))
ok("1." in ol_html and "2." in ol_html,
   "ordered list uses 1. 2. numbers (emoji does not mix in)")
em_html, _ = storyboard.render_list(
    {"items": [{"text": "a", "emoji": "🔥"}], "start": 0.0, "dur": 3},
    dict(_CTX, dur=3.0))
ok("🔥" in em_html and "•" not in em_html,
   "emoji bullet wins over the default dot when not ordered")
dot_html, _ = storyboard.render_list(
    {"items": [{"text": "a"}], "start": 0.0, "dur": 3},
    dict(_CTX, dur=3.0))
ok("•" in dot_html, "bare item uses the • bullet")


# ---------------------------------------------------------------------------
# render_stat numeric count-up vs non-numeric; statement w=3; code indent
# ---------------------------------------------------------------------------
print("render_stat / statement w / code indent / hook escape")
num_html, num_tw = storyboard.render_stat(
    {"value": "42", "unit": "ms", "label": "per call", "start": 0.0, "dur": 3},
    dict(_CTX, dur=3.0))
ok('<span class="stat-num">0</span>' in num_html,
   "numeric stat renders placeholder 0 (count-up writes the real value)")
ok(any("var o={v:0}" in t and "42" in t for t in num_tw),
   "numeric stat emits a GSAP count-up targeting 42")
nn_html, nn_tw = storyboard.render_stat(
    {"value": "N/A", "start": 0.0, "dur": 3}, dict(_CTX, dur=3.0))
ok('<span class="stat-num">N/A</span>' in nn_html,
   "non-numeric stat renders the escaped value directly (no fake 0)")
ok(not any("var o={v:0}" in t for t in nn_tw),
   "non-numeric stat does not emit a count-up")
xss_html, _ = storyboard.render_hook(
    {"text": "<script>alert(1)</script>", "emoji": "<x>", "start": 0.0, "dur": 2},
    dict(_CTX, dur=2.0))
ok("<script>" not in xss_html and "&lt;script&gt;" in xss_html,
   "hook text is HTML-escaped (theme.esc)")
ok("hemoji" not in xss_html and "💸" not in xss_html,
   "hook does not render emoji as the object (Designer hard FAIL)")
ok('class="obj ' in xss_html and "data-kind=" in xss_html,
   "hook always emits an object widget (typography-only is a hard FAIL)")
w3_html, w3_tw = storyboard.render_statement(
    {"text": "Key point", "w": 3, "start": 0.0, "dur": 2}, dict(_CTX, dur=2.0))
ok("calc(var(--fs)*1.3)" in w3_html and "var(--accent)" in w3_html,
   "statement w=3 uses the oversized accent style")
ok(any("scale:1.05" in t for t in w3_tw),
   "statement w=3 gets the punch scale tween")
w1_html, w1_tw = storyboard.render_statement(
    {"text": "plain", "w": 1, "start": 0.0, "dur": 2}, dict(_CTX, dur=2.0))
ok("calc(var(--fs)*1.3)" not in w1_html,
   "statement w=1 has no oversized style")
ok(not any("scale:1.05" in t for t in w1_tw),
   "statement w=1 has no punch tween")
code_html, _ = storyboard.render_code(
    {"lines": ["def f():", "    return 1"], "lang": "py", "highlight": [1],
     "start": 0.0, "dur": 3},
    dict(_CTX, dur=3.0))
ok("    return 1" in code_html,
   "rendered code keeps the 4-space indent (the on-screen 13be882 bug)")
ok('class="ln hl"' in code_html, "highlighted line index 1 gets the hl class")
ok("py" in code_html, "code lang is rendered")


# ---------------------------------------------------------------------------
# _diagram_svg — fanout vs pipeline + portrait 760 viewBox
# ---------------------------------------------------------------------------
print("_diagram_svg fanout / pipeline / portrait")
fan_nodes = [{"id": "h", "label": "Hub"}, {"id": "a", "label": "A"},
             {"id": "b", "label": "B"}]
fan_edges = [{"from": "h", "to": "a", "label": "toA"},
             {"from": "h", "to": "b", "label": "toB"}]
fan_svg, fan_n = storyboard._diagram_svg(fan_nodes, fan_edges, "fanout", True)
ok('<path class="edge"' in fan_svg and '<line class="edge"' not in fan_svg,
   "portrait fanout draws rail paths, not chain <line>s (540d45d)")
ok('viewBox="0 0 760 ' in fan_svg,
   "portrait diagram viewBox width is 760 (not the old 1000 that left 55% dead)")
ok("toA" in fan_svg and "toB" in fan_svg,
   "fanout edge labels key off the spoke id (model edges are labels only)")
ok(fan_n == 2, "fanout reports 2 edges (one per spoke)")
pipe_svg, pipe_n = storyboard._diagram_svg(
    [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
    [{"from": "a", "to": "b", "label": "step"}],
    "pipeline", True)
ok('<line class="edge"' in pipe_svg,
   "pipeline portrait still uses <line> connectors")
ok('viewBox="0 0 760 ' in pipe_svg,
   "pipeline portrait viewBox is also 760 (the 07-04 1000→760 fix lives on this branch)")
ok('text-anchor="start"' in pipe_svg and 'dominant-baseline="middle"' in pipe_svg,
   "portrait pipeline edge label sits BESIDE the line (7ec526f strike-through fix)")
ok(pipe_n == 1, "pipeline reports the one drawn edge")
land_fan, _ = storyboard._diagram_svg(fan_nodes, fan_edges, "fanout", False)
ok('<path class="edge"' in land_fan,
   "landscape fanout also uses rail paths (not a chain)")


# ---------------------------------------------------------------------------
# _follow_verb / _variety_ok extras / prompts
# ---------------------------------------------------------------------------
print("_follow_verb / _rich_types / _variety_ok extras / prompts")
ok(storyboard._follow_verb("English") == "Follow", "English → Follow")
ok(storyboard._follow_verb("  BRAZILIAN PORTUGUESE ") == "Siga",
   "Brazilian Portuguese is case/whitespace-insensitive → Siga")
ok(storyboard._follow_verb("Portuguese") == "Siga", "Portuguese → Siga")
ok(storyboard._follow_verb("Spanish") == "Sigue", "Spanish → Sigue")
ok(storyboard._follow_verb(None) == "Follow", "None language defaults to Follow")
ok(storyboard._follow_verb("") == "Follow", "empty language defaults to Follow")
ok(storyboard._follow_verb("French") == "Follow",
   "unknown language defaults to Follow (never raises, never blank)")
ok(storyboard._rich_types([
    {"type": "hook"}, {"type": "stat"}, {"type": "statement"}, {"type": "cta"}
]) == {"stat"}, "_rich_types drops hook/cta/statement")
ok(not storyboard._variety_ok([{"type": "hook"}, {"type": "cta"}]),
   "hook+cta only (no mid) fails variety")
ok(not storyboard._variety_ok([{"type": "hook"}, {"type": "stat"}, {"type": "cta"}]),
   "a single rich type fails the ≥2 floor")
ok(storyboard._variety_ok([
    {"type": "hook"}, {"type": "statement"}, {"type": "statement"},
    {"type": "stat"}, {"type": "list"}, {"type": "cta"}
]), "two statements + two rich types still passes (≤2 statements)")
ok(not storyboard._variety_ok([
    {"type": "hook"}, {"type": "statement"}, {"type": "statement"},
    {"type": "stat"}, {"type": "list"}, {"type": "cta"}
], "short"), "shorts variety tightens statement cap to 1")
sys_code = storyboard._system_prompt(["hook", "cta", "code", "stat"])
ok("2b." in sys_code and "`code` or `command`" in sys_code,
   "system prompt includes rule 2b when a Phase-B type is allowed")
ok("MUST include exactly one" in sys_code,
   "rule 2b is a MUST (08-26: prompt-level 'include at least' was dropped 2/4)")
ok('"type":"code"' in sys_code,
   "few-shot example includes a code beat when Phase-B types are allowed")
sys_a = storyboard._system_prompt(["hook", "statement", "cta"])
ok("2b." not in sys_a,
   "system prompt omits rule 2b when code/command/diagram are not allowed")
ok('"type":"code"' not in sys_a,
   "Phase-A few-shot example does not show a code beat")
ok(storyboard._code_ok(
    [{"type": "hook"}, {"type": "stat"}, {"type": "cta"}],
    ["hook", "stat", "cta"]),
   "_code_ok is vacuous True when code/command are not allowed")
ok(not storyboard._code_ok(
    [{"type": "hook"}, {"type": "stat"}, {"type": "list"}, {"type": "cta"}],
    ["hook", "stat", "list", "code", "cta"]),
   "_code_ok fails when code is allowed but absent (08-26 ch2-code shape)")
ok(storyboard._code_ok(
    [{"type": "hook"}, {"type": "code"}, {"type": "cta"}],
    ["hook", "code", "cta"]),
   "_code_ok passes with a code beat")
ok(storyboard._code_ok(
    [{"type": "hook"}, {"type": "command"}, {"type": "cta"}],
    ["hook", "code", "command", "cta"]),
   "_code_ok passes with a command beat")
ok("8+ seconds is a DRAG" in sys_a,
   "PACING rule still names the 8s drag line")
ok("DECOLAR LOCK" in sys_a,
   "system prompt names the Decolar lock (frame0 = first spoken sentence)")
ok("Repeating the title is REQUIRED" in sys_a,
   "hook brief requires repeating the title (curiosity-gap invert)")
ok("Follow" in sys_a and "Siga" in sys_a,
   "Follow/Siga named as FORBIDDEN, not as the CTA verb")
ok("Subscribe — next" in sys_a,
   "ending plan is anchored on the series endcard VO")
ok("≤4.0s" in sys_a or "4.0s" in sys_a,
   "system prompt names the 4.0s endcard hold")
ok("Short vertical" in storyboard._user_prompt("t", "s", "short"),
   "short format asks for a punchy hook")
ok("First spoken sentence" in storyboard._user_prompt("t", "spoken line here", "short"),
   "user prompt injects the first spoken sentence as frame 0")
ok("do NOT replace with a curiosity gap" in storyboard._user_prompt("t", "s", "short"),
   "user prompt forbids a curiosity-gap replacement hook")
ok("Long-form" in storyboard._user_prompt("t", "s", "long"),
   "long format asks for more beats / richer visuals")
ok("CRAFT GATE" not in storyboard._user_prompt("t", "s", "long"),
   "longs do not carry the Shorts A+B+C gate in the prompt")
ok("Opening object" in storyboard._user_prompt("t", "Copilot billed the cancelled run.", "short")
   and "BILL" in storyboard._user_prompt("t", "Copilot billed the cancelled run.", "short"),
   "user prompt injects the concrete opening object (BILL) from the spoken phrase")
ok("Video title: My Subject" in storyboard._user_prompt("My Subject", "narration", "short"),
   "user prompt leads with the real subject (not a constant)")


# ---------------------------------------------------------------------------
# build_index_html — unknown type falls back to statement
# ---------------------------------------------------------------------------
print("build_index_html unknown-type fallback")
fallback_beats = [
    {"type": "hook", "text": "H", "start": 0.0, "dur": 2.0},
    {"type": "future_type", "text": "salvage me", "start": 2.0, "dur": 2.0},
    {"type": "stat", "value": "1", "start": 4.0, "dur": 2.0},
    {"type": "cta", "text": "Follow", "start": 6.0, "dur": 2.0},
]
fb_html = storyboard.build_index_html(
    fallback_beats, theme.resolve(1, "x"), "portrait", 1080, 1920, 8.0)
ok('<span class="word">salvage</span>' in fb_html and "beat stmt" in fb_html,
   "unknown renderer type is rebuilt as a statement (words wrapped, valid by construction)")
ok("beat stat" in fb_html and "beat cta" in fb_html,
   "known types around the fallback still render")


# ---------------------------------------------------------------------------
# compose() — the LLM choke point, llm stubbed
# ---------------------------------------------------------------------------
print("compose() with stubbed llm")
WORDS = [{"text": w, "start": i * 0.5, "dur": 0.4}
         for i, w in enumerate("alpha bravo charlie delta echo foxtrot golf hotel".split())]


def _board(cta_text="Try it", sub="Tomorrow the next part", extra=None):
    beats = [
        {"type": "hook", "cue": "alpha bravo", "text": "Hook line"},
        {"type": "stat", "cue": "charlie delta", "value": "42", "unit": "ms",
         "label": "per call"},
        {"type": "list", "cue": "echo foxtrot", "items": ["one", "two"]},
        {"type": "cta", "cue": "golf hotel", "text": cta_text, "sub": sub},
    ]
    if extra:
        beats[2:2] = extra
    return json.dumps({"beats": beats})


def _compose(llm, **kw):
    defaults = dict(subject="Test video", script="alpha bravo charlie delta echo foxtrot golf hotel",
                    words=WORDS, duration=12.0, resolution="portrait",
                    width=1080, height=1920, topic_id=1, content_format="short",
                    language="English", llm=llm)
    defaults.update(kw)
    return storyboard.compose(**defaults)


calls = []


def happy_llm(user, system=None, max_tokens=None):
    calls.append({"user": user, "system": system, "max_tokens": max_tokens})
    return _board()


html = _compose(happy_llm)
ok(html is not None and "<!doctype html>" in html.lower(),
   "happy-path compose returns a full index.html")
hook_html = html.split('class="beat hook"', 1)[1].split('class="beat ', 1)[0]
ok(all(f'<span class="word">{w}</span>' in hook_html
       for w in "alpha bravo charlie delta echo foxtrot golf hotel".split()),
   "Decolar: hook text is the first spoken sentence (script has no period → whole line)")
pt_opener = [{"type": "hook", "text": "old slogan", "cue": "x", "emoji": "x"}]
storyboard._lock_opening_hook(
    pt_opener, "Sua RAG busca lixo e você culpa o modelo. O modelo não errou.", "subject")
ok(pt_opener[0]["text"] == "Sua RAG busca lixo e você culpa o modelo",
   "Decolar: 9-word PT opener keeps the object (8w clip used to drop 'modelo')")
ok(pt_opener[0]["emoji"] == "" and pt_opener[0]["type"] == "hook",
   "lock forces hook type and strips emoji")
keep_obj = [{"type": "hook", "text": "old", "object": "receipt", "emoji": "x"}]
storyboard._lock_opening_hook(keep_obj, "Your RAG reads junk. Then we fix it.", "s")
ok(keep_obj[0]["text"].startswith("Your RAG") and keep_obj[0]["object"] == "RAG",
   "Decolar lock stamps the spoken noun (RAG), not a leftover receipt prop")
ok(pt_opener[0].get("object") == "RAG" and pt_opener[0].get("object_kind") == "object",
   "lock stamps RAG as the opening object (echoes the spoken phrase)")
ok(pt_opener[0].get("object_spec", {}).get("label") == "RAG",
   "lock keeps the full object spec for the widget renderer")
ok('data-object="ALPHA"' in hook_html and 'class="hobj"' in hook_html,
   "compose frame0 object echoes the first spoken token (alpha), not RECEIPT/emoji")
ok(hook_html.find("hobj") < hook_html.find("htext"),
   "object is above the hook type (does not cover line 1)")
ok("Hook" not in hook_html,
   "LLM curiosity-gap hook text is overwritten")
ok("Follow" not in html and "Siga" not in html and "Try it" not in html,
   "compose does NOT force Follow/Siga (CTA ban) and overwrites 'Try it'")
ok("cta-chip" in html and "Subscribe · Copilot Credits" in html,
   "compose shorts lock the OS default series chip")
ok("same series" in html, "compose shorts emit the same-series micro")
ok('class="cta-box"' not in html, "shorts endcard is a chip, not the punch box")
ok("Subscribe · Copilot Credits" in html.split('class="beat cta"', 1)[-1],
   "endcard chip paints Subscribe · {series}")
ok(len(calls) == 1 and calls[0]["max_tokens"] == 1500,
   "happy path is a single llm call at max_tokens=1500")
ok("Video title: Test video" in calls[0]["user"],
   "compose forwards the real subject into the user prompt")
ok("First spoken sentence" in calls[0]["user"],
   "compose injects the spoken sentence into the user prompt")
ok("2b." not in (calls[0]["system"] or ""),
   "default allowlist has no Phase-B types so rule 2b is absent")
ok("CRAFT GATE" in calls[0]["user"] and "hook.object" in calls[0]["user"],
   "shorts user prompt carries the A+B+C craft gate")
ok('id="storyboard-beats"' in html, "compose embeds the beat snapshot for the craft gate")
ok('"object"' in html.split('id="storyboard-beats"', 1)[1].split("</script>", 1)[0]
   or "stat" in html,
   "embedded snapshot is JSON beats (gate evaluates post-compose)")
ok(worker._looks_valid(html), "composed HTML passes the worker validity guard")

pt_html = _compose(lambda *a, **k: _board(), language="Brazilian Portuguese")
ok("Siga" not in pt_html and "Follow" not in pt_html,
   "PT compose does not force Siga/Follow")
es_html = _compose(lambda *a, **k: _board(), language="Spanish")
ok("Sigue" not in es_html, "Spanish compose does not force Sigue")

rr_html = _compose(
    lambda *a, **k: _board(),
    subject="Você lotou a VRAM. · IA 175",
    brand="rr",
)
ok("cta-chip" in rr_html and "Subscribe · IA" in rr_html,
   "RR compose locks the IA series chip")
ok("Copilot Credits" not in rr_html,
   "RR chip does not invent the OS series label")
ok("Follow" not in rr_html and "amanhã" not in rr_html.lower()
   and "waitlist" not in rr_html and "💸" not in rr_html,
   "RR endcard keeps the hard bans")

mem_html = _compose(
    lambda *a, **k: _board(),
    subject="Memory died between chats · Agent memory 2",
)
ok("· Agent memory" in mem_html and "Subscribe — next" not in mem_html,
   "non-Credits/IA series only swaps the chip; no extra on-screen CTA")

n_bad = [0]


def bad_llm(*a, **k):
    n_bad[0] += 1
    return "not json at all"


ok(_compose(bad_llm) is None, "unparseable draft + unparseable retry → None (caller falls back)")
ok(n_bad[0] == 2, "unparseable path calls llm exactly twice (draft then 'ONLY valid JSON')")

seq = [
    json.dumps({"beats": [
        {"type": "hook", "cue": "alpha", "text": "H"},
        {"type": "statement", "cue": "bravo", "text": "S1"},
        {"type": "statement", "cue": "charlie", "text": "S2"},
        {"type": "statement", "cue": "delta", "text": "S3"},
        {"type": "cta", "cue": "echo", "text": "X", "sub": "next thing"},
    ]}),
    json.dumps({"beats": [
        {"type": "hook", "cue": "alpha", "text": "H"},
        {"type": "stat", "cue": "bravo", "value": "1"},
        {"type": "list", "cue": "charlie", "items": ["one", "two"]},
        {"type": "cta", "cue": "delta", "text": "X", "sub": "next thing"},
    ]}),
]
n_var = [0]


def variety_llm(*a, **k):
    out = seq[min(n_var[0], len(seq) - 1)]
    n_var[0] += 1
    return out


var_html = _compose(variety_llm)
ok(n_var[0] == 2, "all-statement draft triggers exactly one variety retry")
ok("beat stat" in var_html and "beat lst" in var_html,
   "compose keeps the richer retry (stat+list), not the all-statement draft")

n_fence = [0]


def fenced_llm(*a, **k):
    n_fence[0] += 1
    return "Sure, here is the storyboard:\n```json\n" + _board() + "\n```\n"


ok(_compose(fenced_llm) is not None and n_fence[0] == 1,
   "fenced-JSON happy path parses on the first call (no spurious retry)")


def code_board_llm(*a, **k):
    return json.dumps({"beats": [
        {"type": "hook", "cue": "alpha", "text": "H"},
        {"type": "code", "cue": "bravo", "lines": ["x = 1"]},
        {"type": "stat", "cue": "charlie", "value": "1"},
        {"type": "cta", "cue": "delta", "text": "X", "sub": "next"},
    ]})


dropped = _compose(code_board_llm, allowed_types=None)
ok("beat code" not in dropped and "beat stmt" in dropped,
   "default allowlist does not include code — the beat is salvaged as a statement")
kept = _compose(code_board_llm, allowed_types=PHASE_A + ["code"])
ok("beat code" in kept, "allowing code keeps the code beat (and its renderer)")

# R2 code retry (08-26): when code is allowed but the first draft has none, push once.
code_seq = [
    _board(),  # varied but no snippet (the 08-26 ch2-code failure)
    json.dumps({"beats": [
        {"type": "hook", "cue": "alpha", "text": "H"},
        {"type": "stat", "cue": "bravo", "value": "1"},
        {"type": "code", "cue": "charlie", "lines": ["@mcp.tool()", "def run(q):", "  return db(q)"]},
        {"type": "cta", "cue": "delta", "text": "X", "sub": "next"},
    ]}),
]
n_code = [0]


def missing_code_llm(*a, **k):
    out = code_seq[min(n_code[0], len(code_seq) - 1)]
    n_code[0] += 1
    return out


code_html = _compose(missing_code_llm, allowed_types=PHASE_A + ["code"])
ok(n_code[0] == 2, "code-less draft with code allowed triggers exactly one R2 retry")
ok("beat code" in code_html, "compose keeps the retry that added a code beat")
ok("def run(q):" in code_html, "retry code snippet lands in the HTML")

n_has = [0]


def already_has_code_llm(*a, **k):
    n_has[0] += 1
    return json.dumps({"beats": [
        {"type": "hook", "cue": "alpha", "text": "H"},
        {"type": "code", "cue": "bravo", "lines": ["x = 1"]},
        {"type": "stat", "cue": "charlie", "value": "1"},
        {"type": "cta", "cue": "delta", "text": "X", "sub": "next"},
    ]})


ok(_compose(already_has_code_llm, allowed_types=PHASE_A + ["code"]) is not None
   and n_has[0] == 1,
   "draft that already has a code beat does not retry")

sub_mid = _compose(
    lambda *a, **k: _board(extra=[{
        "type": "statement", "cue": "echo foxtrot", "text": "Subscribe now",
    }]),
    script="alpha bravo charlie delta echo foxtrot golf hotel. Subscribe — next Copilot Credits trap.",
    subject="alpha bravo charlie · Copilot Credits 1",
)
ok("Subscribe now" not in sub_mid,
   "compose strips Subscribe from a mid-body statement")
hook_mid = sub_mid.split('class="beat hook"', 1)[1].split('class="beat ', 1)[0]
ok("Subscribe" not in hook_mid,
   "frame0 / mid beats do not carry Subscribe (endcard VO only)")
ok("cta-chip" in sub_mid and "Subscribe · Copilot Credits" in sub_mid,
   "endcard chip still renders after a mid-Subscribe scrub")

# validate-fail → even-space rescue, then success
_real_val = storyboard.validate_storyboard
n_val = [0]


def fail_once(beats, duration):
    n_val[0] += 1
    if n_val[0] == 1:
        return False
    return _real_val(beats, duration)


storyboard.validate_storyboard = fail_once
try:
    rescued = _compose(lambda *a, **k: _board())
    ok(rescued is not None, "first validate failure falls through to _even_space and succeeds")
    ok(n_val[0] >= 2, "compose re-validates after even-spacing")
    even_starts = re.findall(
        r'class="beat [^"]+" id="b\d+" data-start="([^"]+)"', rescued)
    ok(even_starts == ["0.0", "3.0", "6.0", "9.0"],
       "rescue even-spaces a 4-beat/12s board to 0/3/6/9 (word-sync was 0/1/2/3 — "
       "a retry-without-_even_space mutant keeps 1.0/2.0/3.0)")
finally:
    storyboard.validate_storyboard = _real_val

storyboard.validate_storyboard = lambda *a, **k: False
try:
    ok(_compose(lambda *a, **k: _board()) is None,
       "validate failing even after even-space → None (never emits invalid HTML)")
finally:
    storyboard.validate_storyboard = _real_val

# Long-form: _sanitize_cta keeps the global CTA ban (Subscribe is endcard-only).
long_beats = [
    {"type": "hook", "text": "H", "cue": "h"},
    {"type": "cta", "text": "Subscribe", "sub": "Follow tomorrow"},
]
storyboard._sanitize_cta(
    long_beats, "The lesson is cut by meaning.",
    subject="Deep dive", brand="os", content_format="long",
)
ok(long_beats[-1]["text"] == "The lesson is cut",
   "long-form CTA still locks to the spoken punch, not Subscribe")
ok(long_beats[-1].get("endcard") is False, "long-form is not the series endcard")
ok("Follow" not in (long_beats[-1].get("sub") or "")
   and "Subscribe" not in (long_beats[-1].get("sub") or ""),
   "long-form still strips Follow/Subscribe from the card")

# ---------------------------------------------------------------------------
print("nonsense diagrams / oars gated (shorts demote; long keeps a real topology)")

oars = {"type": "diagram", "cue": "flow here",
        "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"},
                  {"id": "c", "label": "oar"}],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]}
ok(storyboard._diagram_is_nonsense(oars),
   "generic A/B/oar boxes with unlabeled edges are nonsense")
ok(storyboard._diagram_is_nonsense(
    {"nodes": [{"label": "step 1"}, {"label": "step 2"}], "edges": []}),
   "step-N nodes with no labeled edges are nonsense")
ok(not storyboard._diagram_is_nonsense({
    "nodes": [{"label": "retriever"}, {"label": "rerank"}, {"label": "llm"},
              {"label": "answer"}],
    "edges": [{"from": "r", "to": "k", "label": "top-k"},
              {"from": "k", "to": "l", "label": "scores"}],
}), "labeled real topology is not nonsense")

short_oars = [dict(oars)]
storyboard._demote_nonsense_diagrams(short_oars, "short")
ok(short_oars[0]["type"] == "statement" and short_oars[0].get("emoji") == "",
   "shorts demote oar diagrams to a statement (no emoji soup)")
# shorts demote EVERY diagram, even a labeled one — 9:16 prefers code/command
real = [{"type": "diagram", "cue": "the pipeline",
         "nodes": [{"id": "a", "label": "retriever"}, {"id": "b", "label": "rerank"}],
         "edges": [{"from": "a", "to": "b", "label": "top-k"}]}]
storyboard._demote_nonsense_diagrams(real, "short")
ok(real[0]["type"] == "statement",
   "shorts demote even a labeled diagram (prefer code/command on 9:16)")
keep = [{"type": "diagram", "cue": "the pipeline",
         "nodes": [{"id": "a", "label": "retriever"}, {"id": "b", "label": "rerank"}],
         "edges": [{"from": "a", "to": "b", "label": "top-k"}]}]
storyboard._demote_nonsense_diagrams(keep, "long")
ok(keep[0]["type"] == "diagram",
   "long-form keeps a labeled real topology")
long_oars = [dict(oars)]
storyboard._demote_nonsense_diagrams(long_oars, "long")
ok(long_oars[0]["type"] == "statement",
   "long-form still demotes generic oars")

print(f"\nALL {_checks} CHECKS PASSED")
