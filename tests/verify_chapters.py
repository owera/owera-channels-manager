"""Dependency-free regression checks for app/services/chapters.py
(feature: backlog #13; coverage expansion: backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_chapters.py

Long-form descriptions get a chapters block derived at publish time from the beat
divs the storyboard composer bakes into the render job dir's index.html. YouTube
silently ignores a chapter list that doesn't start at 0:00, has fewer than three
entries, or contains a chapter shorter than 10 seconds — so a regression here means
either broken chapters on every long-form publish or none at all. Covers:
  - headline extraction per beat type against the REAL renderer markup
    (build_index_html), so a renderer markup change that would break extraction
    fails here instead of silently in production
  - the count-up stat placeholder ("0") never titles a chapter; entity unescape
  - the YouTube validity rules: 10s minimum spacing (fold-forward), 10s minimum
    tail, >=3 chapters, first chapter at 0:00, cta beat skipped, 1h+ stamps
  - remaining headline branches the golden composition skipped (labeled-stat,
    unlabeled numeric+unit is NOT a chapter, titled cmp/lst prefer the title,
    fanout diagram node join, 60-char clamp, strip-after-slice)
  - displayed-seconds flooring, exclusive 10s spacing/tail (`<` not `<=`),
    malformed/unknown beats skipped without killing the list
  - chapter_lines_for_video: engine-registry path resolution, missing file /
    missing handle / unreadable file / get_engine raise / missing attrs
    -> None, never raises
  - finalize_description: chapters before the CTA block, localized header,
    idempotent on publish retries, byte-identical behavior when chapter_lines=None

Every non-trivial remaining branch is mutation-verified (hand-built semantic
mutants run from an isolated copy with bytecode caching disabled). Exits
non-zero on the first failed assertion.
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.config import settings
from app.services import chapters, metadata
from app.services.engines import storyboard, theme
import app.services.engines as _engines

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------- real-markup extraction
# Beats in the coerced shape build_index_html consumes, chosen to exercise every
# renderer's headline path plus the spacing/skip rules in one composition.
DUR = 120.0
BEATS = [
    {"type": "hook", "cue": "", "text": "Cada ms custa conversão", "emoji": "⚡",
     "start": 0.0, "dur": 11.0},
    # value "<10" is non-numeric -> rendered literally (entity-escaped); no label.
    {"type": "stat", "cue": "", "value": "<10", "unit": "ms", "label": "", "emoji": "",
     "start": 11.0, "dur": 4.0},
    # only 4s after the previous kept chapter -> folded (skipped) despite a label.
    {"type": "stat", "cue": "", "value": "42", "unit": "", "label": "latência p99",
     "emoji": "", "start": 15.0, "dur": 11.0},
    {"type": "statement", "cue": "", "text": "Multithread vence", "w": 2, "emoji": "",
     "start": 26.0, "dur": 12.0},
    # numeric value renders as the count-up placeholder "0" and has no label ->
    # no headline -> no chapter, and it must NOT advance the spacing anchor.
    {"type": "stat", "cue": "", "value": "7", "unit": "", "label": "", "emoji": "",
     "start": 38.0, "dur": 2.0},
    {"type": "term_define", "cue": "", "term": "Slab allocator",
     "definition": "blocos fixos", "start": 40.0, "dur": 12.0},
    {"type": "list", "cue": "", "title": "", "ordered": False,
     "items": [{"text": "histórico de compras", "emoji": ""}],
     "start": 52.0, "dur": 11.0},
    {"type": "compare", "cue": "", "title": "",
     "left": {"title": "Redis", "items": ["estruturas"]},
     "right": {"title": "Memcached", "items": ["kv puro"]},
     "start": 63.0, "dur": 11.0},
    {"type": "quote", "cue": "", "text": "Meça antes de migrar", "attribution": "",
     "start": 74.0, "dur": 11.0},
    {"type": "code", "cue": "", "lang": "redis",
     "lines": ["HGETALL user:42", "INCR clicks"], "highlight": [0],
     "start": 85.0, "dur": 11.0},
    {"type": "command", "cue": "", "prompt": "$", "command": "redis-cli --latency",
     "output": [], "start": 96.0, "dur": 11.0},
    {"type": "diagram", "cue": "", "layout": "pipeline",
     "nodes": [{"id": "a", "label": "App"}, {"id": "b", "label": "Cache"},
               {"id": "c", "label": "DB"}],
     "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
     "start": 107.0, "dur": 6.0},
    # the subscribe card: never a chapter (would also fail the 10s tail rule).
    {"type": "cta", "cue": "", "text": "Siga", "sub": "Benchmark amanhã",
     "start": 113.0, "dur": 7.0},
]

HTML = storyboard.build_index_html(BEATS, theme.resolve(7, "chapters test"),
                                   "1080p", 1920, 1080, DUR)
lines = chapters.chapter_lines(HTML)

ok(lines is not None, "a rich long-form composition yields a chapter list")
ok(lines == [
    "0:00 Cada ms custa conversão",
    "0:11 ‹10 ms",
    "0:26 Multithread vence",
    "0:40 Slab allocator",
    "0:52 histórico de compras",
    "1:03 Redis vs Memcached",
    "1:14 Meça antes de migrar",
    "1:25 HGETALL user:42",
    "1:36 redis-cli --latency",
    "1:47 App → Cache → DB",
], f"every renderer's headline extracts from the real markup, in order (got {lines})")
ok(lines[0].startswith("0:00 "), "the list starts at 0:00 (YouTube hard requirement)")
ok(all("latência p99" not in ln for ln in lines),
   "a beat <10s after the previous kept chapter is folded into it")
ok(all(" 0" != ln[-2:] and "0:38" not in ln for ln in lines),
   "a numeric count-up stat (placeholder '0') never titles a chapter")
ok(all("Siga" not in ln for ln in lines), "the cta beat never becomes a chapter")
ok("‹10 ms" in lines[1] and not any("<" in ln or ">" in ln for ln in lines),
   "angle brackets are transliterated — the YouTube API rejects < and > in "
   "descriptions, and a 400 would strand the video with the description committed")

# ---------------------------------------------------------------- module contracts

ok(chapters._MIN_CHAPTER_SECONDS == 10, "YouTube min chapter length is 10 displayed seconds")
ok(chapters._MIN_CHAPTERS == 3, "YouTube ignores a list shorter than 3 chapters")
ok(chapters._MAX_HEADLINE_CHARS == 60, "headlines clamp at 60 chars")

# ---------------------------------------------------------------- _stamp / _text (pure)

ok(chapters._stamp(0) == "0:00", "_stamp(0) is M:SS not H:MM:SS")
ok(chapters._stamp(59) == "0:59", "_stamp just under a minute")
ok(chapters._stamp(60) == "1:00", "_stamp rolls minutes, not 0:60")
ok(chapters._stamp(3599) == "59:59", "_stamp just under an hour stays M:SS")
ok(chapters._stamp(3600) == "1:00:00", "_stamp switches to H:MM:SS at one hour")
ok(chapters._stamp(3661) == "1:01:01", "_stamp H:MM:SS padding")

ok(chapters._text("<b>bold</b>  &amp;  extra") == "bold & extra",
   "_text strips tags, unescapes entities, collapses whitespace")
ok(chapters._text("use &lt;b&gt;bold&lt;/b&gt;") == "use ‹b›bold‹/b›",
   "entities are unescaped AFTER tags are stripped, so encoded brackets "
   "survive as ‹ › (unescape-first would eat them as tags)")
ok(chapters._text("") == "" and chapters._text("   ") == "",
   "_text of empty/whitespace is empty")
ok(chapters._headline("cta", '<div class="cta-box">Siga</div>') == "",
   "_headline('cta') is always empty — the subscribe card is not a chapter")
ok(chapters._headline("mystery", '<div class="htext">Ponto</div>') == "",
   "an unknown beat class produces no headline")
ok(chapters._div("<p>nope</p>", "htext") == "", "_div misses return empty, not None")
ok(chapters._span("<p>nope</p>", "stat-num") == "", "_span misses return empty, not None")

# ---------------------------------------------------------------- remaining headline branches (real renderer markup)
# The golden composition skipped these on purpose (spacing fold, empty title)
# so a broken label/title extractor still passed. Isolated 3-chapter comps
# make each fallback the discriminating middle chapter.


def _real(*beats, duration=300.0) -> str:
    return storyboard.build_index_html(
        list(beats), theme.resolve(7, "chapters test"),
        "1080p", 1920, 1080, duration)


def _hook(start: float, text: str = "Ponto") -> dict:
    return {"type": "hook", "cue": "", "text": text, "emoji": "",
            "start": start, "dur": 11.0}


labeled = _real(
    _hook(0.0, "Open"),
    {"type": "stat", "cue": "", "value": "42", "unit": "ms",
     "label": "latência p99", "emoji": "", "start": 20.0, "dur": 11.0},
    _hook(40.0, "Close"),
)
ok(chapters.chapter_lines(labeled) == [
    "0:00 Open", "0:20 latência p99", "0:40 Close",
], "a labeled numeric stat titles the chapter with the label, not the count-up '0'")

unlabeled_num = _real(
    _hook(0.0, "Open"),
    {"type": "stat", "cue": "", "value": "99", "unit": "ms",
     "label": "", "emoji": "", "start": 20.0, "dur": 11.0},
    _hook(40.0, "Mid"),
    _hook(60.0, "Close"),
)
ok(chapters.chapter_lines(unlabeled_num) == [
    "0:00 Open", "0:40 Mid", "1:00 Close",
], "an unlabeled numeric stat is the count-up placeholder '0' in HTML — "
   "no chapter, even when a unit is present (the real number lives in GSAP)")

non_num_unit = _real(
    _hook(0.0, "Open"),
    {"type": "stat", "cue": "", "value": "99%", "unit": "faster",
     "label": "", "emoji": "", "start": 20.0, "dur": 11.0},
    _hook(40.0, "Close"),
)
ok(chapters.chapter_lines(non_num_unit) == [
    "0:00 Open", "0:20 99% faster", "0:40 Close",
], "an unlabeled NON-numeric stat with a unit keeps the literal value + unit")

titled_cmp = _real(
    _hook(0.0, "Open"),
    {"type": "compare", "cue": "", "title": "Dois caches",
     "left": {"title": "Redis", "items": ["estruturas"]},
     "right": {"title": "Memcached", "items": ["kv puro"]},
     "start": 20.0, "dur": 11.0},
    _hook(40.0, "Close"),
)
ok(chapters.chapter_lines(titled_cmp) == [
    "0:00 Open", "0:20 Dois caches", "0:40 Close",
], "a titled compare uses cmp-title, not the 'left vs right' h3 fallback")
ok(all("Redis vs Memcached" not in ln for ln in chapters.chapter_lines(titled_cmp)),
   "the h3 fallback is not consulted when cmp-title is present")

one_h3 = _real(
    _hook(0.0, "Open"),
    {"type": "compare", "cue": "", "title": "",
     "left": {"title": "Redis", "items": ["estruturas"]},
     "right": {"title": "", "items": ["kv puro"]},
     "start": 20.0, "dur": 11.0},
    _hook(40.0, "Mid"),
    _hook(60.0, "Close"),
)
ok(chapters.chapter_lines(one_h3) == [
    "0:00 Open", "0:40 Mid", "1:00 Close",
], "a compare with only one h3 (empty right title) produces no chapter")

titled_lst = _real(
    _hook(0.0, "Open"),
    {"type": "list", "cue": "", "title": "Três regras", "ordered": False,
     "items": [{"text": "histórico de compras", "emoji": ""}],
     "start": 20.0, "dur": 11.0},
    _hook(40.0, "Close"),
)
ok(chapters.chapter_lines(titled_lst) == [
    "0:00 Open", "0:20 Três regras", "0:40 Close",
], "a titled list uses lst-title, not the first item")
ok(all("histórico" not in ln for ln in chapters.chapter_lines(titled_lst)),
   "the first-item fallback is not consulted when lst-title is present")

fanout = _real(
    _hook(0.0, "Open"),
    {"type": "diagram", "cue": "", "layout": "fanout",
     "nodes": [{"id": "a", "label": "Hub"}, {"id": "b", "label": "Spoke1"},
               {"id": "c", "label": "Spoke2"}],
     "edges": [{"from": "a", "to": "b", "label": "x"},
               {"from": "a", "to": "c", "label": "y"}],
     "start": 20.0, "dur": 11.0},
    _hook(40.0, "Close"),
)
ok(chapters.chapter_lines(fanout) == [
    "0:00 Open", "0:20 Hub → Spoke1 → Spoke2", "0:40 Close",
], "a fanout diagram still joins node labels with → (same node markup as pipeline)")

clamped = _real(_hook(0.0, "W" * 80), _hook(20.0, "B"), _hook(40.0, "C"))
ok(chapters.chapter_lines(clamped) == [
    "0:00 " + "W" * 60, "0:20 B", "0:40 C",
], "headlines clamp to _MAX_HEADLINE_CHARS (80 W's -> 60)")

ws_open = _real(_hook(0.0, "   "), _hook(20.0, "B"), _hook(40.0, "C"), _hook(60.0, "D"))
ok(chapters.chapter_lines(ws_open) is None,
   "a whitespace-only opening headline is empty after strip — no fake 0:00 chapter")

# ---------------------------------------------------------------- validity-rule edges

def _mini(beats_html: str, duration: float = 300.0) -> str:
    return ('<div id="root" data-composition-id="master" data-width="1920" '
            f'data-height="1080" data-start="0" data-duration="{duration}">'
            + beats_html + "</div><script>tl</script>")


def _beat(i: int, cls: str, start: float, inner: str) -> str:
    return (f'<div class="beat {cls}" id="b{i}" data-start="{start}" '
            f'data-duration="5.0" data-track-index="{i}">{inner}</div>')


_H = '<div class="htext"><span class="word">Ponto</span> <span class="word">%d</span></div>'

two = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 30.0, _H % 1))
ok(chapters.chapter_lines(two) is None,
   "fewer than 3 chapters -> None (YouTube ignores short lists)")

no_zero = _mini(_beat(0, "stat", 0.0, '<div class="stat-row"><span class="stat-num">0'
                                      "</span></div>")
                + _beat(1, "hook", 20.0, _H % 1) + _beat(2, "hook", 40.0, _H % 2)
                + _beat(3, "hook", 60.0, _H % 3))
ok(chapters.chapter_lines(no_zero) is None,
   "no chapter at 0:00 (opening beat unextractable) -> None, never a fake list")

tail = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 20.0, _H % 1)
             + _beat(2, "hook", 40.0, _H % 2) + _beat(3, "hook", 295.0, _H % 3))
ok(chapters.chapter_lines(tail) == ["0:00 Ponto 0", "0:20 Ponto 1", "0:40 Ponto 2"],
   "a beat within 10s of the video end is dropped (min final-chapter length)")

ok(chapters.chapter_lines('<div class="beat hook">no root</div>') is None,
   "markup without the root duration -> None")
ok(chapters.chapter_lines(_mini("")) is None,
   "a composition with no beat divs (fallback template) -> None")

hour = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 1000.0, _H % 1)
             + _beat(2, "hook", 3661.0, _H % 2), duration=3700.0)
ok(chapters.chapter_lines(hour) == ["0:00 Ponto 0", "16:40 Ponto 1", "1:01:01 Ponto 2"],
   "stamps switch to H:MM:SS past one hour")

redirect = _mini(_beat(0, "hook", 0.0, _H % 0)
                 + _beat(1, "code", 20.0,
                         '<pre class="code"><span class="ln">cat in.txt &gt; out.txt'
                         "</span></pre>")
                 + _beat(2, "hook", 40.0, _H % 2))
ok(chapters.chapter_lines(redirect) == ["0:00 Ponto 0", "0:20 cat in.txt › out.txt",
                                        "0:40 Ponto 2"],
   "a shell redirect in a code line survives as › (no literal >)")

# _text already strips ends, so the post-slice strip is only observable when
# char 60 of a longer headline is whitespace (the window ends on a space).
_H_CLAMP = ('<div class="htext">' + "W" * 59 + " TAILMORE</div>")
_H_B = '<div class="htext">BBB</div>'
_H_C = '<div class="htext">CCC</div>'
clamped_space = _mini(_beat(0, "hook", 0.0, _H_CLAMP) + _beat(1, "hook", 20.0, _H_B)
                      + _beat(2, "hook", 40.0, _H_C))
ok(chapters.chapter_lines(clamped_space) == [
    "0:00 " + "W" * 59, "0:20 BBB", "0:40 CCC",
], "clamp is slice-then-strip: a space at the 60th char is stripped off the window "
   "(a no-strip clamp keeps a trailing space; strip-then-slice also keeps that space)")

frac = _mini(_beat(0, "hook", 0.9, _H % 0) + _beat(1, "hook", 20.0, _H % 1)
             + _beat(2, "hook", 40.0, _H % 2))
ok(chapters.chapter_lines(frac) == ["0:00 Ponto 0", "0:20 Ponto 1", "0:40 Ponto 2"],
   "a first beat at 0.9s floors to displayed 0:00 (YouTube compares displayed seconds)")

ok(chapters.chapter_lines(
    _mini(_beat(0, "hook", 1.0, _H % 0) + _beat(1, "hook", 20.0, _H % 1)
          + _beat(2, "hook", 40.0, _H % 2))) is None,
   "first extractable chapter at 1s (displayed 0:01) -> None, never a list starting at 0:01")

exact_gap = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 10.0, _H % 1)
                  + _beat(2, "hook", 20.0, _H % 2))
ok(chapters.chapter_lines(exact_gap) == ["0:00 Ponto 0", "0:10 Ponto 1", "0:20 Ponto 2"],
   "exactly 10s of displayed gap is kept (`< 10` not `<= 10`)")

folded_nine = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 9.0, _H % 1)
                    + _beat(2, "hook", 20.0, _H % 2) + _beat(3, "hook", 40.0, _H % 3))
ok(chapters.chapter_lines(folded_nine) == ["0:00 Ponto 0", "0:20 Ponto 2", "0:40 Ponto 3"],
   "a 9s displayed gap is folded into the previous kept chapter")

tail_eq = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 20.0, _H % 1)
                + _beat(2, "hook", 40.0, _H % 2), duration=50.0)
ok(chapters.chapter_lines(tail_eq) == ["0:00 Ponto 0", "0:20 Ponto 1", "0:40 Ponto 2"],
   "a last chapter whose remaining duration is exactly 10s is kept (`< 10` not `<= 10`)")

tail_under = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "hook", 20.0, _H % 1)
                   + _beat(2, "hook", 40.1, _H % 2), duration=50.0)
ok(chapters.chapter_lines(tail_under) is None,
   "a last chapter 9.9s from the end is dropped, and the list dies if that leaves <3")

malformed = _mini(
    _beat(0, "hook", 0.0, _H % 0)
    + '<div class="beat stmt" data-start="10.0"><div class="stext">ghost</div></div>'
    + _beat(1, "hook", 20.0, _H % 1)
    + _beat(2, "hook", 40.0, _H % 2)
)
ok(chapters.chapter_lines(malformed) == ["0:00 Ponto 0", "0:20 Ponto 1", "0:40 Ponto 2"],
   "a beat div missing id=bN is skipped (does not match _BEAT_RE) without killing the list")
ok(all("ghost" not in ln for ln in chapters.chapter_lines(malformed)),
   "the malformed beat's inner text is not salvaged as a chapter")

unknown = _mini(_beat(0, "hook", 0.0, _H % 0) + _beat(1, "mystery", 20.0, _H % 1)
                + _beat(2, "hook", 40.0, _H % 2) + _beat(3, "hook", 60.0, _H % 3))
ok(chapters.chapter_lines(unknown) == ["0:00 Ponto 0", "0:40 Ponto 2", "1:00 Ponto 3"],
   "an unknown beat class produces no headline; the other beats still form a valid list")

# ---------------------------------------------------------------- path resolution

with tempfile.TemporaryDirectory() as td:
    _orig = settings.hyperframes_storage_dir
    settings.hyperframes_storage_dir = td
    try:
        job = Path(td) / "handle1"
        job.mkdir()
        (job / "index.html").write_text(HTML)
        v = SimpleNamespace(engine="hyperframes", mpt_task_id="handle1")
        ok(chapters.chapter_lines_for_video(v) == lines,
           "chapter_lines_for_video resolves the job dir via the engine registry")
        v2 = SimpleNamespace(engine="hyperframes", mpt_task_id="gone")
        ok(chapters.chapter_lines_for_video(v2) is None,
           "a missing index.html (cleaned/foreign job dir) -> None")
        v3 = SimpleNamespace(engine="hyperframes", mpt_task_id=None)
        ok(chapters.chapter_lines_for_video(v3) is None, "no engine handle -> None")
        v4 = SimpleNamespace(engine="mpt", mpt_task_id="no-such-task-xyz")
        ok(chapters.chapter_lines_for_video(v4) is None,
           "an MPT-rendered video (no index.html contract) -> None")
        bad = Path(td) / "handle2"
        (bad / "index.html").mkdir(parents=True)   # read_text will raise IsADirectoryError
        v5 = SimpleNamespace(engine="hyperframes", mpt_task_id="handle2")
        ok(chapters.chapter_lines_for_video(v5) is None,
           "an unreadable index.html is swallowed -> None (publish never fails)")
        ok(chapters.chapter_lines_for_video(SimpleNamespace()) is None,
           "a video missing engine/handle attributes is swallowed -> None")
        ok(chapters.chapter_lines_for_video(SimpleNamespace(mpt_task_id="handle1")) is None,
           "a video with a handle but no engine attribute is swallowed -> None")
    finally:
        settings.hyperframes_storage_dir = _orig

_orig_ge = _engines.get_engine

def _boom(name):
    raise RuntimeError("boom")

_engines.get_engine = _boom
try:
    try:
        _boom_got = chapters.chapter_lines_for_video(
            SimpleNamespace(engine="hyperframes", mpt_task_id="handle1"))
    except Exception as e:
        print("FAIL:", "get_engine raising is swallowed -> None "
              "(chapters must never fail a publish)", type(e).__name__)
        sys.exit(1)
    ok(_boom_got is None,
       "get_engine raising is swallowed -> None (chapters must never fail a publish)")
    _ge_calls = []

    def _count(name):
        _ge_calls.append(name)
        raise AssertionError("get_engine should not run for a falsy handle")

    _engines.get_engine = _count
    ok(chapters.chapter_lines_for_video(
        SimpleNamespace(engine="hyperframes", mpt_task_id="")) is None
       and _ge_calls == [],
       "an empty-string handle is treated like missing (falsy), no get_engine")
finally:
    _engines.get_engine = _orig_ge

# ---------------------------------------------------------------- finalize_description

CH = ["0:00 Abertura", "0:15 O problema", "0:40 A solução"]
full = metadata.finalize_description("Base.", "pt-BR", "UCx", "PLx", chapter_lines=CH)
ok("⏱ Capítulos:\n0:00 Abertura\n0:15 O problema\n0:40 A solução" in full,
   "chapters appear as one localized block (pt header)")
ok(full.index("Capítulos") < full.index("sub_confirmation=1"),
   "chapters come before the subscribe-CTA block")
ok(full.startswith("Base."), "the original description stays first")
ok(metadata.finalize_description(full, "pt-BR", "UCx", "PLx", chapter_lines=CH) == full,
   "publish retry with the CTA marker present appends nothing (idempotent)")

en = metadata.finalize_description("Base.", "en-US", "UCx", None, chapter_lines=CH)
ok("⏱ Chapters:" in en, "en localization for the chapters header")

only_ch = metadata.finalize_description("Base.", "en-US", None, None, chapter_lines=CH)
ok(only_ch == "Base.\n\n⏱ Chapters:\n" + "\n".join(CH),
   "chapters append even when there is no channel/playlist link to add")
ok(metadata.finalize_description(only_ch, "en-US", None, None, chapter_lines=CH) == only_ch,
   "retry without the CTA marker still never double-appends chapters (header guard)")

ok(metadata.finalize_description("Base.", "pt-BR", "UCx", "PLx") ==
   metadata.finalize_description("Base.", "pt-BR", "UCx", "PLx", chapter_lines=None),
   "chapter_lines=None is the default (call sites without chapters are unchanged)")
old = metadata.finalize_description("Base.", "pt-BR", "UCx", "PLx")
ok(old == ("Base.\n\n"
           "🔔 Inscreva-se — engenharia de IA na prática, todos os dias: "
           "https://www.youtube.com/channel/UCx?sub_confirmation=1\n"
           "▶ Série completa: https://www.youtube.com/playlist?list=PLx"),
   "without chapters the output is byte-identical to the pre-change format")
ok(metadata.finalize_description("", "en-US", None, None, chapter_lines=None) == "",
   "no chapters and no links still returns the bare description")
ok(len(metadata.finalize_description("x" * 6000, "en-US", "UCx", None,
                                     chapter_lines=CH)) <= 5000,
   "the 5000-char YouTube description cap still applies")
es = metadata.finalize_description("Base.", "es-ES", "UCx", None, chapter_lines=CH)
ok("⏱ Chapters:" in es, "unknown BCP-47 prefix (es) falls through to the EN chapters header")
ok("Capítulos" not in es, "es does not invent a Spanish chapters header")

print(f"\nALL {_checks} CHECKS PASSED")
