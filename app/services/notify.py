"""One-shot operator alerts for terminal channel states.

The issues digest (app/services/issues.py) already *reports* a dead channel, but
only when something reads it — ch2 sat revoked for days because nothing pushed.
This module is the push side: call `oauth_expired()` at the moment a channel
flips CONNECTED -> EXPIRED and it emits exactly one alert (ERROR log line, plus
an optional webhook POST) carrying a ready reconnect recipe.

Exactly-once comes from the caller passing the channel's *previous* status —
captured BEFORE mutating channel.oauth_status: the alert fires only on a genuine
CONNECTED -> EXPIRED transition, so repeated checks against an already-dead
token stay silent, and a reconnect re-arms it.

The webhook is inert until the operator sets MANAGER_ALERT_WEBHOOK_URL in .env.
The payload carries both "text" (Slack) and "content" (Discord) keys so either
incoming-webhook flavor renders it as-is. Delivery is best-effort at-most-once:
a failure is logged and swallowed (the ERROR log line and the issues digest
remain as fallbacks) — alerting must never break the publish path it watches.
"""
import logging

import httpx

from app.config import settings
from app.models import Channel, OAuthStatus

logger = logging.getLogger("manager.notify")

WEBHOOK_TIMEOUT_SECONDS = 5


def reconnect_recipe(channel_id: int) -> str:
    base = (settings.public_base_url or f"http://localhost:{settings.port}").rstrip("/")
    return (f"open {base}/ and hit Reconnect on the channel, or "
            f"POST {base}/api/channels/{channel_id}/oauth/start "
            f"and open the returned auth_url")


def oauth_expired(channel: Channel, error: str | None,
                  prev_status: str | None) -> bool:
    """Alert on a CONNECTED -> EXPIRED flip. Returns True iff the flip was a
    genuine transition (the alert is then attempted, best-effort). Never
    raises — callers sit on the publish path."""
    if prev_status != OAuthStatus.CONNECTED:
        return False
    try:
        recipe = reconnect_recipe(channel.id)
        text = (f"OAuth token for channel '{channel.name}' (id={channel.id}) is "
                f"revoked/expired — publishing is HALTED for this channel until it "
                f"is reconnected. Reconnect: {recipe}. Error: {error or 'unknown'}")
        logger.error(text)
        _post_webhook({
            "event": "oauth_expired",
            "channel_id": channel.id,
            "channel_name": channel.name,
            "error": error,
            "reconnect": recipe,
            "text": text,       # Slack incoming-webhook body
            "content": text,    # Discord incoming-webhook body
        })
    except Exception:
        logger.exception("oauth-expired alert itself failed (alerting must not "
                         "break the caller)")
    return True


def _post_webhook(payload: dict) -> None:
    url = settings.alert_webhook_url
    if not url:
        return
    try:
        httpx.post(url, json=payload,
                   timeout=WEBHOOK_TIMEOUT_SECONDS).raise_for_status()
    except Exception as e:
        logger.warning("alert webhook delivery failed: %s", e)
