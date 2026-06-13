"""One-shot importer: bring the existing file-based channel/ pipeline into the DB.

Creates a Channel, a catch-all Topic ("General") to hold history, and imports every
channel/output/<slug>/ as a Video (published if it has an .uploaded marker, else
rendered), plus the leftover topics.txt subjects as draft Videos. Idempotent.

Run:  cd manager && uv run python -m app.migrate
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from app.config import REPO_DIR, settings
from app.db import init_db, session_scope
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus, utcnow
from app.services import youtube

CHANNEL_DIR = REPO_DIR / "channel"
OUTPUT_DIR = CHANNEL_DIR / "output"
SLUG = "ai-engineering"
NAME = "AI Engineering"
GENERAL_TOPIC = "General (imported)"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _ensure_channel(session) -> Channel:
    ch = session.exec(select(Channel).where(Channel.slug == SLUG)).first()
    if ch:
        return ch
    ch = Channel(slug=SLUG, name=NAME, default_privacy="public", default_skip_gate=False, paused=True)
    cdir = youtube.channel_dir(SLUG)
    cdir.mkdir(parents=True, exist_ok=True)
    # Never overwrite credentials that already exist (e.g. a freshly reconnected
    # youtube-scoped token) — only seed missing ones from the legacy channel/ dir.
    for fn in ("client_secret.json", "token.json"):
        src = CHANNEL_DIR / fn
        dst = cdir / fn
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
    if (cdir / "token.json").exists():
        ch.oauth_status = OAuthStatus.EXPIRED
        ch.oauth_error = "old token is youtube.upload scope — reconnect for playlist support"
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def _ensure_topic(session, channel_id: int) -> Topic:
    t = session.exec(
        select(Topic).where(Topic.channel_id == channel_id, Topic.name == GENERAL_TOPIC)
    ).first()
    if t:
        return t
    t = Topic(channel_id=channel_id, name=GENERAL_TOPIC,
              theme_prompt="Imported from the legacy file-based pipeline.", position=0)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def run() -> None:
    init_db()
    with session_scope() as session:
        ch = _ensure_channel(session)
        topic = _ensure_topic(session, ch.id)
        seen = set(session.exec(select(Video.subject).where(Video.channel_id == ch.id)).all())
        pos = 0
        imported = published = drafts = 0

        if OUTPUT_DIR.exists():
            for d in sorted(OUTPUT_DIR.iterdir()):
                if not d.is_dir():
                    continue
                meta = _read_json(d / "metadata.json")
                subject = meta.get("topic") or d.name
                if subject in seen:
                    continue
                seen.add(subject)
                v = Video(
                    channel_id=ch.id, topic_id=topic.id, subject=subject, position=pos,
                    title=meta.get("title"), description=meta.get("description"),
                    tags_json=json.dumps(meta.get("tags", [])) if meta.get("tags") else None,
                    script=meta.get("script"), mpt_task_id=meta.get("task_id"),
                    metadata_generated=bool(meta.get("title")),
                )
                pos += 1
                uploaded = d / ".uploaded"
                video_file = d / "video.mp4"
                if uploaded.exists():
                    v.status = VideoStatus.PUBLISHED
                    v.yt_video_id = uploaded.read_text().strip() or None
                    v.published_at = datetime.fromtimestamp(uploaded.stat().st_mtime, tz=timezone.utc)
                    published += 1
                elif video_file.exists():
                    v.status = VideoStatus.RENDERED
                else:
                    v.status = VideoStatus.DRAFT
                session.add(v)
                session.commit()
                session.refresh(v)
                if video_file.exists():
                    dest_dir = Path(settings.storage_dir) / "videos" / str(v.id)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / "video.mp4"
                    if not dest.exists():
                        shutil.copy(video_file, dest)
                    v.video_path = str(dest)
                    session.add(v)
                imported += 1
            session.commit()

        # Leftover topics.txt subjects -> draft videos (nothing auto-renders).
        topics_file = CHANNEL_DIR / "topics.txt"
        done_file = CHANNEL_DIR / "done.txt"
        done = set()
        if done_file.exists():
            done = {l.strip() for l in done_file.read_text().splitlines() if l.strip()}
        if topics_file.exists():
            for line in topics_file.read_text().splitlines():
                subj = line.strip()
                if not subj or subj.startswith("#") or subj in done or subj in seen:
                    continue
                seen.add(subj)
                session.add(Video(channel_id=ch.id, topic_id=topic.id, subject=subj,
                                  status=VideoStatus.DRAFT, position=pos))
                pos += 1
                drafts += 1
            session.commit()

        print(f"migration complete: channel '{ch.name}' (id={ch.id}), topic '{topic.name}' (id={topic.id})")
        print(f"  imported {imported} videos ({published} published), {drafts} leftover drafts")
        print(f"  oauth_status={ch.oauth_status} — reconnect in the UI; channel is paused")


if __name__ == "__main__":
    run()
