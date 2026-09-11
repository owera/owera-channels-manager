"""Visual theme tokens for HyperFrames compositions — palette, per-video theme
resolution, and the no-dependency string helpers shared across the engine.

This module has ZERO internal dependencies (it must NOT import worker / storyboard /
thumbnail). That lets every one of those import from here as the single source of
truth, breaking the worker<->thumbnail import cycle.
"""

import hashlib
import unicodedata
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# Owera Software signature mark (white-on-dark O crop). Never staged for RR.
OS_LOGO_FILE = "03-owera-o-avatar.png"

# Hexes OS stills must never carry (magenta / burgundy / rainbow neon).
OS_FORBIDDEN_HEX = (
    "#ff2d55", "#c41e5a", "#4a1528", "#2a0a14",
    "#5b8cff", "#a36bff", "#ff85a1", "#9b5fe0", "#ff3b5c",
)

# Filenames / copy RR stills must never carry.
RR_FORBIDDEN_MARKS = (
    "owera", "wordmark", OS_LOGO_FILE, "02-owera-wordmark-light.png",
    "brand-mark",
)

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


# Owera Software (ch1): B&W. Accent is gray capped at #9A9A9A. Cold glow only —
# no magenta. Topic still shifts the gray so two videos aren't identical.
_OS_PALETTE = [
    ("#9a9a9a", "#1a1a1a"),
    ("#8a8a8a", "#161616"),
    ("#7a7a7a", "#141414"),
    ("#6b6b6b", "#121212"),
    ("#808080", "#181818"),
    ("#909090", "#1a1a1a"),
    ("#757575", "#151515"),
    ("#888888", "#171717"),
]
_OS_VARIANTS = ("scan", "overlay", "gradient", "scan", "overlay")

# Rodrigo Recio (ch2): black canvas + burgundy upper-corner glow. Accent is the
# thin object stroke #C41E5A — never an Owera mark. Topic shifts glow depth.
_RR_ACCENT = "#c41e5a"
_RR_PALETTE = [
    (_RR_ACCENT, "#4a1528"),
    (_RR_ACCENT, "#2a0a14"),
    (_RR_ACCENT, "#3a101e"),
    (_RR_ACCENT, "#4a1528"),
    (_RR_ACCENT, "#2a0a14"),
    (_RR_ACCENT, "#35121c"),
    (_RR_ACCENT, "#421424"),
    (_RR_ACCENT, "#2e0c16"),
]
_RR_VARIANTS = ("gradient", "overlay", "scan", "gradient", "overlay")

RESOLVE_KEYS = (
    "accent", "bg_deep", "bg_base", "fg", "fg_dim", "mono", "sans", "bg_variant",
    "brand", "glow", "glow2", "stroke", "logo",
)


def infer_brand(channel_id=None, channel_slug: str | None = None,
                channel_name: str | None = None) -> str | None:
    """os = Owera Software (ch1); rr = Rodrigo Recio (ch2); None = legacy neon.

    Slug/name tokens win so a renamed row stays correct; numeric id 1/2 is the
    live-fleet fallback when the slug is missing.
    """
    blob = f"{channel_slug or ''} {channel_name or ''}".lower()
    if any(tok in blob for tok in ("recio", "rodrigo", "ch2")):
        return "rr"
    if any(tok in blob for tok in ("owera", "ch1")):
        return "os"
    try:
        cid = int(channel_id) if channel_id is not None and channel_id != "" else 0
    except (TypeError, ValueError):
        cid = 0
    if cid == 2:
        return "rr"
    if cid == 1:
        return "os"
    return None


def logo_filename(brand: str | None) -> str:
    """OS only. Empty for RR / unbranded — RR must never reference an Owera asset."""
    return OS_LOGO_FILE if brand == "os" else ""


def logo_height_px(frame_height: int) -> int:
    """~64px on 1080×1920; clamped 48–72 and ≤8% of frame height."""
    try:
        h = int(frame_height)
    except (TypeError, ValueError):
        h = 1920
    scaled = int(round(h * (64 / 1920)))
    cap = min(72, int(h * 0.08)) if h else 72
    return max(48, min(72, cap, scaled if scaled else 48))


def mark_inset_px(width: int, height: int) -> int:
    """≥48px from edges on the delivered frame (thumbs downscale 1920→1280)."""
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return 48
    if w > h:  # landscape thumb render 1920×1080 → 1280×720 (×2/3)
        return 72
    return 48


def stage_brand_assets(dest: Path, brand: str | None) -> None:
    """Copy the OS mark into a HyperFrames job dir. No-op for RR / unbranded."""
    name = logo_filename(brand)
    if not name:
        return
    src = _ASSETS_DIR / name
    if src.is_file():
        (Path(dest) / name).write_bytes(src.read_bytes())


def _pick_pair(pal, tid, h):
    if tid:
        return pal[tid % len(pal)]
    return pal[h % len(pal)]


def _tokens(*, accent, bg_deep, bg_base, fg, fg_dim, bg_variant, brand,
            glow, glow2, stroke, logo) -> dict:
    return {
        "accent": accent,
        "bg_deep": bg_deep,
        "bg_base": bg_base,
        "fg": fg,
        "fg_dim": fg_dim,
        "mono": MONO_STACK,
        "sans": _SANS_STACK,
        "bg_variant": bg_variant,
        "brand": brand,
        "glow": glow,
        "glow2": glow2,
        "stroke": stroke,
        "logo": logo,
    }


def resolve(topic_id=None, subject: str = "", brand: str | None = None,
            channel_id=None, channel_slug: str | None = None,
            channel_name: str | None = None) -> dict:
    """Resolve the visual theme for a video.

    The palette is keyed by ``topic_id`` (matching the thumbnail, which also keys by
    topic_id) so the in-video accent equals the thumbnail accent; it falls back to a
    subject hash when topic_id is missing. The background variant always varies by
    subject so two videos under the same topic still look distinct.

    ``brand``: ``"os"`` Owera B&W, ``"rr"`` Rodrigo Recio burgundy, ``None`` legacy
    neon (kept so unbranded unit tests and leftover templates stay deterministic).
    When ``brand`` is omitted, ``channel_id`` / ``channel_slug`` / ``channel_name``
    infer it (ch1/Owera → os, ch2/Recio → rr). An explicit ``brand`` always wins.
    """
    if brand is None:
        brand = infer_brand(channel_id, channel_slug, channel_name)
    try:
        tid = int(topic_id) if topic_id else 0
    except (TypeError, ValueError):
        tid = 0
    h = _subject_hash(subject)
    if brand == "os":
        accent, bg_deep = _pick_pair(_OS_PALETTE, tid, h)
        return _tokens(
            accent=accent, bg_deep=bg_deep, bg_base="#000000",
            fg="#ffffff", fg_dim="#6b6b6b",
            bg_variant=_OS_VARIANTS[h % len(_OS_VARIANTS)],
            brand="os", glow="#1a1a1a", glow2="#0a0a0a",
            stroke=accent, logo=OS_LOGO_FILE,
        )
    if brand == "rr":
        accent, bg_deep = _pick_pair(_RR_PALETTE, tid, h)
        # Second glow is the deeper burgundy so the upper corner reads in ~0.3s.
        glow2 = "#2a0a14" if bg_deep.lower() != "#2a0a14" else "#4a1528"
        return _tokens(
            accent=_RR_ACCENT, bg_deep=bg_deep, bg_base="#000000",
            fg="#ffffff", fg_dim="#6b6b6b",
            bg_variant=_RR_VARIANTS[h % len(_RR_VARIANTS)],
            brand="rr", glow=bg_deep, glow2=glow2,
            stroke=_RR_ACCENT, logo="",
        )
    if tid:
        accent, bg_deep = PALETTE[tid % len(PALETTE)]
    else:
        accent, bg_deep = PALETTE[h % len(PALETTE)]
    return _tokens(
        accent=accent, bg_deep=bg_deep, bg_base="#0b0b16",
        fg="#ffffff", fg_dim="#c9d2ff",
        bg_variant=BG_VARIANTS[h % len(BG_VARIANTS)],
        brand=None, glow=bg_deep, glow2="#0b0b16",
        stroke=accent, logo="",
    )


def esc(s) -> str:
    """HTML-escape text for safe injection into a composition (mirrors worker._esc)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fold(s: str) -> str:
    """Lowercase + strip diacritics (NFKD). Used for language-agnostic cue<->word
    matching — Portuguese narration ('produção', 'inferência') needs the fold so a
    cue copied verbatim from the script still matches the spoken word tokens."""
    n = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in n if not unicodedata.combining(c)).lower()
