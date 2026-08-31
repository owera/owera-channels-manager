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
from app.services.publish_loop import next_window_open
from app.services.render_loop import _queued_candidates
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
    ).all()
    if not approved:
        return {}
    # Same drain order as publish_loop._next_approved (long-first until one is
    # out on the quota day, then weight-desc shorts). FIFO-by-approved_at lied:
    # leftover low-weight shorts and buried longs got the earliest ETAs.
    topics = {
        t.id: t
        for t in session.exec(select(Topic).where(Topic.channel_id == channel_id)).all()
    }

    def _pick(remaining: list[Video], want: str | None) -> Video | None:
        scored: list[tuple] = []
        for v in remaining:
            t = topics.get(v.topic_id)
            fmt = t.content_format if t else None
            # Same == "long" / else-short split as _next_approved (empty/"LONG" are shorts).
            if want == "long" and fmt != "long":
                continue
            if want == "short" and fmt == "long":
                continue
            w = t.weight if t is not None and t.weight is not None else 1
            at = v.approved_at or datetime.min.replace(tzinfo=timezone.utc)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            scored.append((-w, at, v.id, v))
        if not scored:
            return None
        scored.sort()
        return scored[0][3]

    drip = timedelta(minutes=cfg_row.publish_drip_minutes)
    daily_limit = min(ch.daily_publish_budget, cfg.youtube_daily_quota_cap // QUOTA_UPLOAD)
    # tick() skips when published_today >= daily_publish_budget, so 0 (and
    # negative) never publish. max(1, ...) lied — the board got ETAs for a
    # channel that isn't publishing. Without this return the plan still
    # drains (one video per future quota day) instead of staying empty.
    if daily_limit <= 0:
        return {}
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
    remaining = list(approved)
    # Mix state starts from live publishes on the current quota day; future
    # simulated days start with no long out (reset on every cur_day change).
    long_out = (
        cur_day == _pt_date(now)
        and quota.published_long_today(session, channel_id) > 0
    )
    while remaining:
        # The loop only publishes inside the channel's audience-peak windows, so
        # the ETA can't land outside one (re-checked after a budget jump too).
        cursor = next_window_open(ch, cursor)
        if _pt_date(cursor) != cur_day:              # natural rollover from dripping
            cur_day, day_count = _pt_date(cursor), 0
            long_out = False
        if day_count >= daily_limit:                 # day's budget spent → next quota day
            cursor = next_window_open(ch, _next_quota_reset(cursor))
            cur_day, day_count = _pt_date(cursor), 0
            long_out = False
        if not long_out:
            v = _pick(remaining, "long") or _pick(remaining, None)
        else:
            v = _pick(remaining, "short") or _pick(remaining, None)
        remaining.remove(v)
        t = topics.get(v.topic_id)
        if t is not None and t.content_format == "long":
            long_out = True
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
    # Mirror _submit_new's gate: in-flight renders hold budget slots too.
    in_flight_ch = quota.in_flight_renders(session, channel_id)
    slots_today = max(0, budget - rendered - in_flight_ch)
    in_flight = quota.in_flight_renders(session)
    reset = _next_midnight_utc(datetime.now(timezone.utc)).isoformat()

    spent = f"{rendered}+{in_flight_ch} rendering" if in_flight_ch else str(rendered)
    # Same drain order as render_loop._submit_new / _queued_candidates (long-first
    # when no approved long, shorts-first when a long is already banked). FIFO-by
    # position/id lied: a queued long behind lower-id shorts was labeled "budget
    # full" while it was actually next to render (the 2026-08-07 ch2 shape).
    ordered = [v for v in _queued_candidates(session) if v.channel_id == channel_id]
    if not ordered:
        ordered = queued
    plan: dict[str, dict] = {}
    for i, v in enumerate(ordered):
        if i >= slots_today:                              # today's render budget spent
            plan[str(v.id)] = entry(f"render budget full ({spent}/{budget})", reset)
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
        # Deletion is the one lifecycle exit with no other trace (the row itself is
        # the record) — log it so bulk deletes are visible in /api/runs.
        quota.log(session, kind="delete", status="success", video_id=video_id,
                  channel_id=v.channel_id,
                  detail=f"deleted: status={v.status} subject={v.subject!r} "
                         f"yt={v.yt_video_id or '-'}")
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
    # Draft->queued from the API was the last unaudited transition (2026-08-01: an
    # operator bulk-produce of 20 drafts was only reconstructable from the uvicorn
    # access log) — log it so /api/runs distinguishes operator queueing from the
    # scheduler's auto-produce rows.
    quota.log(session, kind="produce", status="success", video_id=v.id,
              channel_id=v.channel_id, detail="produced via API: draft queued")
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
            quota.log(session, kind="produce", status="success", video_id=v.id,
                      channel_id=v.channel_id,
                      detail="bulk-produced via API: draft queued")
            n += 1
    session.commit()
    return {"produced": n}


# The review-gate transitions below each log one JobRun (like produce/delete):
# reject/requeue/retry/approve had zero jobrun rows ever, so both of this
# fortnight's operator-vs-agent forensics started blind on these paths. The
# log rides the same commit as the status flip; refused calls write nothing.
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
    quota.log(session, kind="approve", status="success", video_id=v.id,
              channel_id=v.channel_id,
              detail=f"approved via API: {v.status} -> approved")
    v.status = VideoStatus.APPROVED
    v.approved_at = utcnow()
    v.rejected_reason = None
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


@router.post("/{video_id}/reject")
def reject(video_id: int, body: RejectBody, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    quota.log(session, kind="reject", status="success", video_id=v.id,
              channel_id=v.channel_id,
              detail=f"rejected via API: {v.status} -> rejected; reason={body.reason!r}")
    return _set_status(session, video_id, VideoStatus.REJECTED, rejected_reason=body.reason)


@router.post("/{video_id}/requeue")
def requeue(video_id: int, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    quota.log(session, kind="requeue", status="success", video_id=v.id,
              channel_id=v.channel_id,
              detail=f"requeued via API: {v.status} -> queued (re-render)")
    return _set_status(session, video_id, VideoStatus.QUEUED, error=None, mpt_task_id=None, render_progress=0)


@router.post("/{video_id}/retry")
def retry(video_id: int, session: Session = Depends(get_session)):
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    if v.video_path:
        quota.log(session, kind="retry", status="success", video_id=v.id,
                  channel_id=v.channel_id,
                  detail=f"retried via API: {v.status} -> approved (artifact kept, re-publish)")
        return _set_status(session, video_id, VideoStatus.APPROVED, error=None, approved_at=utcnow())
    quota.log(session, kind="retry", status="success", video_id=v.id,
              channel_id=v.channel_id,
              detail=f"retried via API: {v.status} -> queued (re-render)")
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
