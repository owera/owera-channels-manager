"""Dependency-free regression checks for app/services/chapters.py (backlog #13).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_chapters.py

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
  - chapter_lines_for_video: engine-registry path resolution, missing file /
    missing handle / unreadable file -> None, never raises
  - finalize_description: chapters before the CTA block, localized header,
    idempotent on publish retries, byte-identical behavior when chapter_lines=None

No network, no DB, no live YouTube. Exits non-zero on the first failed assertion.
"""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.config import settings
from app.services import chapters, metadata
from app.services.engines import storyboard, theme

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
    finally:
        settings.hyperframes_storage_dir = _orig

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

print(f"\nALL {_checks} CHECKS PASSED")
