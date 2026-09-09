"""Visual theme tokens for HyperFrames compositions — palette, per-video theme
resolution, and the no-dependency string helpers shared across the engine.

This module has ZERO internal dependencies (it must NOT import worker / storyboard /
thumbnail). That lets every one of those import from here as the single source of
truth, breaking the worker<->thumbnail import cycle.
"""

import hashlib
import unicodedata

# Eight (accent, deep-background) pairs. Thumbnails key into this by topic_id; the
# in-video composition resolves the SAME palette by topic_id, so a video's motion
# accent matches its own thumbnail. Canonical home — thumbnail.py imports PALETTE
# from here (it used to keep a private copy that diverged from the in-video accent).
PALETTE = [
    ("#5b8cff", "#1b2a6b"),   # blue
    ("#00c9a7", "#0b2e22"),   # teal
    ("#ff6b35", "#2e1208"),   # orange
    ("#9b5fe0", "#1a0b2e"),   # purple
    ("#ff3b5c", "#2e0b12"),   # red
    ("#2ec4b6", "#0b2228"),   # cyan
    ("#ff85a1", "#2e0b1a"),   # pink
    ("#f9c74f", "#2e2208"),   # gold
]

# Background-motion variants (extracted from the old per-template looks). Chosen by
# subject hash so two same-topic videos still differ visually, independent of accent.
BG_VARIANTS = ("bloom", "dots", "scan", "gradient", "overlay")

# Monospace stack — Chromium ships at least one of these (or falls back to its own
# monospace). Used by code/command beats. No font file is bundled.
MONO_STACK = "ui-monospace,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

_SANS_STACK = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"


def _subject_hash(subject: str) -> int:
    return int(hashlib.sha1((subject or "").encode()).hexdigest(), 16)


# Owera Software (ch1): B&W brand. No neon. Topic still shifts the gray so two
# videos aren't identical, but every token stays in the black/white family.
_OS_PALETTE = [
    ("#f5f5f5", "#0a0a0a"),
    ("#e8e8e8", "#111111"),
    ("#ffffff", "#000000"),
    ("#d0d0d0", "#141414"),
    ("#f0f0f0", "#0d0d0d"),
    ("#c8c8c8", "#101010"),
    ("#eeeeee", "#0b0b0b"),
    ("#dadada", "#121212"),
]
_OS_VARIANTS = ("scan", "overlay", "gradient", "scan", "overlay")

# Rodrigo Recio (ch2): personal warm ink, not the shared neon rainbow.
_RR_PALETTE = [
    ("#c45c26", "#1c1410"),   # rust
    ("#b8956a", "#18140f"),   # ink-gold (muted)
    ("#8b5e3c", "#16120e"),   # leather
    ("#d4a574", "#1a1510"),   # paper
    ("#a0673b", "#14110d"),   # walnut
    ("#c4a484", "#1b160f"),   # kraft
    ("#9a6b4f", "#17130f"),   # clay
    ("#e0c4a0", "#1c1711"),   # cream-ink
]
_RR_VARIANTS = ("gradient", "overlay", "scan", "gradient", "overlay")


def resolve(topic_id=None, subject: str = "", brand: str | None = None) -> dict:
    """Resolve the visual theme for a video.

    The palette is keyed by ``topic_id`` (matching the thumbnail, which also keys by
    topic_id) so the in-video accent equals the thumbnail accent; it falls back to a
    subject hash when topic_id is missing. The background variant always varies by
    subject so two videos under the same topic still look distinct.

    ``brand``: ``"os"`` Owera B&W, ``"rr"`` Rodrigo personal, ``None`` legacy neon
    (kept so unbranded unit tests and leftover templates stay deterministic).
    """
    try:
        tid = int(topic_id) if topic_id else 0
    except (TypeError, ValueError):
        tid = 0
    h = _subject_hash(subject)
    if brand == "os":
        pal, variants = _OS_PALETTE, _OS_VARIANTS
        if tid:
            accent, bg_deep = pal[tid % len(pal)]
        else:
            accent, bg_deep = pal[h % len(pal)]
        return {
            "accent": accent,
            "bg_deep": bg_deep,
            "bg_base": "#000000",
            "fg": "#ffffff",
            "fg_dim": "#b0b0b0",
            "mono": MONO_STACK,
            "sans": _SANS_STACK,
            "bg_variant": variants[h % len(variants)],
        }
    if brand == "rr":
        pal, variants = _RR_PALETTE, _RR_VARIANTS
        if tid:
            accent, bg_deep = pal[tid % len(pal)]
        else:
            accent, bg_deep = pal[h % len(pal)]
        return {
            "accent": accent,
            "bg_deep": bg_deep,
            "bg_base": "#14110e",
            "fg": "#f4efe8",
            "fg_dim": "#c4b8a8",
            "mono": MONO_STACK,
            "sans": _SANS_STACK,
            "bg_variant": variants[h % len(variants)],
        }
    if tid:
        accent, bg_deep = PALETTE[tid % len(PALETTE)]
    else:
        accent, bg_deep = PALETTE[h % len(PALETTE)]
    return {
        "accent": accent,
        "bg_deep": bg_deep,
        "bg_base": "#0b0b16",
        "fg": "#ffffff",
        "fg_dim": "#c9d2ff",
        "mono": MONO_STACK,
        "sans": _SANS_STACK,
        "bg_variant": BG_VARIANTS[h % len(BG_VARIANTS)],
    }


def esc(s) -> str:
    """HTML-escape text for safe injection into a composition (mirrors worker._esc)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fold(s: str) -> str:
    """Lowercase + strip diacritics (NFKD). Used for language-agnostic cue<->word
    matching — Portuguese narration ('produção', 'inferência') needs the fold so a
    cue copied verbatim from the script still matches the spoken word tokens."""
    n = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in n if not unicodedata.combining(c)).lower()
