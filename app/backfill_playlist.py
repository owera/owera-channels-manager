"""One-off: add all already-published videos in a topic to that topic's playlist.

Reuses an existing same-named playlist if present (synced from YouTube), else
creates it, links it to the topic, then inserts each published video.

Run:  cd manager && uv run python -m app.backfill_playlist
"""

import json

from sqlmodel import select

from app.db import session_scope
from app.models import Channel, Playlist, Topic, Video, VideoStatus, utcnow
from app.services import youtube

CHANNEL_SLUG = "ai-engineering"
TOPIC_NAME = "AI Engineering"


def run() -> None:
    with session_scope() as session:
        ch = session.exec(select(Channel).where(Channel.slug == CHANNEL_SLUG)).first()
        topic = session.exec(
            select(Topic).where(Topic.channel_id == ch.id, Topic.name == TOPIC_NAME)
        ).first()
        if not topic:
            print(f"topic '{TOPIC_NAME}' not found")
            return

        service = youtube.get_service(ch.slug)

        # Reuse an existing same-named playlist if there is one; else create.
        pl = session.get(Playlist, topic.playlist_id) if topic.playlist_id else None
        if not pl:
            remote = youtube.list_playlists(service)
            match = next((r for r in remote if r["title"].strip().lower() == TOPIC_NAME.lower()), None)
            if match:
                pl = session.exec(
                    select(Playlist).where(Playlist.channel_id == ch.id,
                                           Playlist.yt_playlist_id == match["yt_playlist_id"])
                ).first()
                if not pl:
                    pl = Playlist(channel_id=ch.id, last_synced_at=utcnow(), **match)
                    session.add(pl)
                    session.commit()
                    session.refresh(pl)
                print(f"reusing existing playlist '{pl.title}' ({pl.yt_playlist_id})")
            else:
                r = youtube.create_playlist(service, TOPIC_NAME, topic.theme_prompt or "",
                                            ch.default_privacy)
                pl = Playlist(channel_id=ch.id, last_synced_at=utcnow(), **r)
                session.add(pl)
                session.commit()
                session.refresh(pl)
                print(f"created playlist '{pl.title}' ({pl.yt_playlist_id})")
            topic.playlist_id = pl.id
            session.add(topic)
            session.commit()

        vids = session.exec(
            select(Video).where(
                Video.topic_id == topic.id, Video.status == VideoStatus.PUBLISHED,
                Video.yt_video_id.is_not(None),
            ).order_by(Video.id)
        ).all()

        added = skipped = failed = 0
        for v in vids:
            if v.added_to_playlist:
                skipped += 1
                continue
            try:
                youtube.add_to_playlist(service, pl.yt_playlist_id, v.yt_video_id)
                v.added_to_playlist = True
                session.add(v)
                session.commit()
                added += 1
                print(f"  + {v.yt_video_id}  {v.subject[:50]}")
            except Exception as e:
                failed += 1
                print(f"  ! {v.yt_video_id}  FAILED: {str(e)[:90]}")

        print(f"\ndone: {added} added, {skipped} already in playlist, {failed} failed "
              f"({len(vids)} published videos total)")
        print(f"playlist: https://youtube.com/playlist?list={pl.yt_playlist_id}")


if __name__ == "__main__":
    run()
