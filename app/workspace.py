"""Workspace resolution for shared claw0 multi-tenant CM (WS4 PR-C / WS5)."""

from __future__ import annotations

import base64

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.db import ADMIN_WORKSPACE_ID
from app.models import Channel, RenderProfile, Topic, TrendSignal, Video, Workspace
from app.services import spa_google_login

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


def request_identity(request: Request) -> tuple[str, str] | None:
    """Basic Auth identity, else SPA Google session identity."""
    ident = basic_auth_identity(request)
    if ident:
        return ident
    return spa_google_login.spa_session_identity(request)


def resolve_workspace_slug(request: Request) -> str:
    """When multi_workspace is on, the slug comes from Basic Auth username.

    Treating the username as tenant id is only safe if Cloudflare Access (or
    equivalent) has already authenticated the user at the tunnel — see
    docs/WS4_CLAW0_RUNBOOK.md. X-Workspace-Slug is dev-only (allow_workspace_header).
    """
    if settings.multi_workspace:
        ident = request_identity(request)
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


# ---- S4: workspace isolation helpers ---------------------------------------
# Rule (CTO spec): a resource owned by another workspace answers **404**, never
# 403, with the same body as a missing row, so its existence does not leak.


def is_admin_workspace(ws: Workspace) -> bool:
    """ws 1 (owera) owns the truly global settings and shared (channel-less) rows."""
    return ws.id == ADMIN_WORKSPACE_ID


def job_run_scope(ws: Workspace):
    """S4: SQL condition for the JobRuns a workspace may read.

    A run belongs to the workspace that owns its channel; a channel-less run that
    names a video belongs to that video's channel's workspace. Runs with neither
    (global maintenance, e.g. music replenish) are admin-only (ws 1), like the
    global settings row."""
    from sqlalchemy import and_, or_

    from app.models import JobRun
    ch_ids = select(Channel.id).where(Channel.workspace_id == ws.id)
    vid_ids = select(Video.id).where(Video.channel_id.in_(ch_ids))
    cond = or_(JobRun.channel_id.in_(ch_ids),
               and_(JobRun.channel_id.is_(None), JobRun.video_id.in_(vid_ids)))
    if is_admin_workspace(ws):
        cond = or_(cond, and_(JobRun.channel_id.is_(None), JobRun.video_id.is_(None)))
    return cond


def require_admin_workspace(ws: Workspace = Depends(current_workspace)) -> Workspace:
    """S4: global (non-workspace) resources such as the shared BGM pool are
    admin-only (ws 1). Any other workspace gets the same 404 as a missing route."""
    if not is_admin_workspace(ws):
        raise HTTPException(404, "Not Found")
    return ws


def workspace_channel_ids(session: Session, ws: Workspace) -> list[int]:
    return [int(i) for i in session.exec(
        select(Channel.id).where(Channel.workspace_id == ws.id)).all()]


def require_topic(session: Session, ws: Workspace, topic_id: int) -> Topic:
    t = session.get(Topic, topic_id)
    if not t:
        raise HTTPException(404, "topic not found")
    ch = session.get(Channel, t.channel_id)
    if not ch or ch.workspace_id != ws.id:
        raise HTTPException(404, "topic not found")
    return t


def trend_visible(session: Session, ws: Workspace, t: TrendSignal) -> bool:
    """Channel-bound trends belong to the channel's workspace; channel-less
    (growth-agent research) trends belong to the admin workspace."""
    if t.channel_id is None:
        return is_admin_workspace(ws)
    ch = session.get(Channel, t.channel_id)
    return bool(ch and ch.workspace_id == ws.id)


def require_trend(session: Session, ws: Workspace, trend_id: int) -> TrendSignal:
    t = session.get(TrendSignal, trend_id)
    if not t or not trend_visible(session, ws, t):
        raise HTTPException(404, "trend not found")
    return t


def profile_visible(session: Session, ws: Workspace, p: RenderProfile) -> bool:
    """Shared (channel-less) profiles are readable by every workspace."""
    if p.channel_id is None:
        return True
    ch = session.get(Channel, p.channel_id)
    return bool(ch and ch.workspace_id == ws.id)


def require_profile(session: Session, ws: Workspace, profile_id: int,
                    *, write: bool = False) -> RenderProfile:
    """Read: own or shared. Write: own, or shared only for the admin workspace
    (a buyer must not restyle ws 1's Default look)."""
    p = session.get(RenderProfile, profile_id)
    if not p or not profile_visible(session, ws, p):
        raise HTTPException(404, "profile not found")
    if write and p.channel_id is None and not is_admin_workspace(ws):
        raise HTTPException(404, "profile not found")
    return p
