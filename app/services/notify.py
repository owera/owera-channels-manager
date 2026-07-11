"""One-shot operator alerts for terminal channel states.

The issues digest (app/services/issues.py) already *reports* a dead channel, but
only when something reads it — ch2 sat revoked for days because nothing pushed.
This module is the push side, and `mark_dead()` / `mark_dead_committed()` are
the single choke point for flipping a channel into a dead OAuth status: capture
the previous status, assign the new one, and emit exactly one alert (ERROR log
line, plus an optional webhook POST) carrying a ready reconnect recipe.

Exactly-once comes from two rules. First, the *previous* status is captured
BEFORE mutating channel.oauth_status: the alert fires only on a genuine
CONNECTED -> dead transition, so repeated checks against an already-dead token
stay silent, and a reconnect re-arms it. Second, the alert fires only after the
flip is durable (mark_dead_committed commits first): a failed commit re-arms
the guard without paging, and the webhook POST never sits inside an open write.

Every dead status alerts — EXPIRED (revoked token), DISCONNECTED (token file
gone), ERROR (failed consent) — because each one silently halts publishing.
The one deliberate exception is the operator /disconnect route, which assigns
DISCONNECTED directly (an intentional action needs no alarm).

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

# Statuses in which the publish loop skips the channel — i.e. "dead".
DEAD_STATUSES = (OAuthStatus.EXPIRED, OAuthStatus.DISCONNECTED, OAuthStatus.ERROR)

_DEAD_PHRASE = {
    OAuthStatus.EXPIRED: "revoked/expired",
    OAuthStatus.DISCONNECTED: "gone (token file missing)",
    OAuthStatus.ERROR: "in error (consent failed or was cancelled)",
}


def reconnect_recipe(channel_id: int) -> str:
    base = (settings.public_base_url or f"http://localhost:{settings.port}").rstrip("/")
    return (f"open {base}/ and hit Reconnect on the channel, or "
            f"POST {base}/api/channels/{channel_id}/oauth/start "
            f"and open the returned auth_url")


def dead_status_for(slug: str) -> str:
    """Classify how dead: EXPIRED = the token file exists but is unusable
    (revoked / unrefreshable); DISCONNECTED = the token file itself is gone."""
    from app.services import youtube
    return OAuthStatus.EXPIRED if youtube.has_token(slug) else OAuthStatus.DISCONNECTED


def mark_dead(channel: Channel, error: str | None, *,
              status: str | None = None, alert: bool = True) -> bool:
    """The single choke point for flipping a channel into a dead OAuth status.

    Captures the previous status BEFORE assigning (the exactly-once guard lives
    here, not at each call site), assigns status + error, and alerts on a
    genuine CONNECTED -> dead transition. `status` defaults to the
    dead_status_for classification. Returns True iff the flip was such a
    transition. Callers holding a session should prefer mark_dead_committed,
    which orders the alert after durability.
    """
    prev = channel.oauth_status
    new_status = status or dead_status_for(channel.slug)
    channel.oauth_status = new_status
    channel.oauth_error = str(error)[:300] if error else None
    flipped = prev == OAuthStatus.CONNECTED and new_status in DEAD_STATUSES
    if flipped and alert:
        alert_dead(channel, error)
    return flipped


def mark_dead_committed(session, channel: Channel, error: str | None, *,
                        status: str | None = None) -> bool:
    """mark_dead with durable-first alerting: flip, commit, and only then
    alert. A commit failure propagates to the caller (whose rollback re-arms
    the transition guard) with no alert sent — so a lost flip can never have
    paged, and the webhook POST never delays an open write."""
    flipped = mark_dead(channel, error, status=status, alert=False)
    session.add(channel)
    session.commit()
    if flipped:
        alert_dead(channel, error)
    return flipped


def alert_dead(channel: Channel, error: str | None) -> None:
    """Emit the one-shot dead-channel alert for the channel's current status.
    The transition guard already ran in mark_dead — call this directly only
    after a mark_dead(alert=False) that returned True. Never raises — callers
    sit on the publish path."""
    try:
        status = channel.oauth_status
        phrase = _DEAD_PHRASE.get(status, status)
        recipe = reconnect_recipe(channel.id)
        text = (f"OAuth token for channel '{channel.name}' (id={channel.id}) is "
                f"{phrase} — publishing is HALTED for this channel until it "
                f"is reconnected. Reconnect: {recipe}. Error: {error or 'unknown'}")
        logger.error(text)
        _post_webhook({
            "event": f"oauth_{status}",
            "channel_id": channel.id,
            "channel_name": channel.name,
            "error": error,
            "reconnect": recipe,
            "text": text,       # Slack incoming-webhook body
            "content": text,    # Discord incoming-webhook body
        })
    except Exception:
        logger.exception("dead-channel alert itself failed (alerting must not "
                         "break the caller)")


def _post_webhook(payload: dict) -> None:
    url = settings.alert_webhook_url
    if not url:
        return
    try:
        httpx.post(url, json=payload,
                   timeout=WEBHOOK_TIMEOUT_SECONDS).raise_for_status()
    except Exception as e:
        logger.warning("alert webhook delivery failed: %s", e)
