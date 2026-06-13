from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings, get_session
from app.models import Channel, JobRun, OAuthStatus, Video, VideoStatus
from app.services import quota
from app.services.mpt_client import mpt
from app.services.youtube import QUOTA_UPLOAD

router = APIRouter(prefix="/api", tags=["dashboard"])

_STATUSES = [VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING, VideoStatus.RENDERED,
             VideoStatus.REVIEW, VideoStatus.APPROVED, VideoStatus.PUBLISHING,
             VideoStatus.PUBLISHED, VideoStatus.FAILED, VideoStatus.REJECTED]


def _next_publish_eta(session: Session, ch: Channel, cfg) -> str | None:
    """ISO time the next approved video should publish (rank-1 estimate)."""
    n = session.exec(select(func.count(Video.id)).where(
        Video.channel_id == ch.id, Video.status == VideoStatus.APPROVED)).one()
    if not n or ch.paused or ch.oauth_status != OAuthStatus.CONNECTED:
        return None
    daily_limit = max(1, min(ch.daily_publish_budget, settings.youtube_daily_quota_cap // QUOTA_UPLOAD))
    now = datetime.now(timezone.utc)
    if quota.published_today(session, ch.id) >= daily_limit:
        nxt = (now + timedelta(days=1)).date()
        return datetime.combine(nxt, time.min, tzinfo=timezone.utc).isoformat()
    last = quota.last_publish_at(session, ch.id)
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    drip = timedelta(minutes=cfg.publish_drip_minutes)
    return (now if not last else max(now, last + drip)).isoformat()


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session)):
    cfg = app_settings(session)
    out = []
    for ch in session.exec(select(Channel).order_by(Channel.id)).all():
        counts = {s: 0 for s in _STATUSES}
        for status, n in session.exec(
            select(Video.status, func.count(Video.id))
            .where(Video.channel_id == ch.id).group_by(Video.status)
        ).all():
            counts[status] = n
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
            "quota_spent_today": quota.quota_spent_today(session, ch.id),
            "quota_cap": settings.youtube_daily_quota_cap,
            "next_publish_eta": _next_publish_eta(session, ch, cfg),
            "active": [{"id": v.id, "subject": v.subject, "status": v.status,
                        "render_progress": v.render_progress} for v in active],
        })
    return out


@router.get("/runs")
def runs(channel_id: int | None = None, video_id: int | None = None, limit: int = 100,
         session: Session = Depends(get_session)):
    q = select(JobRun)
    if channel_id is not None:
        q = q.where(JobRun.channel_id == channel_id)
    if video_id is not None:
        q = q.where(JobRun.video_id == video_id)
    return session.exec(q.order_by(JobRun.created_at.desc()).limit(limit)).all()
