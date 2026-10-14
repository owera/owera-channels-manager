"""Newsletter link on OS (Owera) videos only — never RR.

Config (app.config.Settings, env MANAGER_NEWSLETTER_URL / MANAGER_NEWSLETTER_TEXT),
both default to the placeholder "TODO-CMO" until the CMO picks them.

Safe-by-default choice: while the URL OR the text is empty / the placeholder,
or the URL is not a valid https URL (urllib.parse: scheme https, non-empty
host, no whitespace), NOTHING is published — no description line, no endcard
line. The text has < and > stripped (YouTube rejects them) and is cut to 60
chars at a word boundary. In the description the newsletter line is added
only when it fits whole in YouTube's 5000-char limit (metadata). So shipping this code changes no output until real values are set.

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
    url = getattr(settings, "newsletter_url", "") or ""   # not stripped: any whitespace rejects
    text = _clean(getattr(settings, "newsletter_text", ""))
    text = clean_text(text)
    if is_placeholder(url) or is_placeholder(text) or not valid_url(url):
        return None
    return url, text


TEXT_MAX = 60


def valid_url(url: str) -> bool:
    """https only, non-empty host, no whitespace anywhere."""
    from urllib.parse import urlparse
    if not url or any(c.isspace() for c in url) or "<" in url or ">" in url:
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.hostname)


def clean_text(text: str) -> str:
    """Strip < and > (YouTube rejects them), collapse whitespace, cut to
    TEXT_MAX at a word boundary."""
    t = " ".join((text or "").replace("<", " ").replace(">", " ").split())
    if len(t) <= TEXT_MAX:
        return t
    cut = t[:TEXT_MAX + 1].rsplit(" ", 1)[0] if " " in t[:TEXT_MAX + 1] else t[:TEXT_MAX]
    return cut[:TEXT_MAX].rstrip(" ,;:-·")


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
    text = cfg[1]
    if craft.endcard_scan_banned(text) or craft.contains_subscribe_cta(text):
        return None
    return text
