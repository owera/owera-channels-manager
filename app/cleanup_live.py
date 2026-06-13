"""Permanently delete all currently-live (published) videos from YouTube.

Snapshots the published videos at run time (across all channels), calls
videos.delete on each, and marks the DB row rejected with a reason. Per-video
errors (e.g. a video already gone, or not owned by the token) are logged and
skipped. Run directly — does not require the web server.

    cd manager && uv run python -m app.cleanup_live
"""

from sqlmodel import select

from app.db import session_scope
from app.models import Channel, Video, VideoStatus
from app.services import quota, youtube

QUOTA_DELETE = 50
REASON = "deleted from YouTube — incorrect render profile"


def run() -> None:
    deleted = failed = 0
    with session_scope() as session:
        for ch in session.exec(select(Channel)).all():
            pubs = session.exec(
                select(Video).where(
                    Video.channel_id == ch.id,
                    Video.status == VideoStatus.PUBLISHED,
                    Video.yt_video_id.is_not(None),
                )
            ).all()
            if not pubs:
                continue
            try:
                service = youtube.get_service(ch.slug)
            except Exception as e:
                print(f"channel '{ch.name}': cannot authenticate, skipping ({e})")
                continue
            print(f"channel '{ch.name}': deleting {len(pubs)} live videos")
            for v in pubs:
                vid = v.yt_video_id
                try:
                    service.videos().delete(id=vid).execute()
                except Exception as e:
                    # 404 = already gone → treat as done; else record failure.
                    if "404" in str(e) or "videoNotFound" in str(e):
                        print(f"  ~ {vid} already gone")
                    else:
                        failed += 1
                        print(f"  ! {vid} FAILED: {str(e)[:100]}")
                        quota.log(session, kind="delete", status="error", video_id=v.id,
                                  channel_id=ch.id, detail=f"{vid}: {str(e)[:200]}")
                        continue
                v.status = VideoStatus.REJECTED
                v.rejected_reason = REASON
                v.added_to_playlist = False
                session.add(v)
                session.commit()
                quota.log(session, kind="delete", status="success", video_id=v.id,
                          channel_id=ch.id, quota_cost=QUOTA_DELETE, detail=f"deleted {vid}")
                deleted += 1
                print(f"  - deleted {vid}  {v.subject[:45]}")
    print(f"\ncleanup done: {deleted} deleted, {failed} failed")


if __name__ == "__main__":
    run()
