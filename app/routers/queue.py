from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings, get_session
from app.models import (Channel, JobRun, OAuthStatus, Topic, TrendSignal, TrendStatus,
                        Video, VideoStatus, Workspace)
from app.services import issues, quota
from app.workspace import channels_for_workspace, current_workspace
from app.services.mpt_client import mpt
from app.services.publish_loop import next_window_open
from app.services.youtube import QUOTA_COMMENT_INSERT, QUOTA_THUMBNAIL_SET, QUOTA_UPLOAD

router = APIRouter(prefix="/api", tags=["dashboard"])

# tick() refuses the next upload unless this many units still fit the workspace
# cap (upload + thumbnail + first comment). Playlist insert is not in that
# pre-check. publish_plan and the dashboard ETA must use the same sum — the
# global cap // 1600 ceiling promised slots the loop will not take.
PUBLISH_UNITS = QUOTA_UPLOAD + QUOTA_THUMBNAIL_SET + QUOTA_COMMENT_INSERT

_STATUSES = [VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING, VideoStatus.RENDERED,
             VideoStatus.REVIEW, VideoStatus.APPROVED, VideoStatus.PUBLISHING,
             VideoStatus.PUBLISHED, VideoStatus.FAILED, VideoStatus.REJECTED]


def _publish_hold(ch: Channel, approved: int, session: Session | None = None) -> str | None:
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
    # Callers without a session keep the pre-workspace ceiling so a budget of
    # 0 still reads as "budget". The dashboard passes the session: a workspace
    # cap that cannot hold one upload is not a zero channel budget.
    if session is None:
        daily_limit = min(ch.daily_publish_budget, settings.youtube_daily_quota_cap // QUOTA_UPLOAD)
        if daily_limit <= 0:
            return "budget"
        return None
    if ch.daily_publish_budget <= 0:
        return "budget"
    cap = quota.youtube_quota_cap_for_workspace(session, ch.workspace_id)
    if cap < PUBLISH_UNITS:
        return "quota"
    return None


def _next_publish_eta(session: Session, ch: Channel, cfg) -> str | None:
    """ISO time the next approved video should publish (rank-1 estimate)."""
    n = session.exec(select(func.count(Video.id)).where(
        Video.channel_id == ch.id, Video.status == VideoStatus.APPROVED,
        Video.held.is_not(True))).one()           # held rows never publish
    if not n or ch.paused or ch.oauth_status != OAuthStatus.CONNECTED:
        return None
    # tick() skips on the channel budget, and also when the next upload does
    # not fit the workspace cap. The global cap // 1600 ceiling ignored a
    # lower cap and ignored units already spent by a sibling channel.
    if ch.daily_publish_budget <= 0:
        return None
    cap = quota.youtube_quota_cap_for_workspace(session, ch.workspace_id)
    if cap < PUBLISH_UNITS:
        return None
    now = datetime.now(timezone.utc)
    if quota.published_today(session, ch.id) >= ch.daily_publish_budget:
        nxt = (now + timedelta(days=1)).date()
        return next_window_open(
            ch, datetime.combine(nxt, time.min, tzinfo=timezone.utc)).isoformat()
    if quota.workspace_quota_would_exceed(session, ch.workspace_id, PUBLISH_UNITS):
        # Spend clears at Pacific midnight, not the next UTC date. A UTC jump
        # can still land inside the quota day tick() is skipping.
        return next_window_open(ch, quota._next_pt_midnight_utc(now)).isoformat()
    last = quota.last_publish_at(session, ch.id)
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    drip = timedelta(minutes=cfg.publish_drip_minutes)
    # The loop's _window_ok gates the real publish; mirror it so the ETA is honest
    # for a channel currently outside its audience-peak windows.
    return next_window_open(ch, now if not last else max(now, last + drip)).isoformat()


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
            "publish_hold": _publish_hold(ch, counts.get(VideoStatus.APPROVED, 0) - held, session),
            # Runway / under-publish signals (same computation as the issues digest).
            "publish_signals": issues.publish_signals(session, ch),
            "active": [{"id": v.id, "subject": v.subject, "status": v.status,
                        "render_progress": v.render_progress} for v in active],
        })
    return out


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), ws: Workspace = Depends(current_workspace)):
    return _dashboard_for_workspace(session, ws)


@router.get("/runs")
def runs(channel_id: int | None = None, video_id: int | None = None, limit: int = 100,
         session: Session = Depends(get_session)):
    q = select(JobRun)
    if channel_id is not None:
        q = q.where(JobRun.channel_id == channel_id)
    if video_id is not None:
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

    cfg = app_settings(session)
    channels = session.exec(channels_for_workspace(ws)).all()
    ch_ids = {ch.id for ch in channels}

    analytics_by_topic = {}
    for ch in channels:
        if ch.oauth_status == OAuthStatus.CONNECTED:
            analytics_by_topic[ch.id] = video_analytics_by_topic(ch.id, session)

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
            "weight": t.weight, "pending": pending, "published": published,
        })

    recent_runs_q = select(JobRun).order_by(JobRun.created_at.desc()).limit(runs_limit * 3)
    recent_runs = [
        r for r in session.exec(recent_runs_q).all()
        if r.channel_id is None or r.channel_id in ch_ids
    ][:runs_limit]

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
        "settings": cfg,
        "dashboard": _dashboard_for_workspace(session, ws),
        "analytics_by_topic": analytics_by_topic,
        "monetization_by_channel": monetization_by_channel,
        "topics": topics,
        "trends": trends,
        "recent_runs": recent_runs,
        "issues": issues.detect(session, workspace_id=ws.id),
        "bgm_pool": bgm_pool,
    }
