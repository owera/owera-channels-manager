"""S1 activation instrumentation: A0–A6 per workspace (read side + stamps).

A0–A6 follow the PO's quality bar (QUALITY-BAR-ANTI-REFUND §3):

  A0 Provisioned        workspace row exists; ``paid_at`` vs ``created_at``
  A1 YouTube connected  >= 1 channel with oauth_status=connected
  A2 Niche + topics     a channel niche set and >= 3 active topics
  A3 Drafts             >= 5 videos rendered (reached review); first = first_draft_at
  A4 Buyer decision     >= 1 video approved by a person (approved_by set, not auto:)
  A5 No blocking fail   0 videos FAILED for more than 24 h and no active S8 LLM
                        guard refusal ("llm_blocked"), + render-failed signal
  A6 Published          >= 1 video published

Read-only except ``stamp_first_draft`` (called by the render loop on success).
All times are reported as ISO-8601 UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, func, select

from app.models import (Channel, JobRun, OAuthCredential, OAuthStatus, Topic, Video,
                        VideoStatus, Workspace, utcnow)

logger = logging.getLogger("manager.activation")

A2_MIN_TOPICS = 3
A3_MIN_DRAFTS = 5
A5_FAILED_GRACE = timedelta(hours=24)
AUTO_ACTOR_PREFIX = "auto:"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _ch_ids(session: Session, ws: Workspace) -> list[int]:
    return [c for c in session.exec(
        select(Channel.id).where(Channel.workspace_id == ws.id)).all() if c is not None]


def stamp_first_draft(session: Session, channel: Channel | None) -> bool:
    """Set ``Workspace.first_draft_at`` once, on the first successful render in
    the channel's workspace. Never raises (instrumentation must not fail a
    render). Returns True when it stamped. Caller commits."""
    try:
        if channel is None or channel.workspace_id is None:
            return False
        ws = session.get(Workspace, channel.workspace_id)
        if ws is None or ws.first_draft_at is not None:
            return False
        ws.first_draft_at = utcnow()
        session.add(ws)
        return True
    except Exception:  # pragma: no cover - defensive
        logger.exception("first_draft_at stamp failed (channel=%s)",
                         getattr(channel, "id", None))
        return False


def _render_successes(session: Session, ch_ids: list[int]) -> list[tuple[int, datetime]]:
    """(video_id, first success at) per rendered video, oldest first."""
    if not ch_ids:
        return []
    rows = session.exec(
        select(JobRun.video_id, func.min(JobRun.created_at))
        .where(JobRun.kind == "render", JobRun.status == "success",
               JobRun.channel_id.in_(ch_ids), JobRun.video_id.is_not(None))
        .group_by(JobRun.video_id)
    ).all()
    return sorted(((v, _aware(t)) for v, t in rows), key=lambda r: r[1])


def first_draft_at(session: Session, ws: Workspace) -> tuple[datetime | None, str | None]:
    """(timestamp, source). Stamped column wins; else the oldest render-success
    JobRun of the workspace (workspaces that rendered before S1 shipped)."""
    if ws.first_draft_at is not None:
        return _aware(ws.first_draft_at), "stamped"
    succ = _render_successes(session, _ch_ids(session, ws))
    if succ:
        return succ[0][1], "jobrun"
    return None, None


def render_failed_signal(session: Session, ws: Workspace,
                         now: datetime | None = None) -> dict[str, Any]:
    """Per-workspace render-failure signal (A5; S3 will alert on it)."""
    now = _aware(now) or utcnow()
    ch_ids = _ch_ids(session, ws)
    out = {"failed_now": 0, "failed_over_24h": 0, "render_errors_24h": 0,
           "last_render_failed_at": None}
    if not ch_ids:
        return out
    failed_rows = session.exec(
        select(Video.id, Video.updated_at).where(Video.channel_id.in_(ch_ids),
                                                 Video.status == VideoStatus.FAILED)).all()
    # How long a row has been FAILED: the render/publish loops don't touch
    # Video.updated_at on failure (it can be the idea's create time), so use the
    # video's latest error JobRun; fall back to updated_at only without one.
    last_err: dict[int, datetime] = {}
    if failed_rows:
        last_err = {vid: t for vid, t in session.exec(
            select(JobRun.video_id, func.max(JobRun.created_at))
            .where(JobRun.video_id.in_([r[0] for r in failed_rows]),
                   JobRun.status == "error")
            .group_by(JobRun.video_id)).all() if t is not None}
    failed = [last_err.get(vid) or upd for vid, upd in failed_rows]
    out["failed_now"] = len(failed)
    out["failed_over_24h"] = sum(
        1 for t in failed if t is not None and now - _aware(t) > A5_FAILED_GRACE)
    since = now - timedelta(hours=24)
    errs = session.exec(
        select(JobRun.created_at).where(JobRun.kind == "render", JobRun.status == "error",
                                        JobRun.channel_id.in_(ch_ids))).all()
    errs = [_aware(t) for t in errs if t is not None]
    out["render_errors_24h"] = sum(1 for t in errs if t >= since)
    out["last_render_failed_at"] = iso_utc(max(errs)) if errs else None
    return out


def llm_blocked(ws) -> dict[str, Any] | None:
    """A5 "llm_blocked": the S8 guard's last refusal for this workspace, or None.

    Uses ``llm_guard.blocked_status`` once S8 is merged (None with the release
    flag OFF); until then it reads ``llm_blocked_reason/at`` if the columns
    exist, so this branch works before and after the S8 merge."""
    try:
        from app.services import llm_guard
        fn = getattr(llm_guard, "blocked_status", None)
        if fn is not None:
            out = fn(ws)
            return ({"reason": out["reason"], "step": out.get("step"), "at": out.get("at")}
                    if out else None)
    except Exception:  # noqa: BLE001 - the report must never crash on this
        return None
    reason = getattr(ws, "llm_blocked_reason", None)
    if not reason:
        return None
    return {"reason": str(reason), "step": None, "at": iso_utc(getattr(ws, "llm_blocked_at", None))}


def _step(done: bool, at: datetime | None = None, **extra) -> dict[str, Any]:
    d = {"done": bool(done), "at": iso_utc(at) if done else None}
    d.update(extra)
    return d


def monthly_cap_signal(ws: Workspace) -> dict[str, Any]:
    """S2 subcap for A5: ``reached`` once this external workspace's month-to-date
    ledger cost hits ``MANAGER_LLM_WORKSPACE_MONTHLY_USD_CAP`` (or the guard last
    refused with ``workspace_monthly_cap``). Internal allowlisted ws: exempt."""
    try:
        from app.services import llm_guard
    except Exception:  # noqa: BLE001
        return {"exempt": None, "reached": False, "spent_usd": None, "cap_usd": None}
    exempt = ws.id in llm_guard.internal_workspace_ids()
    spent = None if exempt else llm_guard.workspace_month_usd(ws.id)
    cap = llm_guard.workspace_monthly_cap_usd()
    persisted = (getattr(ws, "llm_blocked_reason", None) or "").startswith("workspace_monthly_cap")
    reached = (not exempt) and (persisted or (spent is not None and spent >= cap and cap > 0))
    return {"exempt": exempt, "reached": bool(reached),
            "spent_usd": round(spent, 4) if spent is not None else None, "cap_usd": cap}


def report(session: Session, ws: Workspace, now: datetime | None = None) -> dict[str, Any]:
    now = _aware(now) or utcnow()
    channels = list(session.exec(select(Channel).where(Channel.workspace_id == ws.id)).all())
    ch_ids = [c.id for c in channels if c.id is not None]
    created = _aware(ws.created_at)
    paid = _aware(ws.paid_at)

    # A0
    lag = int((created - paid).total_seconds()) if (created and paid) else None
    a0 = _step(True, created, paid_at=iso_utc(paid), provision_lag_seconds=lag)

    # A1 — #214 adds oauth_status_changed_at; until then the platform credential
    # write time is the best "connected at" we have (legacy token.json: null).
    connected = [c for c in channels if c.oauth_status == OAuthStatus.CONNECTED]
    a1_at = None
    if connected:
        creds = session.exec(select(OAuthCredential.updated_at).where(
            OAuthCredential.channel_id.in_([c.id for c in connected]))).all()
        a1_at = min((_aware(t) for t in creds if t is not None), default=None)
    a1 = _step(bool(connected), a1_at, connected_channels=len(connected))

    # A2
    niche = any((c.niche or "").strip() for c in channels)
    topics = []
    if ch_ids:
        topics = sorted(_aware(t) for t in session.exec(
            select(Topic.created_at).where(Topic.channel_id.in_(ch_ids),
                                           Topic.active == True)).all())  # noqa: E712
    a2_done = niche and len(topics) >= A2_MIN_TOPICS
    a2 = _step(a2_done, topics[A2_MIN_TOPICS - 1] if a2_done else None,
               niche_set=niche, active_topics=len(topics))

    # A3
    succ = _render_successes(session, ch_ids)
    fd_at, fd_src = first_draft_at(session, ws)
    a3_done = len(succ) >= A3_MIN_DRAFTS
    a3 = _step(a3_done, succ[A3_MIN_DRAFTS - 1][1] if a3_done else None,
               drafts=len(succ), first_draft_at=iso_utc(fd_at), first_draft_source=fd_src,
               first_draft_after_paid_seconds=(int((fd_at - paid).total_seconds())
                                               if fd_at and paid else None))

    # A4
    approvals = []
    if ch_ids:
        approvals = session.exec(
            select(Video.approved_at, Video.approved_by).where(
                Video.channel_id.in_(ch_ids), Video.approved_at.is_not(None))).all()
    human = sorted((_aware(at), by) for at, by in approvals
                   if by and not by.startswith(AUTO_ACTOR_PREFIX))
    a4 = _step(bool(human), human[0][0] if human else None,
               approved_by=human[0][1] if human else None,
               human_approvals=len(human),
               auto_approvals=sum(1 for _, by in approvals
                                  if (by or "").startswith(AUTO_ACTOR_PREFIX)),
               unattributed_approvals=sum(1 for _, by in approvals if not by))

    # A5
    sig = render_failed_signal(session, ws, now)
    blk = llm_blocked(ws)
    cap = monthly_cap_signal(ws)
    a5 = {"done": sig["failed_over_24h"] == 0 and blk is None and not cap["reached"],
          "at": blk["at"] if blk else None, **sig, "llm_blocked": blk,
          # S2: per-workspace monthly LLM subcap (workspace_monthly_cap).
          "monthly_cap": cap}

    # A6
    pubs = []
    if ch_ids:
        pubs = sorted(_aware(t) for t in session.exec(
            select(Video.published_at).where(Video.channel_id.in_(ch_ids),
                                             Video.status == VideoStatus.PUBLISHED,
                                             Video.published_at.is_not(None))).all())
    a6 = _step(bool(pubs), pubs[0] if pubs else None, published=len(pubs))

    return {
        "workspace_id": ws.id, "workspace_slug": ws.slug,
        "ops_alerts": ([{"code": "login_ambiguous_owner",
                         "count": ws.login_ambiguous_owner_count,
                         "last_at": iso_utc(ws.login_ambiguous_owner_last_at)}]
                       if ws.login_ambiguous_owner_count else []),
        "is_test": bool(ws.is_test),
        "paid": paid is not None,
        "subscription_status": ws.subscription_status,
        "created_at": iso_utc(created), "paid_at": iso_utc(paid),
        "first_draft_at": iso_utc(fd_at),
        "A0": a0, "A1": a1, "A2": a2, "A3": a3, "A4": a4, "A5": a5, "A6": a6,
        "generated_at": iso_utc(now),
    }


def report_all(session: Session, *, include_test: bool = True,
               include_owera: bool = False, now: datetime | None = None) -> list[dict]:
    q = select(Workspace).order_by(Workspace.id)
    out = []
    for ws in session.exec(q).all():
        if ws.id == 1 and not include_owera:
            continue
        if ws.is_test and not include_test:
            continue
        out.append(report(session, ws, now))
    return out
