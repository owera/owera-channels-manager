"""Operational issue detection — the growth agent's triage signal.

The background loops already self-heal *transient* states (orphaned renders, stuck
publishing, transient render retries, blank-render fallback). This module surfaces the
**terminal / persistent / judgment-needed** class that nothing else handles: videos
stranded in failed/rejected, channels needing OAuth reconnect, quota walls, recurring
error signatures, gate backlogs, and idea-board overflow.

`detect(session)` is read-only. The agent reads it (via GET /api/agent/issues, and folded
into GET /api/agent/state) and remediates by composing the existing REST endpoints —
requeue / retry / reject / delete / PATCH. Each entry carries `suggested_action` and an
`auto` flag (auto-fixable vs. needs-operator escalation).
"""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.config import settings
from app.db import app_settings
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus)
from app.services import quota

# Same signatures render_loop treats as transient (retryable) render failures. Kept here
# as the single source the agent reasons over; mirror render_loop._advance_* if changed.
TRANSIENT_SIGNATURES = ("overloaded_error", "rate_limit_error", "RateLimitError",
                        "overloaded", "529", "503")

# Tunable thresholds (could move to the Settings table later).
REVIEW_STALE_HOURS = 48          # a video sitting in Review longer than this is a gate backlog
DEAD_VIDEO_AGE_DAYS = 7          # failed/rejected older than this are delete candidates
QUOTA_NEAR_CAP_FRACTION = 0.9    # flag a channel once it has spent this share of the daily cap
MAX_RETRIES = 2                  # render_loop gives up after this many retries


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalize to tz-aware UTC (as the loops do)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(dt: datetime | None, now: datetime) -> float | None:
    dt = _aware(dt)
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 3600, 1)


def _is_transient(error: str | None) -> bool:
    err = error or ""
    return any(sig in err for sig in TRANSIENT_SIGNATURES)


def _signature(detail: str | None) -> str:
    """Collapse a JobRun detail to a recurring-error signature: lowercased, digits
    stripped (so ids/counts don't fragment the group), whitespace-collapsed, truncated."""
    s = (detail or "").lower()
    s = "".join(" " if c.isdigit() else c for c in s)
    s = " ".join(s.split())
    return s[:80]


def _failed_action(v: Video, age_hours: float | None) -> tuple[str, bool]:
    """(suggested_action, auto) for a FAILED video. With full autonomy the agent fixes
    all of these; the action tells it *how*."""
    if v.video_path:
        return "retry", True            # render succeeded, failed at publish → re-approve
    if _is_transient(v.error) and v.retry_count < MAX_RETRIES:
        return "requeue", True          # transient render error → re-render
    if age_hours is not None and age_hours > DEAD_VIDEO_AGE_DAYS * 24:
        return "delete", True           # dead: non-transient/exhausted and old → clear board
    return "requeue", True              # recent non-transient, no file → one more render


def detect(session: Session) -> dict:
    """Classify the system's current operational state into an issues digest."""
    now = datetime.now(timezone.utc)
    cfg = app_settings(session)
    channels = session.exec(select(Channel).order_by(Channel.id)).all()
    names = {c.id: c.name for c in channels}

    failed, rejected = [], []
    stuck_rendering, stuck_publishing, stuck_review = [], [], []

    for v in session.exec(select(Video).where(Video.status == VideoStatus.FAILED)).all():
        age = _age_hours(v.updated_at, now)
        action, auto = _failed_action(v, age)
        failed.append({
            "id": v.id, "channel_id": v.channel_id, "topic_id": v.topic_id,
            "subject": v.subject, "error": v.error, "retry_count": v.retry_count,
            "has_file": bool(v.video_path), "transient": _is_transient(v.error),
            "age_hours": age, "suggested_action": action, "auto": auto,
        })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.REJECTED)).all():
        age = _age_hours(v.updated_at, now)
        old = age is not None and age > DEAD_VIDEO_AGE_DAYS * 24
        rejected.append({
            "id": v.id, "channel_id": v.channel_id, "subject": v.subject,
            "reason": v.rejected_reason, "age_hours": age,
            "suggested_action": "delete" if old else "leave", "auto": True,
        })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all():
        started = _aware(v.last_attempt_at or v.updated_at)
        if started and (now - started).total_seconds() > settings.render_timeout_seconds:
            stuck_rendering.append({
                "id": v.id, "channel_id": v.channel_id, "subject": v.subject,
                "age_hours": _age_hours(started, now), "render_progress": v.render_progress,
                "suggested_action": "requeue", "auto": True,
            })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.PUBLISHING)).all():
        started = _aware(v.last_attempt_at or v.updated_at)
        if started and (now - started).total_seconds() > settings.publish_timeout_seconds:
            stuck_publishing.append({
                "id": v.id, "channel_id": v.channel_id, "subject": v.subject,
                "age_hours": _age_hours(started, now),
                "suggested_action": "retry", "auto": True,
            })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.REVIEW)).all():
        age = _age_hours(v.updated_at, now)
        if age is not None and age > REVIEW_STALE_HOURS:
            stuck_review.append({
                "id": v.id, "channel_id": v.channel_id, "topic_id": v.topic_id,
                "subject": v.subject, "age_hours": age,
                "suggested_action": "approve or reject", "auto": True,
            })

    # Channel health — OAuth (escalate) and quota cooldown / spend (monitor).
    oauth, cooldown, quota_walls = [], [], []
    for ch in channels:
        if ch.oauth_status != OAuthStatus.CONNECTED:
            oauth.append({
                "channel_id": ch.id, "name": ch.name, "status": ch.oauth_status,
                "error": ch.oauth_error,
                "suggested_action": "operator must reconnect OAuth", "auto": False,
            })
        cu = _aware(ch.cooldown_until)
        if cu and cu > now:
            cooldown.append({
                "channel_id": ch.id, "name": ch.name, "until": cu.isoformat(),
                "suggested_action": "self-resets at reset; monitor", "auto": False,
            })
        hit = quota.daily_limit_hit(session, ch.id)
        spent = quota.quota_spent_today(session, ch.id)
        if hit or spent >= settings.youtube_daily_quota_cap * QUOTA_NEAR_CAP_FRACTION:
            quota_walls.append({
                "channel_id": ch.id, "name": ch.name, "daily_limit_hit": hit,
                "quota_spent_today": spent, "quota_cap": settings.youtube_daily_quota_cap,
                "suggested_action": "lower publish budget / widen drip, or monitor",
                "auto": False,
            })

    # Recurring error signatures in the last 24h → root-cause code-fix candidates.
    since = now - timedelta(hours=24)
    groups: dict[tuple[str, str], dict] = {}
    for r in session.exec(
        select(JobRun).where(JobRun.status == "error", JobRun.created_at >= since)
        .order_by(JobRun.created_at.desc())
    ).all():
        key = (r.kind, _signature(r.detail))
        g = groups.setdefault(key, {"kind": r.kind, "signature": key[1],
                                    "count": 0, "last_detail": r.detail})
        g["count"] += 1
    error_runs_24h = sorted(groups.values(), key=lambda g: g["count"], reverse=True)

    # Idea-board overflow — topics over the autogen ceiling (ties to the autofill cap).
    ceiling_base = max(cfg.topic_autogen_min_pending, cfg.topic_autogen_target)
    board_overflow = []
    for t in session.exec(select(Topic).where(Topic.active == True)).all():  # noqa: E712
        pending = session.exec(
            select(func.count(Video.id)).where(
                Video.topic_id == t.id,
                Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED]))
        ).one()
        ceiling = ceiling_base * min(t.weight or 1, 4)
        if pending > ceiling:
            board_overflow.append({
                "topic_id": t.id, "channel_id": t.channel_id, "name": t.name,
                "pending": pending, "ceiling": ceiling,
                "suggested_action": "produce or trim drafts", "auto": True,
            })

    # Board inventory — days of DRAFT+QUEUED work per channel vs the horizon cap.
    # Informational: not counted in issue totals, but visible to the growth agent.
    board_inventory = []
    for ch in channels:
        daily_cap = ch.daily_render_budget
        if daily_cap > 0:
            pending = session.exec(
                select(func.count(Video.id)).where(
                    Video.channel_id == ch.id,
                    Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED])
                )
            ).one()
            days = round(pending / daily_cap, 1)
            board_inventory.append({
                "channel_id": ch.id, "name": ch.name,
                "pending": pending, "daily_render_budget": daily_cap,
                "days_of_inventory": days,
                "board_horizon_days": cfg.board_horizon_days,
                "at_capacity": days >= cfg.board_horizon_days,
            })

    buckets = {
        "failed": failed, "rejected": rejected,
        "stuck_rendering": stuck_rendering, "stuck_publishing": stuck_publishing,
        "stuck_review": stuck_review, "oauth": oauth, "cooldown": cooldown,
        "quota": quota_walls, "error_runs_24h": error_runs_24h,
        "board_overflow": board_overflow,
    }
    # board_inventory is informational — excluded from issue counts.
    extra = {"board_inventory": board_inventory}
    # needs_operator = anything explicitly non-auto (OAuth, quota walls, cooldown).
    needs_operator = sum(
        1 for b in buckets.values() for item in b
        if isinstance(item, dict) and item.get("auto") is False
    )
    total = sum(len(b) for b in buckets.values())
    return {
        "now": now.isoformat(),
        **buckets,
        **extra,
        "summary": {"total_issues": total, "needs_operator": needs_operator,
                    "clean": total == 0},
        "channel_names": names,
    }
