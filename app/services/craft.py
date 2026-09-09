"""Craft gates shared across generate / storyboard / thumbnail / publish.

Decolar lock: the first spoken sentence IS the title hook. Frame 0 and the
thumbnail must repeat that claim (or a faithful ≤8-word compression) — never a
second typographic hook or curiosity gap.

Spoken-title suffix (shorts): `· <series> <nn>` with series in
Copilot Credits | Agent memory | CrewAI | IA | Local | Claude Code.

Pré-pattern leftovers are parked via reject. Do not mass-retitle.
"""

from __future__ import annotations

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

# Builder/confiança shorts: ZERO follow / waitlist / Cloud-as-product / SMY /
# Instagram-LinkedIn CTAs. Matched case-insensitively against folded text.
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
)

BANNED_RE = re.compile("|".join(_BANNED_PHRASES), re.IGNORECASE)

# Idea / script / metadata prompt addenda — enforced in code, not only theme_prompt.
CRAFT_RULES_SHORT = (
    "CRAFT (enforced): first spoken sentence IS the title hook. "
    "No Follow/Siga/'follow for more'/Siga-amanhã. No waitlist, Owera Cloud-as-product, "
    "SMY, Instagram or LinkedIn CTAs. Short = builder/confiança close, not a subscribe ask. "
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


def scan_banned(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in BANNED_RE.finditer(text)]


def contains_banned(text: str | None) -> bool:
    return bool(scan_banned(text))


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


def brand_of(slug: str | None, name: str | None = None) -> str | None:
    """os = Owera Software B&W; rr = Rodrigo Recio personal. None = legacy neon."""
    blob = f"{slug or ''} {name or ''}".lower()
    if any(tok in blob for tok in ("recio", "rodrigo", "ch2")):
        return "rr"
    if any(tok in blob for tok in ("owera", "ch1")):
        return "os"
    return None
