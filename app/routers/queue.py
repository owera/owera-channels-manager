from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings, get_session
from app.models import (Channel, JobRun, OAuthStatus, Topic, TrendSignal, TrendStatus,
                        Video, VideoStatus, Workspace)
from app.services import issues, quota
from app.workspace import (channels_for_workspace, current_workspace, job_run_scope,
                           require_channel, require_video)
from app.services.mpt_client import mpt
from app.services.publish_loop import next_window_open
from app.services.youtube import QUOTA_UPLOAD

router = APIRouter(prefix="/api", tags=["dashboard"])

_STATUSES = [VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING, VideoStatus.RENDERED,
             VideoStatus.REVIEW, VideoStatus.APPROVED, VideoStatus.PUBLISHING,
             VideoStatus.PUBLISHED, VideoStatus.FAILED, VideoStatus.REJECTED]


def _publish_hold(ch: Channel, approved: int) -> str | None:
    """Why next_publish_eta is missing when approved work exists.

    None means either there's nothing approved (no hold to explain) or an
    ETA will be produced (window/drip/cooldown still yield a time). Matches
    the early returns in `_next_publish_eta` so the dashboard/growth agent
    don't have to re-derive "paused vs budget 0 vs oauth" from a bare null.
    """
    if not approved:
        return None
    if ch.paused:
        return "paused"
    if ch.oauth_status != OAuthStatus.CONNECTED:
        return "oauth"
    daily_limit = min(ch.daily_publish_budget, settings.youtube_daily_quota_cap // QUOTA_UPLOAD)
    if daily_limit <= 0:
        return "budget"
    return None


def _next_publish_eta(session: Session, ch: Channel, cfg) -> str | None:
    """ISO time the next approved video should publish (rank-1 estimate)."""
    n = session.exec(select(func.count(Video.id)).where(
        Video.channel_id == ch.id, Video.status == VideoStatus.APPROVED,
        Video.held.is_not(True))).one()           # held rows never publish
    if not n or ch.paused or ch.oauth_status != OAuthStatus.CONNECTED:
        return None
    daily_limit = min(ch.daily_publish_budget, settings.youtube_daily_quota_cap // QUOTA_UPLOAD)
    # Same gate as publish_plan / tick(): budget<=0 means the channel isn't
    # publishing. max(1, ...) claimed a slot the loop will never take.
    if daily_limit <= 0:
        return None
    now = datetime.now(timezone.utc)
    if quota.published_today(session, ch.id) >= daily_limit:
        nxt = (now + timedelta(days=1)).date()
        return next_window_open(
            ch, datetime.combine(nxt, time.min, tzinfo=timezone.utc)).isoformat()
    last = quota.last_publish_at(session, ch.id)
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    drip = timedelta(minutes=cfg.publish_drip_minutes)
    # The loop's _window_ok gates the real publish; mirror it so the ETA is honest
    # for a channel currently outside its audience-peak windows.
    return next_window_open(ch, now if not last else max(now, last + drip)).isoformat()


def _token_refresh_entry(ch) -> dict:
    from app.services import token_refresh_age

    return {
        "last_refresh_ok_at": ch.last_refresh_ok_at,
        "stale_at": ch.token_refresh_stale_at,
        "stale": ch.token_refresh_stale_at is not None,
        "age_days": (token_refresh_age.age_days(ch)
                     if ch.oauth_status == OAuthStatus.CONNECTED else None),
    }


def _dashboard_for_workspace(session: Session, ws: Workspace):
    cfg = app_settings(session)
    out = []
    for ch in session.exec(channels_for_workspace(ws)).all():
        counts = {s: 0 for s in _STATUSES}
        for status, n in session.exec(
            select(Video.status, func.count(Video.id))
            .where(Video.channel_id == ch.id).group_by(Video.status)
        ).all():
            counts[status] = n
        # Held approved rows stay in counts["approved"] (status is unchanged) and
        # are surfaced separately; publish ETA / hold / runway exclude them.
        held = session.exec(select(func.count(Video.id)).where(
            Video.channel_id == ch.id, Video.status == VideoStatus.APPROVED,
            Video.held == True)).one()  # noqa: E712
        active = session.exec(
            select(Video).where(Video.channel_id == ch.id,
                                Video.status.in_([VideoStatus.RENDERING, VideoStatus.PUBLISHING]))
            .order_by(Video.last_attempt_at.desc())
        ).all()
        out.append({
            "channel": ch,
            "counts": counts,
            "rendered_today": quota.rendered_today(session, ch.id),
            "published_today": quota.published_today(session, ch.id),
            "quota_spent_today": quota.quota_spent_workspace_today(session, ch.workspace_id),
            "quota_cap": quota.youtube_quota_cap_for_workspace(session, ch.workspace_id),
            "next_publish_eta": _next_publish_eta(session, ch, cfg),
            "held": held,
            "publish_hold": _publish_hold(ch, counts.get(VideoStatus.APPROVED, 0) - held),
            # Runway / under-publish signals (same computation as the issues digest).
            "publish_signals": issues.publish_signals(session, ch),
            # S7 day-6 token-renewal check (DB-only; no tokens). stale_at is set by
            # the token_refresh_age job and cleared by the next good refresh.
            "token_refresh": _token_refresh_entry(ch),
            "active": [{"id": v.id, "subject": v.subject, "status": v.status,
                        "render_progress": v.render_progress} for v in active],
        })
    return out


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), ws: Workspace = Depends(current_workspace)):
    return _dashboard_for_workspace(session, ws)


@router.get("/runs")
def runs(channel_id: int | None = None, video_id: int | None = None, limit: int = 100,
         session: Session = Depends(get_session),
         ws: Workspace = Depends(current_workspace)):
    """Run audit, scoped to the caller's workspace (S4, ``job_run_scope``). A foreign
    or missing ``channel_id`` / ``video_id`` filter is the same 404."""
    q = select(JobRun).where(job_run_scope(ws))
    if channel_id is not None:
        require_channel(session, ws, channel_id)
        q = q.where(JobRun.channel_id == channel_id)
    if video_id is not None:
        require_video(session, ws, video_id)
        q = q.where(JobRun.video_id == video_id)
    return session.exec(q.order_by(JobRun.created_at.desc()).limit(limit)).all()


@router.get("/agent/issues")
def agent_issues(session: Session = Depends(get_session), ws: Workspace = Depends(current_workspace)):
    """The operational triage digest: terminal/persistent issues the background loops
    don't auto-heal (failed/rejected videos, OAuth walls, quota cooldowns, recurring
    error signatures, gate backlog, idea-board overflow). The agent remediates by
    composing the existing video/topic/channel endpoints; OAuth/quota walls escalate."""
    return issues.detect(session, workspace_id=ws.id)


@router.get("/agent/state")
def agent_state(runs_limit: int = 40, session: Session = Depends(get_session),
                ws: Workspace = Depends(current_workspace)):
    """Everything the autonomous growth agent needs in one read: app settings, the
    full dashboard, the per-topic/format analytics leaderboard per channel, the topic
    control surface (active/weight/pending), and the recent run audit. The agent acts
    through the existing PATCH/POST endpoints; this is its single observation call."""
    # Lazy import avoids any import-order coupling with the youtube-admin router.
    from app.routers.youtube_admin import video_analytics_by_topic, _compute_monetization
    from app.routers.settings import _workspace_view as _settings_workspace_view
    from app.workspace import is_admin_workspace

    cfg = app_settings(session)
    channels = session.exec(channels_for_workspace(ws)).all()
    ch_ids = {ch.id for ch in channels}

    analytics_by_topic = {}
    for ch in channels:
        if ch.oauth_status == OAuthStatus.CONNECTED:
            analytics_by_topic[ch.id] = video_analytics_by_topic(ch.id, session, ws)

    topics = []
    topic_rows = (
        session.exec(
            select(Topic).where(Topic.channel_id.in_(ch_ids))
            .order_by(Topic.channel_id, Topic.position)
        ).all()
        if ch_ids else []
    )
    for t in topic_rows:
        pending = session.exec(
            select(func.count(Video.id)).where(
                Video.topic_id == t.id,
                Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED]))
        ).one()
        published = session.exec(
            select(func.count(Video.id)).where(
                Video.topic_id == t.id, Video.status == VideoStatus.PUBLISHED)
        ).one()
        topics.append({
            "id": t.id, "channel_id": t.channel_id, "name": t.name,
            "content_format": t.content_format, "active": t.active,
            "weight": t.weight, "position": t.position,
            "pending": pending, "published": published,
        })

    # S4: same scope as GET /api/runs (channel-less global runs are admin-only).
    recent_runs = session.exec(
        select(JobRun).where(job_run_scope(ws))
        .order_by(JobRun.created_at.desc()).limit(runs_limit)
    ).all()

    # Active trends (so the agent dedupes + learns from prior research each run).
    trends_q = (
        select(TrendSignal)
        .where(TrendSignal.status.in_(
            [TrendStatus.RESEARCHED, TrendStatus.WATCHING, TrendStatus.ADOPTED]))
        .order_by(TrendSignal.updated_at.desc())
        .limit(30)
    )
    trends = [
        t for t in session.exec(trends_q).all()
        if t.channel_id is None or t.channel_id in ch_ids
    ]

    monetization_by_channel = {ch.id: _compute_monetization(session, ch.id) for ch in channels}

    # BGM pool — surface pool health so the agent can top it up on demand.
    # bgm_dir/bgm_pool_min/bgm_pool_target live on app.config.settings, not the DB model.
    from pathlib import Path
    from app.services import music_gen as _mg
    _bgm_dir = Path(settings.bgm_dir)
    _pool_count = _mg.pool_count(_bgm_dir)
    bgm_pool = {
        "count": _pool_count,
        "min": settings.bgm_pool_min,
        "target": settings.bgm_pool_target,
        "bgm_dir": str(_bgm_dir),
        "is_low": _pool_count < settings.bgm_pool_min,
    }

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        # S4: global Settings only for the admin workspace (ws 1).
        "settings": cfg if is_admin_workspace(ws) else _settings_workspace_view(ws),
        "dashboard": _dashboard_for_workspace(session, ws),
        "analytics_by_topic": analytics_by_topic,
        "monetization_by_channel": monetization_by_channel,
        "topics": topics,
        "trends": trends,
        "recent_runs": recent_runs,
        "issues": issues.detect(session, workspace_id=ws.id),
        "bgm_pool": bgm_pool,
    }
