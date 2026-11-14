"""MPT task-folder retention for EXTERNAL workspaces (PO CM 14/11, round 4).

An MPT render leaves a task folder ``<mpt_storage_dir>/<task_id>/`` (script,
narration audio, clips, final-N.mp4). After ``render_loop._finalize`` the Manager
works from its own copy (``<storage_dir>/videos/<video_id>/video.mp4``), so the
folder is scratch. For an external workspace it is deleted within
``RETENTION_DAYS`` (7) after the job finished (ok or failed), even without a
deletion request.

- **Finished** = the video's status is past rendering (rendered, review,
  approved, publishing, published, failed, rejected) and is not draft / queued /
  rendering. **Finish time** = the newest mtime of any file in the folder
  (symlinks not followed), so a folder still being written is never "old".
- **Never**: internal workspaces (ws 1 / 2 and ``MANAGER_LLM_INTERNAL_WORKSPACE_IDS``,
  OS/RR), a queued / rendering job, a folder shared by two videos, a folder
  with no video row (unknown owner), anything on YouTube.
- Same guards as the #214 media purge (``youtube_purge._safe_dir`` /
  ``_media_files``): one plain path segment directly inside the configured
  dir, no symlink followed, the plan logged first, ``MANAGER_PURGE_MEDIA_DRY_RUN``.

Runs as its own scheduler job ``mpt_task_retention`` on the same 6 h cycle as
S7's ``token_refresh_age`` and the #214 ``youtube_purge`` (``TICK_HOURS = 6``).
A separate job (not a step inside S7's tick) keeps a filesystem cleanup out of
S7's DB-only renewal check and lets either fail without stopping the other.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import settings
from app.models import Channel, Video, VideoStatus

logger = logging.getLogger("manager.mpt_retention")

TICK_HOURS = 6
RETENTION_DAYS = 7
EVENT = "mpt_task_retention"
INTERNAL_FLOOR: frozenset[int] = frozenset({1, 2})   # OS/RR, whatever the env says
RUNNING = frozenset({VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING})


def internal_ids() -> frozenset[int]:
    from app.services import llm_guard
    return INTERNAL_FLOOR | llm_guard.internal_workspace_ids()


def finished_at(folder) -> datetime | None:
    from app.services import youtube_purge
    files = [f for f in youtube_purge._media_files(folder) if not f.is_symlink()]
    if not files:
        return None
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files), tz=timezone.utc)


def candidates(session: Session, now: datetime) -> list[tuple[Video, Channel, object]]:
    from app.services import youtube_purge
    root = getattr(settings, "mpt_storage_dir", "") or ""
    internal = internal_ids()
    out = []
    rows = session.exec(select(Video, Channel).where(
        Video.channel_id == Channel.id, Video.mpt_task_id.is_not(None))).all()
    by_handle: dict[str, int] = {}
    for v, _c in rows:
        by_handle[v.mpt_task_id] = by_handle.get(v.mpt_task_id, 0) + 1
    for v, c in rows:
        if c.workspace_id is None or int(c.workspace_id) in internal:
            continue
        if v.status in RUNNING or by_handle.get(v.mpt_task_id, 0) != 1:
            continue
        d = youtube_purge._safe_dir(root, v.mpt_task_id)
        if d is None:
            continue
        fin = finished_at(d)
        if fin is None or now - fin < timedelta(days=RETENTION_DAYS):
            continue
        out.append((v, c, d))
    return out


def tick(now: datetime | None = None, *, dry_run: bool | None = None) -> int:
    """Delete (or, dry-run, log) MPT task folders of external jobs finished > 7 d."""
    from app.db import engine
    from app.services import youtube_purge
    now = now or datetime.now(timezone.utc)
    dry = bool(settings.purge_media_dry_run) if dry_run is None else bool(dry_run)
    n = 0
    with Session(engine) as session:
        for v, c, d in candidates(session, now):
            files = youtube_purge._media_files(d)
            logger.info("%s plan workspace_id=%s channel_id=%s video_id=%s status=%s dir=%s files=%d dry_run=%s",
                        EVENT, c.workspace_id, c.id, v.id, v.status, d, len(files), dry)
            for f in files:
                logger.info("%s %s %s", EVENT, "would_delete" if dry else "delete", f)
            if not dry:
                try:
                    youtube_purge._remove_tree(d, files)
                except OSError as e:
                    logger.warning("%s video_id=%s not fully removed: %s", EVENT, v.id, type(e).__name__)
                    continue
            n += 1
    if n:
        logger.info("%s tick: %d folder(s) %s", EVENT, n, "would be deleted" if dry else "deleted")
    return n
