"""Per-channel daily counters: render/publish budgets and YouTube quota accounting."""

from datetime import datetime, timezone

from sqlmodel import Session, func, select

from app.models import JobRun, Video, VideoStatus


def _day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _count(session: Session, channel_id: int, kind: str) -> int:
    return session.exec(
        select(func.count(JobRun.id)).where(
            JobRun.channel_id == channel_id, JobRun.kind == kind,
            JobRun.status == "success", JobRun.created_at >= _day_start(),
        )
    ).one()


def published_today(session: Session, channel_id: int) -> int:
    return _count(session, channel_id, "publish")


def rendered_today(session: Session, channel_id: int) -> int:
    return _count(session, channel_id, "render")


def quota_spent_today(session: Session, channel_id: int) -> int:
    total = session.exec(
        select(func.coalesce(func.sum(JobRun.quota_cost), 0)).where(
            JobRun.channel_id == channel_id, JobRun.created_at >= _day_start(),
        )
    ).one()
    return int(total or 0)


def last_publish_at(session: Session, channel_id: int):
    return session.exec(
        select(func.max(JobRun.created_at)).where(
            JobRun.channel_id == channel_id, JobRun.kind == "publish",
            JobRun.status == "success",
        )
    ).one()


def in_flight_renders(session: Session) -> int:
    return session.exec(
        select(func.count(Video.id)).where(Video.status == VideoStatus.RENDERING)
    ).one()


def log(session: Session, *, kind: str, status: str, video_id=None, channel_id=None,
        detail: str = None, quota_cost: int = 0) -> None:
    session.add(JobRun(
        kind=kind, status=status, video_id=video_id, channel_id=channel_id,
        detail=(detail or "")[:1000], quota_cost=quota_cost,
    ))
