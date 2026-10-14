"""Newsletter link on OS (Owera) videos only — never RR.

Config (app.config.Settings, env MANAGER_NEWSLETTER_URL / MANAGER_NEWSLETTER_TEXT),
both default to the placeholder "TODO-CMO" until the CMO picks them.

Safe-by-default choice: while the URL OR the text is empty / the placeholder /
not an http(s) URL, NOTHING is published — no description line, no endcard
line. So shipping this code changes no output until real values are set.

Where it shows (OS brand only, theme.infer_brand == "os"):
  * YouTube description: one "📰 {text} {url}" line in the publish-time links
    block (metadata.finalize_description; idempotent on publish retries);
  * endcard: the TEXT only (no URL on screen — the endcard ban list rejects
    owera.com etc.) as a small extra line under the last cta card, added at
    HTML render time (storyboard.render_cta). The beat JSON / chip
    `Subscribe · {series}` are untouched, so Gate B card checks see the same
    board. A text that trips craft.endcard_scan_banned is dropped.
"""
from __future__ import annotations

PLACEHOLDER = "TODO-CMO"


def _clean(s) -> str:
    return (s or "").strip()


def is_placeholder(s) -> bool:
    t = _clean(s)
    return not t or PLACEHOLDER.lower() in t.lower()


def config() -> tuple[str, str] | None:
    """(url, text) when both are real values, else None (publish nothing)."""
    from app.config import settings
    url = _clean(getattr(settings, "newsletter_url", ""))
    text = _clean(getattr(settings, "newsletter_text", ""))
    if is_placeholder(url) or is_placeholder(text):
        return None
    if not url.lower().startswith(("https://", "http://")):
        return None
    return url, text


def description_line(brand: str | None) -> str | None:
    """Description line for an OS video, else None."""
    cfg = config() if brand == "os" else None
    if not cfg:
        return None
    url, text = cfg
    return f"📰 {text} {url}"


def endcard_text(brand: str | None) -> str | None:
    """Endcard line (text only, no URL) for an OS video, else None."""
    cfg = config() if brand == "os" else None
    if not cfg:
        return None
    from app.services import craft
    text = cfg[1][:60]
    if craft.endcard_scan_banned(text) or craft.contains_subscribe_cta(text):
        return None
    return text
