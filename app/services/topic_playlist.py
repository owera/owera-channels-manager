"""Lazily ensure a topic has its YouTube playlist.

Playlists are created automatically the first time a topic's video enters
production (and again, as a safety net, before publishing) — never manually.
"""

from typing import Optional

from sqlmodel import Session

from app.models import Channel, OAuthStatus, Playlist, Topic, utcnow
from app.services import quota, youtube


def ensure_topic_playlist(session: Session, topic: Topic, channel: Channel) -> Optional[int]:
    if topic is None:
        return None
    if topic.playlist_id:
        return topic.playlist_id
    if channel.oauth_status != OAuthStatus.CONNECTED:
        return None  # can't create yet; will retry when production/publish runs while connected
    try:
        service = youtube.get_service(channel.slug)
        r = youtube.create_playlist(service, topic.name, topic.theme_prompt or "",
                                    channel.default_privacy)
    except Exception as e:
        quota.log(session, kind="playlist_add", status="error", channel_id=channel.id,
                  detail=f"auto-create playlist for '{topic.name}' failed: {e}")
        return None
    pl = Playlist(channel_id=channel.id, last_synced_at=utcnow(), **r)
    session.add(pl)
    session.commit()
    session.refresh(pl)
    topic.playlist_id = pl.id
    session.add(topic)
    session.commit()
    quota.log(session, kind="playlist_add", status="success", channel_id=channel.id,
              detail=f"auto-created playlist '{pl.title}'", quota_cost=youtube.QUOTA_PLAYLIST_INSERT)
    return pl.id
