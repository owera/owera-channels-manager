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
import html as _html
import logging
from datetime import timezone
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.models import Channel, OAuthStatus, utcnow

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
    return (f"run `PYTHONPATH=. uv run python -m app.reconnect {channel_id}` in the "
            f"manager directory (one-command loopback consent; see README), or "
            f"open {base}/ and hit Reconnect on the channel, or "
            f"POST {base}/api/channels/{channel_id}/oauth/start "
            f"and open the returned auth_url")


def dead_status_for(slug: str, channel: Channel | None = None) -> str:
    """Classify how dead: EXPIRED = the token file exists but is unusable
    (revoked / unrefreshable); DISCONNECTED = the token file itself is gone."""
    from app.services import youtube
    ch = channel
    if ch is None:
        return OAuthStatus.EXPIRED if youtube.has_token(slug) else OAuthStatus.DISCONNECTED
    return (
        OAuthStatus.EXPIRED
        if youtube.has_token(slug, channel=ch)
        else OAuthStatus.DISCONNECTED
    )


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
    new_status = status or dead_status_for(channel.slug, channel)
    channel.oauth_status = new_status
    channel.oauth_error = str(error)[:300] if error else None
    if prev != new_status:
        channel.oauth_status_changed_at = utcnow()
    _stamp_access_lost(channel, new_status, error)
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


def mark_connected(session, channel: Channel, identity: dict) -> None:
    """The CONNECTED counterpart of mark_dead_committed: bind the verified
    YouTube identity, clear the error, flip the status, and commit. Shared by
    the web consent callback and the reconnect CLI so the two paths cannot
    drift. Call only AFTER youtube.verify_grant accepted the grant and the
    token is on disk; a commit failure propagates to the caller (the token
    remains saved — only the status flip is missing)."""
    channel.yt_channel_id = identity.get("id")
    channel.yt_channel_title = identity.get("title")
    # The channel's display name follows the real YouTube title.
    if identity.get("title"):
        channel.name = identity["title"]
    if channel.oauth_status != OAuthStatus.CONNECTED:
        channel.oauth_status_changed_at = utcnow()
    channel.oauth_status = OAuthStatus.CONNECTED
    channel.oauth_error = None
    # Access is back: cancel any pending YouTube-data purge (P3).
    channel.access_lost_at = None
    channel.access_lost_reason = None
    channel.youtube_data_purged_at = None
    channel.updated_at = utcnow()
    session.add(channel)
    session.commit()


INVALID_GRANT = "invalid_grant"
REASON_DISCONNECTED = "disconnected"
REASON_DELETION_REQUESTED = "deletion_requested"
REASON_WORKSPACE_CLOSED = "workspace_closed"
# User-initiated losses: LegalOps cláusula 7 v2 / YT III.D.2.3.a and III.E.4.g (7 days).
USER_PURGE_REASONS = frozenset({
    REASON_DISCONNECTED, REASON_DELETION_REQUESTED, REASON_WORKSPACE_CLOSED,
})


def _stamp_access_lost(channel: Channel, new_status: str, error: str | None) -> None:
    """P3: stamp when Google access was definitively lost. A dead flip whose
    error carries invalid_grant (refresh token revoked/expired at Google) starts
    the 30-day purge clock (YT III.D.2.3.b). The first stamp wins; a reconnect
    clears it."""
    if new_status not in DEAD_STATUSES or channel.access_lost_at is not None:
        return
    if error and INVALID_GRANT in str(error):
        channel.access_lost_at = utcnow()
        channel.access_lost_reason = INVALID_GRANT


def _apply_user_purge_clock(channel: Channel, reason: str, now) -> None:
    """Start or shorten the purge clock for a user-initiated 7-day reason.

    The first ``access_lost_at`` wins. If the channel was already on a 30-day
    clock (Google revoke / invalid_grant), the reason switches to the 7-day
    one so the original stamp + 7 days becomes the deadline.
    """
    if channel.access_lost_at is None:
        channel.access_lost_at = now
        channel.access_lost_reason = reason
    elif channel.access_lost_reason not in USER_PURGE_REASONS:
        channel.access_lost_reason = reason


def _end_user_access(channel: Channel, reason: str, now) -> bool:
    """Flip to DISCONNECTED, clear the YouTube identity, start the 7-day clock.

    Does not commit. Returns True iff the channel was CONNECTED.
    """
    prev = channel.oauth_status
    channel.oauth_status = OAuthStatus.DISCONNECTED
    if prev != OAuthStatus.DISCONNECTED:
        channel.oauth_status_changed_at = now
    _apply_user_purge_clock(channel, reason, now)
    channel.yt_channel_id = None
    channel.yt_channel_title = None
    return prev == OAuthStatus.CONNECTED


def mark_operator_disconnect(session, channel: Channel) -> bool:
    """The /disconnect route's status flip: DISCONNECTED, identity cleared,
    access lost now (reason "disconnected", 7-day purge clock starts), committed.
    No reconnect page (intentional), but a real CONNECTED channel still gets
    the one access-lost email (P7). Returns True iff it was CONNECTED."""
    now = utcnow()
    flipped = _end_user_access(channel, REASON_DISCONNECTED, now)
    session.add(channel)
    session.commit()
    if flipped:
        email_access_lost(channel, cause="disconnected in Channels Manager")
    return flipped


def mark_deletion_requested(session, channel: Channel) -> bool:
    """User asked us to delete this channel's YouTube data (YT III.E.4.g).

    Starts the 7-day purge clock (reason "deletion_requested"), flips to
    DISCONNECTED and clears identity. The caller should already have dropped
    the local token (and revoked at Google when #213 is stacked). Returns
    True iff the channel was CONNECTED.
    """
    now = utcnow()
    flipped = _end_user_access(channel, REASON_DELETION_REQUESTED, now)
    session.add(channel)
    session.commit()
    if flipped:
        email_access_lost(channel, cause="deletion requested")
    return flipped


def mark_workspace_closed(session, workspace) -> int:
    """Close a workspace (YT III.E.4.g): stamp every channel with the 7-day
    reason "workspace_closed", drop local tokens, set workspace.closed_at.

    Commits once. Returns how many channels were stamped. Real CONNECTED
    channels still get the one access-lost email each.
    """
    from sqlmodel import select

    from app.services import youtube

    now = utcnow()
    channels = session.exec(
        select(Channel).where(Channel.workspace_id == workspace.id)
    ).all()
    to_email: list[Channel] = []
    for ch in channels:
        try:
            youtube.disconnect_channel(session, ch)
        except OSError:
            pass
        flipped = _end_user_access(ch, REASON_WORKSPACE_CLOSED, now)
        session.add(ch)
        if flipped:
            to_email.append(ch)
    if getattr(workspace, "closed_at", None) is None:
        workspace.closed_at = now
        workspace.updated_at = now
        session.add(workspace)
    session.commit()
    for ch in to_email:
        email_access_lost(ch, cause="workspace closed")
    return len(channels)


# ---- P7: access-lost email to contact@ -------------------------------------

ACCESS_LOST_TO = "contact@owera.com"
EMAIL_STATUSES = (OAuthStatus.EXPIRED, OAuthStatus.DISCONNECTED)
_BELEM = ZoneInfo("America/Belem")


def is_real_channel(channel: Channel) -> bool:
    """A channel whose access loss should email contact@: not paused (an
    intentional stop) and not marked is_test (smoke/dogfood/consent tests).
    is_test is the only test criterion — set it explicitly per channel."""
    return not channel.paused and not bool(getattr(channel, "is_test", False))


def _fmt_times(dt) -> str:
    if dt is None:
        dt = utcnow()
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return (f"{aware.astimezone(_BELEM):%Y-%m-%d %H:%M} America/Belem "
            f"({aware.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC)")


def _error_code(error: str | None) -> str:
    if not error:
        return "unknown"
    if INVALID_GRANT in error:
        return INVALID_GRANT
    import re
    m = re.search(r"refresh failed: ([a-z_]+)", error)
    return m.group(1) if m else "see server log"


def email_access_lost(channel: Channel, *, cause: str, error: str | None = None) -> bool:
    """Email contact@owera.com once per channel + status + transition time.

    Dedupe is persisted in ChannelAlert (primary key), so a retry, a second
    worker or a restart never re-sends for the same transition. Carries the
    channel, workspace, status, cause and time — never a token. Without
    MANAGER_RESEND_API_KEY it only logs a warning. Never raises. Returns True
    iff an email was accepted by Resend."""
    try:
        status = channel.oauth_status
        if status not in EMAIL_STATUSES or not is_real_channel(channel) or channel.id is None:
            return False
        if not (settings.resend_api_key or "").strip():
            logger.warning("access-lost email for channel id=%s not sent: "
                           "MANAGER_RESEND_API_KEY is unset", channel.id)
            return False
        from sqlalchemy.exc import IntegrityError
        from sqlmodel import Session

        from app import db as db_mod
        from app.models import ChannelAlert, Workspace
        from app.services import stripe_email

        at = channel.oauth_status_changed_at or utcnow()
        key = f"{channel.id}:{status}:{at.isoformat()}"
        with Session(db_mod.engine) as s:
            if s.get(ChannelAlert, key) is not None:
                return False
            s.add(ChannelAlert(key=key, channel_id=channel.id, status=status, transition_at=at))
            try:
                s.commit()
            except IntegrityError:
                return False
            ws = s.get(Workspace, channel.workspace_id)
            ws_label = f"{ws.name} ({ws.slug}, id={ws.id})" if ws else f"id={channel.workspace_id}"

            name = channel.name or channel.slug
            when = _fmt_times(at)
            code = _error_code(error or channel.oauth_error)
            subject = f"[Channels Manager] {name}: YouTube access {status}"
            rows = [
                ("Channel", f"{name} (slug {channel.slug}, id={channel.id})"),
                ("Workspace", ws_label),
                ("Status", status),
                ("Cause", cause),
                ("Error code", code),
                ("When", when),
            ]
            body = "".join(
                f"<tr><td><b>{_html.escape(k)}</b></td><td>{_html.escape(str(v))}</td></tr>"
                for k, v in rows)
            html_doc = (
                "<!DOCTYPE html><html><body style=\"font-family:system-ui,sans-serif\">"
                "<p>A Channels Manager channel lost its YouTube access. Publishing "
                "is halted for it until it is reconnected.</p>"
                f"<table cellpadding=\"4\">{body}</table>"
                f"<p>Reconnect: {_html.escape(reconnect_recipe(channel.id))}</p>"
                "<p>If it is not reconnected, its YouTube data is deleted 30 days "
                "after access was lost.</p></body></html>")
            sent = stripe_email.send_email(ACCESS_LOST_TO, subject, html_doc,
                                           label="access-lost email")
            if sent:
                row = s.get(ChannelAlert, key)
                row.delivered = True
                s.add(row)
                s.commit()
            return sent
    except Exception:
        logger.exception("access-lost email failed (alerting must not break the caller)")
        return False


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
        email_access_lost(channel, cause=f"token {phrase}", error=error)
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
