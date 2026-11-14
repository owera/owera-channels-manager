"""S7: day-6 token-renewal check (observability only).

Google refresh tokens for an app in OAuth "Testing" mode die 7 days after the
grant. Once the app is in Production a connected channel's token refreshes many
times a day (the metrics/analytics/publish loops build a YouTube client), so a
channel whose last *successful* refresh is more than 6 days old means renewal is
broken and the day-8 E2E check ("still connected on day 8", activation A1)
would fail. This job makes that visible a day before it bites.

Clock: ``Channel.last_refresh_ok_at`` (naive UTC), stamped by ``record_success``
- in ``token_store.save_credentials`` — every platform-OAuth refresh that
  ``youtube._refresh_and_persist`` persists, and the initial web connect;
- in ``notify.mark_connected`` — every verified connect/reconnect (web and CLI).
A failed refresh (RefreshError) never reaches either, so it is never stamped.
Legacy token.json refreshes (internal ch1/ch2 only) are not stamped.

``tick`` (scheduler, every 6 h) reads the DB only: it never refreshes, never
calls Google and never touches ``oauth_status``. For each CONNECTED channel in
a workspace outside ``token_age_internal_workspace_ids`` whose reference time
(``last_refresh_ok_at``, else the connect time ``oauth_status_changed_at``,
else ``created_at``) is more than 6 days old it logs one WARNING
``token_refresh_stale channel_id=… workspace_id=… days=…`` and stamps
``token_refresh_stale_at``. The marker is cleared by the next successful
refresh/connect. Internal workspaces are info-only: an INFO line, no marker.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.config import settings
from app.models import Channel, OAuthStatus, Workspace, utcnow

logger = logging.getLogger("manager.token_refresh_age")

TICK_HOURS = 6
STALE_AFTER = timedelta(days=6)


def _clock() -> datetime:
    """Indirection so tests can pin the clock."""
    return utcnow()


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def internal_workspace_ids() -> frozenset[int]:
    out: set[int] = set()
    for part in str(getattr(settings, "token_age_internal_workspace_ids", "") or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            return frozenset()   # malformed → watch everything (fail toward visibility)
    return frozenset(out)


def record_success(session: Session, channel: Channel, *, now: datetime | None = None,
                   persist: bool = True) -> None:
    """Stamp a successful grant/refresh and clear the stale marker. Caller commits.

    ``persist=True`` writes by primary key with an UPDATE instead of
    ``session.add(channel)``: the refresh path may hold a Channel that belongs to
    another session (or none), and adding it here would raise and roll back the
    credential save it rides along with."""
    ts = _naive(now or _clock())
    channel.last_refresh_ok_at = ts
    channel.token_refresh_stale_at = None
    if persist and channel.id is not None:
        session.exec(update(Channel).where(Channel.id == channel.id).values(
            last_refresh_ok_at=ts, token_refresh_stale_at=None))


def reference_time(channel: Channel) -> datetime | None:
    ref = channel.last_refresh_ok_at or channel.oauth_status_changed_at or channel.created_at
    return _naive(ref) if ref else None


def age_days(channel: Channel, at: datetime | None = None) -> float | None:
    ref = reference_time(channel)
    if ref is None:
        return None
    return round((_naive(at or _clock()) - ref).total_seconds() / 86400, 2)


def check(session: Session, at: datetime | None = None) -> list[int]:
    """Flag stale external channels. Returns the ids newly flagged. Commits."""
    at = _naive(at or _clock())
    internal = internal_workspace_ids()
    closed = {w.id for w in session.exec(select(Workspace).where(Workspace.closed_at != None)).all()}  # noqa: E711
    flagged: list[int] = []
    rows = session.exec(select(Channel).where(Channel.oauth_status == OAuthStatus.CONNECTED)).all()
    for ch in rows:
        if ch.workspace_id in closed:
            continue
        ref = reference_time(ch)
        if ref is None or at - ref <= STALE_AFTER:
            continue
        days = (at - ref).total_seconds() / 86400
        if ch.workspace_id in internal:
            logger.info("token_refresh_stale_internal channel_id=%s workspace_id=%s days=%.1f",
                        ch.id, ch.workspace_id, days)
            continue
        if ch.token_refresh_stale_at is not None:
            continue     # already flagged this episode; one WARNING per episode
        ch.token_refresh_stale_at = at
        session.add(ch)
        flagged.append(ch.id)
        logger.warning("token_refresh_stale channel_id=%s workspace_id=%s days=%.1f",
                       ch.id, ch.workspace_id, days)
    if flagged:
        session.commit()
    return flagged


def tick(now: datetime | None = None) -> int:
    """Scheduler entry point. Returns how many channels were newly flagged."""
    from app.db import session_scope

    with session_scope() as session:
        return len(check(session, now))
