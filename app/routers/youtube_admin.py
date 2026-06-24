"""YouTube channel administration: branding, metrics, and subscriptions.

All endpoints are channel-scoped and require a connected channel. Live YouTube
calls run in FastAPI's threadpool (the path operations are sync), so they don't
block the event loop.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import Channel, ChannelMetric, Topic, Video, VideoMetric, VideoStatus
from app.schemas import BrandingUpdate, SubscribeBody
from app.services import analytics_loop, metrics_loop, quota, youtube
from app.services.youtube import (NeedsConnect, QUOTA_CHANNEL_UPDATE,
                                  QUOTA_SUBSCRIPTION_WRITE)

router = APIRouter(prefix="/api/channels/{channel_id}", tags=["youtube-admin"])


def _connected(session: Session, channel_id: int):
    """Return (channel, youtube_service) or raise a clean HTTP error."""
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    try:
        return ch, youtube.get_service(ch.slug)
    except NeedsConnect as e:
        raise HTTPException(409, f"channel not connected: {e}")


# ---- Overview & branding -------------------------------------------------

@router.get("/youtube")
def youtube_overview(channel_id: int, session: Session = Depends(get_session)):
    """Live snippet + statistics + branding for the connected channel."""
    _ch, service = _connected(session, channel_id)
    try:
        data = youtube.fetch_channel(service)
    except Exception as e:
        raise HTTPException(502, f"youtube fetch failed: {e}")
    if not data:
        raise HTTPException(502, "no channel on this account")
    return data


@router.put("/branding")
def update_branding(channel_id: int, body: BrandingUpdate,
                    session: Session = Depends(get_session)):
    ch, service = _connected(session, channel_id)
    if not ch.yt_channel_id:
        raise HTTPException(400, "channel identity unknown — reconnect first")
    try:
        result = youtube.update_branding(
            service, ch.yt_channel_id, **body.model_dump(exclude_unset=True))
    except Exception as e:
        quota.log(session, kind="branding", status="error", channel_id=channel_id,
                  detail=str(e)[:300])
        session.commit()
        raise HTTPException(502, f"branding update failed: {e}")
    quota.log(session, kind="branding", status="success", channel_id=channel_id,
              quota_cost=QUOTA_CHANNEL_UPDATE)
    session.commit()
    return result


# ---- Metrics -------------------------------------------------------------

@router.get("/metrics")
def get_metrics(channel_id: int, session: Session = Depends(get_session)):
    """Stored snapshot history (oldest→newest) for the subscriber/view/video trend."""
    if not session.get(Channel, channel_id):
        raise HTTPException(404, "channel not found")
    rows = session.exec(
        select(ChannelMetric).where(ChannelMetric.channel_id == channel_id)
        .order_by(ChannelMetric.captured_at)
    ).all()
    return {"latest": rows[-1] if rows else None, "history": rows}


@router.post("/metrics/refresh")
def refresh_metrics(channel_id: int, session: Session = Depends(get_session)):
    """Fetch live statistics and record a snapshot now."""
    ch, _service = _connected(session, channel_id)
    m = metrics_loop.record_snapshot(session, ch)
    if m is None:
        raise HTTPException(502, "could not fetch channel statistics")
    session.commit()
    session.refresh(m)
    return m


# ---- Subscriptions -------------------------------------------------------

@router.get("/subscriptions")
def list_subscriptions(channel_id: int, session: Session = Depends(get_session)):
    """Channels this account follows."""
    _ch, service = _connected(session, channel_id)
    try:
        return youtube.list_subscriptions(service)
    except Exception as e:
        raise HTTPException(502, f"could not list subscriptions: {e}")


@router.post("/subscriptions", status_code=201)
def add_subscription(channel_id: int, body: SubscribeBody,
                     session: Session = Depends(get_session)):
    _ch, service = _connected(session, channel_id)
    try:
        target = youtube.resolve_channel_id(service, body.channel)
        result = youtube.subscribe(service, target)
    except Exception as e:
        raise HTTPException(400, f"subscribe failed: {e}")
    quota.log(session, kind="subscribe", status="success", channel_id=channel_id,
              quota_cost=QUOTA_SUBSCRIPTION_WRITE, detail=target)
    session.commit()
    return result


@router.delete("/subscriptions/{sub_id}", status_code=204)
def remove_subscription(channel_id: int, sub_id: str,
                        session: Session = Depends(get_session)):
    _ch, service = _connected(session, channel_id)
    try:
        youtube.unsubscribe(service, sub_id)
    except Exception as e:
        raise HTTPException(400, f"unsubscribe failed: {e}")
    quota.log(session, kind="unsubscribe", status="success", channel_id=channel_id,
              quota_cost=QUOTA_SUBSCRIPTION_WRITE)
    session.commit()


@router.get("/subscribers")
def list_subscribers(channel_id: int, session: Session = Depends(get_session)):
    """Recent subscribers to this channel (read-only)."""
    _ch, service = _connected(session, channel_id)
    try:
        return youtube.list_subscribers(service)
    except Exception as e:
        raise HTTPException(502, f"could not list subscribers: {e}")


# ---- Per-video analytics (the performance leaderboard) -------------------

_SORTABLE = {"views", "ctr", "avg_view_pct", "watch_time_minutes", "impressions",
             "likes", "comments", "subscribers_gained", "published_at"}


def _latest_metrics(session: Session, channel_id: int) -> dict[int, VideoMetric]:
    """Most recent VideoMetric per video for the channel (video_id -> row)."""
    rows = session.exec(
        select(VideoMetric).where(VideoMetric.channel_id == channel_id)
        .order_by(VideoMetric.captured_at)            # ascending → last write wins
    ).all()
    latest: dict[int, VideoMetric] = {}
    for m in rows:
        latest[m.video_id] = m
    return latest


def _published_videos(session: Session, channel_id: int) -> list[Video]:
    return session.exec(
        select(Video).where(
            Video.channel_id == channel_id, Video.status == VideoStatus.PUBLISHED)
    ).all()


def _topics_map(session: Session, channel_id: int) -> dict[int, Topic]:
    return {t.id: t for t in session.exec(
        select(Topic).where(Topic.channel_id == channel_id)).all()}


def _age_hours(published_at, now) -> float | None:
    if published_at is None:
        return None
    pub = published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return round((now - pub).total_seconds() / 3600, 1)


def _compute_monetization(session: Session, channel_id: int) -> dict:
    """YouTube Partner Program milestone progress from stored snapshots."""
    latest_cm = session.exec(
        select(ChannelMetric).where(ChannelMetric.channel_id == channel_id)
        .order_by(ChannelMetric.captured_at.desc())
    ).first()
    subscriber_count = latest_cm.subscriber_count if latest_cm else 0

    latest_metrics = _latest_metrics(session, channel_id)
    total_watch_hours = sum(m.watch_time_minutes for m in latest_metrics.values()) / 60

    topics = _topics_map(session, channel_id)
    short_topic_ids = {tid for tid, t in topics.items() if t.content_format == "short"}
    video_topic_map = {v.id: v.topic_id for v in _published_videos(session, channel_id)}
    shorts_views = sum(
        m.views for vid_id, m in latest_metrics.items()
        if video_topic_map.get(vid_id) in short_topic_ids
    )

    def _tier(current, threshold):
        needed = max(0, threshold - current)
        pct = min(100.0, round(current / threshold * 100, 1)) if threshold else 100.0
        return {"current": current, "needed": needed, "achieved": needed == 0, "pct": pct}

    wh = round(total_watch_hours, 1)
    return {
        "channel_id": channel_id,
        "subscriber_count": subscriber_count,
        "total_watch_hours": wh,
        "shorts_views": shorts_views,
        "lower_tier": {
            "subscribers":  _tier(subscriber_count, 500),
            "watch_hours":  _tier(wh, 3000),
            "shorts_views": _tier(shorts_views, 3_000_000),
            "tier_achieved": subscriber_count >= 500 and wh >= 3000 and shorts_views >= 3_000_000,
        },
        "full_tier": {
            "subscribers":  _tier(subscriber_count, 1000),
            "watch_hours":  _tier(wh, 4000),
            "shorts_views": _tier(shorts_views, 10_000_000),
            "tier_achieved": subscriber_count >= 1000 and wh >= 4000 and shorts_views >= 10_000_000,
        },
    }


@router.get("/monetization")
def get_monetization(channel_id: int, session: Session = Depends(get_session)):
    """YouTube Partner Program milestone progress for both tiers (fan funding and ad revenue).
    Computed from stored snapshots — no live API call needed."""
    if not session.get(Channel, channel_id):
        raise HTTPException(404, "channel not found")
    return _compute_monetization(session, channel_id)


@router.get("/video-analytics")
def video_analytics(channel_id: int, sort: str = "views",
                    session: Session = Depends(get_session)):
    """Latest analytics per published video (the leaderboard), sortable. Includes
    `age_hours` so consumers can apply a maturity window (analytics lags 24–72h)."""
    if not session.get(Channel, channel_id):
        raise HTTPException(404, "channel not found")
    if sort not in _SORTABLE:
        sort = "views"
    now = datetime.now(timezone.utc)
    latest = _latest_metrics(session, channel_id)
    topics = _topics_map(session, channel_id)
    items = []
    for v in _published_videos(session, channel_id):
        m = latest.get(v.id)
        t = topics.get(v.topic_id)
        items.append({
            "video_id": v.id,
            "yt_video_id": v.yt_video_id,
            "title": v.title or v.subject,
            "topic_id": v.topic_id,
            "topic": t.name if t else None,
            "content_format": t.content_format if t else None,
            "published_at": v.published_at,
            "age_hours": _age_hours(v.published_at, now),
            "views": m.views if m else 0,
            "impressions": m.impressions if m else 0,
            "ctr": m.ctr if m else 0.0,
            "avg_view_pct": m.avg_view_pct if m else 0.0,
            "watch_time_minutes": m.watch_time_minutes if m else 0,
            "likes": m.likes if m else 0,
            "comments": m.comments if m else 0,
            "subscribers_gained": m.subscribers_gained if m else 0,
            "captured_at": m.captured_at if m else None,
            "has_data": m is not None,
        })
    items.sort(key=lambda it: (it.get(sort) is not None, it.get(sort)), reverse=True)
    measured = sum(1 for it in items if it["has_data"])
    return {"items": items, "sort": sort, "count": len(items), "measured": measured}


@router.get("/video-analytics/by-topic")
def video_analytics_by_topic(channel_id: int, session: Session = Depends(get_session)):
    """Aggregate latest analytics by topic and by content format — the core signal
    the growth agent learns from (which themes / formats actually perform)."""
    if not session.get(Channel, channel_id):
        raise HTTPException(404, "channel not found")
    latest = _latest_metrics(session, channel_id)
    topics = _topics_map(session, channel_id)

    def _blank(extra: dict) -> dict:
        return {**extra, "video_count": 0, "measured": 0, "views": 0,
                "impressions": 0, "watch_time_minutes": 0, "likes": 0,
                "comments": 0, "subscribers_gained": 0, "_ctr_sum": 0.0,
                "_avp_sum": 0.0}

    by_topic: dict[int, dict] = {}
    by_format: dict[str, dict] = {}
    for v in _published_videos(session, channel_id):
        t = topics.get(v.topic_id)
        fmt = t.content_format if t else "unknown"
        tb = by_topic.setdefault(v.topic_id, _blank(
            {"topic_id": v.topic_id, "topic": t.name if t else None, "content_format": fmt}))
        fb = by_format.setdefault(fmt, _blank({"content_format": fmt}))
        m = latest.get(v.id)
        for bucket in (tb, fb):
            bucket["video_count"] += 1
            if m:
                bucket["measured"] += 1
                bucket["views"] += m.views
                bucket["impressions"] += m.impressions
                bucket["watch_time_minutes"] += m.watch_time_minutes
                bucket["likes"] += m.likes
                bucket["comments"] += m.comments
                bucket["subscribers_gained"] += m.subscribers_gained
                bucket["_ctr_sum"] += m.ctr
                bucket["_avp_sum"] += m.avg_view_pct

    def _finish(bucket: dict) -> dict:
        n = bucket.pop("measured")
        ctr_sum = bucket.pop("_ctr_sum")
        avp_sum = bucket.pop("_avp_sum")
        bucket["measured"] = n
        bucket["avg_ctr"] = round(ctr_sum / n, 4) if n else 0.0
        bucket["avg_view_pct"] = round(avp_sum / n, 2) if n else 0.0
        bucket["avg_views"] = round(bucket["views"] / n, 1) if n else 0.0
        return bucket

    topic_rows = sorted((_finish(b) for b in by_topic.values()),
                        key=lambda r: r["avg_views"], reverse=True)
    format_rows = sorted((_finish(b) for b in by_format.values()),
                         key=lambda r: r["avg_views"], reverse=True)
    return {"by_topic": topic_rows, "by_format": format_rows}


@router.post("/video-analytics/refresh")
def refresh_video_analytics(channel_id: int, session: Session = Depends(get_session)):
    """Fetch live per-video analytics now (bypasses the once-per-day gate). Requires
    the channel reconsented for the analytics scope; records a VideoMetric per video."""
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    if not ch.yt_channel_id:
        raise HTTPException(400, "channel identity unknown — reconnect first")
    now = datetime.now(timezone.utc)
    recorded = analytics_loop._snapshot_channel(session, ch, now, force=True)
    session.commit()
    return {"recorded": recorded}
