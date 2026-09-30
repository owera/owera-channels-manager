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
ok(cc.get("beat_timing") == craft.BEAT_TIMING_CURRENT == craft.BEAT_TIMING_HOLD_280,
   "creation_config marks new renders card_hold_280 (mid hold ≤2.80s, hold+fade ≤3.0s — RR 2026-09-30)")

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
ok(storyboard._MID_MAX == 2.80 == craft.MID_BEAT_MAX_S,
   "align max-hold: mid hold 2.80s so hold + measured 0.20s fade ≤ 3.0s (RR 2026-09-30; was 2.88)")
ok(storyboard._MID_MAX == craft.MID_BEAT_MAX_S,
   "gate B mid-hold cap matches the aligner (do not fail-close below _MID_MAX)")
ok(storyboard._GAP == craft.BEAT_GAP_S,
   "gate B fade gap matches the aligner (do not count the 0.12s fade as hold)")
ok(storyboard._ENDCARD_MAX == 3.78 and abs(storyboard._ENDCARD_MAX + storyboard._GAP - 3.9) < 1e-9,
   "series endcard ≤3.9s INCLUDING its fade → hold 3.78s (VM 2026-09-29; was 4.0 hold)")
ok(storyboard._HOOK_MAX == 4.0,
   "frame0 visual-hold target is 4.0s (surplus is later speech, not sentence 1)")
ok(storyboard._HOOK_MAX >= storyboard._ENDCARD_MAX,
   "hook target (4.0, Gate B exempt) is not tighter than the endcard hold")
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
# Gate B (8f9ed39) dumps surplus into hook/mids and caps the endcard at 4s,
# which moves the unmatched mid to ~9.76 after align — the same place a
# min-space-0.5 mutant lands after the dump. Lift the caps so the 2.0
# halfway pin still discriminates interpolation from min-space.
interp = [
    {"type": "hook", "cue": "open", "text": "A"},
    {"type": "stat", "cue": "nomatchzzz", "value": "1"},
    {"type": "statement", "cue": "mid", "text": "B"},
    {"type": "cta", "cue": "follow now", "text": "C"},
]
_orig_mid_max, _orig_endcard = storyboard._MID_MAX, storyboard._ENDCARD_MAX
storyboard._MID_MAX = 999.0
storyboard._ENDCARD_MAX = 999.0
try:
    storyboard.align_storyboard(interp, WORDS20, 20.0)
    ok(interp[0]["start"] == 0.0, "interpolated board still pins hook at 0")
    ok(abs(interp[1]["start"] - 2.0) < 1e-6,
       "unmatched mid interpolates halfway between hook@0 and 'mid'@4 (not min-space 0.5)")
finally:
    storyboard._MID_MAX = _orig_mid_max
    storyboard._ENDCARD_MAX = _orig_endcard

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
_list_b = [b for b in draggy if b.get("type") == "list"][-1]
_cmp_b = [b for b in draggy if b.get("type") == "compare"][-1]
ok(_list_b["dur"] <= storyboard._MID_MAX + 1e-6,
   "9s list is capped at _MID_MAX (was ~9s on word-sync)")
ok(_cmp_b["dur"] <= storyboard._MID_MAX + 1e-6,
   "successor cmp absorbs the surplus without itself exceeding _MID_MAX")
ok(_list_b["start"] < _cmp_b["start"],
   "capped list still precedes the cmp (monotonic)")
ok(storyboard.validate_storyboard(draggy, 43.3),
   "max-hold layout still validates")
# Word-sync would freeze the list ~9s (cmp cue at 19.5). Max-hold + endcard
# walk must keep the list ≤ _MID_MAX; later mids may slide but stay capped.
ok(_list_b["start"] + _list_b["dur"] <= _cmp_b["start"] + 1e-6,
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
_tight_cmp = [b for b in tight_succ if b.get("type") == "compare"][-1]
ok(_tight_cmp["dur"] <= storyboard._MID_MAX + 1e-6,
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

# Hook surplus fill (v1267/golden ch2-code): 3s mids + 4s CTA dump leftover
# onto Gate-B-exempt frame0. Fill with NEW quote cards of the words spoken
# in each slot — never clones of object mids (repeated-card FAIL, VM
# 2026-09-29) and never hook-claim quotes (09-17 R2 drop). Remainder stays
# on hook. Speech is continuous (real narration), so every slot has words;
# the pre-2026-09-29 version of this test had only the 5 cue words (a
# wordless 24s gap) because clones did not need any speech.
print("align_storyboard hook-surplus fill")
_cue_at = {0.0: "open", 4.0: "code", 8.0: "cmp", 12.0: "stat", 36.0: "follow"}
hook_dump_words = []
for _k in range(0, 73):
    _t = round(_k * 0.5, 2)
    hook_dump_words.append({"text": _cue_at.get(_t, "w%d" % _k), "start": _t, "dur": 0.4})
hook_dump = [
    {"type": "hook", "cue": "open", "text": "Você gastou R$80 no Cursor"},
    {"type": "code", "cue": "code", "lang": "python", "lines": ["x=1"]},
    {"type": "compare", "cue": "cmp",
     "left": {"title": "L", "items": ["a"]}, "right": {"title": "R", "items": ["b"]}},
    {"type": "stat", "cue": "stat", "value": "80", "unit": "R$", "label": "bill"},
    {"type": "cta", "cue": "follow", "text": "Subscribe · IA"},
]
storyboard.align_storyboard(hook_dump, hook_dump_words, 40.0)
ok(hook_dump[0]["start"] == 0.0, "hook fill still pins frame0 at 0")
ok(hook_dump[0]["dur"] <= storyboard._HOOK_MAX + storyboard._MID_MAX + 1e-6,
   "frozen hook is capped (was ~13-17s dump onto frame0)")
ok(hook_dump[0]["dur"] >= storyboard._MIN_DUR,
   "capped hook still holds a real card")
fill_types = [b.get("type") for b in hook_dump[1:-1]]
ok(all(t != "statement" for t in fill_types),
   "hook fill never clones statement beats (Gate C)")
ok("code" in fill_types and "compare" in fill_types and "stat" in fill_types,
   "original object mids still present after fill")
ok(all((b.get("type") or "") != "hook" for b in hook_dump[1:]),
   "fill does not clone the hook claim as extra hook cards")
ok(all(float(b["dur"]) <= storyboard._MID_MAX + 1e-6
       for b in hook_dump[1:-1]),
   "inserted mids stay at Gate B _MID_MAX")
ok(hook_dump[-1]["dur"] <= storyboard._ENDCARD_MAX + 1e-6,
   "CTA ceiling holds after hook fill")
ok(hook_dump[-1]["type"] == "cta", "CTA stays last")
ok(storyboard.validate_storyboard(hook_dump, 40.0),
   "hook-fill layout still validates")
fills = [b for b in hook_dump if b.get("_fill")]
ok(fills and all(b["type"] == "quote" for b in fills),
   "hook fill inserts NEW quote cards (no clones of object mids)")
ok([b.get("type") for b in hook_dump].count("code") == 1
   and [b.get("type") for b in hook_dump].count("compare") == 1
   and [b.get("type") for b in hook_dump].count("stat") == 1,
   "each object mid appears exactly once (no replayed cards)")
ok(craft.repeated_card_hits(hook_dump) == [],
   "hook-fill board has no repeated card (craft gate check)")
_fill_texts = [b["text"] for b in fills]
_first_word_at = {}
for _b in fills:
    _in = [w["text"] for w in hook_dump_words
           if _b["start"] - 1e-9 <= w["start"] < _b["start"] + _b["dur"] + storyboard._GAP - 1e-9]
    _first_word_at[_b["text"]] = _in[0] if _in else None
ok(len(set(_fill_texts)) == len(_fill_texts)
   and all(t.split()[0] == _first_word_at[t] for t in _fill_texts),
   "fill quotes carry the words spoken in their own slot")
_n_after = len(hook_dump)
storyboard._cap_endcard(hook_dump, 40.0, hook_dump_words)
ok(len(hook_dump) == _n_after and craft.repeated_card_hits(hook_dump) == [],
   "second _cap_endcard pass (compose) is idempotent — never re-fills from "
   "the fill (the v1370 8GB×2 root cause)")
# No words (even-space / no TTS timings): no fill, never clones.
no_words = [
    {"type": "hook", "cue": "open", "text": "Hook"},
    {"type": "stat", "cue": "s", "value": "8", "unit": "GB", "label": "KV"},
    {"type": "code", "cue": "c", "lines": ["x=1"]},
    {"type": "cta", "cue": "follow", "text": "Subscribe · IA"},
]
storyboard.align_storyboard(no_words, [], 40.0)
ok(len(no_words) == 4 and craft.repeated_card_hits(no_words) == [],
   "no word timings: hook fill does not clone mids")
# Short hook is a no-op (ch1-code 5.2s class).
short_hook_words = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "code", "start": 3.5, "dur": 0.3},
     {"text": "follow", "start": 7.0, "dur": 0.3}]
)
short_hook = [
    {"type": "hook", "cue": "open", "text": "Your RAG is slow"},
    {"type": "code", "cue": "code", "lang": "python", "lines": ["x=1"]},
    {"type": "cta", "cue": "follow", "text": "C"},
]
n_before = len(short_hook)
storyboard.align_storyboard(short_hook, short_hook_words, 11.0)
ok(len(short_hook) == n_before,
   "hook fill is a no-op when frame0 is already near _HOOK_MAX")
ok(short_hook[0]["type"] == "hook" and short_hook[1]["type"] == "code",
   "short-hook board keeps original beat identities")
# No object donor (statement-only mid) — do not clone the statement (Gate C).
no_donor_words = (
    [{"text": "open", "start": 0.0, "dur": 0.4},
     {"text": "mid", "start": 5.0, "dur": 0.3},
     {"text": "follow", "start": 12.0, "dur": 0.3}]
)
no_donor = [
    {"type": "hook", "cue": "open", "text": "H"},
    {"type": "statement", "cue": "mid", "text": "S"},
    {"type": "cta", "cue": "follow", "text": "C"},
]
storyboard.align_storyboard(no_donor, no_donor_words, 20.2)
ok(sum(1 for b in no_donor if b.get("type") == "statement") == 1,
   "no object donor: do not insert extra statements")
ok(no_donor[0]["type"] == "hook" and no_donor[-1]["type"] == "cta",
   "no-donor board keeps hook/cta")

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
    dict(_CTX, content_format="long"))
row2 = [t for t in ltw if "#b0r2" in t]
ok(row2, "third list row emits a tween targeting #b0r2")
ok(any(t.endswith(",2.45);") for t in row2),
   "long 11.29s / 3-item list reveals last row at 0.25+2*1.1=2.45s "
   "(uncapped would be ~7.24s — the 07-29 incomplete-card bug; "
   "content_format=long is explicit so missing/leftover cannot hide here)")
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

# Defect: render_list used content_format == "short" for the 0.6s stagger,
# so empty/"LONG"/"medium"/missing leftovers (treated as shorts by
# _sanitize_cta / _demote / the retry prompt via != "long") used the
# 1.1s long-form cap. Same class as BACKLOG 23–26 / #38.
print("render_list: leftover formats use the Shorts stagger (!= long)")
_LIST3 = {"title": "Steps", "items": [{"text": "one"}, {"text": "two"}, {"text": "three"}],
          "start": 0.0, "dur": 11.29}
_MISSING = object()


def _last_row_at(fmt):
    ctx = dict(_CTX) if fmt is _MISSING else dict(_CTX, content_format=fmt)
    _, tw = storyboard.render_list(_LIST3, ctx)
    row = [t for t in tw if "#b0r2" in t]
    return any(t.endswith(",1.45);") for t in row)

ok(_last_row_at(""),
   "empty-format leftover list last-row at 1.45s "
   "(== 'short' would use the 1.1s long cap → 2.45s)")
ok(_last_row_at("LONG"),
   "'LONG' leftover list last-row at 1.45s "
   "(case-sensitive == 'long' only; == 'short' used 2.45s)")
ok(_last_row_at("medium"),
   "'medium' leftover list last-row at 1.45s "
   "(allowlist short+empty+LONG would still miss this)")
ok(_last_row_at(None),
   "content_format=None list last-row at 1.45s "
   "(== 'short' treated None as long-form 2.45s)")
ok(_last_row_at(_MISSING),
   "missing content_format key list last-row at 1.45s "
   "(ctx.get defaulted to the long cap)")
ok(_last_row_at("short"),
   "canonical short still 1.45s after leftover pins")
_, long_tw = storyboard.render_list(_LIST3, dict(_CTX, content_format="long"))
ok(any(t.endswith(",2.45);") for t in long_tw if "#b0r2" in t),
   "canonical long still 2.45s (leftover gate does not invert longs)")
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
_TWO_STMT = [
    {"type": "hook"}, {"type": "statement"}, {"type": "statement"},
    {"type": "stat"}, {"type": "list"}, {"type": "cta"}
]
ok(storyboard._variety_ok(_TWO_STMT, "long"),
   "long-form allows two statements + two rich types (≤2 statements)")
ok(not storyboard._variety_ok(_TWO_STMT, "short"),
   "shorts variety tightens statement cap to 1")

# Defect: _variety_ok used content_format == "short" for the cap-1 gate,
# so empty/"LONG"/"medium"/None leftovers (the retry prompt, _sanitize_cta,
# and _demote all use != "long") kept the long-form ≤2 cap. Craft gate C
# wants ≤1 statement on leftover shorts. Same class as BACKLOG 23–26 / #38.
print("_variety_ok: leftover formats use the shorts statement cap (!= long)")
ok(not storyboard._variety_ok(_TWO_STMT, ""),
   "empty-format leftover is cap-1 "
   "(== 'short' would allow two statements)")
ok(not storyboard._variety_ok(_TWO_STMT, "LONG"),
   "'LONG' leftover is cap-1 (case-sensitive == 'long' only)")
ok(not storyboard._variety_ok(_TWO_STMT, "medium"),
   "'medium' leftover is cap-1 "
   "(allowlist short+empty+LONG would still miss this)")
ok(not storyboard._variety_ok(_TWO_STMT, None),
   "content_format=None is cap-1 "
   "(== 'short' treated None as the historical ≤2 default)")
ok(storyboard._variety_ok(_TWO_STMT, "long"),
   "canonical long still allows two statements after leftover pins")
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
ok(pt_opener[0]["text"] == "Sua RAG busca lixo e você culpa o modelo.",
   "Decolar: 9-word PT opener keeps the object (8w clip used to drop 'modelo') "
   "and its period (overlay keeps punctuation)")
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
    # even 0/3/6/9, then the 2.80s mid cap (RR 2026-09-30): a 0.08s overflow
    # is too short for its own card, so the next card starts early (visual
    # leads the VO) instead of stretching → 0/3/5.92/8.84.
    ok(even_starts == ["0.0", "3.0", "5.92", "8.84"],
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

print("Gate C statement cap (v1262: 3 statements → 1 quote-converted)")
stmt_board = [
    {"type": "hook", "cue": "h", "text": "H"},
    {"type": "code", "cue": "c", "lines": ["x=1"]},
    {"type": "compare", "cue": "k",
     "left": {"title": "L", "items": ["a"]}, "right": {"title": "R", "items": ["b"]}},
    {"type": "statement", "cue": "s1", "text": "one idea"},
    {"type": "statement", "cue": "s2", "text": "two idea"},
    {"type": "statement", "cue": "s3", "text": "three idea"},
    {"type": "cta", "cue": "t", "text": "Subscribe · IA"},
]
storyboard._cap_statements(stmt_board, "short")
ok(sum(1 for b in stmt_board if b["type"] == "statement") == 1,
   "shorts keep exactly one statement (Gate C max)")
ok(sum(1 for b in stmt_board if b["type"] == "quote") == 2,
   "extra statements become quotes (not dropped)")
ok(stmt_board[3]["type"] == "statement" and stmt_board[4]["type"] == "quote",
   "first statement is kept; later ones convert")
long_stmt = [
    {"type": "hook", "text": "H"},
    {"type": "statement", "text": "A", "cue": "a"},
    {"type": "statement", "text": "B", "cue": "b"},
    {"type": "cta", "text": "C"},
]
storyboard._cap_statements(long_stmt, "long")
ok(sum(1 for b in long_stmt if b["type"] == "statement") == 2,
   "long-form does not convert extra statements")
v1262_types = [
    {"type": "hook", "text": "H"},
    {"type": "code", "lines": ["x=1"]},
    {"type": "compare",
     "left": {"title": "L", "items": ["a"]}, "right": {"title": "R", "items": ["b"]}},
    {"type": "statement", "text": "one", "cue": "a"},
    {"type": "statement", "text": "two", "cue": "b"},
    {"type": "statement", "text": "three", "cue": "c"},
    {"type": "quote", "text": "Q"},
    {"type": "cta", "text": "Subscribe · IA"},
]
storyboard._cap_statements(v1262_types, "short")
ok(sum(1 for b in v1262_types if b["type"] == "statement") == 1,
   "v1262-shaped 3 statements collapse to 1")
ok(craft.video_maker_gate(v1262_types, content_format="short")["checks"]["C"] == "PASS",
   "Gate C PASS after the statement cap (was FAIL on 3 statements)")

print("repeated card (VM 2026-09-29: RR #1340/#1349/#1370/#1376 same card ×2 ~6.2s)")
# v1370 shape: one 8GB stat donor cued on the repeated phrase "Isso não é
# engenharia", a 2-bytes stat, then the rest. Old code filled the hook
# surplus with clones (twice) → hook, 8GB, 8GB, 2B, 8GB, 2B, ...
_v1370_script = ("KV em FP16 estoura a 8GB, não é engenharia. Isso não é engenharia. "
                 "Cada token guarda dois bytes por valor no cache e o custo sobe com o "
                 "contexto até não caber na placa. Quantiza o KV e cada token passa a "
                 "custar metade. Qualidade fica. Engenharia é escolher o byte do cache.")
_v1370_words = []
_t = 0.0
for _w in _v1370_script.replace(",", "").replace(".", "").split():
    _v1370_words.append({"text": _w, "start": round(_t, 3), "dur": 0.3})
    _t += 0.42
_v1370_words.append({"text": "Subscribe", "start": 37.3, "dur": 0.3})
_v1370_words.append({"text": "next", "start": 37.7, "dur": 0.3})


def _v1370_beats():
    return [
        {"type": "hook", "cue": "KV em FP16 estoura",
         "text": "KV em FP16 estoura a 8GB, não é engenharia"},
        {"type": "stat", "cue": "Isso não é engenharia", "value": "8", "unit": "GB",
         "label": "KV FP16 na placa"},
        {"type": "stat", "cue": "dois bytes por", "value": "2",
         "unit": "bytes", "label": "por token em FP16"},
        {"type": "term_define", "cue": "o custo sobe", "term": "KV cache",
         "definition": "memória por token"},
        {"type": "compare", "cue": "caber na placa",
         "left": {"title": "FP16", "items": ["estoura"]},
         "right": {"title": "Q8", "items": ["cabe"]}},
        {"type": "code", "cue": "Quantiza o KV", "lines": ["--cache-type-k q8_0"]},
        {"type": "stat", "cue": "Cada token passa", "value": "1", "unit": "byte",
         "label": "metade do FP16"},
        {"type": "quote", "cue": "Engenharia é escolher",
         "text": "Engenharia é escolher o byte do cache"},
        {"type": "cta", "cue": "Subscribe next", "text": "Subscribe · IA"},
    ]


v1370 = _v1370_beats()
storyboard.align_storyboard(v1370, _v1370_words, 41.17)
storyboard._cap_list_holds(v1370)
storyboard._cap_endcard(v1370, 41.17, _v1370_words)  # compose's second pass
ok(storyboard.validate_storyboard(v1370, 41.17), "v1370-shaped board validates")
ok(craft.repeated_card_hits(v1370) == [],
   "v1370-shaped board: no card shown twice (was 8GB ×2 back-to-back, ×3 total)")
_keys = [craft.screen_text_key(b) for b in v1370 if b.get("type") not in ("hook", "cta")]
ok(len(_keys) == len(set(_keys)), "v1370-shaped board: no mid card replayed later")
ok(sum(1 for b in v1370 if b.get("value") == "8") == 1,
   "the 8GB stat renders exactly once")
ok(v1370[0]["dur"] <= storyboard._HOOK_MAX + storyboard._MID_MAX + 1e-6,
   "frame0 hold stays capped while filling with new cards")
ok(craft.video_maker_gate(v1370)["checks"]["B"] == "PASS",
   "v1370-shaped board passes Gate B incl. the repeated-card check")

# Renderer backstop: the LLM itself emits the same card twice / three times
# (double + triple card) or replays a 3-card block → the repeats become
# quote cards of their own spoken window; the gate check then passes.
_rep_words = [{"text": "w%d" % k, "start": round(k * 0.5, 2), "dur": 0.4} for k in range(0, 60)]


def _stat(v, label, cue):
    return {"type": "stat", "value": v, "unit": "GB", "label": label, "cue": cue}


def _timed(beats):
    t = 0.0
    for b in beats:
        b["start"] = round(t, 3)
        b["dur"] = 3.0 if b["type"] != "cta" else 4.0
        t += b["dur"] + storyboard._GAP
    return beats


double = _timed([{"type": "hook", "text": "Hook claim", "cue": "w0"},
                 _stat("8", "KV na placa", "w6"), _stat("8", "KV na placa", "w12"),
                 {"type": "code", "lines": ["x=1"], "cue": "w18"},
                 {"type": "cta", "text": "Subscribe · IA", "cue": "w24"}])
ok(len(craft.repeated_card_hits(double)) == 1, "double card: the gate sees 1 repeat before the fix")
storyboard._break_repeated_cards(double, _rep_words)
ok(craft.repeated_card_hits(double) == [] and double[2]["type"] == "quote",
   "double card: the second copy becomes a quote (different beat type)")
ok(double[2]["text"].startswith("w12") or double[2]["text"].startswith("w13"),
   "double card: the quote carries the words spoken in its own window")

triple = _timed([{"type": "hook", "text": "Hook claim", "cue": "w0"},
                 _stat("0", "teto fatura sem limite", "a"),
                 _stat("0", "teto fatura sem limite", "b"),
                 _stat("0", "teto fatura sem limite", "c"),
                 {"type": "code", "lines": ["x=1"], "cue": "d"},
                 {"type": "cta", "text": "Subscribe · IA", "cue": "e"}])
ok(len(craft.repeated_card_hits(triple)) == 2, "triple card: the gate sees 2 repeats before the fix")
storyboard._break_repeated_cards(triple, _rep_words)
ok(craft.repeated_card_hits(triple) == [], "triple card: no repeat left after the backstop")
ok([b["type"] for b in triple[1:4]] == ["stat", "quote", "quote"],
   "triple card: first stat kept, the two repeats are distinct quotes")

block = _timed([{"type": "hook", "text": "Hook claim", "cue": "w0"},
                _stat("7", "peso na GPU", "a"),
                {"type": "compare", "title": "Peso vs tokens", "cue": "b",
                 "left": {"title": "Peso", "items": ["7GB"]},
                 "right": {"title": "Tokens", "items": ["1GB"]}},
                {"type": "command", "command": "ollama stop", "cue": "c"},
                _stat("7", "peso na GPU", "d"),
                {"type": "compare", "title": "Peso vs tokens", "cue": "e",
                 "left": {"title": "Peso", "items": ["7GB"]},
                 "right": {"title": "Tokens", "items": ["1GB"]}},
                {"type": "command", "command": "ollama stop", "cue": "f"},
                {"type": "cta", "text": "Subscribe · IA", "cue": "g"}])
ok(len(craft.repeated_card_hits(block)) == 3,
   "cards 1–3 replayed as 4–6: the gate sees 3 replays before the fix")
storyboard._break_repeated_cards(block, _rep_words)
ok(craft.repeated_card_hits(block) == [], "cards 1–3 replay: none left after the backstop")
ok([b["type"] for b in block[4:7]] == ["quote", "quote", "quote"],
   "cards 4–6 become quotes of their own spoken windows")

# compose() end to end: LLM draft with a double card → HTML snapshot passes.
def dup_llm(*a, **k):
    return json.dumps({"beats": [
        {"type": "hook", "cue": "alpha bravo", "text": "Hook line"},
        {"type": "stat", "cue": "charlie", "value": "8", "unit": "GB", "label": "KV"},
        {"type": "stat", "cue": "delta", "value": "8", "unit": "GB", "label": "KV"},
        {"type": "compare", "cue": "echo", "left": {"title": "A", "items": ["x"]},
         "right": {"title": "B", "items": ["y"]}},
        {"type": "cta", "cue": "golf hotel", "text": "Try it"},
    ]})


dup_html = _compose(dup_llm)
ok(dup_html is not None, "compose with a double-card draft still renders")
_dup_beats = craft.beats_from_html(dup_html)
ok(craft.repeated_card_hits(_dup_beats) == [],
   "compose output: repeated card broken before build_index_html")
ok(craft.video_maker_gate(_dup_beats)["checks"]["B"] == "PASS",
   "compose output passes Gate B incl. the repeated-card check")

# Fallback composition never repeats a line back-to-back (title == line 1).
_fb = worker._fallback_composition(
    "KV em FP16 estoura a 8GB, não é engenharia.",
    "KV em FP16 estoura a 8GB, não é engenharia. Isso não é engenharia. "
    "Isso não é engenharia. Quantiza o KV.", "portrait", 1080, 1920, 20.0)
_fb_lines = re.findall(r'class="clip seg-(?:title|line)"[^>]*>([^<]*)<', _fb)
_fb_keys = [craft.screen_text_key({"text": x}) for x in _fb_lines]
ok(all(not craft.screen_text_near(a, b) for a, b in zip(_fb_keys, _fb_keys[1:])),
   "fallback composition: no neighbouring segments with the same text")

print("frame0 full claim at full opacity (VM 2026-09-29 P0 a)")


def _words_of(script, step=0.4, t0=0.0):
    out, t = [], t0
    for w in script.split():
        out.append({"text": w.strip(".,!?"), "start": round(t, 3), "dur": step - 0.05})
        t += step
    return out


_os_title = "Chat routed to Maya. Prod paged Lee. · Agent memory 32"
_os_script = ("Chat routed to Maya. Prod paged Lee. The agent kept two memories and "
              "never merged them, so production trusted the stale one. Pin one store. "
              "Subscribe — next Agent memory trap.")
_whole = [{"type": "hook", "text": "Chat routed to Maya", "cue": "Chat routed"}]
storyboard._show_whole_claim(_whole, _os_script, _os_title)
ok(_whole[0]["text"] == "Chat routed to Maya. Prod paged Lee.",
   "two-part title: frame0 shows BOTH halves verbatim (was sentence 1 only on 9/17)")
_same = [{"type": "hook", "text": "Chat routed to Maya", "cue": "c"}]
storyboard._show_whole_claim(_same, "Chat routed to Maya. Something else was said.", _os_title)
ok(_same[0]["text"] == "Chat routed to Maya",
   "whole claim only when the narration opens on the full head (shown == spoken)")
_other = [{"type": "hook", "text": "Different opener", "cue": "c"}]
storyboard._show_whole_claim(_other, _os_script, _os_title)
ok(_other[0]["text"] == "Different opener",
   "never swaps frame0 for a claim the current hook is not a prefix of")
_long_t = " ".join("w%d" % k for k in range(14)) + ". · IA 9"
_long = [{"type": "hook", "text": "w0 w1 w2", "cue": "c"}]
storyboard._show_whole_claim(_long, _long_t.split(" · ")[0] + " rest.", _long_t)
ok(_long[0]["text"] == "w0 w1 w2", "heads over 12 words are left alone (overlay ceiling)")
_rr = [{"type": "hook", "text": "Com ReBAR o 14B fez 48 tok/s", "cue": "Com ReBAR"}]
storyboard._show_whole_claim(
    _rr, "Com ReBAR o 14B fez 48 tok/s. Sem, 11. A janela da VRAM muda tudo.",
    "Com ReBAR o 14B fez 48 tok/s. Sem, 11. · Local 57")
ok(_rr[0]["text"] == "Com ReBAR o 14B fez 48 tok/s. Sem, 11.",
   "RR #1354 shape: payoff 'Sem, 11.' is on the hook card, digits and punctuation kept")
_nohook = [{"type": "stat", "value": "1"}]
storyboard._show_whole_claim(_nohook, _os_script, _os_title)
ok(_nohook == [{"type": "stat", "value": "1"}], "no hook beat → no-op")


def os_llm(*a, **k):
    return json.dumps({"beats": [
        {"type": "hook", "cue": "Chat routed to Maya", "text": "Chat routed to Maya"},
        {"type": "stat", "cue": "two memories", "value": "2", "unit": "", "label": "memories"},
        {"type": "compare", "cue": "production trusted",
         "left": {"title": "Chat", "items": ["Maya"]}, "right": {"title": "Prod", "items": ["Lee"]}},
        {"type": "code", "cue": "Pin one store", "lines": ["memory.pin('one')"]},
        {"type": "cta", "cue": "Subscribe next", "text": "Subscribe"},
    ]})


_os_words = _words_of(_os_script)
_os_html = _compose(os_llm, subject=_os_title, script=_os_script, words=_os_words,
                    duration=round(_os_words[-1]["start"] + 1.0, 2), brand="os",
                    allowed_types=PHASE_A + ["code", "command", "diagram"])
ok(_os_html is not None, "compose renders the two-part OS title")
_b0 = re.search(r'<div class="beat hook" id="b0"[^>]*>', _os_html).group(0)
ok('data-start="0.0"' in _b0 and 'style="opacity:1"' in _b0,
   "frame0 hook card is at full opacity at t=0 (inline, not a fade-in)")
ok("Prod" in _os_html.split('id="b0"', 1)[1].split('id="b1"', 1)[0],
   "compose: the frame0 card carries the payoff half ('Prod paged Lee.')")
ok(not re.search(r"fromTo\('#b0[^']*',\{[^}]*opacity:0", _os_html),
   "no opacity:0 → 1 entrance tween on the first card (t=0 frame is not blank)")
ok(re.search(r"fromTo\('#b0 \.(htext|sc-top)',\{scale:0\.97\}", _os_html) is not None,
   "first card still has motion (scale settle; split-card top on Agent memory) — ≥1 tween")
_later = storyboard.render_hook({"type": "hook", "text": "Later hook", "start": 5.0, "dur": 2.0},
                                {"i": 3, "start": 5.0, "dur": 2.0, "is_last": False})
ok('style="opacity:1"' not in _later[0] and any("opacity:0" in t for t in _later[1]),
   "a hook that does not start at t=0 keeps its entrance (only frame0 is static)")

print("timing: mid card ≤3.0s incl. fade, endcard ≤3.9s — split, never stretch (P0 b)")
_long_script = " ".join("w%d" % k for k in range(110))
_lw = _words_of(_long_script, step=0.4)  # 44s of continuous speech
_long_board = [
    {"type": "hook", "text": "Hook claim here", "cue": "w0 w1"},
    {"type": "stat", "value": "8", "unit": "GB", "label": "KV", "cue": "w8 w9"},
    {"type": "code", "lines": ["x=1"], "cue": "w40 w41"},       # 12.8s cue span
    {"type": "compare", "cue": "w60 w61", "left": {"title": "A", "items": ["a"]},
     "right": {"title": "B", "items": ["b"]}},
    {"type": "term_define", "term": "KV", "definition": "cache", "cue": "w70 w71"},
    {"type": "cta", "text": "Subscribe · IA", "cue": "w100 w101"},
]
storyboard.align_storyboard(_long_board, _lw, 44.0)
storyboard._cap_list_holds(_long_board)
storyboard._cap_endcard(_long_board, 44.0, _lw)
_mids = [b for b in _long_board if b["type"] not in ("hook", "cta")]
ok(all(float(b["dur"]) + storyboard._GAP <= 3.0 + 1e-6 for b in _mids),
   "every mid card ≤3.0s including its fade")
ok(float(_long_board[-1]["dur"]) + storyboard._GAP <= 3.9 + 1e-6
   and _long_board[-1]["type"] == "cta", "endcard ≤3.9s including its fade")
ok(any(b.get("_split") for b in _long_board),
   "long cue spans are SPLIT into new quote cards (not stretched)")
ok(all(b["type"] == "quote" for b in _long_board if b.get("_split")),
   "split pieces are quote cards of the spoken words")
_code = next(b for b in _long_board if b["type"] == "code")
ok(abs(float(_code["start"]) - 16.0) <= 0.4 + 1e-6,
   "the split card stays on its cue (code within one word of w40 = 16.0s; 2.80s cap walk)")
ok(craft.repeated_card_hits(_long_board) == [], "split never repeats a card (#40 gate)")
ok(craft.video_maker_gate(_long_board)["checks"]["B"] == "PASS",
   "split board passes Gate B (timing incl. fade + repeated card)")
ok(storyboard.validate_storyboard(_long_board, 44.0), "split board validates")
_nw_board = [dict(b) for b in [
    {"type": "hook", "text": "H", "cue": "w0"},
    {"type": "stat", "value": "8", "unit": "GB", "label": "KV", "cue": "w8"},
    {"type": "code", "lines": ["x=1"], "cue": "w40"},
    {"type": "cta", "text": "Subscribe · IA", "cue": "w100"},
]]
storyboard._even_space(_nw_board, 30.0)
storyboard._cap_endcard(_nw_board, 30.0, None)
ok(all(float(b["dur"]) + storyboard._GAP <= 3.0 + 1e-6
       for b in _nw_board if b["type"] not in ("hook", "cta"))
   and float(_nw_board[-1]["dur"]) <= storyboard._ENDCARD_MAX + 1e-6,
   "no word timings: caps still hold (surplus walks to the Gate-B-exempt hook)")

# End to end: PULSE RR shape (≈45s, 3.0s holds → 3.12s cards, 4.0 endcard →
# 4.05–4.08s measured). compose output must now sit inside both limits.
_rr_script = ("O prompt processa no CPU por 8 segundos, não é engenharia. Isso não é engenharia. "
              "O CPU autentica, monta o contexto e orquestra a chamada. Quem produz token é a GPU "
              "ou a API que já está quente. Segurar a resposta 8 segundos no CPU é bloqueio "
              "disfarçado de modelo grande, sem ganho nenhum. O usuário não sente profundidade, "
              "sente espera. Profundidade é chegar rápido com a resposta certa e o hardware certo "
              "no lugar certo. Subscribe — next IA trap.")
_rr_words = _words_of(_rr_script, step=0.52)


def rr_llm(*a, **k):
    return json.dumps({"beats": [
        {"type": "hook", "cue": "O prompt processa", "text": "O prompt processa no CPU por 8 segundos"},
        {"type": "stat", "cue": "Isso não é engenharia", "value": "8", "unit": "s", "label": "no CPU"},
        {"type": "list", "cue": "O CPU autentica", "title": "Papel do CPU",
         "items": ["autentica", "monta contexto", "orquestra"]},
        {"type": "compare", "cue": "Quem produz token", "title": "Quem gera o token",
         "left": {"title": "CPU", "items": ["orquestra"]}, "right": {"title": "GPU", "items": ["gera"]}},
        {"type": "command", "cue": "Segurar a resposta", "command": "curl -N /v1/chat"},
        {"type": "term_define", "cue": "O usuário não sente", "term": "Latência", "definition": "espera"},
        {"type": "quote", "cue": "Profundidade é chegar", "text": "Profundidade é chegar rápido"},
        {"type": "cta", "cue": "Subscribe next", "text": "Subscribe"},
    ]})


_rr_dur = round(_rr_words[-1]["start"] + 1.0, 2)
_rr_html = _compose(rr_llm, subject="O prompt processa no CPU por 8 segundos, não é engenharia. · IA 211",
                    script=_rr_script, words=_rr_words, duration=_rr_dur, brand="rr",
                    language="Brazilian Portuguese",
                    allowed_types=PHASE_A + ["code", "command", "diagram"])
ok(_rr_html is not None, "compose renders the RR 45s board")
_rrb = craft.beats_from_html(_rr_html)
ok(all(float(b["dur"]) + storyboard._GAP <= 3.0 + 1e-6
       for b in _rrb if b.get("type") not in ("hook", "cta")),
   "compose RR: every mid card ≤3.0s incl. fade (PULSE: 3.05–3.20s on 14/17)")
ok(float(_rrb[-1]["dur"]) + storyboard._GAP <= 3.9 + 1e-6,
   "compose RR: endcard ≤3.9s incl. fade (PULSE: 4.05–4.08s on 7/7 RR)")
ok(craft.video_maker_gate(_rrb)["checks"]["B"] == "PASS",
   "compose RR output passes Gate B")
ok('style="opacity:1"' in re.search(r'<div class="beat hook" id="b0"[^>]*>', _rr_html).group(0),
   "compose RR: frame0 at full opacity")
ok(not any(craft.contains_subscribe_cta(b.get("text") or "")
           for b in _rrb if b.get("type") == "quote"),
   "split/fill quotes never carry the endcard Subscribe VO")
_sub_w = [{"text": "resposta", "start": 0.0}, {"text": "certa.", "start": 0.3},
          {"text": "Subscribe", "start": 0.6}, {"text": "—", "start": 0.8},
          {"text": "next", "start": 0.9}, {"text": "IA", "start": 1.1}, {"text": "trap.", "start": 1.3}]
ok(not craft.contains_subscribe_cta(storyboard._window_text(_sub_w, 0.0, 2.0)),
   "_window_text strips the Subscribe endcard VO from a quote window")

print("#38 overlay claim + #45 _show_whole_claim compose (frame0 = whole head)")
import copy as _copy
for _title, _script, _want in (
    ("Chat routed to Maya. Prod paged Lee. · Agent memory 32",
     "Chat routed to Maya. Prod paged Lee. The agent kept two memories.",
     "Chat routed to Maya. Prod paged Lee."),
    ("Com ReBAR o 14B fez 48 tok/s. Sem, 11. · Local 57",
     "Com ReBAR o 14B fez 48 tok/s. Sem, 11. A janela da VRAM muda tudo.",
     "Com ReBAR o 14B fez 48 tok/s. Sem, 11."),
    ("O prompt processa no CPU por 8 segundos, não é engenharia. · IA 211",
     "O prompt processa no CPU por 8 segundos, não é engenharia. Isso não é engenharia.",
     "O prompt processa no CPU por 8 segundos, não é engenharia."),
):
    _bb = [{"type": "hook", "text": "x", "cue": "x"},
           {"type": "stat", "value": "1", "cue": "y"},
           {"type": "cta", "text": "c", "cue": "z"}]
    storyboard._lock_opening_hook(_bb, _script, _title)
    ok(_bb[0]["text"] == _want,
       "#38 lock: frame0 = whole title head with . , and digits: %r" % _want)
    _before = _copy.deepcopy(_bb)
    storyboard._show_whole_claim(_bb, _script, _title)
    ok(_bb == _before, "#45 _show_whole_claim is a no-op after the #38 lock: %r" % _want[:30])
_os_html38 = _compose(os_llm, subject=_os_title, script=_os_script, words=_os_words,
                      duration=round(_os_words[-1]["start"] + 1.0, 2), brand="os",
                      allowed_types=PHASE_A + ["code", "command", "diagram"])
_b0_38 = craft.beats_from_html(_os_html38)[0]
ok(_b0_38["type"] == "hook" and _b0_38["text"] == "Chat routed to Maya. Prod paged Lee.",
   "compose (#38 + #45): hook == whole head, both periods kept")
ok('style="opacity:1"' in re.search(r'<div class="beat hook" id="b0"[^>]*>', _os_html38).group(0),
   "compose (#38 + #45): frame0 still at full opacity at t=0")

# --- Designer contrast split-card on frame0 (2026-09-29 council, P0) --------
_b0s = re.search(r'<div class="beat hook" id="b0"[^>]*>', _os_html38).group(0)
_seg0 = _os_html38.split('id="b0"', 1)[1].split('class="beat ', 1)[0]
ok('data-split="1"' in _b0s and 'style="opacity:1"' in _b0s,
   "split: Agent memory frame0 is the split card at full opacity at t=0 (#45 kept)")
ok('class="sc sc-top"' in _seg0 and 'class="sc sc-bot"' in _seg0 and "✕" in _seg0,
   "split: top card + bottom card with the ✕ stamp on frame0")
ok(">Chat<" in _seg0 and "routed to Maya." in _seg0 and ">Prod<" in _seg0 and "paged Lee." in _seg0,
   "split: whole claim on frame0, periods kept (Chat/Prod as card labels)")
ok('class="hobj"' not in _seg0, "split: no first-word object chip on the split card")
ok("tl.set('#brand-mark',{opacity:0},0)" in _os_html38,
   "split: OS O ring hidden while the split card shows, restored after")
_os_prov = _compose(os_llm, subject=_os_title, script=_os_script, words=_os_words,
                    duration=round(_os_words[-1]["start"] + 1.0, 2), brand="os",
                    allowed_types=PHASE_A + ["code", "command", "diagram"],
                    provided_thumb=True)
_b0p = re.search(r'<div class="beat hook" id="b0"[^>]*>', _os_prov).group(0)
ok('data-split' not in _b0p and 'style="opacity:1"' in _b0p,
   "split: never with a provided thumbnail (thumb_source=provided) — normal hook card, opacity 1")
ok(craft.beats_from_html(_os_prov)[0]["text"] == "Chat routed to Maya. Prod paged Lee.",
   "split off (provided thumb): whole claim still on frame0")
_rr_split = _rr_html.split('id="b0"', 1)[1].split('class="beat ', 1)[0]
ok('class="sc sc-top"' in _rr_split and "O prompt processa no CPU por 8 segundos," in _rr_split
   and "não é engenharia." in _rr_split,
   "split: IA one-sentence head splits at the verdict, comma and digit kept")
# Shipping series never gets the split card (series outside Agent memory / IA / Local).
_sh = [{"type": "hook", "text": "Chat routed to Maya. Prod paged Lee.", "cue": "c"}]
storyboard._apply_split_card(_sh, "Chat routed to Maya. Prod paged Lee. · Shipping 4")
ok("split" not in _sh[0], "split: Shipping series keeps the normal hook card")
# Split kept only when it shows exactly the hook claim.
_mis = [{"type": "hook", "text": "Chat routed to Maya.", "cue": "c"}]
storyboard._apply_split_card(_mis, _os_title)
ok("split" not in _mis[0], "split: hook claim != title head → no split (whole-claim rule)")
_eq = [{"type": "hook", "text": "Chat routed to Maya. Prod paged Lee.", "cue": "c"}]
storyboard._apply_split_card(_eq, _os_title, provided_thumb=True)
ok("split" not in _eq[0], "split: _apply_split_card honours provided_thumb=True")
storyboard._apply_split_card(_eq, _os_title)
ok(_eq[0].get("split", {}).get("head") == "Chat routed to Maya. Prod paged Lee.",
   "split: _apply_split_card sets the spec when claim == head")


# --- RR hook pace (P1 d): first cut by 2.5s on RR, claim ≤8 / spoken by 3.0s ---
print("RR hook pace: first cut by 2.5s (P1 d)")
_rrb0 = craft.beats_from_html(_rr_html)
ok(float(_rrb0[1]["start"]) <= 2.5 + 1e-6,
   "compose RR (brand rr): first cut at %.2fs ≤ 2.5s" % float(_rrb0[1]["start"]))
ok(_rrb0[0]["text"] == "O prompt processa no CPU por 8 segundos, não é engenharia."
   and 'style="opacity:1"' in re.search(r'<div class="beat hook" id="b0"[^>]*>', _rr_html).group(0),
   "compose RR: frame0 still carries the whole claim at full opacity (#45 kept)")
ok(craft.repeated_card_hits(_rrb0) == [] and all(
       float(b["dur"]) + storyboard._GAP <= 3.0 + 1e-6
       for b in _rrb0 if b.get("type") not in ("hook", "cta")),
   "compose RR after the cut: no repeated card, every mid ≤3.0s incl. fade")
_pace = craft.hook_pace_marker(_rrb0, _rr_words, "rr")
ok(_pace["version"] == craft.HOOK_PACE_V1 and _pace["claim_words"] == 11,
   "hook_pace marker on the RR board (11-word claim)")
_g = craft.video_maker_gate(_rrb0, hook_pace=_pace)
ok(_g["checks"]["B"] == "FAIL" and any("claim 11 words" in r for r in _g["reasons"])
   and not any("first cut at" in r for r in _g["reasons"]),
   "RR 11-word claim (#1376 title) → Gate B FAIL on the claim, not on the first cut (compose fixed it)")
_os_b = craft.beats_from_html(_os_html38)
ok(craft.hook_pace_marker(_os_b, _os_words, "os") is None,
   "OS (brand os) is out of hook-pace scope (no marker, no check)")


def _pace_board(hook_end, n_mid=4, dur=24.0, slack=True):
    # Cue-aligned board: card 3 holds 2.0s (slack), the rest sit at the cap;
    # the endcard fills the tail up to `dur`.
    b = [{"type": "hook", "start": 0.0, "dur": round(hook_end - storyboard._GAP, 3),
          "text": "Claim one two three four", "cue": "Claim"}]
    t = hook_end
    for k in range(n_mid):
        hold = 2.0 if (slack and k == 2) else 2.80  # mid cap (RR 2026-09-30)
        b.append({"type": "stat", "start": round(t, 3), "dur": hold, "value": str(k + 1),
                  "unit": "GB", "label": "card %d" % k, "cue": "c%d" % k})
        t += hold + storyboard._GAP
    b.append({"type": "cta", "start": round(t, 3), "dur": round(dur - t, 3),
              "text": "Subscribe · IA", "cue": "Subscribe"})
    return b


_pw = _words_of("Claim one two three four " + " ".join("w%d" % k for k in range(80)), step=0.3)
_pb = _pace_board(3.3, dur=3.3 + 3 * 2.92 + 2.12 + 3.6)
_pbd = 3.3 + 3 * 2.92 + 2.12 + 3.6
storyboard._pull_first_cut(_pb, _pbd, _pw)
ok(abs(float(_pb[1]["start"]) - 2.5) < 1e-6 and abs(float(_pb[0]["dur"]) - 2.38) < 1e-6,
   "_pull_first_cut: hook ends at the cut, first card starts at 2.5s")
ok(all(float(b["dur"]) <= storyboard._MID_MAX + 1e-6 for b in _pb[1:-1])
   and float(_pb[-1]["dur"]) <= storyboard._ENDCARD_MAX + 1e-6,
   "_pull_first_cut: short window → first card leads the VO; caps kept (split, never stretch)")
ok(storyboard.validate_storyboard(_pb, _pbd), "_pull_first_cut: board still valid")
ok([b.get("value") for b in _pb[1:-1]] == ["1", "2", "3", "4"], "_pull_first_cut: no card dropped or copied")
_pb2 = _pace_board(4.76, dur=4.76 + 3 * 2.92 + 2.12 + 3.6)
_pb2d = 4.76 + 3 * 2.92 + 2.12 + 3.6
storyboard._pull_first_cut(_pb2, _pb2d, _pw)
ok(_pb2[1].get("type") == "quote" and _pb2[1].get("_cut") and abs(float(_pb2[1]["start"]) - 2.5) < 1e-6,
   "_pull_first_cut: long window (#1371 shape, hook 4.64s) → NEW quote card of the words spoken there")
ok(not (set(craft.screen_text_key(_pb2[1]).split()) <= set(craft.screen_text_key(_pb2[0]).split())),
   "_pull_first_cut: the quote is not a hook-claim echo")
ok(craft.repeated_card_hits(_pb2) == [] and storyboard.validate_storyboard(_pb2, _pb2d),
   "_pull_first_cut: quote insert leaves no repeated card, board valid")
_pb3 = _pace_board(2.4)
_before3 = json.dumps(_pb3)
storyboard._pull_first_cut(_pb3, 24.0, _pw)
ok(json.dumps(_pb3) == _before3, "_pull_first_cut: first cut already ≤2.5s → no-op")
_pb4 = _pace_board(3.3, dur=3.3 + 4 * 2.92 + 3.78, slack=False)
_before4 = json.dumps(_pb4)
storyboard._pull_first_cut(_pb4, 3.3 + 4 * 2.92 + 3.78, [])
ok(json.dumps(_pb4) == _before4,
   "_pull_first_cut: every card at its cap and endcard full → restored (Gate B reports it)")

# --- Series chip from the real topic (P1 e) ---------------------------------
print("series chip from the real topic (P1 e: Shipping #1308/#1311)")
_ship_html = _compose(happy_llm, subject="You still can't tell if it works.", topic_name="Shipping")
_ship_cta = [b for b in craft.beats_from_html(_ship_html) if b.get("type") == "cta"]
ok(_ship_cta and _ship_cta[-1].get("text") == "Subscribe · Shipping",
   "compose: unsuffixed subject + no brand + topic 'Shipping' → chip 'Subscribe · Shipping' (was Copilot Credits)")
_def_html = _compose(happy_llm, subject="You still can't tell if it works.")
_def_cta = [b for b in craft.beats_from_html(_def_html) if b.get("type") == "cta"]
ok(_def_cta and _def_cta[-1].get("text") == "Subscribe · Copilot Credits",
   "compose without topic/brand keeps the old fallback (no silent change)")
_os_ch = _compose(happy_llm, subject="You still can't tell if it works.", channel_slug="owera-os",
                  channel_id=1)
_os_cta = [b for b in craft.beats_from_html(_os_ch) if b.get("type") == "cta"]
ok(_os_cta and _os_cta[-1].get("text") == "Subscribe · Agent memory",
   "compose: brand resolved from the channel is used for the chip when brand= is not passed")
_sx = [{"type": "cta", "text": "old", "cue": "Subscribe"}]
storyboard._sanitize_cta(_sx, "A. Subscribe — next Shipping trap.", subject="No suffix here",
                         brand="os", topic_name="Shipping")
ok(_sx[0]["text"] == "Subscribe · Shipping", "_sanitize_cta: topic beats the OS brand default")

# --- RR batch 29/09 Gate B (P0 2026-09-30): sentence-bounded cards ----------
print("RR P0 2026-09-30: cards end at sentence boundaries, no CTA leak, holds ≤2.80")
# Live #1380/#1381/#1382/#1383/#1386 (script, LLM board without the synthetic
# quotes, duration) — the per-sentence split leaked "next", fragments and cut clauses.
_RR2909 = {'1380': {'title': 'Contexto 32k come a VRAM. · IA 212',
          'script': 'Contexto 32k come a VRAM. O peso do modelo é só parte da conta. O cache KV '
                    'cresce com cada token parado na conversa e ocupa memória na placa. De 8k para '
                    '32k, esse cache quadruplica. Quantizar o peso emagrece o arquivo. O cache '
                    'segue em 16 bits no padrão local. A placa aguenta o modelo e estoura na '
                    'janela. Marque o contexto que a VRAM paga. Documento gigante sem uso é '
                    'desperdício: corte, busque o trecho, gere de novo. Mede a VRAM antes do '
                    'slider e a geração chega inteira. Subscribe — next IA trap.',
          'duration': 46.8,
          'beats': [{'type': 'hook',
                     'cue': 'Contexto 32k come a VRAM',
                     'text': 'Contexto 32k come a VRAM.',
                     'object': 'GPU',
                     'emoji': ''},
                    {'type': 'code',
                     'cue': 'O peso do modelo',
                     'emoji': '',
                     'lines': ['vram:', '  pesos: arquivo', '  kv: n_tokens']},
                    {'type': 'term_define',
                     'cue': 'cresce com cada token',
                     'term': 'Cache KV',
                     'definition': 'memória dos tokens parados na conversa'},
                    {'type': 'stat',
                     'cue': 'De 8k para 32k',
                     'emoji': '',
                     'value': '4',
                     'unit': '×',
                     'label': 'o cache de 8k'},
                    {'type': 'compare',
                     'cue': 'Quantizar o peso',
                     'title': 'Na placa',
                     'left': {'title': 'Peso', 'items': ['quantizado', 'arquivo encolhe']},
                     'right': {'title': 'Cache KV', 'items': ['segue 16 bits', 'padrão local']}},
                    {'type': 'compare',
                     'cue': 'A placa aguenta o modelo',
                     'title': 'Onde estoura',
                     'left': {'title': 'Modelo', 'items': ['cabe na placa']},
                     'right': {'title': 'Janela', 'items': ['estoura a VRAM']}},
                    {'type': 'term_define',
                     'cue': 'Marque o contexto',
                     'term': 'Contexto pago',
                     'definition': 'janela de tokens que a VRAM banca'},
                    {'type': 'compare',
                     'cue': 'Documento gigante sem uso',
                     'title': 'Desperdício',
                     'left': {'title': 'Doc gigante', 'items': ['sem uso', 'come a VRAM']},
                     'right': {'title': 'No lugar',
                               'items': ['corte', 'busque o trecho', 'gere de novo']}},
                    {'type': 'quote',
                     'cue': 'Mede a VRAM antes',
                     'text': 'Mede a VRAM antes do slider',
                     'emoji': '',
                     'attribution': ''},
                    {'type': 'cta',
                     'cue': 'Subscribe — next',
                     'text': 'Subscribe · IA',
                     'sub': 'same series'}]},
 '1381': {'title': 'Ollama sem CUDA não é engenharia. · IA 213',
          'script': 'Ollama sem CUDA não é engenharia. O pull terminou e você chamou isso de '
                    'stack. Sem CUDA o runtime cai na CPU: contexto curto, RAM comida pelo KV '
                    'cache e token pingando. Modelo de sete bilhões já arrasta. O grande não fecha '
                    'a frase. Placa NVIDIA na máquina e log sem CUDA é a mesma falha: GPU parada, '
                    'latência de brinquedo. Quem constrói mede token por segundo, manda camada pra '
                    'GPU e sabe o teto de VRAM antes de prometer resposta. Sem CUDA no log, você '
                    'não tem stack. Subscribe — next IA trap.',
          'duration': 44.54,
          'beats': [{'type': 'hook',
                     'cue': 'Ollama sem CUDA não é engenharia',
                     'text': 'Ollama sem CUDA não é engenharia.',
                     'object': 'OLLAMA',
                     'emoji': ''},
                    {'type': 'command',
                     'cue': 'O pull terminou',
                     'emoji': '',
                     'command': 'ollama ps',
                     'output': ['qwen2.5:7b   4.7 GB',
                                'PROCESSOR    100% CPU',
                                'CUDA         off']},
                    {'type': 'term_define',
                     'cue': 'Sem CUDA o runtime',
                     'term': 'Runtime CPU',
                     'definition': 'inferência sem GPU, presa na RAM'},
                    {'type': 'term_define',
                     'cue': 'RAM comida pelo',
                     'term': 'KV cache',
                     'definition': 'memória do contexto que devora a RAM'},
                    {'type': 'stat',
                     'cue': 'Modelo de sete bilhões',
                     'emoji': '',
                     'value': '7',
                     'unit': 'B',
                     'label': 'trava na frase'},
                    {'type': 'compare',
                     'cue': 'Placa NVIDIA na',
                     'title': 'Máquina versus log',
                     'left': {'title': 'Máquina', 'items': ['NVIDIA presente']},
                     'right': {'title': 'Log', 'items': ['CUDA ausente']}},
                    {'type': 'stat',
                     'cue': 'é a mesma falha',
                     'emoji': '',
                     'value': '0',
                     'unit': '%',
                     'label': 'uso da GPU'},
                    {'type': 'compare',
                     'cue': 'Quem constrói mede',
                     'title': 'Quem constrói',
                     'left': {'title': 'Mede', 'items': ['tok/s real']},
                     'right': {'title': 'Manda', 'items': ['camada na GPU']}},
                    {'type': 'term_define',
                     'cue': 'e sabe o teto',
                     'term': 'VRAM',
                     'definition': 'teto da GPU antes de prometer resposta'},
                    {'type': 'term_define',
                     'cue': 'Sem CUDA no log',
                     'term': 'Stack',
                     'definition': 'CUDA visível no log, não o pull'},
                    {'type': 'cta',
                     'cue': 'Subscribe — next',
                     'text': 'Subscribe · IA',
                     'sub': 'same series'}]},
 '1382': {'title': 'n_batch alto estoura a prefill. · IA 214',
          'script': 'n_batch alto estoura a prefill. n_batch é quantos tokens do prompt entram '
                    'numa passada. Passou da VRAM, a prefill aloca tudo, dispara e morre. O '
                    'estouro é memória de ativação na passada. O contexto mora no n_ctx. Corta o '
                    'n_batch e o n_ubatch junto, até a passada caber na placa. Testa a prefill com '
                    'o prompt longo de verdade. Você troca um pouco de velocidade no prompt pelo '
                    'modelo no ar. A prefill manda. O n_batch obedece. Subscribe — next IA trap.',
          'duration': 44.66,
          'beats': [{'type': 'hook',
                     'cue': 'n_batch alto estoura a prefill',
                     'text': 'n_batch alto estoura a prefill.',
                     'object': 'BATCH',
                     'emoji': ''},
                    {'type': 'stat',
                     'cue': 'n_batch é quantos tokens',
                     'emoji': '',
                     'value': '2048',
                     'unit': 'tokens',
                     'label': 'exemplo numa passada'},
                    {'type': 'compare',
                     'cue': 'Passou da VRAM',
                     'title': 'A prefill',
                     'left': {'title': 'Cabe', 'items': ['aloca a passada', 'segue viva']},
                     'right': {'title': 'Estoura', 'items': ['aloca tudo', 'dispara e morre']}},
                    {'type': 'term_define',
                     'cue': 'O estouro é memória',
                     'term': 'Memória de ativação',
                     'definition': 'a memória que a passada aloca na placa'},
                    {'type': 'term_define',
                     'cue': 'O contexto mora',
                     'term': 'n_ctx',
                     'definition': 'onde o contexto mora, não a passada'},
                    {'type': 'code',
                     'cue': 'Corta o n_batch e',
                     'lines': ['# corta os dois', '--batch-size 512', '--ubatch-size 512']},
                    {'type': 'term_define',
                     'cue': 'Testa a prefill com',
                     'term': 'Prompt longo',
                     'definition': 'o tamanho real que o modelo vai servir'},
                    {'type': 'compare',
                     'cue': 'Você troca um pouco',
                     'title': 'A troca',
                     'left': {'title': 'Velocidade',
                              'items': ['prompt mais rápido', 'risco de estouro']},
                     'right': {'title': 'No ar', 'items': ['batch menor', 'modelo estável']}},
                    {'type': 'quote',
                     'cue': 'A prefill manda',
                     'text': 'A prefill manda. O n_batch obedece.',
                     'emoji': '',
                     'attribution': ''},
                    {'type': 'cta',
                     'cue': 'Subscribe — next IA',
                     'text': 'Subscribe · IA',
                     'sub': 'same series'}]},
 '1383': {'title': 'num_gpu 0 é só CPU. · IA 215',
          'script': 'num_gpu 0 é só CPU. Zero é nenhuma camada na GPU. O modelo inteiro roda na '
                    'CPU, token por token, com a placa ociosa. O índice da GPU é main_gpu. O '
                    'padrão do Ollama descarrega sozinho o que cabe na VRAM. Zero no Modelfile ou '
                    'na API é CPU de propósito. Sobe as camadas até a VRAM segurar o modelo. Quem '
                    'conta camada manda na velocidade. Subscribe — next IA trap.',
          'duration': 37.58,
          'beats': [{'type': 'hook',
                     'cue': 'num_gpu 0 é só CPU',
                     'text': 'num_gpu 0 é só CPU.',
                     'object': 'NUM',
                     'emoji': ''},
                    {'type': 'stat',
                     'cue': 'Zero é nenhuma camada',
                     'emoji': '',
                     'value': '0',
                     'unit': 'camadas',
                     'label': 'na GPU'},
                    {'type': 'stat',
                     'cue': 'modelo inteiro roda',
                     'emoji': '',
                     'value': '100',
                     'unit': '%',
                     'label': 'modelo na CPU'},
                    {'type': 'compare',
                     'cue': 'token, com a placa',
                     'title': 'Quem trabalha',
                     'left': {'title': 'CPU', 'items': ['token por token']},
                     'right': {'title': 'GPU', 'items': ['placa ociosa']}},
                    {'type': 'term_define',
                     'cue': 'da GPU é main_gpu',
                     'term': 'main_gpu',
                     'definition': 'índice da GPU que recebe as camadas'},
                    {'type': 'compare',
                     'cue': 'Ollama descarrega sozinho',
                     'title': 'Padrão Ollama',
                     'left': {'title': 'Cabe na VRAM', 'items': ['sobe pra GPU']},
                     'right': {'title': 'Não cabe', 'items': ['fica na CPU']}},
                    {'type': 'code',
                     'cue': 'Zero no Modelfile',
                     'lines': ['PARAMETER num_gpu 0', 'options.num_gpu: 0']},
                    {'type': 'compare',
                     'cue': 'CPU de propósito',
                     'title': 'No controle',
                     'left': {'title': 'num_gpu 0', 'items': ['força a CPU']},
                     'right': {'title': 'Sobe camadas', 'items': ['devolve a GPU']}},
                    {'type': 'term_define',
                     'cue': 'até a VRAM segurar',
                     'term': 'Teto de VRAM',
                     'definition': 'máximo de camadas que a memória segura'},
                    {'type': 'quote',
                     'cue': 'Quem conta camada',
                     'text': 'Quem conta camada manda na velocidade',
                     'emoji': '',
                     'attribution': ''},
                    {'type': 'cta',
                     'cue': 'Subscribe — next',
                     'text': 'Subscribe · IA',
                     'sub': 'same series'}]},
 '1386': {'title': 'PDF escaneado não é engenharia. · IA 217',
          'script': 'PDF escaneado não é engenharia. Página escaneada é foto. Não tem texto '
                    'dentro. Você copia e vem vazio. Ou vem um OCR torto, com número trocado. E o '
                    'modelo responde em cima disso com a mesma confiança. Eu testo antes, com '
                    'pdftotext na página. Voltou vazio, a página vai como imagem pro modelo de '
                    'visão. Voltou texto limpo, vai texto. Primeiro você descobre o que o arquivo '
                    'é. Subscribe — next IA trap.',
          'duration': 39.07,
          'beats': [{'type': 'hook',
                     'cue': 'PDF escaneado não é engenharia',
                     'text': 'PDF escaneado não é engenharia.',
                     'object': 'ESCANEADO',
                     'emoji': ''},
                    {'type': 'stat',
                     'cue': 'Página escaneada é foto',
                     'emoji': '',
                     'value': '0',
                     'unit': 'letras',
                     'label': 'texto no PDF'},
                    {'type': 'compare',
                     'cue': 'Você copia e vem vazio',
                     'title': 'Copiar a página',
                     'left': {'title': 'Copia', 'items': ['seleciona a foto']},
                     'right': {'title': 'Cola', 'items': ['campo vazio']}},
                    {'type': 'compare',
                     'cue': 'Ou vem um OCR',
                     'title': 'Número trocado',
                     'left': {'title': 'No papel', 'items': ['NF 1042']},
                     'right': {'title': 'No OCR', 'items': ['NF 1842']}},
                    {'type': 'compare',
                     'cue': 'E o modelo responde',
                     'title': 'Sem checagem',
                     'left': {'title': 'Entrada', 'items': ['OCR torto']},
                     'right': {'title': 'Saída', 'items': ['resposta firme']}},
                    {'type': 'stat',
                     'cue': 'com a mesma',
                     'emoji': '',
                     'value': '100',
                     'unit': '%',
                     'label': 'confiança no erro'},
                    {'type': 'command',
                     'cue': 'Eu testo antes',
                     'command': 'pdftotext pagina.pdf -',
                     'output': ['(vazio)']},
                    {'type': 'compare',
                     'cue': 'Voltou vazio',
                     'title': 'O teste decide',
                     'left': {'title': 'Vazio', 'items': ['vai a imagem', 'modelo de visão']},
                     'right': {'title': 'Limpo', 'items': ['vai o texto']}},
                    {'type': 'term_define',
                     'cue': 'Voltou texto limpo',
                     'term': 'Texto limpo',
                     'definition': 'pdftotext achou texto de verdade'},
                    {'type': 'quote',
                     'cue': 'Primeiro você descobre',
                     'text': 'Primeiro você descobre o que o arquivo é',
                     'emoji': '',
                     'attribution': ''},
                    {'type': 'cta',
                     'cue': 'Subscribe — next',
                     'text': 'Subscribe · IA',
                     'sub': 'same series'}]}}

_DANGLE = {"de", "do", "da", "a", "o", "em", "no", "na", "por", "pra", "para", "com", "e", "ou",
           "que", "isso", "pelo", "pela", "nao"}


def _sent_list(script):
    return [x for x in re.split(r"(?<=[.!?])\s+", script.strip()) if x]


def _card_ok(text, script):
    """None when the card text respects sentence bounds, else why not."""
    sents = [s_ for s_ in _sent_list(script) if "subscribe" not in theme.fold(s_)]
    stream = []  # (token, sentence id, first?, last?)
    for sid, s_ in enumerate(sents):
        tt = storyboard._tok(s_)
        stream += [(t, sid, k == 0, k == len(tt) - 1) for k, t in enumerate(tt)]
    ct = storyboard._tok(text)
    toks = [x[0] for x in stream]
    for j in range(len(toks) - len(ct) + 1):
        if toks[j:j + len(ct)] == ct:
            run = stream[j:j + len(ct)]
            sids = {x[1] for x in run}
            if len(sids) > 1 and not (run[0][2] and run[-1][3]):
                return "spans sentences partially (%r)" % text
            if not run[-1][3] and theme.fold(text.split()[-1]).strip(".,;:!?") in _DANGLE:
                return "ends on a function word (%r)" % text
            nxt = stream[j + len(ct)] if j + len(ct) < len(stream) else None
            if nxt and not run[-1][3] and nxt[0] in ("de", "do", "da", "dos", "das"):
                return "cuts a noun phrase (%r | %s)" % (text, nxt[0])
            return None
    return "not a contiguous script run (%r)" % text


# (1)+(2)+(3) units: whole sentences / clause segments, never the endcard line.
_sc80 = _RR2909["1380"]["script"]
_aw80 = storyboard.annotate_sentences(_words_of(_sc80, step=0.46), _sc80)
_u80 = storyboard._speech_units(_aw80)
ok(_u80 and not any("subscribe" in theme.fold(u["text"]) or theme.fold(u["text"]).startswith("next")
                    for u in _u80),
   "speech units never include the 'Subscribe — next IA trap.' endcard line (item 1)")
ok(all(_card_ok(u["text"], _sc80) is None for u in _u80),
   "speech units stay inside one sentence and never end on a function word (items 2/3): %s"
   % [_card_ok(u["text"], _sc80) for u in _u80 if _card_ok(u["text"], _sc80)])
ok("no padrão local." in [u["text"] for u in _u80] or any(u["text"].endswith("no padrão local.") for u in _u80),
   "#1380: 'O cache segue em 16 bits no padrão local.' ends at its period (never '…local A')")
_sc81 = _RR2909["1381"]["script"]
_aw81 = storyboard.annotate_sentences(_words_of(_sc81, step=0.46), _sc81)
_wt81 = storyboard._window_text(_aw81, 0.46 * 5, 0.46 * 5 + 2.92)
ok(_wt81 == "O pull terminou e você chamou isso de stack.",
   "#1381: window text is the whole sentence, not 'O pull terminou e você chamou isso de' (item 3): %r" % _wt81)
ok(storyboard._split_point("O pull terminou e você chamou isso de stack.".split()) == 3,
   "_split_point: 'O pull terminou | e você chamou isso de stack.' (conjunction seam, never after 'de')")
ok(storyboard._split_point("e sabe o teto de VRAM antes".split()) != 4,
   "_split_point never starts a segment on a binding 'de' ('teto | de VRAM')")
_uw = _words_of("Mede a VRAM antes do slider e a geração chega inteira. Subscribe — next IA trap.", step=0.4)
ok("next" not in storyboard._window_text(_uw, 4.0, 6.4).split(),
   "unannotated words: the window text stops at 'Subscribe' (no 'next' card)")
ok(storyboard._clashes("a prefill manda o n batch obedece", "obedece")
   and storyboard._clashes("no padrao local", "o cache segue em 16 bits no padrao local"),
   "_clashes: a fragment of the neighbour counts as a clash (item 7)")

# Regression: compose the live boards at the live pace and at a fast pace.
for _vid in ("1380", "1381", "1382", "1383", "1386"):
    _f = _RR2909[_vid]
    _n = len(_f["script"].split())
    for _step, _dur in ((round((_f["duration"] - 1.2) / _n, 3), _f["duration"]),
                        (0.36, round(0.36 * _n + 1.2, 2))):
        _ww = _words_of(_f["script"], step=_step)
        _hh = _compose(lambda *a, _b=_f["beats"], **k: json.dumps({"beats": _b}), subject=_f["title"],
                       script=_f["script"], words=_ww, duration=_dur, brand="rr",
                       language="Brazilian Portuguese", allowed_types=PHASE_A + ["code", "command", "diagram"])
        _bb = craft.beats_from_html(_hh)
        _tag = "#%s @%.2fs/word" % (_vid, _step)
        _mids = _bb[1:-1]
        _txt = [(b.get("type"), b.get("text") or "") for b in _mids]
        ok(not any(theme.fold(t).strip(" .") in ("next", "subscribe") or "subscribe" in theme.fold(t)
                   or theme.fold(t).startswith("next") for _, t in _txt),
           "%s: no 'next'/Subscribe card before the endcard (item 1)" % _tag)
        _bad = [_card_ok(t, _f["script"]) for ty, t in _txt if ty == "quote"]
        _bad = [x for x in _bad if x]
        ok(not _bad, "%s: every quote card ends at a sentence/clause boundary (items 2/3): %s" % (_tag, _bad))
        ok(all(float(b["dur"]) <= craft.MID_BEAT_MAX_S + 1e-6 for b in _mids),
           "%s: every mid hold ≤2.80s (item 6): %s" % (_tag, max(float(b["dur"]) for b in _mids)))
        ok(craft.repeated_card_hits(_bb) == [],
           "%s: no repeated / fragment-of-neighbour card (item 7): %s" % (_tag, craft.repeated_card_hits(_bb)))
        _pc = craft.hook_pace_marker(_bb, _ww, "rr", "short")
        _gg = craft.video_maker_gate(_bb, hook_pace=_pc, beat_timing=craft.BEAT_TIMING_CURRENT)
        ok(_gg["checks"]["B"] == "PASS" and float(_bb[1]["start"]) <= craft.HOOK_FIRST_CUT_BY_S + 1e-6,
           "%s: Gate B PASS (card_hold_280), first cut ≤2.5s: %s" % (_tag, _gg["reasons"]))
        ok(storyboard.validate_storyboard(_bb, _dur), "%s: board valid" % _tag)
        _sy = craft.card_sync_marker(_bb, _ww)
        ok(not craft.card_sync_hits(_sy)
           and all(n["status"] in ("ok", "late_capped") for n in _sy["notes"])
           and all(n["lead"] is None or n["lead"] >= -craft.SYNC_TOL_S for n in _sy["notes"]
                   if n["status"] != "late_capped")
           and max(n["lead"] for n in _sy["notes"] if n["lead"] is not None) <= craft.SYNC_LEAD_MAX_S + craft.SYNC_TOL_S,
           "%s: every card in sync — never after its speech, lead ≤1.1s (CoS): %s"
           % (_tag, [(n["i"], n["status"], n["lead"]) for n in _sy["notes"] if n["status"] != "ok"]))
        ok(not any(t == "obedece" for _, t in _txt), "%s: no lone 'obedece' card (#1382)" % _tag)

# _sync_board: a late card is moved to lead its speech; a gap gets a sentence card.
_sy_script = "Um dois três. O peso do modelo é só parte da conta. O cache cresce. Subscribe — next IA trap."
_sy_words = storyboard.annotate_sentences(_words_of(_sy_script, step=0.5), _sy_script)
_sy_b = [{"type": "hook", "start": 0.0, "dur": 2.38, "text": "Um dois três.", "cue": "Um dois"},
         {"type": "stat", "start": 2.5, "dur": 2.8, "value": "1", "unit": "x", "label": "peso", "cue": "O peso"},
         {"type": "compare", "start": 5.42, "dur": 1.3, "cue": "O cache cresce", "title": "Cache",
          "left": {"title": "a", "items": ["b"]}, "right": {"title": "c", "items": ["d"]}},
         {"type": "cta", "start": 6.84, "dur": 3.0, "text": "Subscribe · IA", "cue": "Subscribe"}]
_sy_d = round(_sy_words[-1]["start"] + 1.0, 2)
ok(any(n["status"] == "late" for n in craft.card_sync_notes(_sy_b, _sy_words)),
   "_sync_board fixture: the compare starts 0.9s after 'O cache cresce' is spoken")
ok(storyboard._sync_board(_sy_b, _sy_words, _sy_d, 2.38)
   and not craft.card_sync_hits(craft.card_sync_marker(_sy_b, _sy_words))
   and storyboard.validate_storyboard(_sy_b, _sy_d),
   "_sync_board re-times the board: no card after its speech, lead ≤1.1s: %s"
   % [(b["type"], b["start"], b["dur"]) for b in _sy_b])
ok(all(float(b["dur"]) <= craft.MID_BEAT_MAX_S + 1e-6 for b in _sy_b[1:-1])
   and any(b.get("type") == "quote" for b in _sy_b),
   "_sync_board: the 1.8s hole between cards becomes a NEW sentence card (holds stay ≤2.80)")

print(f"\nALL {_checks} CHECKS PASSED")
