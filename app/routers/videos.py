"""Videos = the produced units that flow through the queue/board."""

import json
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from app.config import settings as cfg
from app.db import app_settings, get_session
from app.models import Channel, Topic, Video, VideoStatus, utcnow
from app.schemas import RejectBody, ReorderBody, VideoCreate, VideoUpdate
from app.services import metadata, quota
from app.services.youtube import QUOTA_UPLOAD

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("")
def list_videos(channel_id: int | None = None, topic_id: int | None = None,
                status: str | None = None, session: Session = Depends(get_session)):
    q = select(Video)
    if channel_id is not None:
        q = q.where(Video.channel_id == channel_id)
    if topic_id is not None:
        q = q.where(Video.topic_id == topic_id)
    if status is not None:
        q = q.where(Video.status == status)
    return session.exec(q.order_by(Video.position, Video.id)).all()


def _next_midnight_utc(dt: datetime) -> datetime:
    """Next UTC midnight after dt — the render-budget day boundary (render uses no quota)."""
    nxt = (dt + timedelta(days=1)).date()
    return datetime.combine(nxt, time.min, tzinfo=timezone.utc)


def _next_quota_reset(dt: datetime) -> datetime:
    """Next YouTube quota-day boundary (Pacific midnight) strictly after dt — the
    publish-budget/quota day boundary."""
    return quota._next_pt_midnight_utc(dt)


def _pt_date(dt: datetime):
    """The Pacific (YouTube quota) calendar date of a UTC datetime."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return (dt - timedelta(hours=8)).date()


@router.get("/publish-plan")
def publish_plan(channel_id: int, session: Session = Depends(get_session)):
    """Estimated publish time for each approved video, honoring drip spacing and the
    per-channel daily publish/quota budget. Returns {video_id: iso8601}."""
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    cfg_row = app_settings(session)
    approved = session.exec(
        select(Video).where(Video.channel_id == channel_id, Video.status == VideoStatus.APPROVED)
        .order_by(Video.approved_at, Video.id)
    ).all()
    if not approved:
        return {}

    drip = timedelta(minutes=cfg_row.publish_drip_minutes)
    daily_limit = min(ch.daily_publish_budget, cfg.youtube_daily_quota_cap // QUOTA_UPLOAD)
    daily_limit = max(1, daily_limit)
    now = datetime.now(timezone.utc)
    last = quota.last_publish_at(session, channel_id)
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    # The publish loop won't touch this channel while it's in a YouTube daily-cap
    # cooldown, so the schedule can't start before the cap resets. Mirror the gates
    # in publish_loop.tick() so the ETA shows the real next-publish time rather than
    # "any moment".
    gate = now
    cooldown = ch.cooldown_until
    if cooldown is not None:
        if cooldown.tzinfo is None:                  # SQLite returns naive datetimes
            cooldown = cooldown.replace(tzinfo=timezone.utc)
        gate = max(gate, cooldown)
    if quota.daily_limit_hit(session, channel_id):
        # Same-day cap with no (or an earlier) cooldown timestamp; daily_limit_hit()
        # clears when YouTube's quota resets (Pacific midnight). Independent of
        # cooldown — the loop skips on either gate, so respect the later of the two.
        gate = max(gate, _next_quota_reset(now))

    # Publishing follows YouTube's quota day (Pacific midnight), so the budget rolls
    # over and the counts reset on that boundary — matching when YouTube replenishes.
    cursor = gate if not last else max(gate, last + drip)
    cur_day = _pt_date(cursor)
    day_count = quota.published_today(session, channel_id) if cur_day == _pt_date(now) else 0

    plan: dict[str, str] = {}
    for v in approved:
        if _pt_date(cursor) != cur_day:              # natural rollover from dripping
            cur_day, day_count = _pt_date(cursor), 0
        if day_count >= daily_limit:                 # day's budget spent → next quota day
            cursor = _next_quota_reset(cursor)
            cur_day, day_count = _pt_date(cursor), 0
        plan[str(v.id)] = cursor.isoformat()
        day_count += 1
        cursor = cursor + drip
    return plan


@router.get("/queue-plan")
def queue_plan(channel_id: int, session: Session = Depends(get_session)):
    """Why each queued video isn't rendering yet — mirrors the gates in
    render_loop._submit_new so the board can label queued cards. Returns
    {video_id: {"reason": str, "eta": iso8601|null}}."""
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    queued = session.exec(
        select(Video).where(Video.channel_id == channel_id, Video.status == VideoStatus.QUEUED)
        .order_by(Video.position, Video.id)
    ).all()
    if not queued:
        return {}

    cfg_row = app_settings(session)

    def entry(reason: str, eta: str | None = None) -> dict:
        return {"reason": reason, "eta": eta}

    # Global / channel-wide stops apply to every queued video.
    if cfg_row.scheduler_paused:
        return {str(v.id): entry("scheduler paused") for v in queued}
    if ch.paused:
        return {str(v.id): entry("channel paused") for v in queued}

    budget = ch.daily_render_budget
    rendered = quota.rendered_today(session, channel_id)
    slots_today = max(0, budget - rendered)
    in_flight = quota.in_flight_renders(session)
    reset = _next_midnight_utc(datetime.now(timezone.utc)).isoformat()

    plan: dict[str, dict] = {}
    for i, v in enumerate(queued):
        if i >= slots_today:                              # today's render budget spent
            plan[str(v.id)] = entry(f"render budget full ({rendered}/{budget})", reset)
        elif i == 0 and in_flight >= cfg_row.render_concurrency:
            plan[str(v.id)] = entry("waiting for render slot")
        elif i == 0:
            plan[str(v.id)] = entry("next to render")
        else:
            plan[str(v.id)] = entry("queued · renders today")
    return plan


@router.post("", status_code=201)
def create_video(body: VideoCreate, session: Session = Depends(get_session)):
    topic = session.get(Topic, body.topic_id)
    if not topic:
        raise HTTPException(404, "topic not found")
    mx = session.exec(select(func.max(Video.position)).where(Video.channel_id == topic.channel_id)).one() or 0
    v = Video(channel_id=topic.channel_id, topic_id=topic.id, subject=body.subject.strip(),
              status=VideoStatus.QUEUED if body.queue else VideoStatus.DRAFT, position=mx + 1)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@router.get("/{video_id}")
def get_video(video_id: int, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    return v


@router.patch("/{video_id}")
def update_video(video_id: int, body: VideoUpdate, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    data = body.model_dump(exclude_unset=True)
    if "overrides" in data:
        ov = data.pop("overrides")
        v.overrides_json = json.dumps(ov) if ov else None
    if "tags" in data:
        v.tags_json = json.dumps(data.pop("tags") or [])
    for k, val in data.items():
        setattr(v, k, val)
    v.updated_at = utcnow()
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@router.delete("/{video_id}", status_code=204)
def delete_video(video_id: int, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if v:
        session.delete(v)
        session.commit()


def _set_status(session, video_id, new, **fields):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    v.status = new
    for k, val in fields.items():
        setattr(v, k, val)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@router.post("/{video_id}/produce")
def produce(video_id: int, session: Session = Depends(get_session)):
    """Promote a draft idea into the render queue."""
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    if v.status != VideoStatus.DRAFT:
        raise HTTPException(409, f"cannot produce from status '{v.status}'")
    return _set_status(session, video_id, VideoStatus.QUEUED)


@router.post("/produce")
def produce_bulk(body: ReorderBody, session: Session = Depends(get_session)):
    """Promote many drafts at once (reuses {channel_id, ordered_ids:[video ids]})."""
    n = 0
    for vid in body.ordered_ids:
        v = session.get(Video, vid)
        if v and v.status == VideoStatus.DRAFT:
            v.status = VideoStatus.QUEUED
            session.add(v)
            n += 1
    session.commit()
    return {"produced": n}


@router.post("/{video_id}/approve")
def approve(video_id: int, body: VideoUpdate | None = None, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    if v.status not in (VideoStatus.REVIEW, VideoStatus.RENDERED):
        raise HTTPException(409, f"cannot approve from status '{v.status}'")
    if body:
        data = body.model_dump(exclude_unset=True)
        if "tags" in data:
            v.tags_json = json.dumps(data.pop("tags") or [])
        for k in ("title", "description", "privacy"):
            if k in data:
                setattr(v, k, data[k])
    v.status = VideoStatus.APPROVED
    v.approved_at = utcnow()
    v.rejected_reason = None
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@router.post("/{video_id}/reject")
def reject(video_id: int, body: RejectBody, session: Session = Depends(get_session)):
    return _set_status(session, video_id, VideoStatus.REJECTED, rejected_reason=body.reason)


@router.post("/{video_id}/requeue")
def requeue(video_id: int, session: Session = Depends(get_session)):
    return _set_status(session, video_id, VideoStatus.QUEUED, error=None, mpt_task_id=None, render_progress=0)


@router.post("/{video_id}/retry")
def retry(video_id: int, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    if v.video_path:
        return _set_status(session, video_id, VideoStatus.APPROVED, error=None, approved_at=utcnow())
    return _set_status(session, video_id, VideoStatus.QUEUED, error=None, mpt_task_id=None, render_progress=0)


@router.post("/{video_id}/regenerate-metadata")
def regenerate_metadata(video_id: int, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    from app.services import video_gen
    topic = session.get(Topic, v.topic_id)
    fmt = "long" if topic and topic.content_format == "long" else "short"
    meta = metadata.generate(v.subject, v.script or "", fmt,
                             language=video_gen.channel_language(session, v.channel_id))
    v.title, v.description = meta["title"], meta["description"]
    v.tags_json = json.dumps(meta["tags"])
    v.metadata_generated = True
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@router.post("/reorder")
def reorder(body: ReorderBody, session: Session = Depends(get_session)):
    for pos, vid in enumerate(body.ordered_ids):
        v = session.get(Video, vid)
        if v and v.channel_id == body.channel_id:
            v.position = pos
            session.add(v)
    session.commit()
    return {"ok": True}
