from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import Channel, OAuthStatus, Playlist, utcnow
from app.schemas import PlaylistCreate
from app.services import notify, youtube

router = APIRouter(prefix="/api/channels/{channel_id}/playlists", tags=["playlists"])


def _require_channel(channel_id: int, session: Session) -> Channel:
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    return ch


@router.get("")
def list_local(channel_id: int, session: Session = Depends(get_session)):
    _require_channel(channel_id, session)
    return session.exec(select(Playlist).where(Playlist.channel_id == channel_id)).all()


@router.post("/sync")
def sync(channel_id: int, session: Session = Depends(get_session)):
    ch = _require_channel(channel_id, session)
    try:
        service = youtube.get_service(ch.slug)
    except youtube.NeedsConnect as e:
        # A dead token must not hide behind the 400 (see youtube_admin._connected).
        notify.mark_dead_committed(session, ch, str(e))
        raise HTTPException(400, f"connect the channel first: {e}")
    remote = youtube.list_playlists(service)
    existing = {p.yt_playlist_id: p for p in
                session.exec(select(Playlist).where(Playlist.channel_id == channel_id)).all()}
    for r in remote:
        p = existing.get(r["yt_playlist_id"])
        if p:
            p.title = r["title"]
            p.description = r["description"]
            p.privacy = r["privacy"]
            p.last_synced_at = utcnow()
        else:
            p = Playlist(channel_id=channel_id, last_synced_at=utcnow(), **r)
        session.add(p)
    session.commit()
    return session.exec(select(Playlist).where(Playlist.channel_id == channel_id)).all()


@router.post("", status_code=201)
def create(channel_id: int, body: PlaylistCreate, session: Session = Depends(get_session)):
    ch = _require_channel(channel_id, session)
    try:
        service = youtube.get_service(ch.slug)
    except youtube.NeedsConnect as e:
        # A dead token must not hide behind the 400 (see youtube_admin._connected).
        notify.mark_dead_committed(session, ch, str(e))
        raise HTTPException(400, f"connect the channel first: {e}")
    r = youtube.create_playlist(service, body.title, body.description, body.privacy)
    p = Playlist(channel_id=channel_id, last_synced_at=utcnow(), **r)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p
