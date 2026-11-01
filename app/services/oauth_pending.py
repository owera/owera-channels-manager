"""Durable OAuth pending flows (state + PKCE). Replaces in-memory _pending_flows (WS4 PR-B)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models import OAuthPending, utcnow

PENDING_TTL_MINUTES = 10


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def new_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def create_pending(
    session: Session,
    *,
    state: str,
    channel_id: int,
    workspace_id: int,
    code_verifier: str,
    redirect_uri: str,
) -> OAuthPending:
    _purge_expired(session)
    expires = utcnow() + timedelta(minutes=PENDING_TTL_MINUTES)
    row = OAuthPending(
        state_hash=_state_hash(state),
        channel_id=channel_id,
        workspace_id=workspace_id,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        expires_at=expires,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _is_expired(row: OAuthPending) -> bool:
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < utcnow()


def consume_pending(
    session: Session,
    *,
    state: str | None,
    channel_id: int,
    workspace_id: int,
) -> OAuthPending | None:
    if not state:
        return None
    sh = _state_hash(state)
    row = session.exec(
        select(OAuthPending).where(
            OAuthPending.state_hash == sh,
            OAuthPending.consumed_at == None,  # noqa: E711
        )
    ).first()
    if row is None:
        return None
    if row.channel_id != channel_id or row.workspace_id != workspace_id:
        return None
    if _is_expired(row):
        row.consumed_at = utcnow()
        session.add(row)
        session.commit()
        return None
    row.consumed_at = utcnow()
    session.add(row)
    session.commit()
    return row


def has_unconsumed_state(session: Session, state: str) -> bool:
    sh = _state_hash(state)
    row = session.exec(
        select(OAuthPending).where(
            OAuthPending.state_hash == sh,
            OAuthPending.consumed_at == None,  # noqa: E711
        )
    ).first()
    return row is not None and not _is_expired(row)


def supersede_channel(session: Session, channel_id: int) -> None:
    rows = session.exec(
        select(OAuthPending).where(
            OAuthPending.channel_id == channel_id,
            OAuthPending.consumed_at == None,  # noqa: E711
        )
    ).all()
    now = utcnow()
    for row in rows:
        row.consumed_at = now
        session.add(row)
    session.commit()


def _purge_expired(session: Session) -> None:
    cutoff = utcnow() - timedelta(minutes=PENDING_TTL_MINUTES * 3)
    old = session.exec(
        select(OAuthPending).where(OAuthPending.created_at < cutoff)
    ).all()
    for row in old:
        session.delete(row)
    if old:
        session.commit()
