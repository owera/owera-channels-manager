"""Per-topic policy flags (one shared structure for every per-topic switch).

settings.topic_flags (env MANAGER_TOPIC_FLAGS, JSON object) maps a flag name to
the Topic ids it applies to. A flag absent from the settings object falls back
to DEFAULTS, so an env override of one flag never drops another one's default.

Flags:
  * VM_PASS_REQUIRED ("vm_pass_required") — approve, skip-gate auto-approve,
    retry-republish and the publish loop need the Video Maker's Gate B PASS
    (creation_config["vm_pass"]) on the CURRENT render artifact
    (review_guard.vm_pass_required_reason). First entry: topic 47, OS "named
    tool" — #1449/#1450 were auto-approved and published without Gate B.
"""
from __future__ import annotations

VM_PASS_REQUIRED = "vm_pass_required"

DEFAULTS: dict[str, tuple[int, ...]] = {
    VM_PASS_REQUIRED: (47,),
}


def topics_with(flag: str) -> frozenset[int]:
    """Topic ids carrying ``flag`` (settings override, else DEFAULTS)."""
    from app.config import settings

    cfg = getattr(settings, "topic_flags", None) or {}
    raw = cfg.get(flag, DEFAULTS.get(flag, ())) if isinstance(cfg, dict) else DEFAULTS.get(flag, ())
    out = set()
    for x in raw or ():
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def has(flag: str, topic_id) -> bool:
    """True when ``topic_id`` carries ``flag``."""
    if topic_id is None:
        return False
    try:
        return int(topic_id) in topics_with(flag)
    except (TypeError, ValueError):
        return False
