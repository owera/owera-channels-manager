"""Craft gates shared across generate / storyboard / thumbnail / publish.

Decolar lock: the first spoken sentence IS the title hook. Frame 0 and the
thumbnail must repeat that claim (or a faithful ≤8-word compression) — never a
second typographic hook or curiosity gap. The opening visual is a concrete
OBJECT on frame0 AND the thumb that echoes that same spoken phrase — not an
abstract diagram, emoji soup, or generic slide.

Spoken-title suffix (shorts): `· <series> <nn>` with series in
Copilot Credits | Agent memory | CrewAI | IA | Local | Claude Code.

Series endcard (shorts, after the claim — not frame0): spoken
`Subscribe — next {series} {noun}.` (≤8 words) + chip `· {series}`.
Optional micro `same series` only if it fits. No Follow / waitlist /
Cloud / SMY. Endcard hold ≤ ENDCARD_MAX_S (4.0s).

Pré-pattern leftovers are parked via reject. Do not mass-retitle.
"""

from __future__ import annotations

import json
import re

from app.services.engines import theme

SERIES_LABELS = (
    "Copilot Credits",
    "Agent memory",
    "CrewAI",
    "IA",
    "Local",
    "Claude Code",
)

# Middle-dot + one of the live series labels + integer episode. Case-insensitive
# so a PT "ia 12" still matches the IA label; labels with spaces stay literal.
SPOKEN_TITLE_RE = re.compile(
    r"·\s*(Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code)\s+\d+\b",
    re.IGNORECASE,
)

TITLE_GATE_REASON = (
    "title does not match spoken series pattern "
    "(need '· Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code <nn>') "
    "— pré-pattern leftovers must be parked via reject, not mass-retitled"
)

# Next-claim noun on the series endcard VO. Only these — never invent a CTA.
NEXT_CLAIM_NOUNS = ("trap", "receipt", "bill", "drop")

# Brand defaults when the title has no · series nn (English public YT).
# OS = Copilot Credits; RR = IA. Other series only swap {series}/{noun}.
DEFAULT_SERIES = {"os": "Copilot Credits", "rr": "IA"}
DEFAULT_SERIES_FALLBACK = "Copilot Credits"
DEFAULT_NOUN = "trap"

# Craft gate: last cta/endcard visual hold. Claim stays on screen; chip is last.
ENDCARD_MAX_S = 4.0

# Spoken series endcard. 1 line, ≤8 words. Subscribe is the YT ask — not Follow.
#   Subscribe — next {series} {noun}.
# ALLOWED only as the last spoken line / last visual slot. Mid-short (miolo)
# Subscribe CTAs are stripped — this is not a global invert of Follow/waitlist.
_ENDCARD_VO_RE = re.compile(
    r"subscribe\s*[—–-]\s*next\s+.+\s+(?:trap|receipt|bill|drop)\.?\s*$",
    re.IGNORECASE,
)

# Subscribe / Inscreva CTAs. Never added to BANNED_RE (that would strip the
# endcard VO). Use strip_mid_subscribe / contains_subscribe_cta instead.
SUBSCRIBE_CTA_RE = re.compile(
    r"\bsubscribe\b"
    r"|\binscreva(?:-se)?"
    r"|\binscreve(?:-se)?"
    r"|\bse\s+inscreve\b",
    re.IGNORECASE,
)

# Endcard-only bans (card + VO). Broader than BANNED_RE: amanhã / owera.com /
# Cloud / "part 2 coming" / 💸 / neon. Do NOT run this on the whole script —
# "cloud GPU" in a lesson is fine; it must not appear on the endcard.
_ENDCARD_BANNED_PHRASES = (
    r"\bfollow(?:\s+tomorrow)?\b",
    r"\bsiga\b",
    r"\bsigue\b",
    r"\bamanh[ãa]\b",
    r"\bwaitlist\b",
    r"\bowera\.com\b",
    r"\bowera\.ai\b",
    r"\bowera cloud\b",
    r"\bcloud[- ]as[- ](?:a[- ])?(?:product|ready|service)\b",
    r"\bcloud is (?:live|ready)\b",
    r"\bpart\s*2\s+coming\b",
    r"\bsmy\b",
    r"\binstagram\b",
    r"\blinkedin\b",
    r"💸",
    r"\bneon\b",
)
ENDCARD_BANNED_RE = re.compile("|".join(_ENDCARD_BANNED_PHRASES), re.IGNORECASE)

# Builder/confiança shorts: ZERO follow / waitlist / Cloud-as-product / SMY /
# Instagram-LinkedIn CTAs. Matched case-insensitively against folded text.
# Subscribe on the series endcard VO is allowed (English public YT).
_BANNED_PHRASES = (
    r"\bfollow\s+for\s+more\b",
    r"\bfollow\s*→",
    r"\bfollow\s+me\b",
    r"\bsiga\s*[-–—]?\s*amanh[ãa]\b",
    r"\bsiga\s+para\s+mais\b",
    r"\bsiga\s*→",
    r"\bsiga\s+me\b",
    r"\bsiga\b",
    r"\bfollow\b",
    r"\bsigue\b",
    r"\bwaitlist\b",
    r"\bjoin the waitlist\b",
    r"\bowera cloud\b",
    r"\bcloud[- ]as[- ](?:a[- ])?(?:product|ready|service)\b",
    r"\bcloud is (?:live|ready)\b",
    r"\bsmy\b",
    r"\binstagram\b",
    r"\blinkedin\b",
    r"\bowera\.ai\b",
    r"\bcli\.owera\.ai\b",
    r"\bowera\.com\b",
)

BANNED_RE = re.compile("|".join(_BANNED_PHRASES), re.IGNORECASE)

# Idea / script / metadata prompt addenda — enforced in code, not only theme_prompt.
CRAFT_RULES_SHORT = (
    "CRAFT (enforced): first spoken sentence IS the title hook. "
    "Frame 0 and the thumbnail show a concrete object that echoes that phrase "
    "(receipt, terminal, bill, the named tool) — not a diagram, emoji soup, or generic slide. "
    "No Follow/Siga/'follow for more'/Siga-amanhã/Follow-tomorrow. No waitlist, "
    "owera.com, Owera Cloud-as-product, SMY, 'part 2 coming', Instagram or LinkedIn. "
    "Subscribe is ALLOWED only as the LAST spoken line of the series endcard "
    "('Subscribe — next {series} {noun}.') + chip '· {series}'. FORBIDDEN in the "
    "mid-short / body / miolo: no Subscribe text, VO, or chip before the endcard. "
    "(noun = trap|receipt|bill|drop; optional micro 'same series' only if it fits). "
    "Title suffix must be '· <series> <nn>' with series one of: "
    + " | ".join(SERIES_LABELS) + "."
)

STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "your", "you",
    "is", "it", "that", "this", "with", "at", "as", "be", "by", "from", "my",
    "o", "a", "os", "as", "de", "da", "do", "em", "um", "uma", "e", "que", "seu",
    "sua", "no", "na", "pra", "pro",
}


def spoken_title_ok(title: str | None) -> bool:
    return bool(SPOKEN_TITLE_RE.search(title or ""))


def title_gate_reason(title: str | None, content_format: str | None = "short") -> str | None:
    """None = allowed to leave review toward publish. Longs are exempt (no series suffix)."""
    if (content_format or "short") == "long":
        return None
    if spoken_title_ok(title):
        return None
    return TITLE_GATE_REASON


def first_spoken_sentence(script: str | None) -> str:
    text = (script or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return (parts[0] if parts else text).strip()


def spoken_hook_source(title: str | None, script: str | None, subject: str | None = None) -> str:
    """The claim frame0/thumb must echo: title before '·', else first spoken sentence."""
    raw = (title or "").strip()
    if raw:
        head = raw.split("·", 1)[0].strip()
        if head:
            return head
    return first_spoken_sentence(script) or (subject or "").strip()


def compress_claim(text: str | None, max_words: int = 8) -> str:
    words = (text or "").split()
    return " ".join(words[:max_words]).strip().rstrip(".!?…,;:").strip()


def claim_aligned(hook: str | None, spoken: str | None) -> bool:
    """True when hook is the same claim (echo/compression), not a second slogan."""
    h = theme.fold(hook or "")
    s = theme.fold(spoken or "")
    if not h or not s:
        return False
    if h == s or h in s or s.startswith(h):
        return True
    hw = {w for w in h.split() if w not in STOPWORDS}
    sw = {w for w in s.split() if w not in STOPWORDS}
    if not hw or not sw:
        return False
    return bool(hw & sw)


# Designer lock (YPP move 1): 1 object = the noun of the spoken first phrase.
# Kind picks the widget (bill UI / receipt / GPU meter / named app / terminal).
# GPU before $ so "VRAM ate $58" is a meter, never 💸. Copilot/credits/$ → bill.
_OBJECT_HINTS = (
    (r"\b(chrome|chromium)\b", "CHROME", "app"),
    (r"\bdiscord\b", "DISCORD", "app"),
    (r"\b(slack|vscode|notion|figma|whatsapp|telegram)\b", None, "app"),
    (r"\bollama\b", "OLLAMA", "terminal"),
    (r"\b(gpu|vram|rtx|egpu|e-gpu|4090|3080|placa|batch)\b", "GPU", "gpu"),
    (r"\b(terminal|cli|shell|stdout|stderr|prompt|comando|command|local)\b",
     "TERMINAL", "terminal"),
    (r"\b(config|\.env\b|yaml|toml)\b", "CONFIG", "terminal"),
    (r"(?:r\$|\$)\s*\d", "BILL", "bill"),
    (r"\b(billed|billing|bill|cobr(?:ou|ar|anca)|credits?|creditos?|copilot)\b",
     "BILL", "bill"),
    (r"\b(recibo|receipt|invoice|fatura)\b", "RECEIPT", "receipt"),
    (r"\b(api|paid|pago|produto|product)\b", "RECEIPT", "receipt"),
    (r"\b(mcp|integra(?:cao|coes)|connector|conector)\b", "MCP", "object"),
    (r"\b(rag|retriev|chunk|embed|rerank)\b", "RAG", "object"),
    (r"\b(memor(?:y|ia)|forget|amnesia|session|sessao)\b", "MEMORY", "object"),
    (r"\bcrew(?:ai)?\b", "CREW", "object"),
    (r"\bclaude\b", "CLAUDE", "terminal"),
)

_AMOUNT_RE = re.compile(
    r"(?:r\$|\$)\s*(\d+(?:[.,]\d{1,2})?)|"
    r"(\d+(?:[.,]\d{1,2})?)\s*(?:usd|credits?|creditos?)",
    re.IGNORECASE,
)
_VRAM_RE = re.compile(r"(\d+)\s*(gb|%|percent)", re.IGNORECASE)

# Hard-FAIL tokens: never the opening object (Designer + VM lock).
FORBIDDEN_OBJECT_EMOJI = ("💸", "🔥")
RAINBOW_HEX = ("#a36bff", "#ff5bb0")

# Extra folds dropped when mining a fallback noun (title grammar, not the object).
_OBJECT_STOP = STOPWORDS | {
    "about", "how", "why", "what", "when", "title", "video", "watch", "here",
    "there", "then", "than", "into", "over", "after", "before", "porque",
    "quando", "onde", "como", "para", "mais", "muito", "ainda", "voce",
    "its", "are", "was", "were", "been", "have", "has", "had", "will", "can",
    "not", "nao", "sem", "only", "just", "very",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E0-\U0001F1FF"
    "]+"
)


def _spoken_amount(text: str | None) -> str:
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return ""
    n = (m.group(1) or m.group(2) or "").replace(",", ".")
    raw = theme.fold(m.group(0))
    if "$" in (m.group(0) or "") or raw.startswith("r$"):
        return "$" + n
    return n


def _vram_fill(text: str | None) -> tuple[str, str]:
    """(meter width %, caption) from a spoken GB/% — else a mute 70% meter."""
    m = _VRAM_RE.search(text or "")
    if not m:
        return "70%", "VRAM"
    n, unit = m.group(1), theme.fold(m.group(2))
    if unit == "gb":
        return "70%", n + " GB"
    return f"{min(100, int(n))}%", n + "%"


def opening_object(text: str | None) -> dict:
    """Concrete object for frame0 + thumb, grounded in the spoken first phrase.

    Returns label / kind / amount / prompt / app. Kind is the widget
    (bill, receipt, gpu, app, terminal, object) — not an OS/RR brand split.
    """
    raw = text or ""
    folded = theme.fold(raw)
    spec = {"label": "OBJECT", "kind": "object", "amount": "", "prompt": "",
            "app": "", "meter": "70%", "meter_caption": "VRAM"}
    for pat, label, kind in _OBJECT_HINTS:
        m = re.search(pat, folded)
        if not m:
            continue
        spec["kind"] = kind
        spec["label"] = (label or m.group(0)).upper()[:16]
        break
    else:
        words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9$]+", raw)
                 if theme.fold(w) not in _OBJECT_STOP and len(w) >= 3]
        if words:
            long_enough = [w for w in words if len(w) >= 4]
            spec["label"] = (long_enough[0] if long_enough else words[0]).upper()[:16]
    spec["amount"] = _spoken_amount(raw)
    if spec["kind"] == "app":
        spec["app"] = spec["label"]
    if spec["kind"] == "terminal":
        spec["prompt"] = "$ ollama run" if "ollama" in folded else "$"
    if spec["kind"] == "gpu":
        spec["meter"], spec["meter_caption"] = _vram_fill(raw)
    return spec


def coerce_object(obj) -> dict:
    """Normalize a label-string / partial dict into a full opening_object spec."""
    if isinstance(obj, dict) and obj.get("kind") and obj.get("label"):
        base = opening_object(obj.get("label"))
        base.update({k: obj[k] for k in obj if obj[k] not in (None, "")})
        return base
    if isinstance(obj, dict) and obj.get("label"):
        guessed = opening_object(str(obj["label"]))
        guessed["label"] = str(obj["label"])
        return guessed
    if isinstance(obj, str) and obj.strip():
        return opening_object(obj)
    return opening_object("")


def object_echoes(label: str | None, spoken: str | None) -> bool:
    """True when chrome label is grounded in the spoken first phrase."""
    if not label or not spoken:
        return False
    derived = opening_object(spoken)["label"]
    if theme.fold(label) == theme.fold(derived):
        return True
    return claim_aligned(label, spoken)


def object_markup(obj: dict | str | None) -> str:
    """HTML widget for the opening object. Never emoji. Never covers hook type."""
    spec = coerce_object(obj)
    kind = spec["kind"]
    label = theme.esc(spec["label"])
    amount = theme.esc(spec.get("amount") or "")
    attrs = (f'class="obj obj-{theme.esc(kind)}" data-object="{label}" '
             f'data-kind="{theme.esc(kind)}"')
    if kind == "bill":
        big = amount or label
        return (f'<div {attrs}><div class="obj-head">INVOICE</div>'
                f'<div class="obj-amt">{big}</div>'
                f'<div class="obj-line"></div><div class="obj-line obj-line-s"></div></div>')
    if kind == "receipt":
        extra = f'<div class="obj-amt">{amount}</div>' if amount else ""
        return (f'<div {attrs}><div class="obj-head">RECEIPT</div>'
                f'<div class="obj-stub">POST /v1/charges</div>{extra}'
                f'<div class="obj-tear"></div></div>')
    if kind == "gpu":
        fill = theme.esc(spec.get("meter") or "70%")
        cap = theme.esc(spec.get("meter_caption") or "VRAM")
        return (f'<div {attrs}><div class="obj-chip"><i></i><i></i><i></i></div>'
                f'<div class="obj-meter"><b style="width:{fill}"></b></div>'
                f'<div class="obj-cap">{cap}</div></div>')
    if kind == "app":
        mark = theme.esc((spec.get("app") or spec["label"])[:2])
        return (f'<div {attrs}><div class="obj-icon">{mark}</div>'
                f'<div class="obj-name">{label}</div></div>')
    if kind == "terminal":
        prompt = theme.esc(spec.get("prompt") or "$")
        return (f'<div {attrs}><div class="obj-tb"><i></i><i></i><i></i></div>'
                f'<div class="obj-prompt">{prompt}</div></div>')
    return (f'<div {attrs}><div class="obj-token">{label}</div></div>')


# Shared widget CSS — storyboard + thumbnail. Object sits ABOVE/BESIDE the hook
# type; it must not overlay line 1. No emoji, no rainbow bar.
OBJECT_CSS = (
    ".obj{display:flex;flex-direction:column;align-items:stretch;justify-content:center;"
    "gap:.28em;width:100%;box-sizing:border-box;border:3px solid var(--obj-accent,#888);"
    "background:rgba(0,0,0,.45);color:#fff;border-radius:10px;padding:.55em .7em;text-align:left}"
    ".obj-head,.obj-cap,.obj-name,.obj-stub,.obj-prompt{font-family:var(--obj-mono,ui-monospace,Menlo,Consolas,monospace);"
    "font-size:clamp(14px,2.1vw,22px);letter-spacing:.1em;opacity:.85;font-weight:700}"
    ".obj-amt{font-weight:900;font-size:clamp(42px,8vw,92px);line-height:.95;letter-spacing:-2px}"
    ".obj-line{height:6px;background:rgba(255,255,255,.22);border-radius:3px;width:88%}"
    ".obj-line-s{width:54%}"
    ".obj-tear{height:10px;margin-top:.2em;background:repeating-linear-gradient(90deg,"
    "transparent 0 8px,rgba(255,255,255,.25) 8px 10px)}"
    ".obj-chip{display:flex;gap:6px;align-items:center}"
    ".obj-chip i{display:block;width:22px;height:22px;border:2px solid var(--obj-accent,#888);"
    "background:rgba(255,255,255,.08)}"
    ".obj-meter{height:14px;background:rgba(255,255,255,.12);border-radius:7px;overflow:hidden}"
    ".obj-meter b{display:block;height:100%;background:var(--obj-accent,#888)}"
    ".obj-icon{width:72px;height:72px;border-radius:16px;border:3px solid var(--obj-accent,#888);"
    "display:flex;align-items:center;justify-content:center;font-weight:900;font-size:28px}"
    ".obj-tb{display:flex;gap:6px}"
    ".obj-tb i{display:block;width:10px;height:10px;border-radius:50%;background:var(--obj-accent,#888)}"
    ".obj-token{font-weight:900;font-size:clamp(28px,5.4vw,56px);letter-spacing:.08em}"
    ".obj-prompt{font-size:clamp(16px,2.6vw,28px)}"
)


def object_hard_fail(html: str | None, hook: str | None, spoken: str | None) -> str | None:
    """None = pass. Reason = Designer hard-FAIL (typography-only / emoji / rainbow / drift)."""
    h = html or ""
    if any(tok in h for tok in FORBIDDEN_OBJECT_EMOJI):
        return "emoji-as-object"
    if any(c in h.lower() for c in RAINBOW_HEX):
        return "neon-bar rainbow"
    if 'class="obj ' not in h or "data-kind=" not in h:
        return "typography-only"
    if spoken and hook and not claim_aligned(hook, spoken):
        return "punchline ≠ spoken first phrase"
    return None


def emoji_first(text: str | None) -> bool:
    raw = (text or "").lstrip()
    return bool(raw) and bool(_EMOJI_RE.match(raw))


def emoji_soup(text: str | None) -> bool:
    """Two+ emoji, or the whole string is emoji — generic 💸 punch, not an object."""
    raw = text or ""
    bits = _EMOJI_RE.findall(raw)
    if len(bits) >= 2:
        return True
    rest = _EMOJI_RE.sub("", raw).strip()
    return bool(bits) and not rest


def scan_banned(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in BANNED_RE.finditer(text)]


def contains_banned(text: str | None) -> bool:
    return bool(scan_banned(text))


def is_endcard_vo(text: str | None) -> bool:
    """True when text is the series endcard spoken closer (trailing match)."""
    return bool(_ENDCARD_VO_RE.search((text or "").strip()))


def contains_subscribe_cta(text: str | None) -> bool:
    return bool(SUBSCRIBE_CTA_RE.search(text or ""))


def strip_subscribe_cta(text: str | None) -> str:
    """Drop subscribe-CTA sentences. Does not protect the endcard VO — use
    strip_mid_subscribe for scripts that may already carry the closer."""
    raw = (text or "").strip()
    if not raw or not contains_subscribe_cta(raw):
        return raw
    parts = re.split(r"(?<=[.!?…])\s+", raw)
    kept = [p for p in parts if p.strip() and not contains_subscribe_cta(p)]
    if kept:
        return " ".join(kept).strip()
    cleaned = SUBSCRIBE_CTA_RE.sub("", raw)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -—,;:→")


def strip_mid_subscribe(script: str | None) -> str:
    """Ban Subscribe in the miolo. Keep only a trailing endcard VO, if present."""
    raw = (script or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", raw) if p.strip()]
    if not parts:
        return ""
    out: list[str] = []
    last = len(parts) - 1
    for i, p in enumerate(parts):
        if i == last and is_endcard_vo(p):
            out.append(p)
            continue
        if contains_subscribe_cta(p):
            continue
        out.append(p)
    return " ".join(out).strip()


def mid_body_has_subscribe(script: str | None) -> bool:
    """True if Subscribe appears before a trailing endcard VO (or anywhere if none)."""
    raw = (script or "").strip()
    if not raw or not contains_subscribe_cta(raw):
        return False
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", raw) if p.strip()]
    if parts and is_endcard_vo(parts[-1]):
        return any(contains_subscribe_cta(p) for p in parts[:-1])
    return True


def strip_banned(text: str | None) -> str:
    """Drop sentences that carry a banned CTA. Keeps the rest (never raises)."""
    raw = (text or "").strip()
    if not raw or not contains_banned(raw):
        return raw
    parts = re.split(r"(?<=[.!?…])\s+", raw)
    kept = [p for p in parts if p.strip() and not contains_banned(p)]
    if kept:
        return " ".join(kept).strip()
    # Whole blob was the CTA — strip the matching phrases in place.
    cleaned = BANNED_RE.sub("", raw)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -—,;:→")


def brand_of(slug: str | None, name: str | None = None, channel_id=None) -> str | None:
    """os = Owera Software B&W; rr = Rodrigo Recio burgundy. None = legacy neon.

    Delegates to ``theme.infer_brand`` so storyboard/thumbnail/render share one map:
    slug/name tokens win; live-fleet ids 1/2 are the fallback.
    """
    return theme.infer_brand(channel_id, slug, name)


# ---------------------------------------------------------------------------
# Video Maker craft gate (Shorts only) — A object 0–3s / B beats ≤3s / C spam
# ---------------------------------------------------------------------------

OBJECT_BEAT_TYPES = frozenset({"code", "command", "diagram", "compare", "stat"})
TYPOGRAPHY_ONLY_TYPES = frozenset({"hook", "statement"})
CTA_TYPES = frozenset({"cta", "endcard"})
OPENING_WINDOW_S = 3.0
MID_BEAT_MAX_S = 3.0
CTA_BEAT_MAX_S = 4.0
STATEMENT_MAX_SHORTS = 1
LIST_MAX_PER_SHORT = 1
LIST_MAX_ITEMS = 3
LIST_STAGGER_MAX_S = 0.6
LIST_REVEAL_PAD_S = 0.8  # matches storyboard.render_list win = dur - 0.8

_BEAT_SNAP_KEYS = (
    "type", "start", "dur", "cue", "text", "sub", "object", "prop", "emoji",
    "items", "value", "unit", "label", "title", "lines", "command", "nodes",
)
# Subscribe CTA regex lives above (never in BANNED_RE). Gate C uses it via
# _subscribe_hits — legal only on the trailing cta/endcard series.
_BEATS_SCRIPT_RE = re.compile(
    r'<script type="application/json" id="storyboard-beats">(.*?)</script>',
    re.DOTALL,
)
_BEAT_DIV_RE = re.compile(
    r'class="beat ([a-z_]+)"[^>]*data-start="([0-9.]+)"[^>]*data-duration="([0-9.]+)"',
)
_HAS_WORD_RE = re.compile(r"[A-Za-z0-9À-ÿ]")


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _emoji_only(text: str | None) -> bool:
    """True when the string has no letters/digits — emoji/punct is not a Decolar object."""
    s = (text or "").strip()
    return (not s) or (_HAS_WORD_RE.search(s) is None)


def hook_object(beat: dict | None) -> str:
    """Non-empty Decolar prop on a hook. Emoji is rejected (does not count as object)."""
    if not isinstance(beat, dict):
        return ""
    raw = beat.get("object") or beat.get("prop") or ""
    if not isinstance(raw, str):
        raw = str(raw or "")
    raw = raw.strip()
    if _emoji_only(raw):
        return ""
    return raw


def snapshot_beats(beats) -> list[dict]:
    """Gate-relevant subset (JSON-safe). Drops renderer-only keys."""
    out = []
    for b in beats or []:
        if not isinstance(b, dict):
            continue
        snap = {k: b[k] for k in _BEAT_SNAP_KEYS if k in b}
        out.append(snap)
    return out


def beats_from_html(html: str | None) -> list[dict]:
    """Prefer the compose-embedded JSON snapshot; scrape data-* divs otherwise."""
    raw = html or ""
    m = _BEATS_SCRIPT_RE.search(raw)
    if m:
        try:
            data = json.loads(m.group(1).replace("<\\/", "</"))
            if isinstance(data, list):
                return [b for b in data if isinstance(b, dict)]
        except (TypeError, ValueError):
            pass
    scraped = []
    for m in _BEAT_DIV_RE.finditer(raw):
        scraped.append({
            "type": m.group(1),
            "start": float(m.group(2)),
            "dur": float(m.group(3)),
        })
    return scraped


def _cue_start(beat: dict) -> float:
    try:
        return float(beat.get("start") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _cue_span(beats: list[dict], i: int) -> float:
    """next_cue_start - cue_start. Last beat falls back to stored dur."""
    start = _cue_start(beats[i])
    if i + 1 < len(beats):
        nxt = beats[i + 1].get("start")
        if nxt is not None:
            try:
                return max(0.0, float(nxt) - start)
            except (TypeError, ValueError):
                pass
    try:
        return max(0.0, float(beats[i].get("dur") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _list_items(beat: dict) -> list:
    items = beat.get("items")
    return items if isinstance(items, list) else []


def _list_stagger(beat: dict, span: float) -> float:
    n = len(_list_items(beat))
    if n <= 0:
        return 0.0
    win = max(0.0, span - LIST_REVEAL_PAD_S)
    return min(win / n, LIST_STAGGER_MAX_S)


def _endcard_series_start(board: list[dict]) -> int:
    """Index of the trailing cta/endcard series. len(board) if the last beat is not one."""
    i = len(board)
    while i > 0 and (board[i - 1].get("type") or "") in CTA_TYPES:
        i -= 1
    return i


def _visible_copy(beat: dict) -> str:
    """On-screen copy only (not cue). Subscribe on a mid card is a CTA, not a sync word."""
    parts: list[str] = []
    for k in ("text", "sub", "title", "label"):
        v = beat.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    for it in _list_items(beat):
        if isinstance(it, dict):
            t = it.get("text")
            if t:
                parts.append(str(t))
        elif it:
            parts.append(str(it))
    for side in ("left", "right"):
        col = beat.get(side)
        if not isinstance(col, dict):
            continue
        if col.get("title"):
            parts.append(str(col["title"]))
        for x in col.get("items") or []:
            parts.append(str(x))
    return " ".join(parts)


def _subscribe_hits(text: str) -> list[str]:
    return [m.group(0) for m in SUBSCRIBE_CTA_RE.finditer(text or "")]


def _echoes_narration(beat: dict) -> bool:
    """True when on-screen copy is just the cue / spoken words (no added information)."""
    cue = theme.fold(str(beat.get("cue") or ""))
    text = theme.fold(str(beat.get("text") or ""))
    if not text:
        parts = []
        for it in _list_items(beat):
            if isinstance(it, dict):
                parts.append(str(it.get("text") or ""))
            else:
                parts.append(str(it))
        text = theme.fold(" ".join(parts))
    if not text or not cue:
        return False
    tw = {w for w in text.split() if w not in STOPWORDS}
    cw = {w for w in cue.split() if w not in STOPWORDS}
    if not tw:
        return False
    return tw <= cw or text in cue or cue in text


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def video_maker_gate(beats, *, content_format: str | None = "short",
                     used_fallback: bool = False) -> dict:
    """A+B+C PASS/FAIL for YouTube Shorts. Longs are exempt (all PASS, no reasons)."""
    checks = {"A": "PASS", "B": "PASS", "C": "PASS"}
    reasons: list[str] = []
    if (content_format or "short") == "long":
        return {"result": "PASS", "checks": checks, "reasons": reasons}

    board = [b for b in (beats or []) if isinstance(b, dict)]

    if used_fallback and not board:
        checks["A"] = "FAIL"
        checks["C"] = "FAIL"
        reasons.append(
            "[A] Object 0–3s: FAIL — kinetic-text fallback has no typed beats "
            "(typography-only; emoji is not an object). Need a code/command/diagram/"
            "compare/stat beat starting before t=3.0s, or hook.object (receipt/"
            "terminal/bill)."
        )
        reasons.append(
            "[C] Spoken list/slide spam: FAIL — fallback re-displays narration as "
            "text cards (no code/command/diagram/compare/stat). statement max 1; "
            "list forbidden-or ≤1 list / ≤3 items / ≤3.0s / stagger ≤0.6s."
        )
        return {"result": "FAIL", "checks": checks, "reasons": reasons}

    if not board:
        # Legacy row / missing snapshot — caller fail-opens via video_maker_gate_reason.
        return {"result": "PASS", "checks": checks, "reasons": reasons,
                "legacy": True}

    # --- A: object in the first 3.0s ---------------------------------------
    opening = [b for b in board if _cue_start(b) < OPENING_WINDOW_S + 1e-9]
    if not opening:
        opening = [board[0]]
    hooks = [b for b in board if (b.get("type") or "") == "hook"]
    has_object_beat = any((b.get("type") or "") in OBJECT_BEAT_TYPES for b in opening)
    has_hook_object = any(hook_object(b) for b in (hooks or opening[:1]))
    if not (has_object_beat or has_hook_object):
        kinds = [b.get("type") or "?" for b in opening]
        checks["A"] = "FAIL"
        reasons.append(
            f"[A] Object 0–3s: FAIL — first {OPENING_WINDOW_S:.1f}s is typography-only "
            f"(types={kinds}; emoji does not count). Need ≥1 beat type in "
            f"{sorted(OBJECT_BEAT_TYPES)} starting before t={OPENING_WINDOW_S:.1f}s, "
            "or a non-empty hook.object Decolar prop (receipt/terminal/bill)."
        )

    # --- B: mid beats ≤3.0s; cta/endcard series ≤4.0s ----------------------
    b_hits = []
    for i, b in enumerate(board):
        span = _cue_span(board, i)
        btype = b.get("type") or "?"
        cap = CTA_BEAT_MAX_S if btype in CTA_TYPES else MID_BEAT_MAX_S
        if span > cap + 1e-9:
            label = "cta/endcard" if btype in CTA_TYPES else "mid"
            b_hits.append(
                f"beat[{i}] type={btype} {label} held {span:.2f}s "
                f"(next_cue − cue; limit {cap:.1f}s)"
            )
    if b_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] Beats ≤3s: FAIL — " + "; ".join(b_hits) +
            ". Mid cards/slides must be ≤3.0s; cta/endcard series ≤4.0s "
            "(not a Follow-tomorrow hold)."
        )

    # --- C: kill spoken list/slide spam ------------------------------------
    types = [b.get("type") or "" for b in board]
    n_stmt = types.count("statement")
    list_idxs = [i for i, t in enumerate(types) if t == "list"]
    rich = {t for t in types if t in OBJECT_BEAT_TYPES}
    c_hits = []
    if n_stmt > STATEMENT_MAX_SHORTS:
        c_hits.append(
            f"{n_stmt} statement beats (max {STATEMENT_MAX_SHORTS} on Shorts; "
            "was tolerated at 2)"
        )
    if len(list_idxs) > LIST_MAX_PER_SHORT:
        c_hits.append(
            f"{len(list_idxs)} list beats (max {LIST_MAX_PER_SHORT}; prefer "
            "code/command/diagram/compare/stat)"
        )
    for i in list_idxs:
        b = board[i]
        n_items = len(_list_items(b))
        span = _cue_span(board, i)
        stagger = _list_stagger(b, span)
        if n_items > LIST_MAX_ITEMS:
            c_hits.append(
                f"list beat[{i}] has {n_items} items (max {LIST_MAX_ITEMS})"
            )
        if span > MID_BEAT_MAX_S + 1e-9:
            c_hits.append(
                f"list beat[{i}] held {span:.2f}s (max {MID_BEAT_MAX_S:.1f}s)"
            )
        if stagger > LIST_STAGGER_MAX_S + 1e-9:
            c_hits.append(
                f"list beat[{i}] item stagger {stagger:.2f}s "
                f"(max {LIST_STAGGER_MAX_S:.1f}s)"
            )
    echo_spam = []
    for i, b in enumerate(board):
        if (b.get("type") or "") not in ("statement", "list"):
            continue
        if _echoes_narration(b) and not rich:
            echo_spam.append(f"beat[{i}] type={b.get('type')}")
    if echo_spam:
        c_hits.append(
            "list/statement only re-displays narration with no rich type "
            f"({', '.join(echo_spam)}; need code/command/diagram/compare/stat "
            "in the middle)"
        )
    elif (n_stmt or list_idxs) and not rich:
        c_hits.append(
            "list/statement mid-board with no code/command/diagram/compare/stat "
            "(spoken-slide spam)"
        )
    # Subscribe CTA is only legal on the trailing cta/endcard series.
    end_i = _endcard_series_start(board)
    for i, b in enumerate(board[:end_i]):
        hits = _subscribe_hits(_visible_copy(b))
        if hits:
            shown = "/".join(dict.fromkeys(hits))
            c_hits.append(
                f"beat[{i}] type={b.get('type') or '?'} has Subscribe CTA "
                f"({shown!r}) — Subscribe is only allowed on the series "
                "endcard, never on a mid card"
            )
    if c_hits:
        checks["C"] = "FAIL"
        reasons.append("[C] Spoken list/slide spam: FAIL — " + "; ".join(c_hits))

    result = "FAIL" if reasons else "PASS"
    return {"result": result, "checks": checks, "reasons": reasons}


def format_craft_gate_reason(gate: dict | None) -> str | None:
    if not gate or gate.get("result") != "FAIL":
        return None
    reasons = [r for r in (gate.get("reasons") or []) if r]
    if not reasons:
        return "Video Maker craft gate FAIL"
    return "Video Maker craft gate FAIL: " + " ".join(reasons)


def video_maker_gate_reason(creation_config=None,
                            content_format: str | None = "short") -> str | None:
    """None = allowed to leave review toward publish. Longs are exempt."""
    if (content_format or "short") == "long":
        return None
    cc = _as_dict(creation_config)
    beats = cc.get("beats")
    if not isinstance(beats, list):
        beats = []
    used_fallback = bool(cc.get("used_fallback"))
    if not beats and not used_fallback:
        stored = cc.get("craft_gate")
        if isinstance(stored, dict) and stored.get("result") == "FAIL":
            return format_craft_gate_reason(stored)
        return None  # no snapshot (pré-gate inventory) — fail-open
    gate = video_maker_gate(beats, content_format=content_format,
                            used_fallback=used_fallback)
    return format_craft_gate_reason(gate)


def review_gate_reason(title: str | None,
                       content_format: str | None = "short",
                       creation_config=None) -> str | None:
    """Title pattern then Video Maker A+B+C. First failure wins (operator-readable)."""
    return (title_gate_reason(title, content_format)
            or video_maker_gate_reason(creation_config, content_format))


# ---------------------------------------------------------------------------
# Series endcard (shorts) — Subscribe VO + chip · {series}. Not in BANNED_RE.
# ---------------------------------------------------------------------------

def _canonical_series(raw: str) -> str:
    folded = theme.fold(raw)
    for label in SERIES_LABELS:
        if theme.fold(label) == folded:
            return label
    return raw.strip()


def series_of(title: str | None, brand: str | None = None) -> str:
    """Series label for the endcard. Title suffix wins; else brand default.

    OS → Copilot Credits; RR → IA. Unknown brand → Copilot Credits (English
    public YT). Never invent a third CTA — only swap {series}/{noun}.
    """
    m = SPOKEN_TITLE_RE.search(title or "")
    if m:
        return _canonical_series(m.group(1))
    return DEFAULT_SERIES.get(brand or "", DEFAULT_SERIES_FALLBACK)


def next_claim_noun(*blobs: str | None) -> str:
    """Pick trap|receipt|bill|drop from title/script; default trap."""
    blob = theme.fold(" ".join(b or "" for b in blobs))
    for noun in ("receipt", "bill", "drop", "trap"):
        if re.search(rf"\b{noun}\b", blob):
            return noun
    return DEFAULT_NOUN


def series_endcard_vo(series: str, noun: str = DEFAULT_NOUN) -> str:
    """Spoken closer: Subscribe — next {series} {noun}.  ≤8 words by construction."""
    n = noun if noun in NEXT_CLAIM_NOUNS else DEFAULT_NOUN
    line = f"Subscribe — next {series} {n}."
    # Safety clip — live series labels are 1–2 words; never invent extra CTA.
    words = line.rstrip(".").split()
    if len(words) > 8:
        line = " ".join(words[:8]) + "."
    return line


def series_endcard_chip(series: str) -> str:
    """On-screen chip, one line. Not a Follow/Subscribe button."""
    return f"· {series}"


def series_endcard_micro(series: str) -> str:
    """Optional second line. Only if the chip is short enough; no extra CTA."""
    chip = series_endcard_chip(series)
    if len(chip) <= 24:
        return "same series"
    return ""


def series_endcard(title: str | None, script: str | None = None,
                   brand: str | None = None, noun: str | None = None) -> dict:
    """VO + chip + optional micro for the series endcard.

    Defaults: OS / Copilot Credits / trap; RR / IA / trap.
    """
    series = series_of(title, brand)
    n = noun if noun in NEXT_CLAIM_NOUNS else next_claim_noun(title, script)
    vo = series_endcard_vo(series, n)
    chip = series_endcard_chip(series)
    micro = series_endcard_micro(series)
    return {"series": series, "noun": n, "vo": vo, "chip": chip, "micro": micro}


def endcard_scan_banned(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in ENDCARD_BANNED_RE.finditer(text)]


def endcard_clean(card: dict) -> bool:
    """True when VO/chip/micro carry no banned endcard CTA and stay in template."""
    vo = (card.get("vo") or "").strip()
    chip = (card.get("chip") or "").strip()
    micro = (card.get("micro") or "").strip()
    if endcard_scan_banned(vo) or endcard_scan_banned(chip) or endcard_scan_banned(micro):
        return False
    if "subscribe" in theme.fold(chip) or "subscribe" in theme.fold(micro):
        return False  # Subscribe is VO-only
    if micro and micro != "same series":
        return False
    if not chip.startswith("· "):
        return False
    if len(vo.split()) > 8:
        return False
    return bool(_ENDCARD_VO_RE.search(vo))


def ensure_series_endcard_vo(script: str | None, subject: str | None,
                             brand: str | None = None,
                             noun: str | None = None) -> str:
    """Pin the last spoken sentence to the series endcard VO. No TTS overhaul —
    one appended (or replaced) English line. Long-form callers should skip this."""
    card = series_endcard(subject, script, brand, noun)
    vo = card["vo"]
    raw = strip_mid_subscribe(script)
    if not raw:
        return vo
    if is_endcard_vo(raw):
        parts = [p for p in re.split(r"(?<=[.!?…])\s+", raw) if p.strip()]
        if parts:
            parts[-1] = vo
            return " ".join(parts).strip()
    return (raw.rstrip() + " " + vo).strip()
