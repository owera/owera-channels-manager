"""YouTube channel administration: branding, metrics, and subscriptions.

All endpoints are channel-scoped and require a connected channel. Live YouTube
calls run in FastAPI's threadpool (the path operations are sync), so they don't
block the event loop.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import Channel, ChannelMetric
from app.schemas import BrandingUpdate, SubscribeBody
from app.services import metrics_loop, quota, youtube
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
