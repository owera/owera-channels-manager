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

import copy
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
_TAIL_MIN = 2.0  # floor for each of the last two beats (payoff + CTA) — see align_storyboard
_MID_MIN = 1.8   # soft floor for every other beat after the hook — see align_storyboard
# Mid-body visual-hold cap — MUST equal craft.MID_BEAT_MAX_S (Gate B HARD).
# VM 2026-09-29: a mid card is ≤3.0s INCLUDING its fade. RR check 2026-09-30:
# a 2.88s hold still measured 3.05–3.08s with the real fade → hold ≤2.80s.
_MID_MAX = 2.80
# Series endcard (last cta) ceiling. Craft gate: chip holds ≤4.0s after the
# claim. Surplus stays on the payoff / earlier mids — never back on frame0
# and never a long neon Follow card. Mid-body still dumps into the CTA first
# (so a penultimate 8s+ list can shrink); _cap_endcard then slides the chip
# to the last 4s and re-caps the penultimate at _MID_MAX.
# VM 2026-09-29: endcard ≤3.9s INCLUDING its fade → hold 3.9 − _GAP = 3.78
# (was 4.0 → measured 4.05–4.08s on every RR endcard). = craft.ENDCARD_MAX_S.
_ENDCARD_MAX = 3.78
# Frame0 visual-hold target. Gate B exempts hook, so _MID_MAX recap walks
# leftover onto beat 0 (a 40s short with 3s mids + 4s CTA freezes frame0
# for 12–17s — v1265/v1267, golden ch2-code). Surplus is later speech,
# not sentence 1. Cap the freeze and fill the window with NEW quote cards
# of the words actually spoken in each slot (not hook-claim quotes — that
# dropped R2 on 2026-09-17). Never copies of existing mids: those clones
# (plus a second fill pass cloning the clones) put the same card twice
# back-to-back (~6.2s) or replayed cards 1-3 as 4-6 on RR #1340/#1349/
# #1354/#1370/#1372/#1373/#1375/#1376 (VM 2026-09-29).
_HOOK_MAX = 4.0
_HOOK_FILL_MAX = 8
# Kept for import compatibility; donors are no longer cloned.
_HOOK_FILL_TYPES = frozenset({
    "code", "command", "diagram", "compare", "stat", "term_define",
})
_ROW_STEP_MAX = 1.1  # max gap between list-row reveals — see render_list
_ROW_STEP_SHORTS = 0.6  # Shorts craft gate C: item stagger ≤0.6s
_LIST_MAX = 6.0      # empty list dumps >6s are a retention hole — see _cap_list_holds


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


def _code_line_clip(text, n: int) -> str:
    """Like _chars_clip but keeps leading indentation — code lines carry meaning in
    leading whitespace (a `return` under a `def` must stay indented)."""
    s = str(text).rstrip()
    return s[:n].rstrip()


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
        if btype == "hook":
            # Decolar prop (receipt/terminal/bill). Emoji is not an object.
            obj = _words_clip(raw.get("object") or raw.get("prop") or "", 4)
            if obj and not re.search(r"[A-Za-z0-9À-ÿ]", obj):
                obj = ""
            b["object"] = obj
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
        # Compose overwrites shorts to the series chip; longs keep a punch.
        return {"type": "cta", "cue": cue, "text": _words_clip(raw.get("text", "") or "Build", 6),
                "sub": _words_clip(raw.get("sub", ""), 6)}

    # Phase B/C: accept + clamp here; rendered only once their renderers are registered.
    if btype == "code":
        lines = [_code_line_clip(x, 60) for x in (raw.get("lines") or []) if str(x).strip()][:8]
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
        _cap_endcard(beats, duration, words)
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

    # Beat floor (backward relaxation): the LLM tends to bunch several cues on one dense
    # sentence — worst at the close, where payoff + CTA anchor on the final words — so
    # beats flash by too fast for their animation to even land. Walk backward pulling
    # starts EARLIER (never later): each beat gets a soft floor (_TAIL_MIN for the last
    # two, _MID_MIN elsewhere), granted only while the predecessors can still fit their
    # own floors (prefix feasibility) — so a bunched tail steals from the first beat
    # with slack, while a genuinely too-short clip keeps pure word-sync. Beat 0 stays
    # pinned at 0.
    def _floor(j: int) -> float:
        return _TAIL_MIN if j >= n - 2 else _MID_MIN

    prefix = [0.0] * (n + 1)  # prefix[i] = minimum span beats 0..i-1 need at their floors
    for j in range(n):
        prefix[j + 1] = prefix[j] + _floor(j)
    for i in range(n - 1, 0, -1):
        nxt = starts[i + 1] if i + 1 < n else duration
        target = nxt - _floor(i)
        if starts[i] > target and prefix[i] <= target:
            starts[i] = target
        elif starts[i] > nxt - _MIN_DUR:
            starts[i] = nxt - _MIN_DUR  # floor infeasible — just follow the moved successor
    for i in range(1, n):  # safety: pathological clips must still validate
        if starts[i] < starts[i - 1] + _MIN_DUR:
            starts[i] = starts[i - 1] + _MIN_DUR

    # Max-hold (forward): a single cue span the LLM will not split freezes a card
    # for 8-10s (R4 DRAG). Prompt-level "split the span" is exhausted (07-13);
    # this is the deterministic counterpart of the min floor. Pull the NEXT start
    # earlier so beat i's visual hold is <= _MID_MAX, but never dump enough into
    # a *mid-body* successor that *it* exceeds _MID_MAX. The last beat still
    # absorbs the penultimate dump (so an 8s+ list can shrink); _cap_endcard
    # then enforces the 4.0s chip ceiling and re-caps the penultimate.
    for i in range(n - 1):
        wanted = starts[i] + _MID_MAX + _GAP
        if starts[i + 1] <= wanted + 1e-9:
            continue
        if i + 1 == n - 1:
            new_next = wanted  # dump into CTA; chip ceiling is applied after
        else:
            succ_end = starts[i + 2] - _GAP
            new_next = max(wanted, succ_end - _MID_MAX)
        if new_next < starts[i + 1] - 1e-9:
            starts[i + 1] = new_next
    for i in range(1, n):
        if starts[i] < starts[i - 1] + _MIN_DUR:
            starts[i] = starts[i - 1] + _MIN_DUR

    for i, b in enumerate(beats):
        b["start"] = round(max(0.0, starts[i]), 3)
        end = duration if i == n - 1 else max(starts[i] + _MIN_DUR, starts[i + 1] - _GAP)
        b["dur"] = round(max(_MIN_DUR, end - starts[i]), 3)
    _cap_endcard(beats, duration, words)
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

def _brand_css(width: int, height: int, th: dict) -> str:
    """OS vs RR mute-scroll split. Legacy neon keeps the accent-fill identity."""
    brand = th.get("brand")
    inset = theme.mark_inset_px(width, height)
    logo_h = theme.logo_height_px(height)
    if brand == "os":
        return (
            "html,body{background:radial-gradient(120% 90% at 18% 0%,var(--glow) 0%,var(--bg) 64%)}"
            "#bg-motion{opacity:.5;background:radial-gradient(ellipse at 78% 8%,#1a1a1a 0%,transparent 58%)}"
            ".cta .cta-box{background:#fff;color:#000}"
            ".cmp .cmp-col{background:rgba(255,255,255,.03);border:2px solid var(--stroke);"
            "border-top:2px solid var(--stroke)}"
            ".cmp .cmp-vs{background:#fff;color:#000}"
            ".stat .stat-num,.stat .stat-unit,.term .term-word{color:#fff}"
            ".term .term-rule{background:var(--stroke)}"
            ".code,.cmd{background:#0a0a0a;border:1px solid #2a2a2a;color:#fff}"
            ".code .ln.hl{border-left-color:var(--stroke)}"
            ".diagram .node rect{fill:#0a0a0a;stroke:var(--stroke)}"
            ".lst .lst-bullet{color:var(--stroke)}"
            "#brand-mark{position:absolute;left:" + str(inset) + "px;bottom:" + str(inset) + "px;"
            "height:" + str(logo_h) + "px;width:auto;opacity:.9;z-index:5;pointer-events:none}"
        )
    if brand == "rr":
        return (
            "html,body{background:radial-gradient(110% 80% at 82% 0%,var(--glow) 0%,var(--glow2) 28%,var(--bg) 62%)}"
            "#bg-motion{opacity:.55;background:radial-gradient(ellipse at 84% 4%,#4a1528 0%,#2a0a14 40%,transparent 64%)}"
            ".cta .cta-box{background:transparent;color:var(--fg);border:2px solid var(--stroke)}"
            ".cmp .cmp-col{background:transparent;border:2px solid var(--stroke);"
            "border-top:2px solid var(--stroke)}"
            ".cmp .cmp-vs{background:transparent;color:var(--fg);border:2px solid var(--stroke)}"
            ".stat .stat-num,.stat .stat-unit,.term .term-word{color:var(--fg)}"
            ".term .term-rule{background:var(--stroke);height:2px}"
            ".code,.cmd{background:#0a0a0a;border:2px solid var(--stroke);color:#fff}"
            ".code .ln.hl{border-left:2px solid var(--stroke)}"
            ".diagram .node rect{fill:#0a0a0a;stroke:var(--stroke);stroke-width:2}"
            ".lst .lst-bullet{color:var(--stroke)}"
        )
    return ""


def _base_css(width: int, height: int, th: dict) -> str:
    from app.services import craft
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
        ";--glow:" + th.get("glow", th["bg_deep"]) + ";--glow2:" + th.get("glow2", th["bg_base"]) +
        ";--stroke:" + th.get("stroke", th["accent"]) +
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
        # hook — object ABOVE type (never covers line 1); no emoji; no rainbow bar
        ".hook{flex-direction:column;justify-content:center;gap:.55em}"
        ".hook{--obj-accent:var(--stroke);--obj-mono:var(--mono)}"
        ".hook .hobj{flex:0 0 auto;width:min(78%,540px);max-height:36%;z-index:1;"
        "pointer-events:none;position:relative}"
        ".hook .htext{position:relative;z-index:2;font-size:calc(var(--fs)*1.28);font-weight:800;"
        "line-height:1.12;letter-spacing:-1px;text-shadow:0 4px 24px rgba(0,0,0,.7)}"
        + craft.OBJECT_CSS + craft.SPLIT_CSS +
        ".hook[data-split]{justify-content:flex-start;padding:9% 6% 0;"
        "--split-top:var(--stroke);color:var(--fg)}" +
        ((".hook{flex-direction:row;align-items:center}"
          ".hook .hobj{width:38%;max-height:62%}") if height < width else "") +
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
        # cta — series endcard chip (outline, no neon fill, no arrow, no 💸)
        ".cta .cta-chip{display:inline-flex;align-items:center;justify-content:center;"
        "border:1px solid var(--fg);background:transparent;color:var(--fg);"
        "border-radius:999px;padding:.28em .75em;font-size:calc(var(--fs)*.72);"
        "font-weight:700;letter-spacing:.03em}"
        ".cta .cta-micro{font-size:calc(var(--body-fs)*.8);color:var(--fg-dim);"
        "margin-top:.4em;font-weight:500;letter-spacing:.02em}"
        # long-form punch box (shorts never use this — _sanitize_cta sets the chip)
        ".cta .cta-box{background:var(--fg);color:var(--bg);font-weight:900;border-radius:18px;"
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
        # paint-order halo: the connector line passes behind the glyphs instead of
        # striking through them (labels sit on/next to the edge line).
        ".diagram .elabel{fill:var(--fg-dim);font-size:26px;opacity:0;"
        "paint-order:stroke;stroke:rgba(12,15,26,.85);stroke-width:7px;stroke-linejoin:round}"
        # cmp — portrait only: side-by-side columns waste the tall frame (small text,
        # ~70% dead space) and squeeze the centered VS badge into the card text. Stack
        # the cards full-width with the badge in normal flow between them.
        + ((".cmp .cmp-cols{flex-direction:column;align-items:center;gap:.5em}"
            ".cmp .cmp-col{flex:none;max-width:88%;width:88%;box-sizing:border-box}"
            ".cmp .cmp-vs{position:static;transform:none;flex:none}")
           if height >= width else "")
        + _brand_css(width, height, th)
    )


# --------------------------------------------------------------------------- Phase A renderers
# Each returns (html_fragment, [tween_strings]). Container is opacity:0 in CSS; we
# tl.set it visible at start and fade it out before the next beat (unless last).

def _shell(i: int, b: dict, cls: str, inner: str) -> str:
    extra = ""
    if b.get("object"):
        extra += ' data-object="' + theme.esc(str(b["object"])) + '"'
    if (b.get("type") or cls) == "list" or cls == "lst":
        extra += ' data-items="' + str(len(b.get("items") or [])) + '"'
    return ('<div class="beat ' + cls + '" id="b' + str(i) + '" data-start="' + _r(b["start"]) +
            '" data-duration="' + _r(b["dur"]) + '" data-track-index="' + str(i) + '"' + extra +
            ">" + inner + "</div>")


# Beats held past this get a slow zoom so the frame never fully freezes (R4 drag):
# entrance tweens settle within ~1s, so anything longer is a static card without it.
_DRIFT_MIN = 5.5


def _wrap(i: int, ctx: dict, base_tweens: list[str]) -> list[str]:
    """Prepend the container reveal and append the exit fade (unless last beat)."""
    bid = "#b" + str(i)
    tw = [_set_on(bid, ctx["start"])] + base_tweens
    end = ctx["start"] + ctx["dur"]
    if ctx["dur"] > _DRIFT_MIN:
        # linear so the motion reads as camera drift, not an animation with an arrival
        t0 = ctx["start"] + 1.1
        drift_dur = (end - 0.45) - t0
        if drift_dur > 1.0:
            tw.append(_to(bid, t0, "scale:1.045", dur=drift_dur, ease="none"))
    if not ctx["is_last"]:
        tw.append(_fade_out(bid, end))
    return tw


def render_hook(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    from app.services import craft
    spec = b.get("object_spec") or craft.opening_object(b.get("text"))
    if b.get("object") and not b.get("object_spec"):
        spec = craft.coerce_object({"label": b["object"], "kind": b.get("object_kind")})
    obj = '<div class="hobj">' + craft.object_markup(spec) + "</div>"
    # Hook emoji is a hard-FAIL as the object — never render 💸/🔥 as a punch.
    inner = obj + '<div class="htext">' + _words_html(b["text"]) + "</div>"
    if s <= 1e-9 and b.get("split"):
        # Designer split-card (2026-09-29 P0): frame0 ≡ thumb. Same markup as
        # thumbnail.py (craft.split_card_markup); whole claim, full opacity at
        # t=0, no first-word object chip, O ring hidden while this card shows.
        track = 'data-track-index="' + str(i) + '"'
        inner = craft.split_card_markup(b["split"], ctx["width"], ctx["height"])
        html = _shell(i, b, "hook", inner).replace(
            track, track + ' data-split="1" style="opacity:1"', 1)
        tw = [_from(bid + " .sc-top", s, "scale:0.97", "scale:1", dur=0.35),
              _from(bid + " .sc-bot", s, "scale:0.97", "scale:1", dur=0.35),
              _from(bid + " .sc-x", s + 0.25, "scale:1.6", "scale:1", dur=0.3,
                    ease="back.out(2)")]
        if ctx.get("brand") == "os":
            tw.append("tl.set('#brand-mark',{opacity:0},0);")
            tw.append("tl.set('#brand-mark',{opacity:0.9,immediateRender:false}," +
                      _r(s + ctx["dur"]) + ");")
        return html, _wrap(i, ctx, tw)
    if s <= 1e-9:
        # Frame0 (VM 2026-09-29, P0 a): the t=0 frame showed only the gradient
        # on 17/17 masters because the hook faded in. The first card is at
        # FULL opacity at t=0 (inline style, not a zero-time tl.set) and the
        # whole claim is readable; motion is a small scale settle only.
        track = 'data-track-index="' + str(i) + '"'
        html = _shell(i, b, "hook", inner).replace(track, track + ' style="opacity:1"', 1)
        tw = [_from(bid + " .hobj", s, "scale:0.94", "scale:1", dur=0.35),
              _from(bid + " .htext", s, "scale:0.97", "scale:1", dur=0.35)]
        return html, _wrap(i, ctx, tw)
    tw = [_from(bid + " .hobj", s, "opacity:0,y:16", "opacity:1,y:0", dur=0.22)]
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
    # Reveal rows across the beat window so they track the narration as it's spoken —
    # but never slower than _ROW_STEP_MAX. A beat's span is set by cue spacing, so a list
    # can be held far longer than the enumeration takes to say; stretching the reveal to
    # fill it starves the card (a 3-item list on an 11s beat used to show its last item
    # 7.3s in, long after the narrator said it). Capping the step keeps tracking on tight
    # beats and completes the card early on long ones.
    n = len(b["items"])
    win = max(0.0, ctx["dur"] - 0.8)
    # Same != "long" gate as _sanitize_cta / _demote / the retry prompt:
    # empty/"LONG"/"medium"/missing leftovers are shorts (craft gate C stagger).
    cap = (_ROW_STEP_SHORTS
           if (ctx.get("content_format") or "short") != "long"
           else _ROW_STEP_MAX)
    step = min(win / n, cap) if n else 0
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
    chip = bool(b.get("endcard")) or (b.get("text") or "").startswith("·")
    if chip:
        # Series endcard: one-line chip, optional micro. No arrow, no neon fill.
        micro = ('<div class="cta-micro">' + theme.esc(b["sub"]) + "</div>") if b.get("sub") else ""
        inner = '<div class="cta-chip">' + theme.esc(b["text"]) + "</div>" + micro
        tw = [
            _from(bid + " .cta-chip", s, "opacity:0,y:12", "opacity:1,y:0", dur=0.28, ease="power2.out"),
        ]
        if b.get("sub"):
            tw.append(_from(bid + " .cta-micro", s + 0.18, "opacity:0", "opacity:1", dur=0.22))
        return _shell(i, b, "cta", inner), _wrap(i, ctx, tw)
    sub = ('<div class="cta-sub">' + theme.esc(b["sub"]) + "</div>") if b.get("sub") else ""
    inner = '<div class="cta-box">' + theme.esc(b["text"]) + "</div>" + sub
    tw = [
        _from(bid + " .cta-box", s, "opacity:0,scale:0.6", "opacity:1,scale:1", dur=0.32, ease="back.out(2)"),
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
    # Shrink the whole block to the longest line so monospace never clips (mono advance ~0.6em).
    maxlen = max((len(ln) for ln in b["lines"]), default=1)
    fs = int(max(24, min(ctx["width"] * 0.052, (ctx["width"] * 0.80) / (maxlen * 0.62))))
    inner = lang + '<pre class="code" style="font-size:' + str(fs) + 'px">' + lines + "</pre>"
    tw = []
    if b.get("lang"):
        tw.append(_from(bid + " .code-lang", s, "opacity:0", "opacity:1", dur=0.2))
    tw.append(_from(bid + " .ln", s + 0.1, "opacity:0,x:-18", "opacity:1,x:0", dur=0.25, stagger=0.12))
    return _shell(i, b, "code", inner), _wrap(i, ctx, tw)


def render_command(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    out = "".join('<span class="cmd-out">' + theme.esc(o) + "</span>" for o in b.get("output", []))
    # Shrink to the longest line (command or any output row) so nothing clips.
    maxlen = max([len(b.get("prompt", "$")) + 1 + len(b["command"])] +
                 [len(o) for o in b.get("output", [])] or [1])
    fs = int(max(22, min(ctx["width"] * 0.05, (ctx["width"] * 0.80) / (maxlen * 0.62))))
    inner = ('<div class="cmd" style="font-size:' + str(fs) + 'px"><div class="cmd-line">'
             '<span class="cmd-prompt">' + theme.esc(b.get("prompt", "$")) + '</span> '
             '<span class="cmd-cmd">' + theme.esc(b["command"]) + "</span></div>" + out + "</div>")
    tw = [_from(bid + " .cmd-cmd", s, "opacity:0", "opacity:1", dur=0.2)]
    for j in range(len(b.get("output", []))):
        # children of .cmd: .cmd-line (1), then .cmd-out (2,3,...)
        tw.append(_from(bid + " .cmd-out:nth-child(" + str(j + 2) + ")",
                        s + 0.4 + j * 0.25, "opacity:0", "opacity:1", dur=0.25))
    return _shell(i, b, "cmd", inner), _wrap(i, ctx, tw)


def _diagram_svg(nodes, edges, layout, portrait):
    """Server-computed layout — never free graph auto-layout (arrow geometry is the
    failure mode). "pipeline"/"request_response": nodes laid in sequence (vertical for
    portrait, horizontal for landscape); edges connect node box edges with an arrowhead
    marker. "fanout": nodes[0] is the hub, the rest its spokes; hub→spoke elbows run
    along a side rail so no arrow ever crosses a box — model edges are used for labels
    only, never for geometry."""
    n = len(nodes)
    centers = {}   # id -> (cx, cy, near, far)  near/far = top/bottom (portrait) or left/right
    defs = ('<defs><marker id="ar" markerWidth="12" markerHeight="12" refX="9" refY="4" '
            'orient="auto"><path d="M0,0 L9,4 L0,8 Z" fill="var(--accent)"/></marker></defs>')
    node_parts, edge_parts = [], []   # edges drawn first (behind), nodes on top

    def node_g(x, y, w, h, label):
        return ('<g class="node"><rect x="%d" y="%d" width="%d" height="%d" rx="16"/>'
                '<text x="%d" y="%d" text-anchor="middle" dominant-baseline="middle">%s</text></g>'
                % (x, y, w, h, x + w / 2, y + h / 2, theme.esc(label)))

    if layout == "fanout" and n >= 3:
        e_labels = {e.get("to"): e["label"] for e in (edges or [])
                    if e.get("label") and e.get("to")}
        e_count = 0
        if portrait:
            vbw, bw, bh, gap, ind = 760, 600, 120, 78, 110
            vbh = int(n * bh + (n - 1) * gap + 20)
            x = (vbw - bw) / 2
            rail = x + 48                      # left of every spoke's left edge
            node_parts.append(node_g(x, 10, bw, bh, nodes[0]["label"]))
            for k, nd in enumerate(nodes[1:]):
                y = 10 + (k + 1) * (bh + gap)
                cy = y + bh / 2
                lx = x + ind                   # spokes indented; right edges stay aligned
                node_parts.append(node_g(lx, y, bw - ind, bh, nd["label"]))
                edge_parts.append('<path class="edge" d="M%d,%d V%d H%d" marker-end="url(#ar)"/>'
                                  % (rail, 10 + bh, cy, lx))
                if e_labels.get(nd["id"]):
                    edge_parts.append('<text class="elabel" x="%d" y="%d" text-anchor="start" '
                                      'dominant-baseline="middle">%s</text>'
                                      % (rail + 12, cy - 14, theme.esc(e_labels[nd["id"]])))
                e_count += 1
        else:
            spokes = nodes[1:]
            bw, bh, gap, vbw = 240, 110, 64, 1000
            vbh = max(220, int(len(spokes) * bh + (len(spokes) - 1) * gap + 20))
            node_parts.append(node_g(60, vbh / 2 - bh / 2, bw, bh, nodes[0]["label"]))
            for k, nd in enumerate(spokes):
                y = 10 + k * (bh + gap)
                cy = y + bh / 2
                node_parts.append(node_g(700, y, bw, bh, nd["label"]))
                edge_parts.append('<path class="edge" d="M%d,%d H%d V%d H%d" marker-end="url(#ar)"/>'
                                  % (60 + bw, vbh / 2, 500, cy, 700))
                if e_labels.get(nd["id"]):
                    edge_parts.append('<text class="elabel" x="%d" y="%d" text-anchor="middle">%s</text>'
                                      % (610, cy - 10, theme.esc(e_labels[nd["id"]])))
                e_count += 1
        return ('<svg viewBox="0 0 %d %d" class="dsvg">%s</svg>'
                % (vbw, vbh, "".join([defs] + edge_parts + node_parts)), e_count)

    if portrait:
        # Narrow viewBox (was 1000) so the vertically-stacked diagram fills more of the
        # portrait frame instead of leaving ~55% dead space: .dsvg is width-fit, so a
        # taller aspect ratio means more vertical fill (nodes also read larger on mobile).
        vbw, bw, bh, gap = 760, 600, 120, 78
        vbh = int(n * bh + (n - 1) * gap + 20)
        x = (vbw - bw) / 2
        for k, nd in enumerate(nodes):
            y = 10 + k * (bh + gap)
            centers[nd["id"]] = (x + bw / 2, y + bh / 2, y, y + bh)
            node_parts.append(node_g(x, y, bw, bh, nd["label"]))
    else:
        bw, bh, gap, vbh = 240, 110, 64, 220
        total = n * bw + (n - 1) * gap
        vbw = max(1000, int(total))
        x0 = (vbw - total) / 2
        y = (vbh - bh) / 2
        for k, nd in enumerate(nodes):
            x = x0 + k * (bw + gap)
            centers[nd["id"]] = (x + bw / 2, y + bh / 2, x, x + bw)
            node_parts.append(node_g(x, y, bw, bh, nd["label"]))

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
        edge_parts.append('<line class="edge" x1="%d" y1="%d" x2="%d" y2="%d" marker-end="url(#ar)"/>'
                          % (x1, y1, x2, y2))
        if e.get("label"):
            if portrait:
                # Vertical connector: a centered label sits ON the line (strike-through)
                # and its -8 baseline shift can clip the node border above — place it
                # beside the line, vertically centered in the gap.
                edge_parts.append('<text class="elabel" x="%d" y="%d" text-anchor="start" dominant-baseline="middle">%s</text>'
                                  % ((x1 + x2) / 2 + 18, (y1 + y2) / 2, theme.esc(e["label"])))
            else:
                edge_parts.append('<text class="elabel" x="%d" y="%d" text-anchor="middle">%s</text>'
                                  % ((x1 + x2) / 2, (y1 + y2) / 2 - 8, theme.esc(e["label"])))
        e_count += 1
    return '<svg viewBox="0 0 %d %d" class="dsvg">%s</svg>' % (vbw, vbh, "".join([defs] + edge_parts + node_parts)), e_count


def render_diagram(b, ctx):
    i, s = ctx["i"], ctx["start"]
    bid = "#b" + str(i)
    portrait = ctx["height"] >= ctx["width"]
    svg, e_count = _diagram_svg(b["nodes"], b.get("edges") or [], b.get("layout", "pipeline"), portrait)
    # Fanout rail paths run far longer than chain connectors; a 300 dash on them
    # reads as a broken dashed line, so size the draw-on dash to the longest path.
    dash = 1200 if b.get("layout") == "fanout" else 300
    tw = [
        _from(bid + " .node", s + 0.1, "opacity:0,y:18", "opacity:1,y:0", dur=0.3, ease="back.out(1.4)", stagger=0.16),
        _from(bid + " .edge", s + 0.45, "strokeDasharray:%d,strokeDashoffset:%d" % (dash, dash), "strokeDashoffset:0",
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

def build_index_html(beats, th, resolution, width, height, duration,
                     content_format: str = "short") -> str:
    from app.services import craft
    body, tweens = [], []
    for i, b in enumerate(beats):
        renderer = _RENDERERS.get(b["type"])
        if renderer is None:                      # type allowed but renderer not shipped yet
            b = {"type": "statement", "cue": b.get("cue", ""),
                 "text": _words_clip(b.get("text") or b.get("term") or b.get("title") or b.get("cue", ""), 8),
                 "w": 2, "start": b["start"], "dur": b["dur"]}
            renderer = render_statement
        ctx = {"i": i, "start": b["start"], "dur": b["dur"],
               "is_last": i == len(beats) - 1, "width": width, "height": height,
               "duration": duration, "content_format": content_format,
               "brand": th.get("brand")}
        html, tw = renderer(b, ctx)
        body.append(html)
        tweens.extend(tw)
    bg_tween = _from("#bg-motion", 0, "opacity:0.2,scale:1", "opacity:0.4,scale:1.08",
                     dur=duration, ease="sine.inOut")
    brand = th.get("brand") or ""
    brand_attr = (' data-brand="' + brand + '"') if brand else ""
    logo = th.get("logo") or ""
    # OS signature mark only. RR must never get an Owera asset path or #brand-mark.
    mark = ""
    if brand == "os" and logo:
        mark = ('    <img id="brand-mark" src="' + theme.esc(logo) +
                '" alt="" />\n')
    snap = json.dumps(craft.snapshot_beats(beats), ensure_ascii=False).replace("</", "<\\/")
    embed = '<script type="application/json" id="storyboard-beats">' + snap + "</script>\n"
    return (
        "<!doctype html>\n<html lang=\"en\" data-resolution=\"" + resolution +
        "\"" + brand_attr + ">\n"
        "<head><meta charset=\"UTF-8\"/>\n<script src=\"gsap.min.js\"></script>\n<style>\n" +
        _base_css(width, height, th) + "\n</style></head>\n<body>\n"
        "  <div id=\"root\" data-composition-id=\"master\" data-width=\"" + str(width) +
        "\" data-height=\"" + str(height) + "\" data-start=\"0\" data-duration=\"" + _r(duration) + "\">\n"
        "    <div id=\"bg-motion\"></div>\n" + mark +
        "    " + "\n    ".join(body) + "\n  </div>\n"
        "  " + embed +
        "  <script>\n  window.__timelines = window.__timelines || {};\n"
        "  const tl = gsap.timeline({paused:true});\n  " + bg_tween + "\n  " +
        "\n  ".join(tweens) + "\n  window.__timelines[\"master\"] = tl;\n  </script>\n</body></html>"
    )


# --------------------------------------------------------------------------- LLM prompt

_TYPE_DOCS = {
    "hook": 'hook: {"cue","text"(≤8w),"object"?} — DECOLAR LOCK: text MUST equal the first spoken sentence '
            '(the title hook) or a faithful ≤8-word compression of that SAME claim. Repeating the '
            'title is REQUIRED. Frame 0 visual is the concrete OBJECT of that phrase (receipt / '
            'terminal / bill / the named tool) — no emoji, no abstract diagram, no generic slide. '
            'Exactly one, first. "object" is the Decolar prop of the angle — never an emoji.',
    "statement": 'statement: {"cue","text"(≤8w),"w":1|2|3} — an emphasized line (w=3 = the single key point). Not a second hook.',
    "stat": 'stat: {"cue","value","unit"?,"label"(≤6w)} — a number/percentage that animates (e.g. value "300", unit "ms").',
    "compare": 'compare: {"cue","title"?,"left":{"title","items"(≤3)},"right":{"title","items"(≤3)}} — A vs B.',
    "list": 'list: {"cue","title"(≤6w),"ordered":bool,"items":[{"text"(≤6w)}](≤5)} — points revealed one by one. Never hold a list >6s.',
    "term_define": 'term_define: {"cue","term","definition"(≤14w)} — define a key term as it is introduced.',
    "quote": 'quote: {"cue","text"(≤16w),"attribution"?} — a memorable line; good for the payoff.',
    "cta": 'cta: {"cue","text","sub"?} — series endcard, exactly one, last, AFTER the claim. '
           'On-screen chip text is "Subscribe · {series}" (one line). Optional micro sub '
           '"same series" only if it fits — no extra CTA. Spoken VO: '
           '"Subscribe — next {series} {noun}." ≤8 words; noun ∈ trap|receipt|bill|drop. '
           'FORBIDDEN on the card: Follow, Follow tomorrow, amanhã, waitlist, owera.com, '
           'Cloud, "part 2 coming", SMY, 💸, neon. Subscribe text is FORBIDDEN on every '
           'beat before this last card (no mid-short Subscribe VO/chip). '
           'Must not compete with frame0. Hold ≤4.0s.',
    "code": 'code: {"cue","lang","lines":[str](≤8 lines, each ≤~30 chars — abbreviate to fit a phone screen; PRESERVE indentation as literal leading spaces, 2 per level, so a line inside a `def`/`if`/`for`/`class` block is visibly indented — never flush-left under its header),"highlight":[int]} — a short snippet; highlight key line indices. Prefer a real receipt / API bill / config dump over a toy.',
    "command": 'command: {"cue","prompt":"$","command"(≤~34 chars),"output":[str](≤4, each ≤~34 chars)} — a REAL terminal / UI still. Prefer this over diagrams on 9:16.',
    "diagram": 'diagram: {"cue","layout":"pipeline"|"request_response"|"fanout","nodes":[{"id","label"(≤3w)}](≤5),"edges":[{"from","to","label"?}]} — boxes and arrows that CARRY THE CLAIM (labeled edges, real topology). Forbidden on vertical shorts when the boxes would be generic oars/A-B-C. layout MUST match the real topology: "pipeline" only when each node feeds the NEXT in a chain; "fanout" when ONE hub serves/connects ALL the others.',
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
        ".\n" +
        ("2b. MUST include exactly one `code` or `command` beat with a minimal realistic snippet "
         "(<=5 lines, <=30 chars per line) that demonstrates the narration's claim — even when "
         "the narration never reads code aloud. A technical explainer with no snippet on screen "
         "is WRONG: SHOW the thing the words only describe. Prefer replacing a `statement` over "
         "dropping the snippet.\n" if has_bc else "") +
        "3. Use `statement` SPARINGLY — at MOST 2 in the whole video. A storyboard that is mostly "
        "`statement` is WRONG: it just re-displays the spoken words. Convert those into the richer "
        "types (" + rich + ") instead.\n"
        "4. Structure: EXACTLY one `hook` first, then 4-9 varied explanatory beats, EXACTLY one "
        "`cta` last. 6-11 beats total, in chronological order.\n"
        "4b. DECOLAR LOCK: the hook `text` IS the first spoken sentence (or a faithful ≤8-word "
        "compression of that same claim / the title). Repeating the title is REQUIRED. The opening "
        "visual is a concrete OBJECT that echoes that spoken phrase (receipt, terminal, bill, "
        "the named tool) — never an abstract diagram, emoji soup, or generic slide. Forbidden: "
        "a curiosity-gap headline, a second typographic hook, a different slogan, a hook emoji "
        "used as a second punch. Beat 2 must add information (stat/code/command/list), not another "
        "headline.\n"
        "5. Write all visible text in the SAME language as the narration. Keep code, commands, and "
        "identifiers in their original language.\n"
        "6. PACING — cue spacing IS screen time: a beat runs from its cue until the NEXT beat's "
        "cue, so the narration words between consecutive cues are all the time that beat gets. "
        "Budget one beat per ~8-14 words and distribute cues over the WHOLE script: hook covers "
        "ONLY the first sentence (anchor beat 2 where sentence 2 begins), no gap over ~18 words "
        "— a card frozen on screen for 8+ seconds is a DRAG that kills retention; split a long "
        "span with a `stat`/`term_define`/`list` that visualizes what those words say — and "
        "`command`/`compare`/`code` get ~10+ words of room. Never hold a `list` longer than 6s. "
        "Plan the ending BACKWARDS: the `cta` cue sits on the FIRST words of the series "
        "endcard VO ('Subscribe — next …'), after the claim/payoff — never on frame0. "
        "Endcard visual hold ≤4.0s. The payoff beat before it carries the lesson. "
        "NEVER anchor two beats inside the same short sentence. FORBIDDEN on the cta and anywhere "
        "on screen: Follow, Follow tomorrow, Siga, waitlist, owera.com, Cloud-as-product, "
        "'part 2 coming', SMY, Instagram, LinkedIn, 💸, neon. Endcard also forbids amanhã.\n"
        "7. 9:16 MUST carry the claim with ≥1 real UI still: a `command` (terminal) or `code` "
        "(receipt / API bill / config). Do NOT draw nonsense diagrams (generic A→B oars, unlabeled "
        "boxes). Prefer code/command over diagram on vertical shorts.\n\n"
        "Allowed beat types:\n" + types + "\n\n"
        "Example for narration about RAG chunking (notice the VARIED types and verbatim cues"
        + (" — and the required code beat" if has_bc else "") + "):\n"
        '{"beats":[\n'
        ' {"type":"hook","cue":"Your RAG pulls junk","text":"Your RAG pulls junk"},\n'
        ' {"type":"term_define","cue":"chunking by character count","term":"Fixed-size chunking","definition":"splitting text every N characters"},\n'
        ' {"type":"stat","cue":"five hundred characters","value":"500","unit":"chars","label":"cut mid-idea"},\n'
        ' {"type":"compare","cue":"chunk by meaning instead","left":{"title":"By characters","items":["splits ideas","loses context"]},"right":{"title":"By meaning","items":["whole thoughts","keeps context"]}},\n'
        + (' {"type":"code","cue":"split on sections paragraphs","lang":"python","lines":["split(text,","  by=\\"section\\",","  overlap=50)"],"highlight":[0]},\n'
           if has_bc else
           ' {"type":"list","cue":"split on sections paragraphs","title":"Chunk by","ordered":false,"items":[{"text":"sections"},{"text":"paragraphs"},{"text":"with overlap"}]},\n')
        + ' {"type":"cta","cue":"Subscribe next","text":"Subscribe · Copilot Credits","sub":"same series"}\n]}'
    )


def _user_prompt(subject: str, script: str, content_format: str) -> str:
    from app.services import craft
    first = craft.first_spoken_sentence(script) or subject
    obj = craft.opening_object(first)["label"]
    pace = ("Short vertical video: favor the spoken hook on frame 0, 1-2 claim-carrying "
            "visuals (terminal/receipt/code), then the series endcard chip (Subscribe · series). "
            "No Follow/Siga/waitlist/Cloud/SMY. No Subscribe on any beat before the last "
            "endcard. Endcard after the claim, not on frame0. "
            "CRAFT GATE (PASS/FAIL before publish): "
            "(A) first 3.0s MUST show a real object — a code/command/diagram/compare/stat beat "
            "starting before t=3, OR hook.object (receipt/terminal/bill; emoji is NOT an object). "
            "(B) every mid beat ≤3.0s (next cue − this cue); cta/endcard ≤4.0s. "
            "(C) at most ONE statement in the whole short; list discouraged — if used: max 1 list, "
            "≤3 items, beat ≤3.0s, item stagger ≤0.6s. Prefer code/command/diagram/compare/stat "
            "in the middle. Do not re-display narration as statement/list. "
            "Subscribe/Inscreva CTA text is FORBIDDEN on any mid beat — only the final "
            "cta/endcard series may say Subscribe."
            if content_format != "long" else
            "Long-form video: use more beats and richer visuals (code, terminal, comparisons) "
            "to sustain a longer narration. Still: frame 0 = first spoken sentence.")
    return ("Video title: " + subject + "\n"
            "First spoken sentence (THIS is frame 0 — repeat or compress to ≤8 words, "
            "do NOT replace with a curiosity gap): " + first + "\n"
            "Opening object (frame 0 AND thumb chrome — echo this, do not swap for a "
            "diagram or emoji): " + obj + "\n" +
            pace + "\n\nNarration script:\n" + script +
            "\n\nReturn the storyboard JSON now.")


# --------------------------------------------------------------------------- entry point

def _rich_types(beats) -> set:
    """Distinct explanatory (non hook/cta/statement) beat types — the variety signal."""
    return {b["type"] for b in beats if b["type"] not in ("hook", "cta", "statement")}


def _variety_ok(beats, content_format=None) -> bool:
    """A storyboard is varied enough when it isn't mostly plain statements and uses at
    least two distinct explanatory beat types (the whole point of the redesign).

    Shorts tighten statement ≤1 (Video Maker craft gate C). Long-form keeps the
    historical ≤2. Leftover formats (empty / "LONG" / "medium" / None) follow
    the same != "long" gate as the retry prompt, _sanitize_cta, and _demote.
    """
    stmt_cap = 1 if (content_format or "short") != "long" else 2
    mid = [b["type"] for b in beats if b["type"] not in ("hook", "cta")]
    return bool(mid) and mid.count("statement") <= stmt_cap and len(_rich_types(beats)) >= 2


def _code_ok(beats, allowed) -> bool:
    """When code/command is an allowed type, the storyboard must carry at least one.
    Prompt-level rule 2b is violated ~half the time (08-26 baseline: 2/4 golden
    subjects, including an MCP explainer, had zero snippets) — same class as the
    R7 follow-verb force. Vacuous True when those types are not allowed."""
    if not any(t in allowed for t in ("code", "command")):
        return True
    return any(b.get("type") in ("code", "command") for b in beats)


# CTA used to force a Follow/Siga verb (R7). Strategy inverted: shorts close on
# builder/confiança, and Follow/Siga is banned in generation + validation.
_FOLLOW_VERBS = {
    "english": "Follow",
    "brazilian portuguese": "Siga",
    "portuguese": "Siga",
    "spanish": "Sigue",
}


def _follow_verb(language: str | None) -> str:
    """Kept as a detector (banned verbs), not as the CTA text we force on screen."""
    return _FOLLOW_VERBS.get((language or "").strip().lower(), "Follow")


_GENERIC_NODE = re.compile(
    r"^(a|b|c|d|e|x|y|z|step\s*\d+|node\s*\d+|oar|oars)$", re.IGNORECASE)


def _lock_opening_hook(beats, script, subject) -> None:
    """Decolar: frame0 text = the spoken title claim (safety-clip 12w), no second hook.

    The claim is the title head when the narration opens on it (a two-sentence
    head stays two sentences), else the first spoken sentence. Punctuation and
    digits are kept verbatim (``craft.overlay_claim``).

    The title lock already pins YouTube title to that sentence. An 8-word first-N
    clip dropped PT objects (e.g. 'Sua RAG busca lixo e você culpa o' without
    'modelo') so frame0 diverged from spoken/title. The opener is already ~10
    words; 12 is a wrap-safe ceiling, not a compression slogan.

    Also stamps a concrete ``object_spec`` on beat 0 so render_hook / the thumb
    share the same widget (bill / receipt / GPU meter / app / terminal).
    """
    from app.services import craft
    # Overlay copy keeps the claim as written: every sentence of the title head
    # the narration opens on (not just sentence 1), . , ? ! and every digit.
    # compress_claim (rstrip punctuation) dropped "Prod paged Lee." / "Sem, 11."
    # and the final period on #1363 / #1354.
    claim = craft.overlay_hook_source(None, script, subject)
    hook = craft.overlay_claim(claim) or craft.overlay_claim(craft.title_head(subject))
    # Frame0 is the claim — never the endcard Subscribe VO or a mid-body ask.
    if hook and (craft.contains_subscribe_cta(hook) or craft.is_endcard_vo(hook)):
        hook = craft.overlay_claim(craft.title_head(subject))
    if not beats or not hook:
        return
    if claim and hook and not craft.preserves_dollar_numerals(claim, hook):
        # Frame0 must keep $79 as $79 — never "seventy-nine dollars".
        hook = craft.overlay_claim(claim) or hook
    derived = craft.opening_object(claim or hook)
    b0 = beats[0]
    b0["type"] = "hook"
    b0["text"] = hook
    b0["emoji"] = ""
    # YPP1: object is the noun of the spoken first phrase (not a leftover LLM prop).
    b0["object"] = derived["label"]
    b0["object_kind"] = derived["kind"]
    b0["object_spec"] = derived
    if len(beats) > 1:
        b1 = beats[1]
        b1["emoji"] = ""
        if b1.get("type") == "hook":
            b1["type"] = "statement"
            b1["text"] = _words_clip(b1.get("text") or b1.get("cue") or "", 8)
            b1["w"] = 1


_SERIES_SUFFIX_RE = re.compile(r"\s+·\s+.*$")
_HOOK_CLAIM_MAX_WORDS = 12


def _title_head(subject) -> str:
    """Title text before the ' · Series N' suffix."""
    return _SERIES_SUFFIX_RE.sub("", str(subject or "").strip()).strip()


def _show_whole_claim(beats, script, subject) -> None:
    """Frame0 shows the WHOLE claim — both halves of a two-part title.

    VM 2026-09-29: 9/17 hooks printed only sentence 1 ('Chat routed to Maya.')
    and the payoff ('Prod paged Lee.') only reached the thumb. When the
    narration opens on the full title head (≤12 words) and the current hook
    is a prefix of it, the hook card shows the head verbatim (punctuation and
    digits kept). Separate from _lock_opening_hook on purpose: once
    #38's overlay claim lands, the hook already equals the head → no-op.
    """
    from app.services import craft
    if not beats or (beats[0].get("type") or "") != "hook":
        return
    head = _title_head(subject)
    if not head or len(head.split()) > _HOOK_CLAIM_MAX_WORDS:
        return
    ht = _tok(head)
    hk = _tok(beats[0].get("text") or "")
    if not ht or ht == hk:
        return
    if hk and ht[:len(hk)] != hk:
        return  # a different claim — never swap frame0 for another sentence
    if _tok(script)[:len(ht)] != ht:
        return  # shown must be spoken: the narration opens on the whole head
    if craft.contains_subscribe_cta(head) or craft.is_endcard_vo(head):
        return
    beats[0]["text"] = head


def _apply_split_card(beats, subject, provided_thumb=False) -> None:
    """Designer split-card on frame0 (Agent memory / IA / Local only). Kept
    only when the card shows exactly the hook claim (whole-claim rule, #45);
    never with an operator-provided thumbnail (#39)."""
    from app.services import craft
    if not beats or (beats[0].get("type") or "") != "hook":
        return
    spec = craft.contrast_split(subject, provided_thumb=provided_thumb)
    if not spec:
        return
    if _tok(craft.split_card_text(spec)) != _tok(beats[0].get("text") or ""):
        return  # frame0 claim is not the title head (narration opened elsewhere)
    beats[0]["split"] = spec


def _last_sentence(script: str) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", (script or "").strip()) if p.strip()]
    return parts[-1] if parts else (script or "").strip()


def _sanitize_cta(beats, script, subject=None, brand=None, content_format="short",
                  topic_name=None) -> None:
    """Shorts: lock the last card to the series chip. Longs: punch + CTA ban.

    Subscribe is a Rodrigo/CoS exception on the series endcard
    (``ensure_series_endcard_vo`` + chip ``Subscribe · {series}``). This
    sanitizer does NOT invert the global Follow/waitlist/Cloud ban.
    Mid-video / title / Follow-tomorrow stay sanitized. Long-form cards
    still reject Follow/Subscribe verbs.
    """
    from app.services import craft
    banned_verbs = {theme.fold(v) for v in _FOLLOW_VERBS.values()} | {"subscribe", "inscreva"}
    shorts = (content_format or "short") != "long"
    if shorts:
        card = craft.series_endcard(subject, script, brand, topic_name=topic_name)
        chip, micro = card["chip"], card["micro"]
        if craft.endcard_scan_banned(chip) or craft.endcard_scan_banned(micro):
            chip, micro = craft.series_endcard_chip(card["series"]), ""
    else:
        chip = craft.compress_claim(craft.strip_banned(_last_sentence(script)), 4) or "Build"
        micro = ""
    for b in beats:
        if b.get("type") != "cta":
            continue
        if shorts:
            # Series chip after the claim — never Follow, never a second hook.
            b["text"] = chip
            b["sub"] = micro
            b["endcard"] = True
        else:
            b["text"] = chip
            b["sub"] = craft.strip_banned(b.get("sub") or "")
            if craft.contains_banned(b["sub"]) or theme.fold(b["sub"]) in banned_verbs:
                b["sub"] = ""
            b["endcard"] = False


def _strip_mid_subscribe_beats(beats) -> None:
    """No Subscribe text/chip/cue on any beat before the last endcard."""
    from app.services import craft

    def _scrub(value):
        if isinstance(value, str):
            if craft.contains_subscribe_cta(value) and not craft.is_endcard_vo(value):
                return craft.strip_subscribe_cta(value)
            return value
        if isinstance(value, list):
            return [_scrub(x) for x in value]
        if isinstance(value, dict):
            return {k: _scrub(v) for k, v in value.items()}
        return value

    n = len(beats)
    for i, b in enumerate(beats):
        last_endcard = i == n - 1 and b.get("type") == "cta" and b.get("endcard")
        if last_endcard:
            # Chip may be the YPP#5 template `Subscribe · {series}`.
            # Micro stays Subscribe-free. Cue may match the VO words.
            text = b.get("text") or ""
            if craft.contains_subscribe_cta(text) and not craft.is_endcard_chip(text):
                b["text"] = craft.strip_subscribe_cta(text)
            if craft.contains_subscribe_cta(b.get("sub") or ""):
                b["sub"] = craft.strip_subscribe_cta(b.get("sub") or "")
            continue
        for key, val in list(b.items()):
            if key in ("type", "start", "dur", "w", "endcard"):
                continue
            b[key] = _scrub(val)


# Sentence-bounded quote cards (RR 2026-09-30): windowed word runs leaked a
# word of the next sentence ("no padrão local A", "na placa Testa"), cut a
# sentence mid-way ("O pull terminou e você chamou isso de") and put the CTA
# "next" on a card. Words are annotated with their script sentence
# (annotate_sentences) and cards show whole sentences only.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_SENT_CARD_MAX_WORDS = 12
_CLAUSE_RE = re.compile(r"[,;:—–]\s")


def annotate_sentences(words, script) -> list[dict]:
    """Copy of ``words`` where each word carries its script sentence:
    ``_s`` (index), ``_sfirst`` (first word), ``_stext`` (sentence as written),
    ``_scta`` (series endcard / Subscribe line). Unmatched → words unchanged."""
    from app.services import craft
    out = [dict(w) for w in (words or []) if isinstance(w, dict)]
    sents = [x.strip() for x in _SENT_SPLIT_RE.split((script or "").strip()) if x.strip()]
    if not out or not sents:
        return out
    wtoks = [_tok(w.get("text") or "") for w in out]
    wi = 0
    for si, sent in enumerate(sents):
        need = _tok(sent)
        if not need:
            continue
        cta = craft.is_endcard_vo(sent) or craft.contains_subscribe_cta(sent)
        # resync: find the first word whose tokens start this sentence
        j = wi
        while j < len(out) and (not wtoks[j] or wtoks[j][0] != need[0]):
            j += 1
        if j >= len(out):
            return [dict(w) for w in (words or []) if isinstance(w, dict)]
        got, first = 0, True
        while j < len(out) and got < len(need):
            if wtoks[j]:
                out[j].update({"_s": si, "_sfirst": first, "_stext": sent, "_scta": cta})
                first = False
                got += len(wtoks[j])
            j += 1
        wi = j
    return out


def _clause_clip(sentence: str, max_words: int) -> str:
    """Longest leading run of whole clauses (ending at , ; : —) within
    max_words, or "" when the first clause alone is longer."""
    best = ""
    for m in _CLAUSE_RE.finditer(sentence + " "):
        head = sentence[:m.start()].strip()
        if len(head.split()) <= max_words:
            best = head
        else:
            break
    return best


def _window_text(words, t0: float, t1: float, max_words: int = 8) -> str:
    """Card copy for the window [t0, t1).

    Sentence-annotated words (compose): the whole sentence(s) that START in
    the window, as written in the script (punctuation, identifiers kept),
    up to _SENT_CARD_MAX_WORDS; when none starts there, the sentence being
    spoken. A sentence longer than that is cut at a clause boundary, never
    mid-clause; no clause fits → "". The Subscribe/endcard line never lands
    on a quote card. Unannotated words: the old word run, stopped at the
    first Subscribe word.
    """
    from app.services import craft
    ws = []
    for w in words or []:
        try:
            st = float(w.get("start") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        ws.append((st, w))
    if any("_s" in w for _, w in ws):
        inwin = [w for st, w in ws if t0 - 1e-9 <= st < t1 - 1e-9 and "_s" in w
                 and not w.get("_scta")]
        if not inwin:
            return ""
        order = []
        for w in inwin:
            if w["_s"] not in order:
                order.append(w["_s"])
        starting = [w["_s"] for w in inwin if w.get("_sfirst")]
        chosen = starting or order[:1]
        text_by = {w["_s"]: w["_stext"] for w in inwin}
        text = ""
        for sid in chosen:
            cand = (text + " " + text_by[sid]).strip()
            if len(cand.split()) > _SENT_CARD_MAX_WORDS:
                break
            text = cand
        if not text:
            text = _clause_clip(text_by[chosen[0]], _SENT_CARD_MAX_WORDS)
        return text
    toks = []
    for st, w in ws:
        if t0 - 1e-9 <= st < t1 - 1e-9:
            t = str(w.get("text") or "").strip()
            if theme.fold(t).strip(".,!?—–-") == "subscribe":
                break  # the endcard line ("Subscribe — next …") never feeds a card
            if t:
                toks.append(t)
    text = " ".join(toks)
    if craft.contains_subscribe_cta(text):
        # Endcard VO words never land on a mid quote card (Subscribe = endcard only).
        text = craft.strip_subscribe_cta(text)
    return _words_clip(text, max_words)


_QUOTE_MIN = 1.0  # shortest sentence card worth showing (s)
_LEAD_MAX = _QUOTE_MIN + _GAP  # max visual lead when a card starts before its VO (s)
_CONJ_SPLIT = frozenset({
    "e", "ou", "mas", "porque", "quando", "entao", "enquanto", "pra", "para",
    "and", "or", "but", "because", "when", "so", "while", "then",
})
_REL_SPLIT = frozenset({
    "que", "onde", "como", "sem", "com", "no", "na", "nos", "nas", "em", "ate",
    "which", "that", "who", "where", "with", "without", "in", "on", "until",
})
# A display segment never ENDS on one of these (#1381 "…chamou isso de").
_DANGLING = frozenset({
    "de", "do", "da", "dos", "das", "a", "o", "as", "os", "um", "uma", "em", "no",
    "na", "por", "pra", "para", "com", "sem", "e", "ou", "que", "se", "isso", "seu",
    "sua", "mais", "pelo", "pela", "pelos", "pelas", "num", "numa", "ao", "aos",
    "nem", "mas", "porque", "quando", "como", "ate", "sobre", "entre", "cada",
    "muito", "pouco", "nao", "the", "an", "of", "to", "in", "on", "for", "with", "and",
    "or", "that", "is", "are", "your", "my", "this", "by", "at", "from", "than",
})


# ...and never STARTS on a word that binds to the previous noun ("teto | de VRAM").
_BINDING = frozenset({"de", "do", "da", "dos", "das", "of"})


def _is_annotated(words) -> bool:
    return any(isinstance(w, dict) and "_s" in w for w in (words or []))


def _wfold(w: str) -> str:
    return theme.fold(w).strip(",.;:!?\"'()—–")


def _split_point(ws: list[str], lo: int = 2, min_side: int = 3) -> int | None:
    """Best word index k to split ws into ws[:k] / ws[k:] at a clause seam.

    Tiers: clause punctuation, then before a conjunction, then before a
    relative pronoun / preposition, then any boundary whose left side does
    not end on a function word. Nearest the middle wins within a tier."""
    n = len(ws)
    ks = range(lo, n - lo + 1)
    ks3 = [k for k in range(min_side, n - min_side + 1) if _wfold(ws[k]) not in _BINDING]
    tiers = [
        [k for k in ks if ws[k - 1][-1:] in ",;:" or ws[k] in ("—", "–")],
        [k for k in ks3 if _wfold(ws[k]) in _CONJ_SPLIT],
        [k for k in ks3 if _wfold(ws[k]) in _REL_SPLIT],
        ks3,
    ]
    for t in tiers:
        t = [k for k in t if _wfold(ws[k - 1]) not in _DANGLING]
        if t:
            return min(t, key=lambda k: abs(k - n / 2))
    return None


def _speech_units(words, split_over: float | None = None, min_side: int = 3) -> list[dict]:
    """Display units {start, end, text} from sentence-annotated words.

    One unit per script sentence; a sentence spoken longer than a card
    (_MID_MAX) or longer than 12 words is split at clause seams (never after
    a function word), recursively. The endcard/Subscribe line never becomes a
    unit, and a unit never carries a word of the next sentence.
    ``split_over`` lowers the spoken-span threshold (finer clause units when
    a window needs more cards)."""
    over = (_MID_MAX + _QUOTE_MIN) if split_over is None else float(split_over)
    sents: dict[int, list[dict]] = {}
    for w in words or []:
        if isinstance(w, dict) and "_s" in w and not w.get("_scta"):
            sents.setdefault(w["_s"], []).append(w)
    units = []

    def t_of(w, end=False):
        t = float(w.get("start") or 0.0)
        return t + float(w.get("dur") or 0.0) if end else t

    def emit(tws: list[str], wws: list[dict], first: bool, last: bool) -> None:
        # tws: display tokens (script text); wws: the TTS words spoken for them
        span = t_of(wws[-1], True) - t_of(wws[0]) if wws else 0.0
        if (span > over + 1e-9 or len(tws) > _SENT_CARD_MAX_WORDS) and len(tws) >= 4:
            k = _split_point(tws, min_side=min_side)
            if k:
                need = len(_tok(" ".join(tws[:k])))
                got, m = 0, 0
                while m < len(wws) and got < need:
                    got += len(_tok(wws[m].get("text") or ""))
                    m += 1
                if 0 < m < len(wws):
                    emit(tws[:k], wws[:m], first, False)
                    emit(tws[k:], wws[m:], False, last)
                    return
        if not wws:
            return
        text = " ".join(tws).strip().rstrip(",;:—–").strip()
        units.append({"start": t_of(wws[0]), "end": t_of(wws[-1], True), "text": text,
                      "sfirst": first, "send": last})

    for sid in sorted(sents):
        ws = sents[sid]
        emit(ws[0]["_stext"].split(), ws, True, True)
    return units


def _sentence_cards(words, a: float, b: float, *, taken=(), left_key: str = "",
                    right_key: str = "", hook_toks=frozenset(), flag: str = "_fill",
                    lead_max: float = 0.0):
    """NEW quote cards covering [a, b) — one per display unit spoken there.

    Units are whole sentences (or clause segments of a long sentence), so a
    card never ends mid-clause nor carries the next sentence's first word,
    and never shows the Subscribe line. When nothing new starts at ``a`` the
    unit being spoken at ``a`` leads. Each card starts near its unit's first
    word and holds ≤ _MID_MAX (the visual may lead or trail the VO by <1s).
    Units already on screen (``taken``), echoes of the hook claim and
    fragment/near clashes with neighbours are skipped. Returns None when the
    window cannot be covered with valid cards (caller keeps its fallback).
    With ``lead_max`` the cards may stop up to that much before ``b`` (the
    caller starts the next card early — visual leads the VO).
    """
    for over in (None, _MID_MAX, 2 * _QUOTE_MIN + _GAP):
        cards = _sentence_cards_at(words, a, b, _speech_units(words, over), taken=taken,
                                   left_key=left_key, right_key=right_key,
                                   hook_toks=hook_toks, flag=flag, lead_max=lead_max)
        if cards:
            return cards
    return None


def _sentence_cards_at(words, a, b, allu, *, taken, left_key, right_key, hook_toks, flag,
                       lead_max=0.0):
    from app.services import craft
    units = [dict(u) for u in allu if a - 1e-9 <= u["start"] < b - _QUOTE_MIN]
    if not units or units[0]["start"] > a + 0.5:
        act = [u for u in allu if u["start"] < a - 1e-9 and u["end"] > a + 0.2]
        if act:
            units.insert(0, dict(act[-1]))

    def key_of(t):
        return craft.screen_text_key({"type": "quote", "text": t})

    keep, prev_key = [], left_key
    for u in units:
        k = key_of(u["text"])
        if (not k or k in taken or set(k.split()) <= set(hook_toks)
                or (prev_key and _clashes(k, prev_key))):
            continue
        u["key"] = k
        keep.append(u)
        prev_key = k
    # merge units too close to show alone (text joined when short enough)
    merged = []
    for u in keep:
        # the first card starts at ``a`` (not at its unit's first word)
        prev_start = (a if len(merged) == 1 else max(a, merged[-1]["start"])) if merged else a
        if merged and u["start"] - prev_start < _QUOTE_MIN + _GAP:
            # join only a WHOLE next sentence to a card that ends its own
            # sentence ("A prefill manda. O n_batch obedece."); a partial
            # segment is skipped (never "arrasta. O grande não").
            cand = merged[-1]["text"] + " " + u["text"]
            if (merged[-1].get("send") and u.get("sfirst") and u.get("send")
                    and len(cand.split()) <= _SENT_CARD_MAX_WORDS):
                merged[-1]["text"] = cand
                merged[-1]["key"] = key_of(cand)
            continue
        merged.append(u)
    if right_key:
        while merged and _clashes(merged[-1]["key"], right_key):
            merged.pop()
    n = len(merged)
    if not n:
        return None
    short = (b - a) - n * (_MID_MAX + _GAP)
    if short > 1e-9:
        if short > lead_max + 1e-9:
            return None
        b = a + n * (_MID_MAX + _GAP)
    # starts: s0 = a, sn = b; each slot in [_QUOTE_MIN+_GAP, _MID_MAX+_GAP]
    st = [a] + [max(a, u["start"]) for u in merged[1:]] + [b]
    slot_hi, slot_lo = _MID_MAX + _GAP, _QUOTE_MIN + _GAP
    for _ in range(3):
        for i in range(1, n):  # forward: not too long / not too short
            st[i] = min(max(st[i], st[i - 1] + slot_lo), st[i - 1] + slot_hi)
        for i in range(n - 1, 0, -1):  # backward: honour the fixed end b
            st[i] = max(min(st[i], st[i + 1] - slot_lo), st[i + 1] - slot_hi)
    cards, keys = [], []
    for i, u in enumerate(merged):
        d = st[i + 1] - _GAP - st[i]
        if d > _MID_MAX + 1e-6 or d < _QUOTE_MIN - 1e-6:
            return None
        if u["key"] in keys or (keys and _clashes(u["key"], keys[-1])):
            return None
        keys.append(u["key"])
        cards.append({"type": "quote", "cue": u["text"], "text": u["text"],
                      "attribution": "", flag: True, "start": round(st[i], 3),
                      "dur": round(d, 3)})
    return cards


def _clashes(key: str, other: str) -> bool:
    """Same/near card or a fragment (prefix/suffix/middle run) of it."""
    from app.services import craft
    return craft.screen_text_near(key, other) or craft.screen_text_fragment(key, other)


def _fill_hook_surplus(beats, duration: float, words=None) -> None:
    """Cap a frozen frame0. Surplus is later speech over the hook card.

    Quote-pulse of the hook claim dropped R2 (09-17), and cloning existing
    object mids into the window put the same card twice back-to-back or
    replayed cards 1-3 as 4-6 (VM 2026-09-29, repeated-card FAIL). Each
    slot (walking back from the first mid) becomes a NEW quote card of the
    words spoken in that slot. A slot with no words, or whose words would
    repeat a neighbouring/earlier card or only re-quote the hook claim,
    stops the fill; the remainder stays on the hook (Gate B exempt). One
    pass only (idempotent): a second pass used to re-fill from the clones.
    """
    if not beats or len(beats) < 3 or not words:
        return
    hook = beats[0]
    if (hook.get("type") or "") != "hook":
        return
    if any(b.get("_fill") for b in beats):
        return
    from app.services import craft
    nxt = beats[1]
    nxt_start = float(nxt.get("start") or 0.0)
    hook_dur = float(hook.get("dur") or 0.0)
    if hook_dur <= _HOOK_MAX + 1e-9:
        return
    slot = _MID_MAX + _GAP
    n = int((hook_dur - _HOOK_MAX) / slot)
    # Parity with the old second pass: one more slot when the remainder on
    # the hook would still leave >= _MID_MIN over _HOOK_MAX.
    if hook_dur - n * slot - _HOOK_MAX >= _MID_MIN:
        n += 1
    n = min(_HOOK_FILL_MAX, n)
    if n <= 0:
        return
    hook_toks = set(craft.screen_text_key(hook).split())
    taken = {craft.screen_text_key(b) for b in beats[1:]}
    taken.discard("")
    if _is_annotated(words):
        # Sentence cards (RR 2026-09-30): frame0 keeps ≤ _HOOK_MAX, the rest
        # of the window becomes one card per spoken sentence unit.
        a = min(nxt_start, _HOOK_MAX + _GAP)
        cards = _sentence_cards(words, a, nxt_start, taken=taken,
                                right_key=craft.screen_text_key(nxt),
                                left_key=craft.screen_text_key(hook),
                                hook_toks=hook_toks, flag="_fill")
        if cards:
            hook["dur"] = round(max(_MIN_DUR, float(cards[0]["start"]) - _GAP), 3)
            beats[1:1] = cards
        return
    inserts = []
    end = nxt_start
    right_key = craft.screen_text_key(nxt)
    for _ in range(n):
        start = end - _GAP - _MID_MAX
        if start < _GAP + _MIN_DUR:
            break
        text = _window_text(words, start, end)
        nb = {"type": "quote", "cue": text, "text": text, "attribution": "",
              "_fill": True, "start": round(start, 3), "dur": round(_MID_MAX, 3)}
        key = craft.screen_text_key(nb)
        if (not key or key in taken or _clashes(key, right_key)
                or set(key.split()) <= hook_toks):
            break
        inserts.append(nb)
        taken.add(key)
        right_key = key
        end = start
    if not inserts:
        return
    inserts.reverse()
    first_start = float(inserts[0]["start"])
    hook["dur"] = round(max(_MIN_DUR, first_start - _GAP), 3)
    beats[1:1] = inserts


def _break_repeated_cards(beats, words=None) -> None:
    """Renderer backstop for the craft-gate repeated-card check.

    When the LLM storyboard itself shows the same card again (e.g. one stat
    for "…não é engenharia. Isso não é engenharia."), the repeat becomes a
    different beat type: a quote of the words spoken in its own window, or
    of its cue. Timing is untouched. If neither candidate clears the check
    the beat is left as-is and the craft gate FAILs it (never ships quietly).
    """
    from app.services import craft
    n = len(beats)
    for i in range(1, n):
        b = beats[i]
        if (b.get("type") or "") in ("hook", "cta"):
            continue
        if not craft.repeated_card_reason(beats, i):
            continue
        s0 = float(b.get("start") or 0.0)
        s1 = s0 + float(b.get("dur") or 0.0)
        old = dict(b)
        if _is_annotated(words):
            # sentence units only (whole sentence / clause segment): never the
            # LLM cue clip, which ends mid-sentence (#1381 "…chamou isso de").
            us = _speech_units(words)
            cands = ([u["text"] for u in us if s0 - 1e-9 <= u["start"] < s1 - 1e-9]
                     + [u["text"] for u in us if u["start"] < s0 <= u["end"]][-1:])
        else:
            cands = [_window_text(words, s0, s1), _words_clip(b.get("cue") or "", 8)]
        for cand in cands:
            if not cand:
                continue
            b.clear()
            b.update({"type": "quote", "cue": old.get("cue", ""), "text": cand,
                      "attribution": "", "start": old.get("start"), "dur": old.get("dur")})
            nxt_key = craft.screen_text_key(beats[i + 1]) if i + 1 < n else ""
            if not craft.repeated_card_reason(beats, i) and not _clashes(
                    craft.screen_text_key(b), nxt_key):
                break
            b.clear()
            b.update(old)


def _split_long_mids(beats, words=None) -> None:
    """Split, don't stretch (VM 2026-09-29, P0 b): a mid whose hold exceeds
    _MID_MAX keeps its first _MID_MAX and the rest of its window becomes NEW
    quote card(s) of the words spoken there, each ≤ _MID_MAX. Never a copy
    (repeated-card gate). All-or-nothing per beat: if a piece has no words or
    would repeat a card, the beat is left to the backward walk in _cap_endcard.
    """
    if not words or len(beats) < 3:
        return
    from app.services import craft
    slot = _MID_MAX + _GAP
    i = 1
    while i < len(beats) - 1:
        b = beats[i]
        if (b.get("type") or "") in ("hook", "cta"):
            i += 1
            continue
        s0 = float(b.get("start") or 0.0)
        rem = float(b.get("dur") or 0.0) - _MID_MAX - _GAP
        if _is_annotated(words) and float(b.get("dur") or 0.0) > _MID_MAX + 1e-9 \
                and rem < _QUOTE_MIN - 1e-9:
            # Overflow too short for its own card: the next card starts early
            # (visual leads the VO by < 1s) instead of stretching this one.
            nxt = beats[i + 1]
            n_end = float(nxt.get("start") or 0.0) + float(nxt.get("dur") or 0.0)
            n_start = s0 + _MID_MAX + _GAP
            cap = _ENDCARD_MAX if i + 1 == len(beats) - 1 else None
            if cap is None or n_end - n_start <= cap + 1e-9:
                b["dur"] = round(_MID_MAX, 3)
                nxt["start"] = round(n_start, 3)
                nxt["dur"] = round(n_end - n_start, 3)
            i += 1
            continue
        if (rem < _MID_MIN - 1e-9 and not _is_annotated(words)) \
                or float(b.get("dur") or 0.0) <= _MID_MAX + 1e-9:
            i += 1
            continue
        taken = {craft.screen_text_key(x) for x in beats}
        taken.discard("")
        if _is_annotated(words):
            w_end = s0 + float(b.get("dur") or 0.0) + _GAP
            nxt = beats[i + 1]
            cards = _sentence_cards(words, s0 + _MID_MAX + _GAP, w_end,
                                    taken=taken, left_key=craft.screen_text_key(b),
                                    right_key=craft.screen_text_key(nxt),
                                    flag="_split", lead_max=_LEAD_MAX)
            if cards:
                c_end = float(cards[-1]["start"]) + float(cards[-1]["dur"]) + _GAP
                if c_end < w_end - 1e-6:
                    # cards stop short: the next card starts early (≤ _LEAD_MAX)
                    n_end = float(nxt.get("start") or 0.0) + float(nxt.get("dur") or 0.0)
                    if i + 1 == len(beats) - 1 and n_end - c_end > _ENDCARD_MAX + 1e-9:
                        cards = None
                    else:
                        nxt["start"] = round(c_end, 3)
                        nxt["dur"] = round(n_end - c_end, 3)
            if not cards:
                i += 1
                continue
            b["dur"] = round(_MID_MAX, 3)
            beats[i + 1:i + 1] = cards
            i += 1 + len(cards)
            continue
        k = int(-(-(rem + _GAP) // slot))  # ceil
        piece = (rem + _GAP) / k - _GAP
        prev_key = craft.screen_text_key(b)
        next_key = craft.screen_text_key(beats[i + 1])
        inserts = []
        t = s0 + _MID_MAX + _GAP
        for j in range(k):
            text = _window_text(words, t, t + piece + _GAP)
            nb = {"type": "quote", "cue": text, "text": text, "attribution": "",
                  "_split": True, "start": round(t, 3), "dur": round(piece, 3)}
            key = craft.screen_text_key(nb)
            if (not key or key in taken or _clashes(key, prev_key)
                    or (j == k - 1 and _clashes(key, next_key))):
                break
            inserts.append(nb)
            taken.add(key)
            prev_key = key
            t += piece + _GAP
        if len(inserts) != k:
            i += 1
            continue
        b["dur"] = round(_MID_MAX, 3)
        beats[i + 1:i + 1] = inserts
        i += 1 + k


def _pull_first_cut(beats, duration: float, words=None, cut_by: float | None = None) -> None:
    """RR hook pace (P1 d): the first cut lands by craft.HOOK_FIRST_CUT_BY_S.

    Frame0 keeps the whole claim (#45) and ends at the cut. The window
    [cut, old first card) becomes a NEW quote card of the words spoken there
    when it is long enough and not a repeat (never a hook-claim echo);
    otherwise the first card starts early (visual leads the VO). A card that
    then exceeds its cap keeps the cap and pushes the next card earlier
    (split, never stretch). All-or-nothing: if the endcard would overflow or
    the board becomes invalid, the board is restored and Gate B reports it.
    """
    from app.services import craft
    cut = craft.HOOK_FIRST_CUT_BY_S if cut_by is None else float(cut_by)
    if len(beats) < 3 or (beats[0].get("type") or "") != "hook":
        return
    first = float(beats[1].get("start") or 0.0)
    if first <= cut + 1e-9:
        return
    snap = copy.deepcopy(beats)
    beats[0]["dur"] = round(max(_MIN_DUR, cut - _GAP), 3)
    if _is_annotated(words):
        cards = None
        if first - cut >= _QUOTE_MIN + _GAP - 1e-9:
            taken = {craft.screen_text_key(b) for b in beats[1:]}
            taken.discard("")
            cards = _sentence_cards(
                words, cut, first, taken=taken,
                left_key=craft.screen_text_key(beats[0]),
                right_key=craft.screen_text_key(beats[1]),
                hook_toks=set(craft.screen_text_key(beats[0]).split()), flag="_cut")
        if cards:
            beats[1:1] = cards
    elif first - cut >= _MID_MIN + _GAP - 1e-9 and words:
        text = _window_text(words, cut, first)
        nb = {"type": "quote", "cue": text, "text": text, "attribution": "",
              "_cut": True, "start": round(cut, 3), "dur": round(first - _GAP - cut, 3)}
        key = craft.screen_text_key(nb)
        hook_toks = set(craft.screen_text_key(beats[0]).split())
        taken = {craft.screen_text_key(b) for b in beats[1:]}
        taken.discard("")
        if (key and key not in taken and set(key.split()) - hook_toks
                and not _clashes(key, craft.screen_text_key(beats[1]))):
            beats.insert(1, nb)
    beats[1]["start"] = round(cut, 3)
    i = 1
    while True:
        cur = beats[i]
        last = i == len(beats) - 1
        cur_start = float(cur["start"])
        if last:
            end = float(snap[-1].get("start") or 0.0) + float(snap[-1].get("dur") or 0.0)
        else:
            end = float(beats[i + 1].get("start") or 0.0) - _GAP
        cap = _ENDCARD_MAX if (cur.get("type") or "") == "cta" else _MID_MAX
        if end - cur_start <= cap + 1e-9:
            cur["dur"] = round(end - cur_start, 3)
            break
        if last:
            beats[:] = snap
            return
        cur["dur"] = round(cap, 3)
        beats[i + 1]["start"] = round(cur_start + cap + _GAP, 3)
        i += 1
    if not validate_storyboard(beats, duration):
        beats[:] = snap


def _cap_endcard(beats, duration: float, words=None) -> None:
    """Craft gate: last cta/endcard ≤ 4.0s, after the claim, not on frame0.

    Slide the chip to the tail. Surplus walks backward onto earlier mids,
    each re-capped at _MID_MAX. Hook stays pinned at 0; leftover that
    would freeze frame0 is then filled by _fill_hook_surplus.
    """
    if not beats or beats[-1].get("type") != "cta":
        return
    last = beats[-1]
    start = float(last.get("start") or 0.0)
    dur = float(last.get("dur") or 0.0)
    end = start + dur
    if end < float(duration) - 1e-9:
        end = float(duration)
        dur = end - start
    if dur > _ENDCARD_MAX + 1e-9:
        extra = dur - _ENDCARD_MAX
        last["start"] = round(start + extra, 3)
        last["dur"] = round(_ENDCARD_MAX, 3)
        if len(beats) >= 2:
            prev = beats[-2]
            prev_start = float(prev.get("start") or 0.0)
            prev["dur"] = round(max(_MIN_DUR, last["start"] - _GAP - prev_start), 3)
    elif dur >= _MIN_DUR:
        last["dur"] = round(dur, 3)

    # Split, don't stretch: long mids (incl. the penultimate that just absorbed
    # the endcard slide) become card + new quote cards before the walk below.
    _split_long_mids(beats, words)

    # Walk surplus backward so a 4s chip does not revive R4 DRAG on mids.
    if _is_annotated(words) and beats and (beats[0].get("type") or "") == "hook":
        # Surplus that would reach the hook (frame0 freeze / RR first cut)
        # is absorbed by a NEW sentence card inserted where an unshown
        # sentence starts inside a mid (#1386: the pre-endcard window only
        # holds the Subscribe line, which never becomes a card).
        base = copy.deepcopy(beats)
        _walk_back(beats)
        for _ in range(3):
            surplus = float(beats[0].get("dur") or 0.0) - float(base[0].get("dur") or 0.0)
            if surplus <= 0.01:
                break
            trial = copy.deepcopy(base)
            if not _insert_slack_card(trial, words, surplus):
                break
            walked = copy.deepcopy(trial)
            _walk_back(walked)
            if float(walked[0]["dur"]) >= float(beats[0]["dur"]) - 1e-6:
                break
            base = trial
            beats[:] = walked
    else:
        _walk_back(beats)
    _fill_hook_surplus(beats, duration, words)


def _walk_back(beats) -> None:
    """Re-cap mids at _MID_MAX from the end; overflow moves a card later and
    lengthens the one before it (the leftover ends on the hook)."""
    for i in range(len(beats) - 2, 0, -1):
        nxt = beats[i + 1]
        cur = beats[i]
        cur_start = float(cur.get("start") or 0.0)
        nxt_start = float(nxt.get("start") or 0.0)
        cur["dur"] = round(max(_MIN_DUR, nxt_start - _GAP - cur_start), 3)
        if float(cur["dur"]) <= _MID_MAX + 1e-9:
            continue
        extra = float(cur["dur"]) - _MID_MAX
        new_start = min(cur_start + extra, nxt_start - _MIN_DUR)
        if new_start <= cur_start + 1e-9:
            continue
        cur["start"] = round(new_start, 3)
        cur["dur"] = round(max(_MIN_DUR, nxt_start - _GAP - cur["start"]), 3)
        prev = beats[i - 1]
        prev_start = float(prev.get("start") or 0.0)
        prev["dur"] = round(max(_MIN_DUR, cur["start"] - _GAP - prev_start), 3)


def _insert_slack_card(beats, words, surplus: float) -> bool:
    """Insert one sentence card that can absorb ``surplus`` seconds.

    Latest mid first: a unit (whole sentence / clause segment) not yet on
    screen that starts inside the mid, leaving the mid ≥ _MID_MIN (quotes
    ≥ _QUOTE_MIN) and giving the new card ≤ _MID_MAX once the surplus lands
    on it. Never a repeat, a neighbour fragment or a hook-claim echo."""
    from app.services import craft
    keys = [craft.screen_text_key(b) for b in beats]
    taken = {k for k in keys if k}
    hook_toks = set(keys[0].split()) if keys else set()
    units = _speech_units(words)
    for i in range(len(beats) - 2, 0, -1):
        b = beats[i]
        if (b.get("type") or "") in ("hook", "cta"):
            continue
        s_i = float(b.get("start") or 0.0)
        e_i = s_i + float(b.get("dur") or 0.0)
        keep = _QUOTE_MIN if (b.get("type") or "") == "quote" else _MID_MIN
        for u in units:
            st = float(u["start"])
            if st - _GAP - s_i < keep - 1e-9 or st >= e_i - 1e-9:
                continue
            if (e_i - st) + surplus > _MID_MAX + 1e-9 or (e_i - st) + surplus < _QUOTE_MIN:
                continue
            k = craft.screen_text_key({"type": "quote", "text": u["text"]})
            if (not k or k in taken or set(k.split()) <= hook_toks
                    or _clashes(k, keys[i]) or _clashes(k, keys[i + 1])):
                continue
            b["dur"] = round(st - _GAP - s_i, 3)
            beats.insert(i + 1, {"type": "quote", "cue": u["text"], "text": u["text"],
                                 "attribution": "", "_split": True, "start": round(st, 3),
                                 "dur": round(e_i - st, 3)})
            return True
    return False


_GEN_FLAGS = ("_fill", "_split", "_cut")


def _card_min(b) -> float:
    """Shortest hold a card may get when the sync solver squeezes it."""
    t = b.get("type") or ""
    if t == "hook":
        return _MIN_DUR
    return _QUOTE_MIN if t in ("quote", "statement") else 1.4


def _sync_solve_once(beats, v, duration: float, hook_max: float):
    """Starts that put every card within [speech - lead max, speech] (never
    after its own words) with holds in [card min, cap]; endcard ≤ _ENDCARD_MAX.
    Returns ("ok", starts) or ("gap"|"crowd", i): no solution between card i
    and card i+1 (too far apart → needs a card; too close → drop one)."""
    from app.services import craft
    n = len(beats)
    lead = craft.SYNC_LEAD_MAX_S
    D = float(duration)
    inf = float("inf")
    A = []
    for i, b in enumerate(beats):
        if i == 0 and (b.get("type") or "") == "hook":
            A.append((0.0, 0.0))
            continue
        lo, hi = 0.0, D - _MIN_DUR
        if v[i] is not None:
            lo, hi = max(lo, float(v[i]) - lead), min(hi, float(v[i]))
        if i == n - 1 and (b.get("type") or "") == "cta":
            # the endcard cap wins over its own VO start: when the Subscribe
            # line starts earlier than the cap allows, the chip sits at the
            # cap (Gate B notes it as late_capped, never a mid card).
            lo = max(lo, D - _ENDCARD_MAX)
            hi = max(hi, lo)
        A.append((lo, hi))

    def dmax(i):
        return (hook_max if (beats[i].get("type") or "") == "hook" else _MID_MAX) + _GAP

    def dmin(i):
        return _card_min(beats[i]) + _GAP

    B = [None] * n
    B[n - 1] = A[n - 1]
    if B[n - 1][0] > B[n - 1][1] + 1e-9:
        return "crowd", n - 2
    for i in range(n - 2, -1, -1):
        lo = max(A[i][0], B[i + 1][0] - dmax(i))
        hi = min(A[i][1], B[i + 1][1] - dmin(i))
        if lo > hi + 1e-9:
            return ("gap" if A[i][1] < B[i + 1][0] - dmax(i) - 1e-9 else "crowd"), i
        B[i] = (lo, hi)
    st = []
    for i, b in enumerate(beats):
        cur = float(b.get("start") or 0.0)
        lo, hi = B[i]
        if i:
            lo = max(lo, st[-1] + dmin(i - 1))
            hi = min(hi, st[-1] + dmax(i - 1))
        st.append(min(max(cur, lo), hi))
    return "ok", st


def _sync_board(beats, words, duration: float, hook_max: float) -> bool:
    """CoS 2026-09-30: no card after its own speech; lead ≤ 1.1s.

    Re-times the board on the speech starts Gate B measures
    (craft.card_speech_starts). Where cards are too far apart a NEW sentence
    card (unit not yet on screen) is inserted; where they are too close a
    generated card is dropped. All-or-nothing: False leaves the board as it
    was (Gate B then reports the out-of-sync cards)."""
    from app.services import craft
    if len(beats) < 2:
        return False
    snap = copy.deepcopy(beats)
    levels = [_speech_units(words, over) for over in (None, _MID_MAX, 2 * _QUOTE_MIN + _GAP)]
    levels.append(_speech_units(words, 2 * _QUOTE_MIN + _GAP, min_side=2))
    for _ in range(24):
        v = craft.card_speech_starts(beats, words)
        state, res = _sync_solve_once(beats, v, duration, hook_max)
        if state == "ok":
            for i, b in enumerate(beats):
                b["start"] = round(res[i], 3)
            for i in range(len(beats) - 1):
                beats[i]["dur"] = round(res[i + 1] - _GAP - res[i], 3)
            beats[-1]["dur"] = round(float(duration) - res[-1], 3)
            if validate_storyboard(beats, duration) and not craft.repeated_card_hits(beats) \
                    or craft.repeated_card_hits(beats) == craft.repeated_card_hits(snap):
                return True
            break
        i = res
        if state == "crowd":
            gen = [j for j in (i + 1, i) if 0 < j < len(beats) - 1
                   and any(beats[j].get(f) for f in _GEN_FLAGS)]
            if not gen:
                break
            del beats[gen[0]]
            continue
        # gap: a unit whose speech starts between card i and card i+1
        lo_t = float(v[i]) if v[i] is not None else float(beats[i].get("start") or 0.0)
        hi_t = float(v[i + 1]) if v[i + 1] is not None else float(
            beats[i + 1].get("start") or duration)
        keys = [craft.screen_text_key(b) for b in beats]
        taken = {k for k in keys if k}
        hook_toks = set(keys[0].split()) if (beats[0].get("type") or "") == "hook" else set()
        mid = (lo_t + hi_t) / 2

        def gate_clash(k, j):
            # what Gate B itself rejects next to beat j (near-same text; a
            # fragment only between text cards)
            return craft.screen_text_near(k, keys[j]) or (
                (beats[j].get("type") or "") in craft.FRAGMENT_TYPES
                and craft.screen_text_fragment(k, keys[j]))

        best = None
        for units, strict in [(lv, st_) for st_ in (True, False) for lv in levels]:
            for u in units:
                t = float(u["start"])
                if not (lo_t + 0.1 <= t <= hi_t - 0.1):
                    continue
                k = craft.screen_text_key({"type": "quote", "text": u["text"]})
                clash = ((_clashes(k, keys[i]) or _clashes(k, keys[i + 1])) if strict
                         else (gate_clash(k, i) or gate_clash(k, i + 1)))
                if not k or k in taken or set(k.split()) <= hook_toks or clash:
                    continue
                if best is None or abs(t - mid) < abs(float(best["start"]) - mid):
                    best = u
            if best is not None:
                break
        if best is None and (beats[i].get("type") or "") in ("quote", "statement") and i > 0:
            # the quote itself holds the long sentence: it keeps the head
            # segment and the tail segment becomes the new card (#1386
            # "Primeiro você descobre" | "o que o arquivo é.")
            ct = (beats[i].get("text") or "").split()
            for u in levels[-1]:
                t = float(u["start"])
                ut = u["text"].split()
                if not (lo_t + 0.1 <= t <= hi_t - 0.1):
                    continue
                if (len(ut) >= len(ct) - 1 or _tok(" ".join(ct[-len(ut):])) != _tok(u["text"])):
                    continue
                head = " ".join(ct[:-len(ut)]).rstrip(",;:—–").strip()
                if len(head.split()) < 2 or _wfold(head.split()[-1]) in _DANGLING:
                    continue
                beats[i]["text"] = head
                best = u
                break
        if best is None:
            break
        beats.insert(i + 1, {"type": "quote", "cue": best["text"], "text": best["text"],
                             "attribution": "", "_split": True,
                             "start": round(float(best["start"]), 3), "dur": _QUOTE_MIN})
    beats[:] = snap
    return False


def _cap_statements(beats, content_format=None) -> None:
    """Gate C backstop: Shorts keep ≤1 statement. Extra cards become quotes.

    Variety retry is prompt-level and loses (v1262: 3 statements + code/cmp
    still shipped FAIL C). Deterministic conversion after demote, before align.
    Quotes keep the on-screen line; they are not statement-spam. Long-form
    is unchanged (stmt cap 2, no Gate C).
    """
    from app.services import craft
    if (content_format or "short") == "long":
        return
    cap = craft.STATEMENT_MAX_SHORTS
    seen = 0
    for b in beats:
        if (b.get("type") or "") != "statement":
            continue
        seen += 1
        if seen <= cap:
            continue
        cue = b.get("cue") or ""
        text = _words_clip(b.get("text") or cue or "·", 8) or "·"
        b.clear()
        b.update({"type": "quote", "cue": cue, "text": text})


def _diagram_is_nonsense(b: dict) -> bool:
    nodes = b.get("nodes") or []
    if len(nodes) < 2:
        return True
    labels = [theme.fold(n.get("label") or "") for n in nodes]
    if labels and all((not x) or _GENERIC_NODE.match(x) for x in labels):
        return True
    edges = b.get("edges") or []
    labeled = [e for e in edges if (e.get("label") or "").strip()]
    return not labeled and len(nodes) <= 3


def _demote_nonsense_diagrams(beats, content_format) -> None:
    shorts = (content_format or "short") != "long"
    for b in beats:
        if b.get("type") != "diagram":
            continue
        if not (shorts or _diagram_is_nonsense(b)):
            continue
        labels = [n.get("label") for n in (b.get("nodes") or []) if n.get("label")]
        text = _words_clip(" ".join(labels) or b.get("cue") or "", 8) or "·"
        cue = b.get("cue", "")
        b.clear()
        b.update({"type": "statement", "cue": cue, "text": text, "w": 2, "emoji": ""})


def _cap_list_holds(beats) -> None:
    """Kill empty list dumps >6s by dumping the leftover into the next beat."""
    for i, b in enumerate(beats):
        if b.get("type") != "list":
            continue
        dur = float(b.get("dur") or 0)
        if dur <= _LIST_MAX:
            continue
        extra = dur - _LIST_MAX
        b["dur"] = _LIST_MAX
        if i + 1 < len(beats) and b.get("start") is not None:
            nxt = beats[i + 1]
            nxt["start"] = b["start"] + _LIST_MAX
            nxt["dur"] = float(nxt.get("dur") or 0) + extra


def compose(*, subject, script, words, duration, resolution, width, height,
            topic_id=None, content_format="short", allowed_types=None, language=None,
            llm, brand=None, channel_id=None, channel_slug=None,
            provided_thumb=False, topic_name=None) -> str | None:
    """Generate a composition index.html via the typed-storyboard path.

    Returns the HTML string, or None on failure (the caller then uses the deterministic
    _fallback_composition). ``llm`` is the worker._llm callable (kept as a param to avoid
    importing worker)."""
    allowed = list(allowed_types or ["hook", "statement", "stat", "compare", "list",
                                      "term_define", "quote", "cta"])
    th = theme.resolve(topic_id, subject, brand=brand,
                       channel_id=channel_id, channel_slug=channel_slug)
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
    if not _variety_ok(beats, content_format):
        stmt_n = "one" if content_format != "long" else "two"
        retry = llm(
            user + "\n\nYour draft relied on plain 'statement' beats that just repeat the spoken "
            "words. Redo it: use AT MOST " + stmt_n + " 'statement' beat(s) and convert the rest into "
            "stat / compare / list / term_define" +
            ("/ code / command / diagram" if any(t in allowed for t in ("code", "command", "diagram")) else "") +
            ". Exactly one hook first and one cta last. Hook text = first spoken sentence.",
            system=system, max_tokens=1500).strip()
        rb = parse_storyboard(retry, allowed)
        if rb and (_variety_ok(rb, content_format) or len(_rich_types(rb)) > len(_rich_types(beats))):
            beats = rb

    # R2: if code/command is allowed but the draft has neither, push once for a snippet.
    # Keep the retry only when it actually adds one (otherwise keep the varied original).
    if not _code_ok(beats, allowed):
        retry = llm(
            user + "\n\nYour draft had no code or command beat. Redo it: keep hook-first and "
            "cta-last, keep variety (at most " +
            ("one" if content_format != "long" else "two") +
            " statement beat(s)), and include EXACTLY one "
            "`code` or `command` beat with a minimal realistic snippet (<=5 lines, <=30 chars) "
            "that shows the thing the narration only describes (terminal, receipt, API bill).",
            system=system, max_tokens=1500).strip()
        rb = parse_storyboard(retry, allowed)
        if rb and _code_ok(rb, allowed):
            beats = rb

    # Quote cards are sentence-bounded: each word knows its script sentence.
    words = annotate_sentences(words, script)
    _lock_opening_hook(beats, script, subject)
    _show_whole_claim(beats, script, subject)
    _apply_split_card(beats, subject, provided_thumb=provided_thumb)
    _demote_nonsense_diagrams(beats, content_format)
    _sanitize_cta(beats, script, subject=subject, brand=brand or th.get("brand"),
                  content_format=content_format, topic_name=topic_name)
    _strip_mid_subscribe_beats(beats)
    _cap_statements(beats, content_format)

    align_storyboard(beats, words, duration)
    _cap_list_holds(beats)
    _cap_endcard(beats, duration, words)
    if not validate_storyboard(beats, duration):
        beats[:] = [b for b in beats if not (b.get("_fill") or b.get("_split"))]
        _even_space(beats, duration)
        _cap_list_holds(beats)
        _cap_endcard(beats, duration, words)
        if not validate_storyboard(beats, duration):
            logger.info("storyboard: timing invalid for %r — falling back", subject)
            return None
    _break_repeated_cards(beats, words)
    from app.services import craft as _craft
    rr_pace = ((content_format or "short") != "long"
               and (brand or th.get("brand") or "") in _craft.HOOK_PACE_BRANDS)
    if rr_pace:
        _pull_first_cut(beats, duration, words)
    if (content_format or "short") != "long" and _is_annotated(words):
        # Card ↔ speech sync (CoS 2026-09-30): never after its words, lead ≤ 1.1s.
        _sync_board(beats, words, duration,
                    (_craft.HOOK_FIRST_CUT_BY_S - _GAP) if rr_pace else _HOOK_MAX)
    return build_index_html(beats, th, resolution, width, height, duration,
                            content_format=content_format)
