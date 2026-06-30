"""Typed-storyboard composition for the HyperFrames engine.

Instead of asking the LLM for free-form HTML (which failed ~30-40% of the time and
caused a blank-render incident) or for evenly-spaced text "clips" (which merely echo
the narration), we ask it for a compact, schema-validated **storyboard of typed
beats**. Each beat carries content + a ``cue`` (the verbatim narration words it should
land on) but NO timing numbers. The server derives every ``start``/``duration`` by
aligning the cue against edge-tts WordBoundary data, then a deterministic component
renderer turns typed beats into one self-contained ``index.html`` on the single GSAP
master timeline.

Design rules:
  * Output is valid by CONSTRUCTION — trusted per-type renderers, never model HTML.
  * Every beat fades fully out before/as the next begins → one visible at a time,
    matching the no-overlap discipline of worker._fallback_composition.
  * Every renderer emits ≥1 GSAP tween so worker._looks_valid stays a real guard.
  * This module imports only ``theme`` (a leaf) and takes the LLM callable as a
    parameter, so it never imports ``worker`` (no import cycle).
"""

import json
import logging
import re

from app.services.engines import theme

logger = logging.getLogger("manager.storyboard")

# Beat counts and the small float gap between consecutive beats (matches the 0.12s
# rounding tolerance in worker._validate_clips).
_MIN_BEATS = 4
_MAX_BEATS = 14
_GAP = 0.12
_MIN_DUR = 0.5


# --------------------------------------------------------------------------- JS/string helpers
# Tween strings are built by plain concatenation (NOT f-strings) so literal JS braces
# stay readable. ``frm``/``to`` are raw GSAP vars object bodies, e.g. "opacity:0,y:28".

def _r(x: float) -> str:
    return str(round(float(x), 3))


def _set_on(sel: str, t: float) -> str:
    """Make a beat container visible exactly at t (it is opacity:0 before)."""
    return "tl.set('" + sel + "',{opacity:1,immediateRender:false}," + _r(t) + ");"


def _from(sel: str, t: float, frm: str, to: str, dur: float = 0.3,
          ease: str = "power2.out", stagger: float | None = None) -> str:
    s = "tl.fromTo('" + sel + "',{" + frm + "},{" + to + ",duration:" + _r(dur) + ",ease:'" + ease + "'"
    if stagger is not None:
        s += ",stagger:" + _r(stagger)
    s += "}," + _r(t) + ");"
    return s


def _to(sel: str, t: float, to: str, dur: float = 0.45, ease: str = "power2.in") -> str:
    return "tl.to('" + sel + "',{" + to + ",duration:" + _r(dur) + ",ease:'" + ease + "'}," + _r(t) + ");"


def _fade_out(sel: str, end: float, dur: float = 0.45, extra: str = "") -> str:
    return _to(sel, max(0.0, end - dur), "opacity:0" + extra, dur=dur)


def _countup(sel: str, target: float, t: float, dur: float = 0.9) -> str:
    """A GSAP count-up that writes Math.round into the element's textContent."""
    return ("(function(){var o={v:0};var el=document.querySelector('" + sel + "');"
            "tl.to(o,{v:" + _r(target) + ",duration:" + _r(dur) + ",ease:'power1.out',snap:{v:1},"
            "onUpdate:function(){if(el)el.textContent=String(Math.round(o.v));}}," + _r(t) + ");})();")


def _words_html(text: str) -> str:
    return " ".join('<span class="word">' + theme.esc(w) + "</span>" for w in str(text).split())


# --------------------------------------------------------------------------- schema / parse

# Required fields per beat type. Phase A is renderable today; code/command/diagram are
# accepted by the schema but only requested + rendered once the config allowlist
# (settings.composition_beat_types) includes them and their renderers exist (Phase B/C).
_BEAT_SPECS = {
    "hook":        {"req": ["text"]},
    "statement":   {"req": ["text"]},
    "stat":        {"req": ["value"]},
    "compare":     {"req": ["left", "right"]},
    "list":        {"req": ["items"]},
    "term_define": {"req": ["term", "definition"]},
    "quote":       {"req": ["text"]},
    "cta":         {"req": ["text"]},
    "code":        {"req": ["lines"]},
    "command":     {"req": ["command"]},
    "diagram":     {"req": ["nodes"]},
}


def _words_clip(text, n: int) -> str:
    return " ".join(str(text).split()[:n]).strip()


def _chars_clip(text, n: int) -> str:
    s = str(text).strip()
    return s[:n].strip()


def _coerce_beat(raw: dict, allowed: set) -> dict | None:
    """Validate + clamp one raw beat. Unknown/out-of-allowlist types downgrade to
    ``statement``. Returns None only if it can't be salvaged into anything."""
    if not isinstance(raw, dict):
        return None
    btype = raw.get("type")
    cue = _chars_clip(raw.get("cue", ""), 160)
    emoji = _chars_clip(raw.get("emoji", ""), 2)

    if btype not in _BEAT_SPECS or btype not in allowed:
        # downgrade: keep the message as a statement so the beat count/arc survives.
        text = _words_clip(raw.get("text") or raw.get("term") or raw.get("title") or cue, 8)
        return {"type": "statement", "cue": cue, "text": text, "w": 2, "emoji": emoji} if text else None

    if btype in ("hook", "statement", "quote"):
        text = _words_clip(raw.get("text", ""), 16 if btype == "quote" else 8)
        if not text:
            return None
        b = {"type": btype, "cue": cue, "text": text, "emoji": emoji}
        if btype == "statement":
            try:
                b["w"] = max(1, min(3, int(raw.get("w", 1))))
            except (TypeError, ValueError):
                b["w"] = 1
        if btype == "quote":
            b["attribution"] = _words_clip(raw.get("attribution", ""), 6)
        return b

    if btype == "stat":
        value = _chars_clip(raw.get("value", ""), 12)
        if not value:
            return None
        return {"type": "stat", "cue": cue, "value": value,
                "unit": _chars_clip(raw.get("unit", ""), 8),
                "label": _words_clip(raw.get("label", ""), 6), "emoji": emoji}

    if btype == "compare":
        def _col(c):
            c = c if isinstance(c, dict) else {}
            return {"title": _words_clip(c.get("title", ""), 4),
                    "items": [_words_clip(x, 4) for x in (c.get("items") or [])][:3]}
        left, right = _col(raw.get("left")), _col(raw.get("right"))
        if not (left["title"] or left["items"]) or not (right["title"] or right["items"]):
            return None
        return {"type": "compare", "cue": cue, "title": _words_clip(raw.get("title", ""), 5),
                "left": left, "right": right}

    if btype == "list":
        items = []
        for it in (raw.get("items") or [])[:5]:
            if isinstance(it, dict):
                t = _words_clip(it.get("text", ""), 6)
                e = _chars_clip(it.get("emoji", ""), 2)
            else:
                t, e = _words_clip(it, 6), ""
            if t:
                items.append({"text": t, "emoji": e})
        if not items:
            return None
        return {"type": "list", "cue": cue, "title": _words_clip(raw.get("title", ""), 6),
                "ordered": bool(raw.get("ordered", False)), "items": items}

    if btype == "term_define":
        term = _words_clip(raw.get("term", ""), 4)
        definition = _words_clip(raw.get("definition", ""), 14)
        if not term or not definition:
            return None
        return {"type": "term_define", "cue": cue, "term": term, "definition": definition}

    if btype == "cta":
        return {"type": "cta", "cue": cue, "text": _words_clip(raw.get("text", "") or "Subscribe", 4),
                "sub": _words_clip(raw.get("sub", ""), 6)}

    # Phase B/C: accept + clamp here; rendered only once their renderers are registered.
    if btype == "code":
        lines = [_chars_clip(x, 60) for x in (raw.get("lines") or []) if str(x).strip()][:8]
        if not lines:
            return None
        hl = [i for i in (raw.get("highlight") or []) if isinstance(i, int)]
        return {"type": "code", "cue": cue, "lang": _chars_clip(raw.get("lang", ""), 16),
                "lines": lines, "highlight": hl}

    if btype == "command":
        cmd = _chars_clip(raw.get("command", ""), 80)
        if not cmd:
            return None
        return {"type": "command", "cue": cue, "prompt": _chars_clip(raw.get("prompt", "$"), 3),
                "command": cmd, "output": [_chars_clip(x, 60) for x in (raw.get("output") or [])][:4]}

    if btype == "diagram":
        nodes = [{"id": _chars_clip(n.get("id", ""), 12), "label": _words_clip(n.get("label", ""), 3)}
                 for n in (raw.get("nodes") or []) if isinstance(n, dict) and n.get("id")][:5]
        if len(nodes) < 2:
            return None
        edges = [{"from": _chars_clip(e.get("from", ""), 12), "to": _chars_clip(e.get("to", ""), 12),
                  "label": _words_clip(e.get("label", ""), 3)}
                 for e in (raw.get("edges") or []) if isinstance(e, dict)][:6]
        layout = raw.get("layout") if raw.get("layout") in ("pipeline", "request_response", "fanout") else "pipeline"
        return {"type": "diagram", "cue": cue, "layout": layout, "nodes": nodes, "edges": edges}

    return None


def parse_storyboard(raw: str, allowed) -> list[dict] | None:
    """Parse the LLM's storyboard JSON into clean, clamped beats. Returns None on a
    structural failure (caller then uses the deterministic fallback composition)."""
    allowed = set(allowed or [])
    try:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        data = json.loads(m.group(0) if m else raw)
    except Exception:
        return None
    beats_raw = data.get("beats") if isinstance(data, dict) else None
    if not isinstance(beats_raw, list) or not beats_raw:
        return None
    beats = []
    for rb in beats_raw[:_MAX_BEATS + 4]:
        cb = _coerce_beat(rb, allowed)
        if cb:
            beats.append(cb)
    if len(beats) < _MIN_BEATS:
        return None
    return beats[:_MAX_BEATS]


# --------------------------------------------------------------------------- alignment

_TOK_RE = re.compile(r"[a-z0-9]+")


def _tok(s: str) -> list[str]:
    return _TOK_RE.findall(theme.fold(s))


def _find_subseq(stream_tokens: list[str], needle: list[str], start: int) -> int:
    """Earliest index >= start where ``needle`` matches contiguously in the stream."""
    m, n = len(needle), len(stream_tokens)
    if m == 0:
        return -1
    for p in range(start, n - m + 1):
        if stream_tokens[p:p + m] == needle:
            return p
    return -1


def _even_space(beats: list[dict], duration: float) -> None:
    """Today's baseline: evenly spaced, non-overlapping beats."""
    n = len(beats)
    step = duration / n
    for i, b in enumerate(beats):
        b["start"] = round(i * step, 3)
        b["dur"] = round((duration - b["start"]) if i == n - 1 else step, 3)


def align_storyboard(beats: list[dict], words: list[dict], duration: float) -> list[dict]:
    """Derive each beat's start/duration from real edge-tts word timings by matching
    its ``cue`` against the spoken-word stream. Degrades to interpolation for unmatched
    cues, and to even spacing when no timings are available — never worse than baseline."""
    n = len(beats)
    if n == 0:
        return beats

    tokens, tok_start = [], []
    for w in (words or []):
        ws = float(w.get("start") or 0.0)
        for t in _tok(w.get("text", "")):
            tokens.append(t)
            tok_start.append(ws)

    if not tokens:
        _even_space(beats, duration)
        return beats

    starts: list[float | None] = [None] * n
    cursor = 0
    matched = 0
    for i, b in enumerate(beats):
        ct = _tok(b.get("cue", ""))
        pos = _find_subseq(tokens, ct, cursor)
        if pos >= 0:
            starts[i] = tok_start[pos]
            cursor = pos + len(ct)
            matched += 1

    if matched == 0:
        _even_space(beats, duration)
        return beats

    # Interpolate unmatched starts between known anchors (virtual 0.0 at -1, duration at n).
    known = [(-1, 0.0)] + [(i, s) for i, s in enumerate(starts) if s is not None] + [(n, duration)]
    for a in range(len(known) - 1):
        li, ls = known[a]
        ri, rs = known[a + 1]
        span = ri - li
        for k in range(li + 1, ri):
            starts[k] = ls + (rs - ls) * ((k - li) / span)

    # The opening beat must cover the start — a short with dead air over its first
    # seconds is the worst case (the hook is everything). Pin beat 0 to 0.0 regardless
    # of where its cue matched; later beats stay word-synced.
    starts[0] = 0.0

    # Enforce monotonic minimum spacing, then derive durations with the inter-beat gap.
    for i in range(1, n):
        if starts[i] < starts[i - 1] + _MIN_DUR:
            starts[i] = starts[i - 1] + _MIN_DUR
    for i, b in enumerate(beats):
        b["start"] = round(max(0.0, starts[i]), 3)
        end = duration if i == n - 1 else max(starts[i] + _MIN_DUR, starts[i + 1] - _GAP)
        b["dur"] = round(max(_MIN_DUR, end - starts[i]), 3)
    return beats


def validate_storyboard(beats: list[dict], duration: float) -> bool:
    """Monotonic, non-overlapping, in-bounds, min-duration — extends _validate_clips."""
    if not beats:
        return False
    prev_end = -1.0
    for b in beats:
        s, d = b.get("start"), b.get("dur")
        if s is None or d is None:
            return False
        e = s + d
        if s < -0.05 or e > duration + 0.6 or d < _MIN_DUR:
            return False
        if s < prev_end - _GAP:
            return False
        prev_end = e
    return True


# --------------------------------------------------------------------------- CSS

def _base_css(width: int, height: int, th: dict) -> str:
    fs = max(32, int(width * 0.065))
    body_fs = max(28, int(width * 0.045))
    pad = max(60, int(width * 0.08))
    variant = th.get("bg_variant", "bloom")
    bg_motion = {
        "bloom":    "radial-gradient(ellipse at 80% 12%,var(--accent) 0%,transparent 55%)",
        "dots":     "radial-gradient(var(--accent) 1.5px,transparent 1.6px);background-size:44px 44px",
        "scan":     "repeating-linear-gradient(0deg,transparent 0 22px,var(--accent) 22px 23px)",
        "gradient": "radial-gradient(ellipse at 35% 22%,var(--accent) 0%,transparent 60%)",
        "overlay":  "linear-gradient(145deg,var(--accent) 0%,transparent 80%)",
    }.get(variant, "radial-gradient(ellipse at 80% 12%,var(--accent) 0%,transparent 55%)")
    return (
        ":root{--accent:" + th["accent"] + ";--bg:" + th["bg_base"] + ";--bg-deep:" + th["bg_deep"] +
        ";--fg:" + th["fg"] + ";--fg-dim:" + th["fg_dim"] + ";--mono:" + th["mono"] +
        ";--fs:" + str(fs) + "px;--body-fs:" + str(body_fs) + "px;--pad:" + str(pad) + "px}"
        "html,body{margin:0;padding:0;width:" + str(width) + "px;height:" + str(height) + "px;overflow:hidden;"
        "background:radial-gradient(120% 120% at 20% 0%,var(--bg-deep) 0%,var(--bg) 62%);"
        "font-family:" + th["sans"] + ";color:var(--fg)}"
        "#root{width:" + str(width) + "px;height:" + str(height) + "px;position:relative}"
        "#bg-motion{position:absolute;inset:0;z-index:0;pointer-events:none;opacity:.32;background:" + bg_motion + "}"
        ".beat{position:absolute;inset:0;z-index:1;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;gap:.4em;padding:0 var(--pad);box-sizing:border-box;"
        "text-align:center;opacity:0}"
        ".word{display:inline-block}"
        # hook
        ".hook .htext{font-size:calc(var(--fs)*1.28);font-weight:800;line-height:1.12;letter-spacing:-1px;"
        "text-shadow:0 4px 24px rgba(0,0,0,.7)}.hook .hemoji{font-size:calc(var(--fs)*1.1);line-height:1}"
        # statement
        ".stmt .stext{font-size:var(--fs);font-weight:800;line-height:1.3;letter-spacing:-.5px;"
        "text-shadow:0 3px 18px rgba(0,0,0,.6)}.stmt .semoji{font-size:calc(var(--fs)*.9);line-height:1}"
        # stat
        ".stat .stat-num{font-size:calc(var(--fs)*2.4);font-weight:900;color:var(--accent);line-height:1}"
        ".stat .stat-unit{font-size:calc(var(--fs)*1.1);font-weight:800;color:var(--accent)}"
        ".stat .stat-label{font-size:var(--body-fs);color:var(--fg-dim);font-weight:600;margin-top:.2em}"
        ".stat .stat-row{display:flex;align-items:baseline;justify-content:center;gap:.12em}"
        # compare
        ".cmp{flex-direction:column}.cmp .cmp-title{font-size:var(--body-fs);color:var(--fg-dim);"
        "font-weight:700;margin-bottom:.5em}"
        ".cmp .cmp-cols{display:flex;gap:1.1em;align-items:stretch;justify-content:center;width:100%}"
        ".cmp .cmp-col{flex:1;max-width:42%;background:rgba(255,255,255,.06);border-radius:18px;"
        "padding:.7em .5em;border-top:5px solid var(--accent)}"
        ".cmp .cmp-col h3{margin:0 0 .35em;font-size:calc(var(--fs)*.78);font-weight:800}"
        ".cmp .cmp-col .ci{font-size:var(--body-fs);color:var(--fg-dim);line-height:1.5;font-weight:600}"
        ".cmp .cmp-vs{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);"
        "background:var(--accent);color:#08080f;font-weight:900;border-radius:999px;"
        "width:1.7em;height:1.7em;display:flex;align-items:center;justify-content:center;"
        "font-size:calc(var(--fs)*.6);z-index:3}"
        # list
        ".lst{justify-content:center}.lst .lst-title{font-size:calc(var(--fs)*.95);font-weight:800;"
        "margin-bottom:.55em}.lst .lst-row{font-size:var(--body-fs);font-weight:700;line-height:1.5;"
        "display:flex;align-items:center;gap:.4em;opacity:0}"
        ".lst .lst-bullet{color:var(--accent);font-weight:900}"
        # term_define
        ".term .term-word{font-size:calc(var(--fs)*1.4);font-weight:900;color:var(--accent);line-height:1.05}"
        ".term .term-rule{height:5px;width:42%;background:var(--accent);margin:.45em auto;transform-origin:left}"
        ".term .term-def{font-size:var(--body-fs);color:var(--fg-dim);font-weight:600;line-height:1.4}"
        # quote
        ".quote .qtext{font-size:calc(var(--fs)*1.05);font-weight:700;font-style:italic;line-height:1.3}"
        ".quote .qmark{color:var(--accent);font-size:calc(var(--fs)*1.8);font-weight:900;line-height:.2}"
        ".quote .qattr{font-size:var(--body-fs);color:var(--fg-dim);margin-top:.4em}"
        # cta
        ".cta .cta-box{background:var(--accent);color:#08080f;font-weight:900;border-radius:18px;"
        "padding:.5em .9em;font-size:calc(var(--fs)*1.05);display:inline-flex;align-items:center;gap:.3em}"
        ".cta .cta-sub{font-size:var(--body-fs);color:var(--fg-dim);margin-top:.5em;font-weight:600}"
        # --- Phase B/C (code / command / diagram) ---
        ".code-lang{font-family:var(--mono);font-size:var(--body-fs);color:var(--accent);"
        "font-weight:700;margin-bottom:.3em;text-transform:lowercase}"
        ".code{font-family:var(--mono);font-size:calc(var(--fs)*0.6);line-height:1.5;color:#e8ecff;"
        "background:#121521;border-radius:18px;padding:.6em .75em;text-align:left;white-space:pre;"
        "width:100%;box-sizing:border-box;border:1px solid #2a2f45;overflow:hidden;margin:0}"
        ".code .ln{display:block;opacity:0}"
        ".code .ln.hl{background:rgba(255,255,255,.08);border-left:5px solid var(--accent);"
        "margin-left:-.75em;padding-left:calc(.75em - 5px)}"
        ".cmd{font-family:var(--mono);font-size:calc(var(--fs)*0.58);background:#0d0f17;border-radius:16px;"
        "padding:.7em .8em;text-align:left;width:100%;box-sizing:border-box;border:1px solid #2a2f45;line-height:1.6}"
        ".cmd .cmd-prompt{color:var(--accent);font-weight:800}.cmd .cmd-cmd{color:#fff}"
        ".cmd .cmd-out{display:block;color:#9aa3c0;opacity:0}"
        ".diagram .dsvg{width:92%;height:auto;max-height:72%}"
        ".diagram .node rect{fill:#161a2b;stroke:var(--accent);stroke-width:3}"
        ".diagram .node text{fill:var(--fg);font-size:34px;font-weight:700}"
        ".diagram .node{opacity:0}"
        ".diagram .edge{stroke:var(--accent);stroke-width:4;fill:none}"
        ".diagram .elabel{fill:var(--fg-dim);font-size:26px;opacity:0}"
    )


# --------------------------------------------------------------------------- Phase A renderers
# Each returns (html_fragment, [tween_strings]). Container is opacity:0 in CSS; we
# tl.set it visible at start and fade it out before the next beat (unless last).

def _shell(i: int, b: dict, cls: str, inner: str) -> str:
    return ('<div class="beat ' + cls + '" id="b' + str(i) + '" data-start="' + _r(b["start"]) +
            '" data-duration="' + _r(b["dur"]) + '" data-track-index="' + str(i) + '">' + inner + "</div>")


def _wrap(i: int, ctx: dict, base_tweens: list[str]) -> list[str]:
    """Prepend the container reveal and append the exit fade (unless last beat)."""
    bid = "#b" + str(i)
    tw = [_set_on(bid, ctx["start"])] + base_tweens
    if not ctx["is_last"]:
        tw.append(_fade_out(bid, ctx["start"] + ctx["dur"]))
    return tw


def render_hook(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    emoji = ('<div class="hemoji">' + theme.esc(b["emoji"]) + "</div>") if b.get("emoji") else ""
    inner = emoji + '<div class="htext">' + _words_html(b["text"]) + "</div>"
    tw = []
    if b.get("emoji"):
        tw.append(_from(bid + " .hemoji", s, "opacity:0,scale:0.4", "opacity:1,scale:1", dur=0.2, ease="back.out(2)"))
    tw.append(_from(bid + " .word", s + 0.05, "opacity:0,y:30", "opacity:1,y:0", dur=0.3, stagger=0.045))
    return _shell(i, b, "hook", inner), _wrap(i, ctx, tw)


def render_statement(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    w = b.get("w", 1)
    style = ""
    if w == 3:
        style = ' style="font-size:calc(var(--fs)*1.3);color:var(--accent)"'
    elif w == 2:
        style = ' style="font-size:calc(var(--fs)*1.12)"'
    emoji = ('<div class="semoji">' + theme.esc(b["emoji"]) + "</div>") if b.get("emoji") else ""
    inner = emoji + '<div class="stext"' + style + ">" + _words_html(b["text"]) + "</div>"
    tw = []
    if b.get("emoji"):
        tw.append(_from(bid + " .semoji", s, "opacity:0,scale:0.4", "opacity:1,scale:1", dur=0.18, ease="back.out(2)"))
    tw.append(_from(bid + " .word", s, "opacity:0,y:24", "opacity:1,y:0", dur=0.28, stagger=0.04))
    if w == 3:
        tw.append(_to(bid + " .stext", s + 0.32, "scale:1.05", dur=0.16, ease="power1.inOut"))
    return _shell(i, b, "stmt", inner), _wrap(i, ctx, tw)


_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


def render_stat(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    value, unit = b["value"], b.get("unit", "")
    label = b.get("label", "")
    num_html = '<span class="stat-num">' + ("0" if _NUM_RE.match(value) else theme.esc(value)) + "</span>"
    unit_html = ('<span class="stat-unit">' + theme.esc(unit) + "</span>") if unit else ""
    label_html = ('<div class="stat-label">' + _words_html(label) + "</div>") if label else ""
    inner = '<div class="stat-row">' + num_html + unit_html + "</div>" + label_html
    tw = [_from(bid + " .stat-num", s, "opacity:0,scale:0.55", "opacity:1,scale:1", dur=0.3, ease="back.out(1.7)")]
    if _NUM_RE.match(value):
        tw.append(_countup(bid + " .stat-num", float(value), s + 0.1, dur=min(1.1, max(0.5, ctx["dur"] * 0.5))))
    if unit:
        tw.append(_from(bid + " .stat-unit", s + 0.2, "opacity:0", "opacity:1", dur=0.25))
    if label:
        tw.append(_from(bid + " .stat-label", s + 0.25, "opacity:0,y:16", "opacity:1,y:0", dur=0.3))
    return _shell(i, b, "stat", inner), _wrap(i, ctx, tw)


def render_compare(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)

    def col(c, side):
        items = "".join('<div class="ci ' + side + '-i">' + theme.esc(x) + "</div>" for x in c["items"])
        title = ("<h3>" + theme.esc(c["title"]) + "</h3>") if c["title"] else ""
        return '<div class="cmp-col ' + side + '">' + title + items + "</div>"

    title = ('<div class="cmp-title">' + theme.esc(b["title"]) + "</div>") if b.get("title") else ""
    inner = (title + '<div class="cmp-cols">' + col(b["left"], "l") +
             '<div class="cmp-vs">VS</div>' + col(b["right"], "r") + "</div>")
    tw = [
        _from(bid + " .cmp-col.l", s, "opacity:0,x:-60", "opacity:1,x:0", dur=0.35, ease="power3.out"),
        _from(bid + " .cmp-col.r", s + 0.08, "opacity:0,x:60", "opacity:1,x:0", dur=0.35, ease="power3.out"),
        _from(bid + " .cmp-vs", s + 0.3, "opacity:0,scale:0.3", "opacity:1,scale:1", dur=0.25, ease="back.out(2)"),
        _from(bid + " .ci", s + 0.35, "opacity:0,y:14", "opacity:1,y:0", dur=0.25, stagger=0.06),
    ]
    if b.get("title"):
        tw.insert(0, _from(bid + " .cmp-title", s, "opacity:0", "opacity:1", dur=0.25))
    return _shell(i, b, "cmp", inner), _wrap(i, ctx, tw)


def render_list(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    title = ('<div class="lst-title">' + theme.esc(b["title"]) + "</div>") if b.get("title") else ""
    rows = []
    for j, it in enumerate(b["items"]):
        if b.get("ordered"):                       # numbers win — don't mix with emoji bullets
            bullet = str(j + 1) + "."
        elif it.get("emoji"):
            bullet = theme.esc(it["emoji"])
        else:
            bullet = "•"
        rows.append('<div class="lst-row" id="b' + str(i) + 'r' + str(j) + '">'
                    '<span class="lst-bullet">' + bullet + "</span><span>" + theme.esc(it["text"]) + "</span></div>")
    inner = title + "".join(rows)
    # Reveal rows across the beat window so they track the narration as it's spoken.
    n = len(b["items"])
    win = max(0.0, ctx["dur"] - 0.8)
    step = (win / n) if n else 0
    tw = []
    if b.get("title"):
        tw.append(_from(bid + " .lst-title", s, "opacity:0,y:-10", "opacity:1,y:0", dur=0.25))
    for j in range(n):
        tw.append(_from("#b" + str(i) + "r" + str(j), s + 0.25 + j * step,
                        "opacity:0,x:-22", "opacity:1,x:0", dur=0.3, ease="power2.out"))
    return _shell(i, b, "lst", inner), _wrap(i, ctx, tw)


def render_term_define(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    inner = ('<div class="term-word">' + theme.esc(b["term"]) + "</div>"
             '<div class="term-rule"></div>'
             '<div class="term-def">' + _words_html(b["definition"]) + "</div>")
    tw = [
        _from(bid + " .term-word", s, "opacity:0,scale:0.7", "opacity:1,scale:1", dur=0.3, ease="back.out(1.6)"),
        _from(bid + " .term-rule", s + 0.2, "scaleX:0", "scaleX:1", dur=0.3, ease="power2.out"),
        _from(bid + " .word", s + 0.35, "opacity:0,y:14", "opacity:1,y:0", dur=0.28, stagger=0.03),
    ]
    return _shell(i, b, "term", inner), _wrap(i, ctx, tw)


def render_quote(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    attr = ('<div class="qattr">— ' + theme.esc(b["attribution"]) + "</div>") if b.get("attribution") else ""
    inner = '<div class="qmark">“</div><div class="qtext">' + _words_html(b["text"]) + "</div>" + attr
    tw = [
        _from(bid + " .qmark", s, "opacity:0,scale:0.4", "opacity:1,scale:1", dur=0.25, ease="back.out(2)"),
        _from(bid + " .word", s + 0.1, "opacity:0,y:18", "opacity:1,y:0", dur=0.3, stagger=0.035),
    ]
    if b.get("attribution"):
        tw.append(_from(bid + " .qattr", s + 0.4, "opacity:0", "opacity:1", dur=0.3))
    return _shell(i, b, "quote", inner), _wrap(i, ctx, tw)


def render_cta(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    sub = ('<div class="cta-sub">' + theme.esc(b["sub"]) + "</div>") if b.get("sub") else ""
    inner = '<div class="cta-box">' + theme.esc(b["text"]) + ' <span class="cta-arrow">→</span></div>' + sub
    tw = [
        _from(bid + " .cta-box", s, "opacity:0,scale:0.6", "opacity:1,scale:1", dur=0.32, ease="back.out(2)"),
        _to(bid + " .cta-arrow", s + 0.5, "x:10", dur=0.4, ease="power1.inOut"),
    ]
    if b.get("sub"):
        tw.append(_from(bid + " .cta-sub", s + 0.3, "opacity:0", "opacity:1", dur=0.3))
    return _shell(i, b, "cta", inner), _wrap(i, ctx, tw)


# --------------------------------------------------------------------------- Phase B/C renderers
# Verified to render under hyperframes@0.6.97 (monospace glyphs + inline-SVG arrowheads).
# Off by default — enable per the rollout by adding the type to settings.composition_beat_types.

def render_code(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    hl = set(b.get("highlight") or [])
    lang = ('<div class="code-lang">' + theme.esc(b["lang"]) + "</div>") if b.get("lang") else ""
    lines = "".join('<span class="ln' + (" hl" if j in hl else "") + '">' + theme.esc(ln) + "</span>"
                    for j, ln in enumerate(b["lines"]))
    inner = lang + '<pre class="code">' + lines + "</pre>"
    tw = []
    if b.get("lang"):
        tw.append(_from(bid + " .code-lang", s, "opacity:0", "opacity:1", dur=0.2))
    tw.append(_from(bid + " .ln", s + 0.1, "opacity:0,x:-18", "opacity:1,x:0", dur=0.25, stagger=0.12))
    return _shell(i, b, "code", inner), _wrap(i, ctx, tw)


def render_command(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    out = "".join('<span class="cmd-out">' + theme.esc(o) + "</span>" for o in b.get("output", []))
    inner = ('<div class="cmd"><div class="cmd-line"><span class="cmd-prompt">' +
             theme.esc(b.get("prompt", "$")) + '</span> <span class="cmd-cmd">' +
             theme.esc(b["command"]) + "</span></div>" + out + "</div>")
    tw = [_from(bid + " .cmd-cmd", s, "opacity:0", "opacity:1", dur=0.2)]
    for j in range(len(b.get("output", []))):
        # children of .cmd: .cmd-line (1), then .cmd-out (2,3,...)
        tw.append(_from(bid + " .cmd-out:nth-child(" + str(j + 2) + ")",
                        s + 0.4 + j * 0.25, "opacity:0", "opacity:1", dur=0.25))
    return _shell(i, b, "cmd", inner), _wrap(i, ctx, tw)


def _diagram_svg(nodes, edges, layout, portrait):
    """Server-computed layout — never free graph auto-layout (arrow geometry is the
    failure mode). Nodes are laid in sequence (vertical for portrait, horizontal for
    landscape); edges connect node box edges with an arrowhead marker."""
    n = len(nodes)
    centers = {}   # id -> (cx, cy, near, far)  near/far = top/bottom (portrait) or left/right
    parts = ['<defs><marker id="ar" markerWidth="12" markerHeight="12" refX="9" refY="4" '
             'orient="auto"><path d="M0,0 L9,4 L0,8 Z" fill="var(--accent)"/></marker></defs>']

    def node_g(x, y, w, h, label):
        return ('<g class="node"><rect x="%d" y="%d" width="%d" height="%d" rx="16"/>'
                '<text x="%d" y="%d" text-anchor="middle" dominant-baseline="middle">%s</text></g>'
                % (x, y, w, h, x + w / 2, y + h / 2, theme.esc(label)))

    if portrait:
        vbw, bw, bh, gap = 1000, 560, 120, 78
        vbh = int(n * bh + (n - 1) * gap + 20)
        x = (vbw - bw) / 2
        for k, nd in enumerate(nodes):
            y = 10 + k * (bh + gap)
            centers[nd["id"]] = (x + bw / 2, y + bh / 2, y, y + bh)
            parts.append(node_g(x, y, bw, bh, nd["label"]))
    else:
        bw, bh, gap, vbh = 240, 110, 64, 220
        total = n * bw + (n - 1) * gap
        vbw = max(1000, int(total))
        x0 = (vbw - total) / 2
        y = (vbh - bh) / 2
        for k, nd in enumerate(nodes):
            x = x0 + k * (bw + gap)
            centers[nd["id"]] = (x + bw / 2, y + bh / 2, x, x + bw)
            parts.append(node_g(x, y, bw, bh, nd["label"]))

    drawn = edges or [{"from": nodes[k]["id"], "to": nodes[k + 1]["id"]} for k in range(n - 1)]
    e_count = 0
    for e in drawn:
        a, c = centers.get(e.get("from")), centers.get(e.get("to"))
        if not a or not c:
            continue
        if portrait:
            x1, y1, x2, y2 = a[0], a[3], c[0], c[2]
        else:
            x1, y1, x2, y2 = a[3], a[1], c[2], c[1]
        parts.append('<line class="edge" x1="%d" y1="%d" x2="%d" y2="%d" marker-end="url(#ar)"/>'
                     % (x1, y1, x2, y2))
        if e.get("label"):
            parts.append('<text class="elabel" x="%d" y="%d" text-anchor="middle">%s</text>'
                         % ((x1 + x2) / 2, (y1 + y2) / 2 - 8, theme.esc(e["label"])))
        e_count += 1
    return '<svg viewBox="0 0 %d %d" class="dsvg">%s</svg>' % (vbw, vbh, "".join(parts)), e_count


def render_diagram(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    portrait = ctx["height"] >= ctx["width"]
    svg, e_count = _diagram_svg(b["nodes"], b.get("edges") or [], b.get("layout", "pipeline"), portrait)
    tw = [
        _from(bid + " .node", s + 0.1, "opacity:0,y:18", "opacity:1,y:0", dur=0.3, ease="back.out(1.4)", stagger=0.16),
        _from(bid + " .edge", s + 0.45, "strokeDasharray:300,strokeDashoffset:300", "strokeDashoffset:0",
              dur=0.4, stagger=0.16),
    ]
    if e_count:
        tw.append(_from(bid + " .elabel", s + 0.7, "opacity:0", "opacity:1", dur=0.3, stagger=0.12))
    return _shell(i, b, "diagram", svg), _wrap(i, ctx, tw)


_RENDERERS = {
    "hook": render_hook,
    "statement": render_statement,
    "stat": render_stat,
    "compare": render_compare,
    "list": render_list,
    "term_define": render_term_define,
    "quote": render_quote,
    "cta": render_cta,
    "code": render_code,
    "command": render_command,
    "diagram": render_diagram,
}


# --------------------------------------------------------------------------- assembly

def build_index_html(beats, th, resolution, width, height, duration) -> str:
    body, tweens = [], []
    for i, b in enumerate(beats):
        renderer = _RENDERERS.get(b["type"])
        if renderer is None:                      # type allowed but renderer not shipped yet
            b = {"type": "statement", "cue": b.get("cue", ""),
                 "text": _words_clip(b.get("text") or b.get("term") or b.get("title") or b.get("cue", ""), 8),
                 "w": 2, "start": b["start"], "dur": b["dur"]}
            renderer = render_statement
        ctx = {"i": i, "start": b["start"], "dur": b["dur"],
               "is_last": i == len(beats) - 1, "width": width, "height": height, "duration": duration}
        html, tw = renderer(b, ctx)
        body.append(html)
        tweens.extend(tw)
    bg_tween = _from("#bg-motion", 0, "opacity:0.2,scale:1", "opacity:0.4,scale:1.08",
                     dur=duration, ease="sine.inOut")
    return (
        "<!doctype html>\n<html lang=\"en\" data-resolution=\"" + resolution + "\">\n"
        "<head><meta charset=\"UTF-8\"/>\n<script src=\"gsap.min.js\"></script>\n<style>\n" +
        _base_css(width, height, th) + "\n</style></head>\n<body>\n"
        "  <div id=\"root\" data-composition-id=\"master\" data-width=\"" + str(width) +
        "\" data-height=\"" + str(height) + "\" data-start=\"0\" data-duration=\"" + _r(duration) + "\">\n"
        "    <div id=\"bg-motion\"></div>\n    " + "\n    ".join(body) + "\n  </div>\n"
        "  <script>\n  window.__timelines = window.__timelines || {};\n"
        "  const tl = gsap.timeline({paused:true});\n  " + bg_tween + "\n  " +
        "\n  ".join(tweens) + "\n  window.__timelines[\"master\"] = tl;\n  </script>\n</body></html>"
    )


# --------------------------------------------------------------------------- LLM prompt

_TYPE_DOCS = {
    "hook": 'hook: {"cue","text"(≤8w),"emoji"?} — the opening punch; exactly one, first.',
    "statement": 'statement: {"cue","text"(≤8w),"w":1|2|3,"emoji"?} — an emphasized line (w=3 = the single key point).',
    "stat": 'stat: {"cue","value","unit"?,"label"(≤6w),"emoji"?} — a number/percentage that animates (e.g. value "300", unit "ms").',
    "compare": 'compare: {"cue","title"?,"left":{"title","items"(≤3)},"right":{"title","items"(≤3)}} — A vs B.',
    "list": 'list: {"cue","title"(≤6w),"ordered":bool,"items":[{"text"(≤6w),"emoji"?}](≤5)} — points revealed one by one.',
    "term_define": 'term_define: {"cue","term","definition"(≤14w)} — define a key term as it is introduced.',
    "quote": 'quote: {"cue","text"(≤16w),"attribution"?} — a memorable line; good for the payoff.',
    "cta": 'cta: {"cue","text"(≤4w),"sub"?} — closing call to action; exactly one, last.',
    "code": 'code: {"cue","lang","lines":[str](≤8),"highlight":[int]} — a short code snippet; highlight key line indices.',
    "command": 'command: {"cue","prompt":"$","command","output":[str](≤4)} — a terminal command and its output.',
    "diagram": 'diagram: {"cue","layout":"pipeline"|"request_response"|"fanout","nodes":[{"id","label"(≤3w)}](≤5),"edges":[{"from","to","label"?}]} — boxes and arrows.',
}


def _system_prompt(allowed: list[str]) -> str:
    types = "\n".join("- " + _TYPE_DOCS[t] for t in allowed if t in _TYPE_DOCS)
    has_bc = any(t in allowed for t in ("code", "command", "diagram"))
    rich = "stat / compare / list / term_define" + (" / code / command / diagram" if has_bc else "")
    return (
        "You design the VISUAL storyboard for a technical-explainer video. The narration "
        "audio already exists; you design the on-screen visuals that play over it.\n"
        "Output ONLY a JSON object: {\"beats\":[ ... ]}. No prose, no markdown, no code fences.\n\n"
        "CRITICAL RULES:\n"
        "1. NO timing fields. For each beat set \"cue\" to the EXACT consecutive words from the "
        "narration where the visual appears — copy them verbatim, 2 to 6 words. Never reuse a cue.\n"
        "2. Each beat must ADD information the spoken words cannot carry. TRANSLATE the narration "
        "into visuals: a number becomes a `stat`; a contrast/'X vs Y' becomes a `compare`; steps or "
        "reasons become a `list`; a key term becomes a `term_define`" +
        ("; code or a command becomes `code`/`command`; a flow/pipeline becomes a `diagram`" if has_bc else "") +
        ".\n"
        "3. Use `statement` SPARINGLY — at MOST 2 in the whole video. A storyboard that is mostly "
        "`statement` is WRONG: it just re-displays the spoken words. Convert those into the richer "
        "types (" + rich + ") instead.\n"
        "4. Structure: EXACTLY one `hook` first, then 4-9 varied explanatory beats, EXACTLY one "
        "`cta` last. 6-11 beats total, in chronological order.\n"
        "5. Write all visible text in the SAME language as the narration. Keep code, commands, and "
        "identifiers in their original language.\n\n"
        "Allowed beat types:\n" + types + "\n\n"
        "Example for narration about RAG chunking (notice the VARIED types and verbatim cues):\n"
        '{"beats":[\n'
        ' {"type":"hook","cue":"keeps pulling the wrong chunks","text":"Your RAG pulls junk","emoji":"🗑️"},\n'
        ' {"type":"term_define","cue":"chunking by character count","term":"Fixed-size chunking","definition":"splitting text every N characters"},\n'
        ' {"type":"stat","cue":"five hundred characters","value":"500","unit":"chars","label":"cut mid-idea"},\n'
        ' {"type":"compare","cue":"chunk by meaning instead","left":{"title":"By characters","items":["splits ideas","loses context"]},"right":{"title":"By meaning","items":["whole thoughts","keeps context"]}},\n'
        ' {"type":"list","cue":"split on sections paragraphs","title":"Chunk by","ordered":false,"items":[{"text":"sections"},{"text":"paragraphs"},{"text":"with overlap"}]},\n'
        ' {"type":"cta","cue":"cut it into thoughts","text":"Follow"}\n]}'
    )


def _user_prompt(subject: str, script: str, content_format: str) -> str:
    pace = ("Short vertical video: favor a punchy hook, 1-2 key visuals, and a fast payoff."
            if content_format != "long" else
            "Long-form video: use more beats and richer visuals (code, diagrams, comparisons) "
            "to sustain a longer narration.")
    return ("Video title: " + subject + "\n" + pace + "\n\nNarration script:\n" + script +
            "\n\nReturn the storyboard JSON now.")


# --------------------------------------------------------------------------- entry point

def _rich_types(beats) -> set:
    """Distinct explanatory (non hook/cta/statement) beat types — the variety signal."""
    return {b["type"] for b in beats if b["type"] not in ("hook", "cta", "statement")}


def _variety_ok(beats) -> bool:
    """A storyboard is varied enough when it isn't mostly plain statements and uses at
    least two distinct explanatory beat types (the whole point of the redesign)."""
    mid = [b["type"] for b in beats if b["type"] not in ("hook", "cta")]
    return bool(mid) and mid.count("statement") <= 2 and len(_rich_types(beats)) >= 2


def compose(*, subject, script, words, duration, resolution, width, height,
            topic_id=None, content_format="short", allowed_types=None, llm) -> str | None:
    """Generate a composition index.html via the typed-storyboard path.

    Returns the HTML string, or None on failure (the caller then uses the deterministic
    _fallback_composition). ``llm`` is the worker._llm callable (kept as a param to avoid
    importing worker)."""
    allowed = list(allowed_types or ["hook", "statement", "stat", "compare", "list",
                                      "term_define", "quote", "cta"])
    th = theme.resolve(topic_id, subject)
    system = _system_prompt(allowed)
    user = _user_prompt(subject, script, content_format)

    raw = llm(user, system=system, max_tokens=1500).strip()
    beats = parse_storyboard(raw, allowed)
    if not beats:
        raw = llm(user + "\n\nReturn ONLY valid JSON {\"beats\":[...]} using the allowed types.",
                  system=system, max_tokens=1500).strip()
        beats = parse_storyboard(raw, allowed)
    if not beats:
        logger.info("storyboard: unparseable for %r — falling back", subject)
        return None

    # Quality guard: if the model leaned on plain statements (echoing the audio), push
    # once for the varied explanatory types and keep whichever draft is richer.
    if not _variety_ok(beats):
        retry = llm(
            user + "\n\nYour draft relied on plain 'statement' beats that just repeat the spoken "
            "words. Redo it: use AT MOST two 'statement' beats and convert the rest into "
            "stat / compare / list / term_define" +
            ("/ code / command / diagram" if any(t in allowed for t in ("code", "command", "diagram")) else "") +
            ". Exactly one hook first and one cta last.",
            system=system, max_tokens=1500).strip()
        rb = parse_storyboard(retry, allowed)
        if rb and (_variety_ok(rb) or len(_rich_types(rb)) > len(_rich_types(beats))):
            beats = rb

    align_storyboard(beats, words, duration)
    if not validate_storyboard(beats, duration):
        _even_space(beats, duration)
        if not validate_storyboard(beats, duration):
            logger.info("storyboard: timing invalid for %r — falling back", subject)
            return None
    return build_index_html(beats, th, resolution, width, height, duration)
