"""Per-topic policy flags (one shared structure for every per-topic switch).

settings.topic_flags (env MANAGER_TOPIC_FLAGS, JSON object) maps a flag name to
the Topic ids it applies to; "*" in the list means every topic. A flag absent
from the settings object falls back to DEFAULTS, so an env override of one flag
never drops another one's default.

Flags:
  * (removed 14/10) vm_pass_required / vm_pass_exempt: the VM Gate B PASS is
    now mandatory for EVERY video with no per-topic switch
    (review_guard.vm_pass_required_reason). Those keys in MANAGER_TOPIC_FLAGS
    are ignored.
  * LITERAL_CARDS ("literal_cards", default [47] — OS "named tool") — the
    storyboard never invents on-screen evidence (VM FAIL P0 #1449: invented
    PowerShell `MissingTerminator`, a misquote attributed to the Cursor forum,
    "3.20.21" on three cards in a row): rule 2b (required snippet) off, the
    no-invented-output rule on; log / error / command / quote / stat cards
    ONLY from the video's explicit allowlist (overrides "on_screen_allow",
    craft.on_screen_allow) — anything else becomes a plain text card;
    provided "on_screen" cards used verbatim; attribution / stat values only
    when the script says them (else Gate B FAIL); no version number on two
    consecutive cards; RR hook pace first cut (≤2.5s). See craft.literal_*
    and storyboard._apply_literal_cards.
  * TIGHT_MID_CARDS ("tight_mid_cards", default [47]) — mid cards hold ≤ 2.48s
    (≈2.60s as the VM measures it, with the 0.12s gap) instead of 2.80
    (≈2.92): generator and continuation threshold only; Gate B keeps 2.80
    (craft.mid_hold_cap).
"""
from __future__ import annotations

ALL = "*"
LITERAL_CARDS = "literal_cards"
TIGHT_MID_CARDS = "tight_mid_cards"

DEFAULTS: dict[str, tuple] = {
    LITERAL_CARDS: (47,),
    TIGHT_MID_CARDS: (47,),
}


def _raw(flag: str) -> tuple:
    from app.config import settings

    cfg = getattr(settings, "topic_flags", None) or {}
    raw = cfg.get(flag, DEFAULTS.get(flag, ())) if isinstance(cfg, dict) else DEFAULTS.get(flag, ())
    return tuple(raw or ())


def applies_to_all(flag: str) -> bool:
    """True when ``flag`` lists "*" (every topic)."""
    return any(str(x).strip() == ALL for x in _raw(flag))


def topics_with(flag: str) -> frozenset[int]:
    """Explicit Topic ids carrying ``flag`` (settings override, else DEFAULTS);
    "*" is reported by applies_to_all, not here."""
    out = set()
    for x in _raw(flag):
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def has(flag: str, topic_id) -> bool:
    """True when ``topic_id`` carries ``flag`` (listed, or the flag is "*")."""
    if applies_to_all(flag):
        return True
    if topic_id is None:
        return False
    try:
        return int(topic_id) in topics_with(flag)
    except (TypeError, ValueError):
        return False
