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


def send_welcome_email(to_email: str) -> bool:
    """Send the provisioning welcome email once. Returns True if sent (or skipped blocklist)."""
    key = spa_google_login.normalized_email(to_email)
    if not key or key in _BLOCKED_RECIPIENTS:
        return False

    api_key = (settings.resend_api_key or "").strip()
    if not api_key:
        logger.warning(
            "welcome email not sent to %s: MANAGER_RESEND_API_KEY is unset",
            key.split("@", 1)[0] + "@…",
        )
        return False

    payload = {
        "from": FROM_ADDRESS,
        "reply_to": REPLY_TO,
        "to": [key],
        "subject": "Welcome to Channels Manager",
        "html": welcome_email_html(),
    }
    try:
        httpx.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        ).raise_for_status()
        logger.info("welcome email queued for %s", key.split("@", 1)[0] + "@…")
        return True
    except Exception as e:
        logger.warning("welcome email delivery failed for %s: %s", key.split("@", 1)[0] + "@…", e)
        return False
