"""Long-form YouTube chapters derived from the storyboard baked into a render.

The typed-storyboard composition (engines/storyboard.py) writes every beat into the
render job dir's index.html as
    <div class="beat <cls>" id="bN" data-start="…" data-duration="…">…</div>
and the job dir outlives the render (storage/hyperframes/<handle>/ is never cleaned),
so at publish time the beat starts are still on disk — including for videos rendered
before this module existed. We re-read that file and turn beat starts into
"M:SS <headline>" chapter lines for the video description.

YouTube only renders a chapter list when it starts at 0:00, has at least three
entries, and every chapter runs at least 10 seconds — violating any of these makes
it silently ignore the whole list — so those rules are enforced here: beats closer
than 10s to the previous kept chapter (or to the end of the video) are folded into
it, the closing `cta` beat (the subscribe card) never becomes a chapter, and fewer
than three survivors yields no chapters at all.

Best-effort by design: any surprise (missing file, an MPT-rendered video, the
fallback composition without beat divs, unparseable markup) yields None and the
publish proceeds without chapters.
"""

import html as _html
import re

# YouTube's own thresholds for rendering a chapter list.
_MIN_CHAPTER_SECONDS = 10
_MIN_CHAPTERS = 3
_MAX_HEADLINE_CHARS = 60

_ROOT_DUR_RE = re.compile(r'id="root"[^>]*data-duration="([0-9.]+)"')
_BEAT_RE = re.compile(r'<div class="beat ([a-z_]+)" id="b\d+" data-start="([0-9.]+)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    """Strip tags, unescape entities, collapse whitespace. Literal angle brackets
    (common in code/command beats: `List<String>`, `cat a > b`) are transliterated —
    the YouTube API rejects descriptions containing < or >, and a rejected upload
    would strand the video with the bad description already committed."""
    text = " ".join(_html.unescape(_TAG_RE.sub(" ", fragment)).split()).strip()
    return text.replace("<", "‹").replace(">", "›")


def _div(chunk: str, cls: str) -> str:
    m = re.search(r'<div class="' + re.escape(cls) + r'"[^>]*>(.*?)</div>', chunk, re.DOTALL)
    return _text(m.group(1)) if m else ""


def _span(chunk: str, cls: str) -> str:
    m = re.search(r'<span class="' + re.escape(cls) + r'"[^>]*>(.*?)</span>', chunk, re.DOTALL)
    return _text(m.group(1)) if m else ""


def _headline(cls: str, chunk: str) -> str:
    """The chapter title for one beat, from the markup each renderer emits
    (engines/storyboard.py render_*). Empty = this beat gets no chapter."""
    if cls == "hook":
        return _div(chunk, "htext")
    if cls == "stmt":
        return _div(chunk, "stext")
    if cls == "quote":
        return _div(chunk, "qtext")
    if cls == "term":
        return _div(chunk, "term-word")
    if cls == "stat":
        label = _div(chunk, "stat-label")
        if label:
            return label
        num = _span(chunk, "stat-num")
        # A purely numeric value renders as the count-up placeholder "0" (the real
        # number only exists in the GSAP tween) — never title a chapter with it.
        if num and num != "0":
            unit = _span(chunk, "stat-unit")
            return (num + " " + unit).strip() if unit else num
        return ""
    if cls == "cmp":
        title = _div(chunk, "cmp-title")
        if title:
            return title
        h3s = re.findall(r"<h3>(.*?)</h3>", chunk)
        if len(h3s) >= 2:
            return _text(h3s[0]) + " vs " + _text(h3s[1])
        return ""
    if cls == "lst":
        title = _div(chunk, "lst-title")
        if title:
            return title
        m = re.search(r'<span class="lst-bullet">.*?</span><span>(.*?)</span>', chunk)
        return _text(m.group(1)) if m else ""
    if cls == "code":
        m = re.search(r'<span class="ln(?: hl)?">(.*?)</span>', chunk)
        return _text(m.group(1)) if m else ""
    if cls == "cmd":
        return _span(chunk, "cmd-cmd")
    if cls == "diagram":
        nodes = re.findall(r'<g class="node">.*?<text[^>]*>(.*?)</text>', chunk, re.DOTALL)
        return " → ".join(t for t in (_text(n) for n in nodes) if t)
    return ""  # cta (the subscribe card is not a chapter) and unknown classes


def _stamp(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapter_lines(index_html: str) -> list[str] | None:
    """Derive "M:SS <headline>" chapter lines from a composed index.html.
    None whenever the result would not render as chapters on YouTube."""
    m = _ROOT_DUR_RE.search(index_html)
    if not m:
        return None
    duration = float(m.group(1))
    region = index_html.split("<script>", 1)[0]
    kept: list[tuple[int, str]] = []
    for chunk in re.split(r'(?=<div class="beat )', region)[1:]:
        bm = _BEAT_RE.match(chunk)
        if not bm:
            continue
        start = float(bm.group(2))
        headline = _headline(bm.group(1), chunk)[:_MAX_HEADLINE_CHARS].strip()
        if not headline:
            continue
        # Compare on displayed (floored) seconds — that is what YouTube sees.
        sec = int(start)
        if kept and sec - kept[-1][0] < _MIN_CHAPTER_SECONDS:
            continue
        if duration - start < _MIN_CHAPTER_SECONDS:
            continue
        kept.append((sec, headline))
    if len(kept) < _MIN_CHAPTERS or kept[0][0] > 0:
        return None
    return [f"{_stamp(sec)} {headline}" for sec, headline in kept]


def chapter_lines_for_video(video) -> list[str] | None:
    """Locate the video's render job dir via the engine registry and derive chapters.
    Never raises — a publish must not fail over chapters."""
    try:
        if not video.mpt_task_id:
            return None
        from app.services.engines import get_engine
        index = (get_engine(video.engine).final_path(video.mpt_task_id).parent
                 / "index.html")
        if not index.is_file():
            return None
        return chapter_lines(index.read_text())
    except Exception:
        return None
