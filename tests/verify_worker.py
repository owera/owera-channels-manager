"""Dependency-free regression checks for app/services/engines/worker.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_worker.py

``worker`` is the HyperFrames render pipeline that run_job executes on a daemon
thread: script → TTS → composition → silent render → blank-frame guard → mux.
A silent break here ships a blank MP4 (the dea9405 incident), an English
script on a PT voice (2026-07-07), or a composition the CLI cannot render.
Previously only ``_has_visible_frames``, ``_looks_valid``, and
``_creation_config`` had a few pins in ``verify_storyboard.py``; the rest of
the 879-line module had zero direct coverage.

Covers, dependency-free (no network, no HyperFrames CLI, no live ffmpeg/TTS/LLM):
  - module contracts: aspects, accent==theme.PALETTE, _esc is theme.esc,
    five templates, _RENDER_TIMEOUT, gsap asset
  - ``_word_count_bounds``: short band vs long target floor/cap + 0.65/1.35
  - ``_generate_script`` (``_llm`` stubbed): short vs long prompts, spoken-CTA
    + forbidden-opener pins, HARD RULE language, quote-strip, word-count retry
    (empty retry keeps original)
  - ``_pick_template``: deterministic subject-hash, name+accent from tables
  - ``_clips_from_json``: fence/prose unwrap, <3/missing-key/bad-json → None,
    text/w/emoji clamps
  - ``_validate_clips``: overlap tolerance, short window, start/end bounds
  - ``_assemble_composition``: placeholder injection, HTML-escape, w=1/2/3
    styles, emoji slot, every template ``_looks_valid``
  - ``_looks_valid``: missing master/tween/html, truncated timeline
  - ``_fallback_composition`` / ``_key_lines``: guaranteed-valid HTML, 7-word
    clip + ellipsis, empty → sentinel
  - ``_voice``: default, -Male/-Female strip, already-bare, case-sensitive
  - ``_creation_config``: never-raises, beat scrape, bgm name, volume-0 pin
  - ``_pick_bgm``: explicit-off, missing dir, techno_* preferred, named file,
    handle-hash pick
  - ``_probe_duration`` / ``_has_visible_frames`` (subprocess stubbed):
    parse/fail, dur<=0 does not block, uniform vs varied 32x32 gray
  - ``_render`` / ``_mux`` command contracts
  - ``_generate_composition`` version switch + generic compose-exception → ""
    (GrokCLIError re-raises so the render loop can retry)
  - ``run_job``: happy path, unknown-aspect fallback, invalid HTML →
    fallback, render-raise rebuild, blank-frame rebuild, any exception →
    STATE_FAILED

Every non-trivial behavior is intended to be mutation-verified (hand-built
semantic mutants from an isolated copy with bytecode caching disabled).
Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.services.engines import theme, worker
from app.services.engines.base import STATE_COMPLETE, STATE_FAILED

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------
print("module contracts: aspects, accents, templates, esc, timeout, asset")

ok(set(worker._ASPECTS) == {"9:16", "16:9", "1:1"},
   "_ASPECTS covers the three render-profile video_aspect values")
ok(worker._ASPECTS["9:16"] == ("portrait", 1080, 1920),
   "9:16 → portrait 1080x1920 (shorts default)")
ok(worker._ASPECTS["16:9"] == ("landscape", 1920, 1080),
   "16:9 → landscape 1920x1080 (long-form)")
ok(worker._ASPECTS["1:1"] == ("square", 1080, 1080), "1:1 → square 1080x1080")

ok(worker._ACCENTS == [pair[0] for pair in theme.PALETTE],
   "_ACCENTS is theme.PALETTE accents in the same order (single brand)")
ok(worker._esc is theme.esc, "_esc IS theme.esc (no private copy)")

ok(worker._TEMPLATE_KEYS == [
    "bold_dark", "light_minimal", "gradient_kinetic", "neon_accent", "vivid_color",
], "exactly the five named templates, insertion order")
ok(set(worker._TEMPLATES) == set(worker._TEMPLATE_KEYS),
   "_TEMPLATE_KEYS is list(_TEMPLATES) — a rename without the list update fails")

ok(worker._RENDER_TIMEOUT == 1800, "CLI hard cap is 30 minutes")
ok((worker._ASSETS / "gsap.min.js").is_file(),
   "bundled gsap.min.js exists (run_job copies it into the job dir)")


# ---------------------------------------------------------------------------
# _word_count_bounds
# ---------------------------------------------------------------------------
print("_word_count_bounds: short band vs long floor/cap")

ok(worker._word_count_bounds({}) == (50, 140),
   "missing format → short band [50,140]")
ok(worker._word_count_bounds({"content_format": "short", "paragraph_number": 9})
   == (50, 140),
   "short ignores paragraph_number (always [50,140])")
ok(worker._word_count_bounds({"content_format": None}) == (50, 140),
   "content_format None → short (or 'short')")

# long: target = max(400, min(700, 500 + (n-6)*50)); return 0.65x / 1.35x
ok(worker._word_count_bounds({"content_format": "long"}) == (260, 540),
   "long default n=2 floors target at 400 → [260,540]")
ok(worker._word_count_bounds({"content_format": "long", "paragraph_number": 0})
   == (260, 540),
   "paragraph_number 0 is falsy → same as default n=2 (existing or-2 pin)")
ok(worker._word_count_bounds({"content_format": "long", "paragraph_number": 6})
   == (325, 675),
   "long n=6: target 500 → [325,675] (discriminates a dropped 0.65/1.35)")
ok(worker._word_count_bounds({"content_format": "long", "paragraph_number": 10})
   == (455, 945),
   "long n=10: target hits the 700 cap → [455,945]")
ok(worker._word_count_bounds({"content_format": "long", "paragraph_number": 20})
   == (455, 945),
   "long n=20 still capped at 700 (a dropped min(700,…) overshoots)")


# ---------------------------------------------------------------------------
# _generate_script — _llm stubbed
# ---------------------------------------------------------------------------
print("_generate_script: prompts, HARD RULE, quote-strip, word-count retry")

_llm_calls: list[dict] = []


def _in_band_short(_prompt, system=None, max_tokens=2000):
    _llm_calls.append({"prompt": _prompt, "system": system, "max_tokens": max_tokens})
    # 60 words — inside [50,140]
    return " ".join(["word"] * 60)


def _out_then_in(_prompt, system=None, max_tokens=2000):
    _llm_calls.append({"prompt": _prompt, "system": system, "max_tokens": max_tokens})
    if len(_llm_calls) == 1:
        return "too short"
    return " ".join(["retry"] * 80)


def _out_then_empty(_prompt, system=None, max_tokens=2000):
    _llm_calls.append({"prompt": _prompt, "system": system, "max_tokens": max_tokens})
    if len(_llm_calls) == 1:
        return "\"quoted original that is way too short\""
    return "   "


_llm_calls.clear()
with patch.object(worker, "_llm", side_effect=_in_band_short):
    text = worker._generate_script(
        "Cache misses cost conversions",
        {"content_format": "short", "paragraph_number": 2},
    )
ok(text == " ".join(["word"] * 60), "in-band short script returned as-is")
ok(len(_llm_calls) == 1, "in-band script does not retry")
p0 = _llm_calls[0]["prompt"]
ok("Cache misses cost conversions" in p0, "subject is interpolated into the prompt")
ok("In this video" in p0 and "Welcome" in p0 and "Today" in p0,
   "short prompt forbids the 07-07-class wind-up openers")
ok("EXPLICIT" in p0 and "follow/subscribe" in p0 and "welded" in p0,
   "short prompt mandates the spoken follow-ask (R7)")
ok("HARD RULE" not in p0,
   "no voice_name → no HARD RULE (language pin is voice-driven, not _voice default)")
ok(_llm_calls[0]["max_tokens"] == 600, "short script max_tokens=600")

_llm_calls.clear()
with patch.object(worker, "_llm", side_effect=_in_band_short):
    worker._generate_script(
        "Deep dive",
        {"content_format": "long", "paragraph_number": 8,
         "voice_name": "pt-BR-AntonioNeural-Male"},
    )
p1 = _llm_calls[0]["prompt"]
ok("450-700" in p1 and "in-depth" in p1, "long prompt is the in-depth branch")
ok("FINAL sentence" in p1 and "EXPLICIT" in p1,
   "long wrap-up still requires the spoken follow-ask")
ok("HARD RULE" in p1 and "Brazilian Portuguese" in p1,
   "pt-BR voice pins HARD RULE Brazilian Portuguese (07-07 EN-on-PT)")
ok(_llm_calls[0]["max_tokens"] == 1500, "long script max_tokens=1500")

_llm_calls.clear()
with patch.object(worker, "_llm", side_effect=_out_then_in):
    retried = worker._generate_script("x", {"content_format": "short"})
ok(retried == " ".join(["retry"] * 80), "out-of-band first try is replaced by the retry")
ok(len(_llm_calls) == 2, "out-of-band triggers exactly one retry")
ok("MUST be between 50 and 140 words" in _llm_calls[1]["prompt"],
   "retry prompt names the short [50,140] band")

_llm_calls.clear()
with patch.object(worker, "_llm", side_effect=_out_then_empty):
    kept = worker._generate_script("x", {"content_format": "short"})
ok(kept == "quoted original that is way too short",
   "empty retry keeps the (quote-stripped) original; leading/trailing quotes stripped")


# ---------------------------------------------------------------------------
# _pick_template
# ---------------------------------------------------------------------------
print("_pick_template: deterministic subject-hash")

n1, a1 = worker._pick_template("same title every time")
n2, a2 = worker._pick_template("same title every time")
ok((n1, a1) == (n2, a2), "same subject → same template + accent")
ok(n1 in worker._TEMPLATE_KEYS, "picked name is a real template key")
ok(a1 in worker._ACCENTS, "picked accent is a real brand accent")
# Pin the hash formula itself so a constant-return mutant cannot pass.
h = int(hashlib.sha1(b"same title every time").hexdigest(), 16)
ok(n1 == worker._TEMPLATE_KEYS[h % len(worker._TEMPLATE_KEYS)],
   "template index is sha1(subject) % n_templates")
ok(a1 == worker._ACCENTS[h % len(worker._ACCENTS)],
   "accent index is sha1(subject) % n_accents")
empty_n, empty_a = worker._pick_template("")
ok(empty_n in worker._TEMPLATE_KEYS and empty_a in worker._ACCENTS,
   "empty subject still resolves (sha1 of empty bytes)")


# ---------------------------------------------------------------------------
# _clips_from_json
# ---------------------------------------------------------------------------
print("_clips_from_json: unwrap, schema, clamps")

raw3 = json.dumps([
    {"text": "one", "start": 0.0, "duration": 2.0, "w": 1, "emoji": "🔥"},
    {"text": "two", "start": 2.0, "duration": 2.0, "w": 2},
    {"text": "three", "start": 4.0, "duration": 2.0, "w": 3},
])
c3 = worker._clips_from_json(raw3, 8.0)
ok(c3 is not None and len(c3) == 3, "valid 3-clip array parses")
ok(c3[0]["emoji"] == "🔥" and c3[0]["w"] == 1, "emoji + w forwarded")
ok(c3[1]["w"] == 2 and c3[2]["w"] == 3, "w=2 and w=3 kept")

fenced = "Here you go:\n```json\n" + raw3 + "\n```\n"
ok(worker._clips_from_json(fenced, 8.0) is not None,
   "unwraps a ```json fence (LLM often wraps)")

ok(worker._clips_from_json("not json at all", 8.0) is None, "invalid JSON → None")
ok(worker._clips_from_json("{}", 8.0) is None, "object (not list) → None")
ok(worker._clips_from_json("[]", 8.0) is None, "empty list → None")
ok(worker._clips_from_json(json.dumps([
    {"text": "a", "start": 0, "duration": 1},
    {"text": "b", "start": 1, "duration": 1},
]), 8.0) is None, "fewer than 3 clips → None")
ok(worker._clips_from_json(json.dumps([
    {"text": "a", "start": 0},  # missing duration
    {"text": "b", "start": 1, "duration": 1},
    {"text": "c", "start": 2, "duration": 1},
]), 8.0) is None, "missing required key → None")
ok(worker._clips_from_json("", 8.0) is None, "empty string → None")

long_text = "x" * 200
clamped = worker._clips_from_json(json.dumps([
    {"text": long_text, "start": 0.1234, "duration": 1.9876, "w": 0, "emoji": "ABCD"},
    {"text": "b", "start": 2, "duration": 1, "w": 9},
    {"text": "c", "start": 3, "duration": 1},
]), 8.0)
ok(clamped[0]["text"] == "x" * 120, "text clamped to 120 chars")
ok(clamped[0]["start"] == 0.123 and clamped[0]["duration"] == 1.988,
   "start/duration rounded to 3 decimals")
ok(clamped[0]["w"] == 1, "w=0 clamps up to 1")
ok(clamped[1]["w"] == 3, "w=9 clamps down to 3")
ok(clamped[2]["w"] == 1, "missing w defaults to 1")
ok(clamped[0]["emoji"] == "AB", "emoji sliced to 2 chars")

ok(worker._clips_from_json(json.dumps([
    {"text": "a", "start": 0, "duration": 1, "w": "nope"},
    {"text": "b", "start": 1, "duration": 1},
    {"text": "c", "start": 2, "duration": 1},
]), 8.0) is None, "non-int w raises inside the loop → None (never partial)")


# ---------------------------------------------------------------------------
# _validate_clips
# ---------------------------------------------------------------------------
print("_validate_clips: overlap tolerance + bounds")

ok(worker._validate_clips([
    {"start": 0.0, "duration": 2.0},
    {"start": 2.0, "duration": 2.0},
    {"start": 4.0, "duration": 2.0},
], 6.0), "abutting windows (no overlap) pass")
ok(worker._validate_clips([
    {"start": 0.0, "duration": 2.0},
    {"start": 1.90, "duration": 2.0},  # 100ms overlap < 120ms tolerance
], 6.0), "≤120ms overlap is tolerated")
ok(not worker._validate_clips([
    {"start": 0.0, "duration": 2.0},
    {"start": 1.80, "duration": 2.0},  # 200ms overlap
], 6.0), ">120ms overlap is rejected")
ok(not worker._validate_clips([{"start": 0.0, "duration": 0.4}], 6.0),
   "window shorter than 0.5s is rejected")
ok(not worker._validate_clips([{"start": -0.1, "duration": 2.0}], 6.0),
   "start < -0.05 is rejected")
ok(worker._validate_clips([{"start": -0.04, "duration": 2.0}], 6.0),
   "start just above -0.05 is accepted (float slack)")
ok(not worker._validate_clips([{"start": 0.0, "duration": 7.0}], 6.0),
   "end > duration+0.6 is rejected")
ok(worker._validate_clips([{"start": 0.0, "duration": 6.5}], 6.0),
   "end at duration+0.5 is accepted (within the 0.6 tail)")


# ---------------------------------------------------------------------------
# _assemble_composition + _looks_valid
# ---------------------------------------------------------------------------
print("_assemble_composition + _looks_valid")

clips = [
    {"text": "Hello <world> & co", "start": 0.0, "duration": 2.0, "w": 1, "emoji": ""},
    {"text": "mid", "start": 2.0, "duration": 2.0, "w": 2, "emoji": "🔥"},
    {"text": "PUNCH", "start": 4.0, "duration": 2.0, "w": 3, "emoji": ""},
]
html_dark = worker._assemble_composition(
    clips, "bold_dark", "#5b8cff", "portrait", 1080, 1920, 8.0)
ok(worker._looks_valid(html_dark), "assembled bold_dark passes _looks_valid")
ok("&lt;world&gt;" in html_dark and "&amp;" in html_dark,
   "clip text is HTML-escaped (no raw <> or &)")
ok("Hello" in html_dark and "class=\"word\"" in html_dark,
   "words are wrapped in .word spans")
ok('data-w="1"' in html_dark and 'data-w="2"' in html_dark and 'data-w="3"' in html_dark,
   "data-w forwarded for the GSAP punch scale")
ok(html_dark.count('class="clip-emoji"') == 1 and "🔥" in html_dark,
   "emoji slot rendered only on the clip that has one (not empty slots)")
ok("portrait" in html_dark and "1080" in html_dark and "1920" in html_dark,
   "resolution + pixel placeholders replaced")
ok("#5b8cff" in html_dark, "accent placeholder replaced")
ok("__CLIPS__" not in html_dark and "__ACCENT__" not in html_dark
   and "__DUR__" not in html_dark and "__PAD__" not in html_dark
   and "__FS__" not in html_dark,
   "no leftover __PLACEHOLDER__ tokens")
_fs = max(32, int(1080 * 0.065))
ok(f"font-size:{int(_fs * 1.15)}px" in html_dark,
   "w=2 uses 1.15x font-size")

html_light = worker._assemble_composition(
    clips, "light_minimal", "#00c9a7", "portrait", 1080, 1920, 8.0)
ok('font-weight:900' in html_light, "w=3 on light_minimal uses weight not color")
ok("color:#5b8cff" in html_dark, "w=3 on dark templates tints with the accent")

for name in worker._TEMPLATE_KEYS:
    h = worker._assemble_composition(
        clips, name, "#ff6b35", "landscape", 1920, 1080, 8.0)
    ok(worker._looks_valid(h), f"template {name!r} assembles to a valid composition")

ok(not worker._looks_valid(""), "empty string is not valid")
ok(not worker._looks_valid("<html><body>hi</body></html>"),
   "html without master timeline is not valid")
ok(not worker._looks_valid(
    '<html data-composition-id="master">gsap.timeline window.__timelines["master"] = x</html>'),
   "master registered but no tween (.fromTo/.to/.from/.set) is not valid "
   "(the dea9405 blank-render class)")
ok(not worker._looks_valid(
    '<html>gsap.timeline tl.fromTo("#x",{a:1},{b:2}) </html>'),
   "tween without master registration is not valid")
# A close that uses single quotes must still count (the regex accepts both).
ok(worker._looks_valid(
    '<html data-composition-id="master">gsap.timeline '
    "window.__timelines['master'] = tl; tl.set(el,{opacity:1})</html>"),
   "single-quoted __timelines['master'] still counts as closed")


# ---------------------------------------------------------------------------
# _key_lines + _fallback_composition
# ---------------------------------------------------------------------------
print("_key_lines + _fallback_composition")

ok(worker._key_lines("One. Two. Three.", k=4) == ["One.", "Two.", "Three."],
   "splits on sentence boundaries")
ok(worker._key_lines("alpha\nbeta\ngamma", k=2) == ["alpha", "beta"],
   "splits on newlines and respects k")
long_sent = "one two three four five six seven eight nine"
ok(worker._key_lines(long_sent, k=1) == ["one two three four five six seven…"],
   "lines longer than 7 words are clipped with an ellipsis")
ok(worker._key_lines("short", k=4) == ["short"], "short sentence is kept whole")
ok(worker._key_lines("", k=4) == ["Watch to the end"],
   "empty script → sentinel so the fallback still has a line")
ok(worker._key_lines("   \n\n  ", k=4) == ["Watch to the end"],
   "whitespace-only script → same sentinel")

fb = worker._fallback_composition(
    "Title <script>", "First line. Second line. Third. Fourth.",
    "portrait", 1080, 1920, 40.0)
ok(worker._looks_valid(fb), "fallback composition is guaranteed _looks_valid")
ok("Title &lt;script&gt;" in fb, "fallback HTML-escapes the subject")
ok("data-composition-id=\"master\"" in fb and "gsap.timeline" in fb,
   "fallback registers the paused master timeline")
ok("First line" in fb and "Second line" in fb, "fallback embeds key lines")
# k = max(4, min(8, int(duration // 18))); 40//18 = 2 → k=4, plus title = 5 segs
ok(fb.count('id="seg') == 5, "40s fallback: k floors at 4 lines + title = 5 segs")
fb_long = worker._fallback_composition("T", "A. B. C. D. E. F. G. H. I.",
                                       "portrait", 1080, 1920, 200.0)
# 200//18 = 11 → k=8, plus title = 9, but only 9 sentences exist so 1+9=10? 
# _key_lines returns up to k=8; + title = 9
ok(fb_long.count('id="seg') == 9, "long fallback caps at 8 lines + title")


# ---------------------------------------------------------------------------
# _voice
# ---------------------------------------------------------------------------
print("_voice: default + gender-suffix strip")

ok(worker._voice({"voice_name": "pt-BR-AntonioNeural-Male"}) == "pt-BR-AntonioNeural",
   "-Male suffix stripped (edge-tts wants the bare id)")
ok(worker._voice({"voice_name": "en-US-AvaNeural-Female"}) == "en-US-AvaNeural",
   "-Female suffix stripped")
ok(worker._voice({}) == "en-US-AndrewNeural",
   "missing voice_name → bare default (gender suffix already stripped)")
ok(worker._voice({"voice_name": "en-US-AndrewNeural"}) == "en-US-AndrewNeural",
   "already-bare id is unchanged")
ok(worker._voice({"voice_name": "en-US-AndrewNeural-male"}) == "en-US-AndrewNeural-male",
   "strip is case-sensitive (only -Male/-Female)")
ok(worker._voice({"voice_name": "X-Male-Extra"}) == "X-Male-Extra",
   "only a trailing -Male/-Female is stripped")


# ---------------------------------------------------------------------------
# _creation_config
# ---------------------------------------------------------------------------
print("_creation_config: snapshot + never-raises")

html_beats = (
    '<div class="beat hook"></div><div class="beat stat"></div>'
    '<div class="beat cta"></div>'
)
_orig_cc_ver = settings.composition_version
settings.composition_version = "sentinel-ver"
try:
    cc = worker._creation_config(
        "subj", {"topic_id": 1, "content_format": "long", "bgm_volume": 0.4,
                 "voice_name": "pt-BR-AntonioNeural-Male"},
        html_beats, "one two three four", 12.5, "portrait",
        Path("/tmp/techno_123.wav"), True,
    )
    ok(cc["content_format"] == "long", "content_format forwarded")
    ok(cc["resolution"] == "portrait", "resolution forwarded")
    ok(cc["voice"] == "pt-BR-AntonioNeural", "voice is the stripped edge-tts id")
    ok(cc["theme"]["accent"] == theme.resolve(1, "subj")["accent"],
       "theme accent matches theme.resolve(topic_id, subject)")
    ok(cc["beat_types"] == ["hook", "stat", "cta"] and cc["beat_count"] == 3,
       "beat types scraped from class=\"beat <type>\"")
    ok(cc["bgm"] == "techno_123.wav", "bgm records the file name, not the path")
    ok(cc["bgm_volume"] == 0.4, "explicit bgm_volume forwarded")
    ok(cc["script_words"] == 4 and cc["duration"] == 12.5, "script_words + duration")
    ok(cc["used_fallback"] is True, "used_fallback forwarded")
    ok(cc["composition_version"] == "sentinel-ver",
       "composition_version comes from settings (not a hardcoded 'storyboard')")
finally:
    settings.composition_version = _orig_cc_ver

cc0 = worker._creation_config("s", {"bgm_volume": 0}, "<html>", "x", 1.0,
                              "portrait", None, False)
ok(cc0["bgm"] is None, "no bgm → None")
ok(cc0["bgm_volume"] == 0.2,
   "bgm_volume 0 is falsy → existing `or 0.2` default (pinned, not changed)")
ok(cc0["content_format"] == "short", "missing content_format → short")

def _boom(*_a, **_k):
    raise RuntimeError("theme down")

cc_err = None
cc_raised = False
try:
    with patch.object(theme, "resolve", side_effect=_boom):
        cc_err = worker._creation_config("s", {}, "<html>", "x", 1.0, "p", None, False)
except Exception:
    cc_raised = True
ok((not cc_raised) and cc_err is not None
   and "error" in cc_err and "RuntimeError" in cc_err["error"],
   "creation_config never raises — error dict on theme.resolve failure")


# ---------------------------------------------------------------------------
# _pick_bgm
# ---------------------------------------------------------------------------
print("_pick_bgm: off / missing / techno-preferred / named / hash")

_orig_bgm = settings.bgm_dir
try:
    ok(worker._pick_bgm({"bgm_type": ""}, "h") is None,
       "bgm_type='' is explicit-off → None")

    with tempfile.TemporaryDirectory() as td:
        settings.bgm_dir = str(Path(td) / "missing")
        ok(worker._pick_bgm({}, "h") is None, "missing bgm_dir → None")

    with tempfile.TemporaryDirectory() as td:
        settings.bgm_dir = td
        ok(worker._pick_bgm({}, "h") is None, "empty bgm_dir → None")
        # Foreign stems + mp3 must lose to techno_*.wav
        Path(td, "other.wav").write_bytes(b"x")
        Path(td, "song.mp3").write_bytes(b"x")
        Path(td, "techno_aaa.wav").write_bytes(b"x")
        Path(td, "techno_bbb.wav").write_bytes(b"x")
        techno = sorted(p for p in Path(td).glob("techno_*.wav"))
        idx = int(hashlib.sha1(b"handle-one").hexdigest(), 16) % len(techno)
        picked = worker._pick_bgm({}, "handle-one")
        ok(picked == techno[idx],
           "prefers techno_*.wav and picks sha1(handle) % n (not tracks[0])")
        p2 = worker._pick_bgm({}, "handle-one")
        ok(p2 == picked, "same handle → same track (deterministic)")
        # "alpha" hashes to a different index than "handle-one" when n=2
        # (sha1 % 2 → 1 vs 0), so return tracks[0] cannot pass both.
        idx2 = int(hashlib.sha1(b"alpha").hexdigest(), 16) % len(techno)
        p3 = worker._pick_bgm({}, "alpha")
        ok(p3 == techno[idx2] and p3 != picked,
           "different handle uses its own sha1 index (not tracks[0])")
        named = worker._pick_bgm({"bgm_type": "song.mp3"}, "handle-one")
        ok(named is not None and named.name == "song.mp3",
           "named bgm_type wins when the file exists")
        missing_named = worker._pick_bgm({"bgm_type": "nope.wav"}, "handle-one")
        ok(missing_named is not None and missing_named.name.startswith("techno_"),
           "missing named file falls through to the hash pick")
        rand = worker._pick_bgm({"bgm_type": "random"}, "handle-one")
        ok(rand == picked, "bgm_type='random' uses the hash pick, not a name")

    # No techno_*.wav: fall through to any mp3/m4a/wav (the `wav_tracks or all_tracks` else).
    with tempfile.TemporaryDirectory() as td:
        settings.bgm_dir = td
        Path(td, "bed.mp3").write_bytes(b"x")
        Path(td, "notes.txt").write_text("ignore")
        only = worker._pick_bgm({}, "handle-one")
        ok(only is not None and only.name == "bed.mp3",
           "no techno_*.wav → fall back to other audio (mp3/m4a/wav)")
finally:
    settings.bgm_dir = _orig_bgm


# ---------------------------------------------------------------------------
# _probe_duration + _has_visible_frames (subprocess stubbed)
# ---------------------------------------------------------------------------
print("_probe_duration / _has_visible_frames (no live ffmpeg)")

with patch.object(worker.subprocess, "run",
                  return_value=SimpleNamespace(stdout="12.50\n", returncode=0)):
    ok(worker._probe_duration(Path("x.mp3")) == 12.5, "parses ffprobe duration")

with patch.object(worker.subprocess, "run",
                  return_value=SimpleNamespace(stdout="not-a-float", returncode=0)):
    ok(worker._probe_duration(Path("x.mp3")) is None, "non-float stdout → None")

with patch.object(worker.subprocess, "run",
                  side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
    ok(worker._probe_duration(Path("x.mp3")) is None,
       "ffprobe timeout → None (never raises)")

_sampled = {"n": 0}


def _count_sample(*_a, **_k):
    _sampled["n"] += 1
    raise subprocess.TimeoutExpired("ffmpeg", 30)


with patch.object(worker, "_probe_duration", return_value=0.0), \
        patch.object(worker.subprocess, "run", side_effect=_count_sample):
    _blank_early = worker._has_visible_frames(Path("x.mp4"))
ok(_blank_early is True and _sampled["n"] == 0,
   "dur<=0 returns True without sampling")

uniform = bytes([10] * 1024)
varied = bytes([i % 256 for i in range(1024)])


def _ff_uniform(*_a, **_k):
    return SimpleNamespace(stdout=uniform, returncode=0)


def _ff_varied(*_a, **_k):
    return SimpleNamespace(stdout=varied, returncode=0)


def _ff_short(*_a, **_k):
    return SimpleNamespace(stdout=b"xx", returncode=0)


def _ff_raise(*_a, **_k):
    raise subprocess.TimeoutExpired("ffmpeg", 30)


with patch.object(worker, "_probe_duration", return_value=6.0), \
        patch.object(worker.subprocess, "run", side_effect=_ff_varied):
    ok(worker._has_visible_frames(Path("x.mp4")) is True,
       "a varied 32x32 frame is visible")

with patch.object(worker, "_probe_duration", return_value=6.0), \
        patch.object(worker.subprocess, "run", side_effect=_ff_uniform):
    ok(worker._has_visible_frames(Path("x.mp4")) is False,
       "every sampled frame uniform → blank (dea9405 class)")

with patch.object(worker, "_probe_duration", return_value=6.0), \
        patch.object(worker.subprocess, "run", side_effect=_ff_raise):
    ok(worker._has_visible_frames(Path("x.mp4")) is True,
       "cannot sample any frame → do not block (True)")

with patch.object(worker, "_probe_duration", return_value=6.0), \
        patch.object(worker.subprocess, "run", side_effect=_ff_short):
    ok(worker._has_visible_frames(Path("x.mp4")) is True,
       "short stdout (<1024) skipped; no samples → do not block")


# ---------------------------------------------------------------------------
# _render / _mux command contracts
# ---------------------------------------------------------------------------
print("_render / _mux command contracts")

_captured: dict = {}


def _run_ok_write(cmd, check=True, capture_output=True, text=True, timeout=None, env=None):
    _captured["cmd"] = list(cmd)
    _captured["timeout"] = timeout
    _captured["env"] = dict(env or {})
    if "-o" in cmd:
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"mp4")
    else:
        Path(cmd[-1]).write_bytes(b"mux")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _run_fail(cmd, check=True, capture_output=True, text=True, timeout=None, env=None):
    err = subprocess.CalledProcessError(7, cmd, output="", stderr="hf-stderr-tail")
    raise err


def _run_ok_nofile(cmd, check=True, capture_output=True, text=True, timeout=None, env=None):
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


with tempfile.TemporaryDirectory() as td:
    job = Path(td) / "job"
    job.mkdir()
    out = Path(td) / "out.mp4"
    with patch.object(worker.subprocess, "run", side_effect=_run_ok_write):
        worker._render(job, out)
    cmd = _captured["cmd"]
    ok(cmd[0] == "npx" and "--yes" in cmd, "render via npx --yes")
    ok(f"hyperframes@{settings.hyperframes_version}" in cmd,
       "pinned hyperframes@version (same pin as thumbnail._render)")
    ok("render" in cmd and str(job) in cmd, "subcommand render + job_dir")
    ok("-o" in cmd and str(out) in cmd, "-o out_path")
    ok("--quality" in cmd and settings.hyperframes_render_quality in cmd,
       "quality matches settings")
    ok("--quiet" in cmd, "--quiet present")
    ok(_captured["timeout"] == worker._RENDER_TIMEOUT, "timeout is _RENDER_TIMEOUT")
    env = _captured["env"]
    ok(env.get("HYPERFRAMES_TELEMETRY") == "0", "HYPERFRAMES_TELEMETRY=0")
    ok(env.get("CI") == "1", "CI=1")
    ok(env.get("npm_config_yes") == "true", "npm_config_yes=true")

with tempfile.TemporaryDirectory() as td:
    job = Path(td) / "job"
    job.mkdir()
    out = Path(td) / "out.mp4"
    raised = False
    try:
        with patch.object(worker.subprocess, "run", side_effect=_run_fail):
            worker._render(job, out)
    except RuntimeError as e:
        raised = True
        ok("hyperframes render failed" in str(e) and "hf-stderr-tail" in str(e),
           "CalledProcessError → RuntimeError with stderr tail")
    ok(raised, "nonzero render raises (does not swallow)")

with tempfile.TemporaryDirectory() as td:
    job = Path(td) / "job"
    job.mkdir()
    out = Path(td) / "out.mp4"
    raised = False
    err = ""
    try:
        with patch.object(worker.subprocess, "run", side_effect=_run_ok_nofile):
            worker._render(job, out)
    except RuntimeError as e:
        raised = True
        err = str(e)
    ok(raised and "produced no file" in err,
       "success-but-missing-out raises (does not claim complete)")

with tempfile.TemporaryDirectory() as td:
    video = Path(td) / "v.mp4"
    narr = Path(td) / "n.mp3"
    bgm = Path(td) / "b.wav"
    out = Path(td) / "final.mp4"
    video.write_bytes(b"v")
    narr.write_bytes(b"n")
    bgm.write_bytes(b"b")
    with patch.object(worker, "_probe_duration", return_value=9.0), \
            patch.object(worker.subprocess, "run", side_effect=_run_ok_write):
        worker._mux(video, narr, bgm, 0.15, out)
    cmd = _captured["cmd"]
    ok(cmd[0] == "ffmpeg" and "-y" in cmd, "mux via ffmpeg -y")
    ok("-stream_loop" in cmd and "-1" in cmd, "BGM is looped")
    flt = cmd[cmd.index("-filter_complex") + 1]
    ok("volume=0.15" in flt and "amix=inputs=2" in flt,
       "BGM volume + amix when a track is present")
    ok("atrim=0:9.0" in flt,
       "mux duration is the probed 9.0s (not a hardcoded 12.0)")
    ok("-map" in cmd and "0:v" in cmd and "-c:v" in cmd and "copy" in cmd,
       "video stream copied; audio AAC")
    ok(_captured["timeout"] == 180, "mux timeout is 180s")

with tempfile.TemporaryDirectory() as td:
    video = Path(td) / "v.mp4"
    narr = Path(td) / "n.mp3"
    out = Path(td) / "final.mp4"
    with patch.object(worker, "_probe_duration", return_value=None), \
            patch.object(worker.subprocess, "run", side_effect=_run_ok_write):
        worker._mux(video, narr, None, 0.2, out)
    flt = _captured["cmd"][_captured["cmd"].index("-filter_complex") + 1]
    ok("-stream_loop" not in _captured["cmd"], "no BGM → no looped third input")
    ok("amix" not in flt and "atrim=0:12.0" in flt,
       "no BGM + no probe → 12.0s fallback filter, narration only")


# ---------------------------------------------------------------------------
# _generate_composition version switch
# ---------------------------------------------------------------------------
print("_generate_composition: storyboard vs legacy vs exception")

_orig_ver = settings.composition_version
try:
    settings.composition_version = "storyboard"
    with patch("app.services.engines.storyboard.compose",
               return_value="<html>from-compose</html>") as compose:
        got = worker._generate_composition(
            "subj", "script", [{"text": "a", "start": 0, "dur": 1}],
            "portrait", 1080, 1920, 12.0, topic_id=3,
            content_format="long", language="Brazilian Portuguese")
    ok(got == "<html>from-compose</html>", "storyboard version forwards compose()")
    kw = compose.call_args.kwargs
    ok(kw["subject"] == "subj" and kw["language"] == "Brazilian Portuguese",
       "compose gets subject + language (PT voice path)")
    ok(kw["topic_id"] == 3 and kw["content_format"] == "long",
       "topic_id + content_format reach compose")
    ok(kw["llm"] is worker._llm, "compose is given worker._llm (same seam)")

    with patch("app.services.engines.storyboard.compose",
               return_value="<html>from-compose</html>") as compose_en:
        worker._generate_composition(
            "subj", "script", [], "portrait", 1080, 1920, 12.0,
            language="English")
    ok(compose_en.call_args.kwargs["language"] == "English",
       "compose language='English' is forwarded (not hardcoded PT)")
    with patch("app.services.engines.storyboard.compose",
               return_value="<html>from-compose</html>") as compose_none:
        worker._generate_composition(
            "subj", "script", [], "portrait", 1080, 1920, 12.0, language=None)
    ok(compose_none.call_args.kwargs["language"] is None,
       "compose language=None is forwarded (not hardcoded PT)")

    with patch("app.services.engines.storyboard.compose", return_value=None):
        ok(worker._generate_composition("s", "x", [], "portrait", 1080, 1920, 8.0)
           == "",
           "compose() returning None/empty → '' (run_job falls back)")

    raised = False
    got = None
    try:
        with patch("app.services.engines.storyboard.compose",
                   side_effect=RuntimeError("llm down")):
            got = worker._generate_composition(
                "s", "x", [], "portrait", 1080, 1920, 8.0)
    except Exception:
        raised = True
    ok((not raised) and got == "",
       "compose() generic exception → '' (never raises into run_job)")

    from app.services.llm import GrokCLIError
    grok_raised = False
    grok_got = "sentinel"
    try:
        with patch("app.services.engines.storyboard.compose",
                   side_effect=GrokCLIError(
                       "grok.Timeout: grok -p timed out after 300s. "
                       "Refresh the Grok CLI OIDC session (`grok login`), then retry.")):
            grok_got = worker._generate_composition(
                "s", "x", [], "portrait", 1080, 1920, 8.0)
    except GrokCLIError as e:
        grok_raised = True
        grok_err = str(e)
    ok(grok_raised and grok_got == "sentinel" and "grok.Timeout" in grok_err,
       "compose() GrokCLIError re-raises (08-31 v1213/v1223: do not swallow into fallback)")

    settings.composition_version = "legacy"
    with patch.object(worker, "_generate_composition_legacy",
                      return_value="<html>legacy</html>") as leg:
        got = worker._generate_composition(
            "s", "script", [], "portrait", 1080, 1920, 8.0)
    ok(got == "<html>legacy</html>", "legacy version calls the clip-array path")
    ok(leg.call_args.args[0] == "s", "legacy path receives the subject")
finally:
    settings.composition_version = _orig_ver


# ---------------------------------------------------------------------------
# run_job pipeline (all I/O stubbed)
# ---------------------------------------------------------------------------
print("run_job: happy / fallback / blank-rebuild / fail")

VALID = worker._fallback_composition("ok", "A. B. C. D.", "portrait", 1080, 1920, 12.0)
ORIG = worker._fallback_composition(
    "COMPOSE-ORIG", "OrigA. OrigB. OrigC. OrigD.", "portrait", 1080, 1920, 12.0)
PICKED_BGM = Path("/tmp/techno_picked.wav")


def _tts_write(text, voice, path: Path):
    path.write_bytes(b"mp3")
    return [{"text": "A", "start": 0.0, "dur": 0.4}]


def _render_write(job_dir, out: Path):
    out.write_bytes(b"mp4")


_orig_storage = settings.hyperframes_storage_dir
try:
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "happy"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        compose_args = {}
        bgm_calls = []
        mux_calls = []

        def _gen_comp(subject, script, words, resolution, width, height, duration,
                      topic_id=None, content_format="short", language=None):
            compose_args.update(resolution=resolution, width=width, height=height,
                                duration=duration, language=language,
                                content_format=content_format, topic_id=topic_id)
            return VALID

        def _pick(params, h):
            bgm_calls.append((params, h))
            return PICKED_BGM

        def _mux_rec(*args):
            mux_calls.append(args)
            args[-1].write_bytes(b"final")

        params_happy = {"video_aspect": "16:9", "voice_name": "pt-BR-AntonioNeural-Male",
                        "content_format": "long", "topic_id": 2}
        with patch.object(worker, "_generate_script", return_value="spoken words here"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=10.0), \
                patch.object(worker, "_generate_composition", side_effect=_gen_comp), \
                patch.object(worker, "_render", side_effect=_render_write), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", side_effect=_pick), \
                patch.object(worker, "_mux", side_effect=_mux_rec):
            worker.run_job(handle, job_dir, "The Subject", params_happy)

        status = json.loads((job_dir / "status.json").read_text())
        ok(status["state"] == STATE_COMPLETE and status["progress"] == 100,
           "happy path ends STATE_COMPLETE progress=100")
        ok((job_dir / "index.html").is_file(), "happy path writes index.html")
        ok((job_dir / "gsap.min.js").is_file(), "happy path copies gsap.min.js")
        ok((job_dir / "narration.mp3").is_file(), "happy path writes narration.mp3")
        words = json.loads((job_dir / "narration_words.json").read_text())
        ok(words and words[0]["text"] == "A", "word timings persisted for storyboard sync")
        ok((job_dir / "final.mp4").read_bytes() == b"final", "mux target is final.mp4")
        ok(compose_args["resolution"] == "landscape"
           and compose_args["width"] == 1920 and compose_args["height"] == 1080,
           "16:9 aspect selects landscape 1920x1080")
        ok(compose_args["duration"] == 10.6,
           "duration is max(4, round(narr_secs+0.6, 2)) — 10.0 → 10.6")
        ok(compose_args["language"] == "Brazilian Portuguese",
           "pt-BR voice → language_from_voice Brazilian Portuguese")
        ok(compose_args["content_format"] == "long" and compose_args["topic_id"] == 2,
           "content_format + topic_id reach compose (not defaults)")
        ok(bgm_calls == [(params_happy, handle)],
           "_pick_bgm called with the job params + handle")
        ok(mux_calls and mux_calls[0][2] == PICKED_BGM,
           "_mux received the _pick_bgm return (not a hardcoded None)")
        ok(status.get("creation_config", {}).get("used_fallback") is False,
           "happy path used_fallback is False")

    # English voice: language must be English (kills a hardcoded-PT mutant)
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "envoice"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        seen_lang = {}

        def _gen_en(subject, script, words, resolution, width, height, duration,
                    topic_id=None, content_format="short", language=None):
            seen_lang["language"] = language
            return VALID

        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=10.0), \
                patch.object(worker, "_generate_composition", side_effect=_gen_en), \
                patch.object(worker, "_render", side_effect=_render_write), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S",
                           {"voice_name": "en-US-AndrewNeural-Male"})
        ok(seen_lang["language"] == "English",
           "en-US voice → language English (not hardcoded Brazilian Portuguese)")

    # no voice_name → language None
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "novoice"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        seen_lang = {}

        def _gen_none(subject, script, words, resolution, width, height, duration,
                      topic_id=None, content_format="short", language=None):
            seen_lang["language"] = language
            return VALID

        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=10.0), \
                patch.object(worker, "_generate_composition", side_effect=_gen_none), \
                patch.object(worker, "_render", side_effect=_render_write), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S", {})
        ok(seen_lang["language"] is None,
           "no voice_name → language None (not hardcoded PT)")

    # unknown aspect → 9:16 default; probe 0.0 → or 12.0 → duration 12.6
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "aspect"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        seen = {}

        def _gen_comp2(subject, script, words, resolution, width, height, duration,
                       **_k):
            seen.update(resolution=resolution, width=width, height=height,
                        duration=duration)
            return VALID

        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=0.0), \
                patch.object(worker, "_generate_composition", side_effect=_gen_comp2), \
                patch.object(worker, "_render", side_effect=_render_write), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S", {"video_aspect": "4:3"})
        ok(seen["resolution"] == "portrait"
           and seen["width"] == 1080 and seen["height"] == 1920,
           "unknown aspect falls back to 9:16 portrait")
        ok(seen["duration"] == 12.6,
           "probe 0.0 is falsy → narr_secs 12.0 → duration 12.6")

    # short probe hits the max(4.0, …) floor
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "shortprobe"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        seen = {}

        def _gen_short(subject, script, words, resolution, width, height, duration,
                       **_k):
            seen["duration"] = duration
            return VALID

        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=2.0), \
                patch.object(worker, "_generate_composition", side_effect=_gen_short), \
                patch.object(worker, "_render", side_effect=_render_write), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S", {})
        ok(seen["duration"] == 4.0,
           "probe 2.0 → 2.6 floors at max(4.0, …) = 4.0")

    # invalid HTML → fallback rewrite
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "badhtml"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=12.0), \
                patch.object(worker, "_generate_composition", return_value="<html>nope</html>"), \
                patch.object(worker, "_render", side_effect=_render_write), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S", {})
        html = (job_dir / "index.html").read_text()
        ok(worker._looks_valid(html),
           "invalid compose HTML is replaced by _fallback_composition")
        ok("nope" not in html and ">S<" in html.replace(" ", ""),
           "invalid-HTML rewrite replaces the compose bytes with fallback subject")
        st = json.loads((job_dir / "status.json").read_text())
        ok(st["creation_config"]["used_fallback"] is True,
           "invalid-HTML path records used_fallback")

    # first render raises → rewrite fallback + retry
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "rerender"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        renders = {"n": 0}

        def _render_once_fail(job_dir, out: Path):
            renders["n"] += 1
            if renders["n"] == 1:
                raise RuntimeError("cli exploded")
            out.write_bytes(b"mp4")

        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=12.0), \
                patch.object(worker, "_generate_composition", return_value=ORIG), \
                patch.object(worker, "_render", side_effect=_render_once_fail), \
                patch.object(worker, "_has_visible_frames", return_value=True), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S", {})
        ok(renders["n"] == 2, "render exception retries exactly once with fallback")
        ok((job_dir / "render-error.txt").read_text() == "cli exploded",
           "first render error is kept for inspection")
        rebuilt = (job_dir / "index.html").read_text()
        ok("COMPOSE-ORIG" not in rebuilt and "S" in rebuilt,
           "render-retry rewrites index.html via _fallback_composition (not the same HTML)")
        st = json.loads((job_dir / "status.json").read_text())
        ok(st["state"] == STATE_COMPLETE and st["creation_config"]["used_fallback"] is True,
           "render-retry still completes and records used_fallback")

    # blank frames → rebuild once
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "blank"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        renders = {"n": 0}

        def _render_count(job_dir, out: Path):
            renders["n"] += 1
            out.write_bytes(b"mp4")

        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=12.0), \
                patch.object(worker, "_generate_composition", return_value=ORIG), \
                patch.object(worker, "_render", side_effect=_render_count), \
                patch.object(worker, "_has_visible_frames", return_value=False), \
                patch.object(worker, "_pick_bgm", return_value=None), \
                patch.object(worker, "_mux", side_effect=lambda *a: a[-1].write_bytes(b"f")):
            worker.run_job(handle, job_dir, "S", {})
        ok(renders["n"] == 2, "blank frames force exactly one fallback re-render")
        ok("blank" in (job_dir / "blank-detected.txt").read_text(),
           "blank-detected.txt records the rebuild reason")
        rebuilt = (job_dir / "index.html").read_text()
        ok("COMPOSE-ORIG" not in rebuilt and "S" in rebuilt,
           "blank-rebuild rewrites index.html via _fallback_composition")
        st = json.loads((job_dir / "status.json").read_text())
        ok(st["state"] == STATE_COMPLETE and st["creation_config"]["used_fallback"] is True,
           "blank-rebuild still completes (accepted unconditionally, no loop)")

    # any uncaught exception → STATE_FAILED (does not raise)
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "boom"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        with patch.object(worker, "_generate_script",
                          side_effect=RuntimeError("llm dead")):
            worker.run_job(handle, job_dir, "S", {})
        st = json.loads((job_dir / "status.json").read_text())
        ok(st["state"] == STATE_FAILED, "uncaught exception → STATE_FAILED (never raises)")
        ok("RuntimeError" in (st.get("error") or "") and "llm dead" in (st.get("error") or ""),
           "failed status carries type + message")
        ok(not (job_dir / "final.mp4").exists(),
           "failed job does not write final.mp4")

    # GrokCLIError from compose (the 08-31 long timeout) must FAIL the job,
    # not complete with kinetic-text fallback. render_loop already retries
    # "grok.Timeout" as transient.
    from app.services.llm import GrokCLIError as _GrokCLIError
    with tempfile.TemporaryDirectory() as td:
        settings.hyperframes_storage_dir = td
        handle = "grokto"
        job_dir = Path(td) / handle
        job_dir.mkdir()
        with patch.object(worker, "_generate_script", return_value="s"), \
                patch.object(worker, "_tts", side_effect=_tts_write), \
                patch.object(worker, "_probe_duration", return_value=12.0), \
                patch("app.services.engines.storyboard.compose",
                      side_effect=_GrokCLIError(
                          "grok.Timeout: grok -p timed out after 300s")):
            worker.run_job(handle, job_dir, "S", {})
        st = json.loads((job_dir / "status.json").read_text())
        ok(st["state"] == STATE_FAILED, "compose GrokCLIError → STATE_FAILED (not fallback complete)")
        ok("grok.Timeout" in (st.get("error") or ""),
           "failed status carries grok.Timeout so render_loop classifies it transient")
        ok(not (job_dir / "final.mp4").exists(),
           "timeout-failed job does not write final.mp4")
finally:
    settings.hyperframes_storage_dir = _orig_storage


print()
print(f"ALL {_checks} CHECKS PASSED")
