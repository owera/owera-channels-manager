"""Workspace resolution for shared claw0 multi-tenant CM (WS4 PR-C / WS5)."""

from __future__ import annotations

import base64

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Channel, Video, Workspace

# Basic Auth usernames that map to a different workspace slug (multi_workspace only).
_BASIC_AUTH_USERNAME_TO_WORKSPACE_SLUG: dict[str, str] = {"admin": "owera"}

# Legacy bare usernames → (workspace slug, review actor).
_LEGACY_BARE_IDENTITY: dict[str, tuple[str, str]] = {
    "channels": ("owera", "channels"),
    "vm": ("owera", "vm"),
    "video-maker": ("owera", "vm"),
    "videomaker": ("owera", "vm"),
}

_COMPOUND_CHANNELS = "channels"
_COMPOUND_VM_SUFFIXES = frozenset({"vm", "video-maker", "videomaker"})


def _raw_username_from_basic_auth(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
        username, _, _ = decoded.partition(":")
        return (username or "").strip() or None
    except Exception:
        return None


def parse_basic_auth_username(raw: str) -> tuple[str, str]:
    """Return (workspace_slug, actor) from the Basic Auth username.

    Multi-workspace product rules (A1):
      * ``{slug}`` → workspace slug, actor = slug (observe / connect / hold)
      * ``{slug}+channels`` → workspace slug, actor ``channels``
      * ``{slug}+vm`` (+video-maker / +videomaker) → workspace slug, actor ``vm``
      * Legacy bare ``channels`` / ``vm`` / … → workspace ``owera`` + role actor
      * ``admin`` → workspace ``owera``, actor ``admin``
    """
    username = (raw or "").strip()
    if not username:
        raise ValueError("empty username")

    lower = username.lower()
    if lower in _LEGACY_BARE_IDENTITY:
        return _LEGACY_BARE_IDENTITY[lower]

    if "+" in username:
        prefix, _, suffix = username.rpartition("+")
        ws_part = prefix.strip()
        suf = suffix.strip().lower()
        if not ws_part:
            raise ValueError("invalid compound username")
        ws_slug = _BASIC_AUTH_USERNAME_TO_WORKSPACE_SLUG.get(ws_part, ws_part)
        if suf == _COMPOUND_CHANNELS:
            return ws_slug, "channels"
        if suf in _COMPOUND_VM_SUFFIXES:
            return ws_slug, "vm"
        # Unknown suffix: treat whole string as a workspace slug (no role suffix).
        ws_slug = _BASIC_AUTH_USERNAME_TO_WORKSPACE_SLUG.get(username, username)
        return ws_slug, ws_slug

    ws_slug = _BASIC_AUTH_USERNAME_TO_WORKSPACE_SLUG.get(username, username)
    if lower == "admin":
        return "owera", "admin"
    return ws_slug, ws_slug


def basic_auth_identity(request: Request) -> tuple[str, str] | None:
    """Parsed (workspace_slug, actor) from Basic Auth, or None if absent."""
    raw = _raw_username_from_basic_auth(request)
    if not raw:
        return None
    try:
        return parse_basic_auth_username(raw)
    except ValueError:
        return None


def resolve_workspace_slug(request: Request) -> str:
    """When multi_workspace is on, the slug comes from Basic Auth username.

    Treating the username as tenant id is only safe if Cloudflare Access (or
    equivalent) has already authenticated the user at the tunnel — see
    docs/WS4_CLAW0_RUNBOOK.md. X-Workspace-Slug is dev-only (allow_workspace_header).
    """
    if settings.multi_workspace:
        ident = basic_auth_identity(request)
        if ident:
            return ident[0]
        slug = _raw_username_from_basic_auth(request)
        if slug:
            slug = _BASIC_AUTH_USERNAME_TO_WORKSPACE_SLUG.get(slug, slug)
        if not slug and settings.allow_workspace_header:
            slug = (request.headers.get("X-Workspace-Slug") or "").strip() or None
        if not slug:
            raise HTTPException(
                401,
                "multi-workspace: Basic Auth username = workspace slug "
                "(or slug+channels / slug+vm) or X-Workspace-Slug (dev only)",
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


def require_video(session: Session, ws: Workspace, video_id: int) -> Video:
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    ch = session.get(Channel, v.channel_id)
    if not ch or ch.workspace_id != ws.id:
        raise HTTPException(404, "video not found")
    return v
