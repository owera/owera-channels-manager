"""Dependency-free regression checks for app/services/thumbnail.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_thumbnail.py

``thumbnail.make_thumbnail_png`` is the publish-path CTR lever: a bold hook card
rendered via HyperFrames + ffmpeg, uploaded after every successful publish.
Best-effort by contract — any failure must return None and never block a publish.
Previously only the palette identity was pinned (via verify_storyboard); the hook
LLM fallback, HTML escape, render/extract command contracts, topic_id accent
selection, and the never-raises outer wrapper had zero direct coverage.

Covers, dependency-free (no network, no HyperFrames, no ffmpeg, no live YouTube):
  - module contracts: YouTube 1280x720 output, landscape render size, palette
    is theme.PALETTE (single source), render timeout pin
  - ``_hook_text``: LLM happy path, quote/multi-line strip, word-count + length
    gates that force the title fallback, LLM exception fallback, empty →
    "Watch This", title-over-subject, content_format short vs long prompt pin
    (empty/"LONG"/"medium"/None leftovers use the short-form hint — same
    != "long" gate as render/issues/publish/autofill; the old == "short"
    treated leftovers as long-form), max_tokens=100
  - ``_thumbnail_html``: dimensions, accent/bg injection, HTML-escape of the
    hook (XSS), gsap timeline shell so HyperFrames has a seekable clip
  - ``_render`` / ``_extract_frame``: command shape + env pins, nonzero
    returncode raises, missing output raises even on returncode 0
  - ``make_thumbnail_png``: happy path writes gsap + index.html and returns the
    PNG path; topic_id palette selection (incl. wrap); topic_id 0/None/omitted
    share theme.resolve's zero-is-missing gate (hello → cyan, not palette[0]
    blue — kills a leftover ``% 8`` index); content_format forwarded; any
    failure → None (never raises)

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.services import thumbnail
from app.services.engines import theme

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
print("module contracts: sizes, palette identity, timeout")

ok(thumbnail._OUT_W == 1280 and thumbnail._OUT_H == 720,
   "YouTube thumbnail output is 1280x720")
ok(thumbnail._W == 1920 and thumbnail._H == 1080,
   "HyperFrames render canvas is landscape 1920x1080")
ok(thumbnail._THUMB_PALETTE is theme.PALETTE,
   "thumbnail palette IS theme.PALETTE (single source — no private copy)")
ok(len(thumbnail._THUMB_PALETTE) == 8,
   f"palette has 8 brand pairs (got {len(thumbnail._THUMB_PALETTE)})")
ok(thumbnail.resolve is theme.resolve,
   "thumbnail.resolve IS theme.resolve (shared zero-is-missing gate, not a private % 8 index)")
ok(inspect.signature(thumbnail.make_thumbnail_png).parameters["topic_id"].default is None,
   "topic_id default is None (missing), not 0")
ok(thumbnail._RENDER_TIMEOUT == 240,
   "render timeout is 240s (static card must never stall a publish)")


# ---------------------------------------------------------------------------
# _hook_text
# ---------------------------------------------------------------------------
print("_hook_text: LLM happy path + fallback gates")

_llm_calls: list[dict] = []


def _llm_ok(prompt, system=None, max_tokens=None):
    _llm_calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
    return "The Cache Is Lying"


with patch.object(thumbnail, "_llm", side_effect=_llm_ok):
    _llm_calls.clear()
    out = thumbnail._hook_text("subject ignored", "Reranking in 5 lines",
                               content_format="short")
ok(out == "The Cache Is Lying", "LLM hook used when 3–6 words and ≤60 chars")
ok(len(_llm_calls) == 1, "exactly one LLM call on happy path")
ok(_llm_calls[0]["max_tokens"] == 100, "hook LLM capped at max_tokens=100")
ok("short-form vertical video" in _llm_calls[0]["prompt"],
   "content_format=short → short-form hint in prompt")
ok("Reranking in 5 lines" in _llm_calls[0]["prompt"],
   "title is what the LLM sees (not the subject when title is set)")
ok("thumbnail hooks" in (_llm_calls[0]["system"] or "").lower()
   or "You write YouTube thumbnail hooks" in (_llm_calls[0]["system"] or ""),
   "system prompt is the thumbnail-hook brief")

# content_format=long pins the long-form hint
with patch.object(thumbnail, "_llm", side_effect=_llm_ok):
    _llm_calls.clear()
    thumbnail._hook_text("s", "A long title about systems", content_format="long")
ok("long-form YouTube video" in _llm_calls[0]["prompt"],
   "content_format=long → long-form hint in prompt")
ok("short-form" not in _llm_calls[0]["prompt"],
   "long-form path does not mention short-form")

# Defect: _hook_text used content_format == "short" for the short-form hint,
# so empty/"LONG"/"medium"/None leftovers (treated as shorts by render /
# issues / publish / autofill) asked the LLM for a long-form hook. Same
# class as BACKLOG 23–25. publish_loop now normalizes before calling, but
# rubric_review and any direct caller still pass the raw format through.
print("_hook_text: leftover formats use the short-form hint (!= long)")


def _fmt_hint(content_format):
    _llm_calls.clear()
    thumbnail._hook_text("s", "T", content_format=content_format)
    prompt = _llm_calls[0]["prompt"]
    short = "short-form vertical video" in prompt
    long_ = "long-form YouTube video" in prompt
    return short, long_


with patch.object(thumbnail, "_llm", side_effect=_llm_ok):
    short, long_ = _fmt_hint("")
    ok(short and not long_,
       "empty-format leftover → short-form hint "
       "(== 'short' would pick long-form)")
    short, long_ = _fmt_hint("LONG")
    ok(short and not long_,
       "'LONG' leftover → short-form hint (case-sensitive == 'long' only)")
    short, long_ = _fmt_hint("medium")
    ok(short and not long_,
       "'medium' leftover → short-form hint "
       "(allowlist short+empty+LONG would still miss this)")
    short, long_ = _fmt_hint(None)
    ok(short and not long_,
       "content_format=None → short-form hint "
       "(== 'short' treated None as long-form)")
    short, long_ = _fmt_hint("short")
    ok(short and not long_,
       "canonical short still short-form after leftover pins")
    short, long_ = _fmt_hint("long")
    ok(long_ and not short,
       "canonical long still long-form (leftover gate does not invert longs)")

# quote strip is whole-string (re ^…$ before splitlines), then first line only
with patch.object(thumbnail, "_llm", return_value='"Quoted Hook Words"'):
    out = thumbnail._hook_text("s", "T")
ok(out == "Quoted Hook Words",
   "surrounding double quotes stripped from a single-line LLM response")

with patch.object(thumbnail, "_llm", return_value="`backtick hook here`"):
    out = thumbnail._hook_text("s", "T")
ok(out == "backtick hook here", "leading/trailing backticks stripped")

with patch.object(thumbnail, "_llm",
                  return_value="First Line Hook\nsecond line discarded"):
    out = thumbnail._hook_text("s", "T")
ok(out == "First Line Hook",
   "multi-line LLM response keeps only the first line")

# Pin the real order: strip quotes on the FULL string, THEN take first line.
# When line 1 is quote-wrapped but more lines follow, the closing quote is
# mid-string (not at $), so it survives on the kept first line.
with patch.object(thumbnail, "_llm",
                  return_value='"Quoted Hook Words"\nsecond line discarded'):
    out = thumbnail._hook_text("s", "T")
ok(out == 'Quoted Hook Words"',
   "multi-line: opening quote stripped at ^; trailing quote on line 1 KEPT "
   "(it is not at end-of-string — documents the real strip-then-splitlines order)")

# word-count / length gates force fallback (first 5 title words, Title Case)
def _assert_fallback(llm_return, title, expected, label):
    with patch.object(thumbnail, "_llm", return_value=llm_return):
        got = thumbnail._hook_text("subject", title)
    ok(got == expected, label)


_assert_fallback("one", "Alpha Beta Gamma Delta Epsilon Zeta",
                 "Alpha Beta Gamma Delta Epsilon",
                 "1-word LLM output → title fallback (first 5 words, Title Case)")
_assert_fallback("a b c d e f g h i", "Alpha Beta Gamma",
                 "Alpha Beta Gamma",
                 "9-word LLM output → title fallback (word gate is ≤8)")
_assert_fallback("X" * 61, "Alpha Beta",
                 "Alpha Beta",
                 "61-char LLM output → title fallback (len gate is ≤60)")
# boundary: exactly 2 words and exactly 8 words and exactly 60 chars all ACCEPT
with patch.object(thumbnail, "_llm", return_value="Two Words"):
    ok(thumbnail._hook_text("s", "T") == "Two Words",
       "exactly 2 words accepted (lower bound inclusive)")
with patch.object(thumbnail, "_llm", return_value="one two three four five six seven eight"):
    ok(thumbnail._hook_text("s", "T") == "one two three four five six seven eight",
       "exactly 8 words accepted (upper bound inclusive)")
# 60 chars across ≥2 words so the word-count gate does not fire first
_sixty = ("x" * 29) + " " + ("y" * 30)  # len=60, 2 words
assert len(_sixty) == 60 and 2 <= len(_sixty.split()) <= 8
with patch.object(thumbnail, "_llm", return_value=_sixty):
    ok(thumbnail._hook_text("s", "T") == _sixty,
       "exactly 60 chars (with 2–8 words) accepted (length bound inclusive)")
# 61 chars with valid word count still falls back
_sixty_one = ("x" * 30) + " " + ("y" * 30)  # len=61, 2 words
assert len(_sixty_one) == 61
with patch.object(thumbnail, "_llm", return_value=_sixty_one):
    ok(thumbnail._hook_text("s", "Alpha Beta") == "Alpha Beta",
       "61 chars even with valid word count → title fallback")

# LLM exception → fallback
with patch.object(thumbnail, "_llm", side_effect=RuntimeError("llm down")):
    out = thumbnail._hook_text("s", "Title Case Words Here Extra")
ok(out == "Title Case Words Here Extra",
   "LLM exception → first 5 title words Title-Cased (no raise)")

# title preferred; subject when title is None; empty everything
# Note: base = (title or subject or "").strip() — a whitespace title is truthy,
# so it wins over subject and then strips to "" (subject is NOT consulted).
with patch.object(thumbnail, "_llm", side_effect=RuntimeError("x")):
    ok(thumbnail._hook_text("Only Subject Words", None) == "Only Subject Words",
       "title=None → subject drives the fallback")
    ok(thumbnail._hook_text("Only Subject Words", "") == "Only Subject Words",
       "empty-string title is falsy → subject drives the fallback")
    ok(thumbnail._hook_text("Only Subject Words", "   ") == "Watch This",
       "whitespace title is truthy, strips to '' → sentinel (subject NOT used)")
    ok(thumbnail._hook_text("", None) == "Watch This",
       "empty subject+title → sentinel 'Watch This'")
    ok(thumbnail._hook_text("   ", None) == "Watch This",
       "whitespace-only subject → sentinel 'Watch This'")

# fallback truncates to 5 words
with patch.object(thumbnail, "_llm", side_effect=RuntimeError("x")):
    ok(thumbnail._hook_text("s", "one two three four five six seven")
       == "One Two Three Four Five",
       "fallback keeps only the first 5 words, Title-Cased")


# ---------------------------------------------------------------------------
# _thumbnail_html
# ---------------------------------------------------------------------------
print("_thumbnail_html: dimensions, palette injection, HTML escape")

html = thumbnail._thumbnail_html("Hello Hook", accent="#ff00aa", bg_deep="#112233")
ok(f'width="{thumbnail._W}"' in html or f"width:{thumbnail._W}px" in html,
   "render width embedded in HTML")
ok(f"height:{thumbnail._H}px" in html or f'data-height="{thumbnail._H}"' in html,
   "render height embedded in HTML")
ok("#ff00aa" in html, "accent color injected into the card")
ok("#112233" in html, "bg_deep color injected into the radial background")
ok("Hello Hook" in html, "hook text appears in the card body")
ok("gsap.timeline" in html, "gsap timeline shell present (HyperFrames needs a clip)")
ok('data-duration="1"' in html, "composition duration is 1s (static card)")
ok('data-composition-id="master"' in html, "composition id is 'master'")

# XSS / HTML escape — hook with angle brackets must not become raw markup
# (the page itself has legitimate <script> tags for gsap; pin the HOOK slot)
evil = '<img onerror=alert(1)> & more'
html_evil = thumbnail._thumbnail_html(evil)
ok("<img onerror=alert(1)>" not in html_evil,
   "raw attacker markup must NOT appear unescaped in the HTML")
ok("&lt;img onerror=alert(1)&gt;" in html_evil,
   "angle brackets HTML-escaped via theme.esc / worker._esc")
ok("&amp;" in html_evil, "ampersand HTML-escaped")
# And the hook lands inside the #hook clip, escaped
ok('id="hook"' in html_evil and "&lt;img" in html_evil,
   "escaped hook is rendered inside the card (not dropped)")

# default palette args still produce a valid card
html_default = thumbnail._thumbnail_html("Default")
ok("#5b8cff" in html_default and "#1b2a6b" in html_default,
   "default accent/bg match palette[0] (blue brand)")


# ---------------------------------------------------------------------------
# _render / _extract_frame command contracts
# ---------------------------------------------------------------------------
print("_render / _extract_frame: command shape + failure raises")

_captured: dict = {}


def _fake_run_ok(cmd, capture_output=True, text=True, timeout=None, env=None):
    _captured["cmd"] = list(cmd)
    _captured["timeout"] = timeout
    _captured["env"] = dict(env or {})
    # create the expected output so the exists() check passes
    if "-o" in cmd:
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(b"mp4")
    else:
        # ffmpeg form: last arg is out_png
        Path(cmd[-1]).write_bytes(b"png")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _fake_run_fail(cmd, capture_output=True, text=True, timeout=None, env=None):
    _captured["cmd"] = list(cmd)
    return SimpleNamespace(returncode=1, stdout="", stderr="boom-stderr-tail")


def _fake_run_ok_no_file(cmd, capture_output=True, text=True, timeout=None, env=None):
    # returncode 0 but never writes the output — must still raise
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


with tempfile.TemporaryDirectory() as td:
    job = Path(td) / "job"
    job.mkdir()
    out_mp4 = Path(td) / "out.mp4"
    with patch.object(thumbnail.subprocess, "run", side_effect=_fake_run_ok):
        thumbnail._render(job, out_mp4)
    cmd = _captured["cmd"]
    ok(cmd[0] == "npx" and "--yes" in cmd, "render via npx --yes")
    versioned = f"hyperframes@{settings.hyperframes_version}"
    ok(versioned in cmd,
       f"pinned hyperframes version in command ({versioned})")
    ok("render" in cmd, "subcommand is 'render'")
    ok(str(job) in cmd, "job_dir passed to hyperframes")
    ok("-o" in cmd and str(out_mp4) in cmd, "-o out_mp4 present")
    ok("--quality" in cmd and settings.hyperframes_render_quality in cmd,
       "quality flag matches settings.hyperframes_render_quality")
    ok("--quiet" in cmd, "--quiet flag present")
    ok(_captured["timeout"] == thumbnail._RENDER_TIMEOUT,
       "render timeout is _RENDER_TIMEOUT")
    env = _captured["env"]
    ok(env.get("HYPERFRAMES_TELEMETRY") == "0",
       "HYPERFRAMES_TELEMETRY=0 (no phone-home during publish)")
    ok(env.get("CI") == "1", "CI=1 in render env")
    ok(env.get("npm_config_yes") == "true", "npm_config_yes=true in render env")

with tempfile.TemporaryDirectory() as td:
    job = Path(td) / "job"
    job.mkdir()
    out_mp4 = Path(td) / "out.mp4"
    raised = False
    try:
        with patch.object(thumbnail.subprocess, "run", side_effect=_fake_run_fail):
            thumbnail._render(job, out_mp4)
    except RuntimeError as e:
        raised = True
        ok("hyperframes" in str(e).lower(),
           f"nonzero render raises RuntimeError naming hyperframes ({e!r})")
        ok("boom-stderr-tail" in str(e),
           "render error carries the stderr tail")
    ok(raised, "nonzero render returncode raises (does not swallow)")

with tempfile.TemporaryDirectory() as td:
    job = Path(td) / "job"
    job.mkdir()
    out_mp4 = Path(td) / "missing.mp4"
    raised = False
    try:
        with patch.object(thumbnail.subprocess, "run",
                          side_effect=_fake_run_ok_no_file):
            thumbnail._render(job, out_mp4)
    except RuntimeError:
        raised = True
    ok(raised, "render returncode 0 but missing out_mp4 still raises")

with tempfile.TemporaryDirectory() as td:
    mp4 = Path(td) / "in.mp4"
    mp4.write_bytes(b"x")
    out_png = Path(td) / "out.png"
    with patch.object(thumbnail.subprocess, "run", side_effect=_fake_run_ok):
        thumbnail._extract_frame(mp4, out_png)
    cmd = _captured["cmd"]
    ok(cmd[0] == "ffmpeg", "extract via ffmpeg")
    ok("-y" in cmd, "ffmpeg -y (overwrite)")
    ok("-ss" in cmd and "0.4" in cmd, "seek to 0.4s (mid-clip of the 1s card)")
    ok("-frames:v" in cmd and "1" in cmd, "exactly one frame extracted")
    scale = f"scale={thumbnail._OUT_W}:{thumbnail._OUT_H}"
    ok(scale in cmd,
       f"ffmpeg scales to YouTube size ({scale})")
    ok(str(mp4) in cmd and str(out_png) in cmd,
       "ffmpeg input mp4 + output png present")
    ok(_captured["timeout"] == 60, "extract timeout is 60s")

with tempfile.TemporaryDirectory() as td:
    mp4 = Path(td) / "in.mp4"
    mp4.write_bytes(b"x")
    out_png = Path(td) / "out.png"
    raised = False
    try:
        with patch.object(thumbnail.subprocess, "run", side_effect=_fake_run_fail):
            thumbnail._extract_frame(mp4, out_png)
    except RuntimeError as e:
        raised = True
        ok("ffmpeg" in str(e).lower(),
           f"nonzero extract raises RuntimeError naming ffmpeg ({e!r})")
    ok(raised, "nonzero extract returncode raises")

with tempfile.TemporaryDirectory() as td:
    mp4 = Path(td) / "in.mp4"
    mp4.write_bytes(b"x")
    out_png = Path(td) / "missing.png"
    raised = False
    try:
        with patch.object(thumbnail.subprocess, "run",
                          side_effect=_fake_run_ok_no_file):
            thumbnail._extract_frame(mp4, out_png)
    except RuntimeError:
        raised = True
    ok(raised, "extract returncode 0 but missing out_png still raises")


# ---------------------------------------------------------------------------
# make_thumbnail_png — the outer best-effort wrapper
# ---------------------------------------------------------------------------
print("make_thumbnail_png: happy path, palette by topic_id / zero-is-missing, never-raises")


def _fake_render_write(job_dir: Path, out_mp4: Path) -> None:
    # Observe the work dir the wrapper prepared, then write a dummy mp4.
    _captured["job_dir"] = Path(job_dir)
    _captured["index_html"] = (Path(job_dir) / "index.html").read_text()
    _captured["has_gsap"] = (Path(job_dir) / "gsap.min.js").is_file()
    out_mp4.write_bytes(b"fake-mp4")


def _fake_extract_write(mp4: Path, out_png: Path) -> None:
    out_png.write_bytes(b"\x89PNG\r\nfake")


# "hello" hashes to palette[5] cyan — NOT palette[0] blue. Reserved as the
# discriminator against a leftover `_THUMB_PALETTE[topic_id % 8]` (always
# blue at topic_id=0) and against `if topic_id is None` (0 still indexes [0]).
# Do NOT substitute "cache": that subject hashes TO palette[0] and is vacuous.
HELLO = "hello"
CYAN, CYAN_BG = theme.PALETTE[5]
BLUE, BLUE_BG = theme.PALETTE[0]
TEAL, TEAL_BG = theme.PALETTE[1]
ok(theme.resolve(0, HELLO)["accent"] == CYAN and CYAN == "#2ec4b6",
   "fixture: hello + topic_id=0 hashes to cyan #2ec4b6 (not palette[0] blue)")
ok(CYAN != BLUE, "discriminator is non-vacuous: hello's cyan ≠ palette[0] blue")

with tempfile.TemporaryDirectory() as td:
    out_png = Path(td) / "thumb.png"
    with patch.object(thumbnail, "_hook_text", return_value="Hook Words Here"), \
         patch.object(thumbnail, "_render", side_effect=_fake_render_write), \
         patch.object(thumbnail, "_extract_frame", side_effect=_fake_extract_write):
        result = thumbnail.make_thumbnail_png(
            HELLO, "Title About Caches", out_png,
            topic_id=0, content_format="short",
        )
    ok(result == out_png, "happy path returns the out_png path")
    ok(out_png.is_file() and out_png.read_bytes().startswith(b"\x89PNG"),
       "happy path writes a PNG at out_png")
    ok(_captured["has_gsap"] is True,
       "work dir gets a copy of gsap.min.js (HyperFrames requires it)")
    ok("Hook Words Here" in _captured["index_html"],
       "index.html embeds the hook from _hook_text")
    ok(CYAN in _captured["index_html"] and CYAN_BG in _captured["index_html"],
       f"topic_id=0 + hello → subject-hash cyan/bg ({CYAN}/{CYAN_BG}), not palette[0]")
    ok(BLUE not in _captured["index_html"] and BLUE_BG not in _captured["index_html"],
       "topic_id=0 + hello does NOT embed palette[0] blue (kills % 8 fallback)")
    ok(_captured["job_dir"].name == ".thumb_work",
       "work dir is a .thumb_work sibling of out_png")

# topic_id palette selection + wrap (positive ids, including wrap-to-[0])
for tid, label in [(1, "topic_id=1 → palette[1]"),
                   (7, "topic_id=7 → palette[7] (last)"),
                   (8, "topic_id=8 → palette[0] (wrap)"),
                   (9, "topic_id=9 → palette[1] (wrap)")]:
    with tempfile.TemporaryDirectory() as td:
        out_png = Path(td) / "t.png"
        with patch.object(thumbnail, "_hook_text", return_value="H"), \
             patch.object(thumbnail, "_render", side_effect=_fake_render_write), \
             patch.object(thumbnail, "_extract_frame",
                          side_effect=_fake_extract_write):
            thumbnail.make_thumbnail_png("s", "t", out_png, topic_id=tid)
        accent, bg = theme.PALETTE[tid % len(theme.PALETTE)]
        ok(accent in _captured["index_html"] and bg in _captured["index_html"],
           f"{label} → {accent}/{bg}")

# zero-is-missing: None and omitted default share the hash gate with explicit 0.
# topic_id=1 + hello still teal (bound id wins — kills an always-hash mutant).


def _thumb_html(subject, **kw):
    with tempfile.TemporaryDirectory() as td:
        out_png = Path(td) / "t.png"
        with patch.object(thumbnail, "_hook_text", return_value="H"), \
             patch.object(thumbnail, "_render", side_effect=_fake_render_write), \
             patch.object(thumbnail, "_extract_frame",
                          side_effect=_fake_extract_write):
            thumbnail.make_thumbnail_png(subject, "t", out_png, **kw)
        return _captured["index_html"]


html_none = _thumb_html(HELLO, topic_id=None)
ok(CYAN in html_none and CYAN_BG in html_none,
   f"topic_id=None + hello → subject-hash cyan ({CYAN})")
ok(BLUE not in html_none,
   "topic_id=None + hello does NOT embed palette[0] blue")

html_omit = _thumb_html(HELLO)
ok(CYAN in html_omit and BLUE not in html_omit,
   "omitted topic_id + hello → subject-hash cyan (default is missing, not 0-as-blue)")

html_bound = _thumb_html(HELLO, topic_id=1)
ok(TEAL in html_bound and TEAL_BG in html_bound,
   f"topic_id=1 + hello → palette[1] teal ({TEAL}), not hello's cyan")
ok(CYAN not in html_bound,
   "topic_id=1 + hello does NOT embed hello's cyan (kills always-hash)")

# content_format is forwarded to _hook_text
_hook_args: list[tuple] = []


def _spy_hook(subject, title, content_format="short"):
    _hook_args.append((subject, title, content_format))
    return "Spy Hook"


with tempfile.TemporaryDirectory() as td:
    out_png = Path(td) / "t.png"
    with patch.object(thumbnail, "_hook_text", side_effect=_spy_hook), \
         patch.object(thumbnail, "_render", side_effect=_fake_render_write), \
         patch.object(thumbnail, "_extract_frame", side_effect=_fake_extract_write):
        thumbnail.make_thumbnail_png("subj", "titl", out_png,
                                     content_format="long")
ok(_hook_args == [("subj", "titl", "long")],
   "content_format forwarded to _hook_text unchanged")

# any failure → None, never raises (the publish-path contract)
failure_cases = [
    ("_render raises",
     dict(_render=RuntimeError("hf down"))),
    ("_extract_frame raises",
     dict(_extract_frame=RuntimeError("ffmpeg down"))),
    ("_hook_text raises (should be internal, but wrapper still catches)",
     dict(_hook_text=RuntimeError("hook boom"))),
]


def _raise(exc):
    def _inner(*a, **k):
        raise exc
    return _inner


for label, overrides in failure_cases:
    with tempfile.TemporaryDirectory() as td:
        out_png = Path(td) / "t.png"
        patches = {
            "_hook_text": patch.object(thumbnail, "_hook_text",
                                       return_value="H"),
            "_render": patch.object(thumbnail, "_render",
                                    side_effect=_fake_render_write),
            "_extract_frame": patch.object(
                thumbnail, "_extract_frame", side_effect=_fake_extract_write),
        }
        # Apply the failure override
        for name, exc in overrides.items():
            patches[name] = patch.object(thumbnail, name, side_effect=_raise(exc))
        try:
            for p in patches.values():
                p.start()
            result = thumbnail.make_thumbnail_png("s", "t", out_png)
        finally:
            for p in patches.values():
                p.stop()
        ok(result is None, f"{label} → None (best-effort, never raises)")
        ok(not out_png.exists(),
           f"{label} → no partial PNG left behind")

# gsap source missing → None (read_bytes on missing assets file)
with tempfile.TemporaryDirectory() as td:
    out_png = Path(td) / "t.png"
    missing_assets = Path(td) / "no-assets"
    missing_assets.mkdir()
    with patch.object(thumbnail, "_ASSETS", missing_assets), \
         patch.object(thumbnail, "_hook_text", return_value="H"), \
         patch.object(thumbnail, "_render", side_effect=_fake_render_write), \
         patch.object(thumbnail, "_extract_frame", side_effect=_fake_extract_write):
        result = thumbnail.make_thumbnail_png("s", "t", out_png)
    ok(result is None,
       "missing gsap.min.js in _ASSETS → None (best-effort)")


print()
print(f"ALL {_checks} CHECKS PASSED")
