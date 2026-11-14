"""Transactional email for Stripe provisioning (Resend)."""

from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.services import spa_google_login

logger = logging.getLogger("manager.stripe_email")

RESEND_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "channels@mail.owera.com"
REPLY_TO = "contact@owera.com"
SIGN_IN_URL = "https://channels.owera.com"

_BLOCKED_RECIPIENTS = frozenset({spa_google_login.normalized_email("rodrigo@owera.com")})


def welcome_email_html() -> str:
    return f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#111;">
<p>Welcome to Owera Channels Manager.</p>
<p><a href="{SIGN_IN_URL}">Sign in at {SIGN_IN_URL}</a> with the Google account you used at checkout.</p>
<p><strong>Next step:</strong> connect your YouTube channel to start publishing.</p>
</body></html>"""


def _redact(addr: str) -> str:
    return addr.split("@", 1)[0] + "@…"


def send_email(to_email: str, subject: str, html: str, *, label: str = "email") -> bool:
    """Send one transactional email through Resend from channels@mail.owera.com.

    Returns True if Resend accepted it. Never sends to rodrigo@owera.com. With
    MANAGER_RESEND_API_KEY unset it logs a warning and sends nothing. Never raises."""
    key = spa_google_login.normalized_email(to_email)
    if not key or key in _BLOCKED_RECIPIENTS:
        return False

    api_key = (settings.resend_api_key or "").strip()
    if not api_key:
        logger.warning("%s not sent to %s: MANAGER_RESEND_API_KEY is unset", label, _redact(key))
        return False

    payload = {
        "from": FROM_ADDRESS,
        "reply_to": REPLY_TO,
        "to": [key],
        "subject": subject,
        "html": html,
    }
    try:
        httpx.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("%s queued for %s", label, _redact(key))
        return True
    except Exception as e:
        logger.warning("%s delivery failed for %s: %s", label, _redact(key), type(e).__name__)
        return False


def send_welcome_email(to_email: str) -> bool:
    """Send the provisioning welcome email once. Returns True if sent (or skipped blocklist)."""
    return send_email(to_email, "Welcome to Channels Manager", welcome_email_html(),
                      label="welcome email")
