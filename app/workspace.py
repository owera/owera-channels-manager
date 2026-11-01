"""Workspace resolution for shared claw0 multi-tenant CM (WS4 PR-C / WS5)."""

from __future__ import annotations

import base64

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Channel, Workspace


def _slug_from_basic_auth(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
        username, _, _ = decoded.partition(":")
        return (username or "").strip() or None
    except Exception:
        return None


def resolve_workspace_slug(request: Request) -> str:
    if settings.multi_workspace:
        slug = _slug_from_basic_auth(request)
        if not slug and settings.allow_workspace_header:
            slug = (request.headers.get("X-Workspace-Slug") or "").strip() or None
        if not slug:
            raise HTTPException(
                401,
                "multi-workspace: Basic Auth username = workspace slug "
                "or X-Workspace-Slug (dev)",
            )
        return slug
    return "owera"


def get_workspace(session: Session, slug: str) -> Workspace:
    ws = session.exec(select(Workspace).where(Workspace.slug == slug)).first()
    if not ws:
        raise HTTPException(404, f"workspace '{slug}' not found")
    return ws


def current_workspace(
    request: Request,
    session: Session = Depends(get_session),
) -> Workspace:
    return get_workspace(session, resolve_workspace_slug(request))


def channels_for_workspace(ws: Workspace):
    return select(Channel).where(Channel.workspace_id == ws.id).order_by(Channel.id)


def require_channel(session: Session, ws: Workspace, channel_id: int) -> Channel:
    ch = session.get(Channel, channel_id)
    if not ch or ch.workspace_id != ws.id:
        raise HTTPException(404, "channel not found")
    return ch
