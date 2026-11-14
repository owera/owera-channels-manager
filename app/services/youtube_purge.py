"""P3: delete a channel's YouTube API data after access is lost.

YouTube API Services Developer Policies and the Google API Services User
Data Policy, as applied by LegalOps cláusula 7 v2:

- Disconnect in the app (revocation via the app's own mechanism): delete
  within 7 calendar days (YT III.D.2.3.a).
- User asks us to delete their data, or the workspace is closed: delete
  within 7 calendar days (YT III.E.4.g).
- Revocation on Google's security-settings page, or a token that will not
  refresh (RefreshError / invalid_grant): delete within 30 days (YT III.D.2.3.b).

Clock: ``Channel.access_lost_at`` (naive UTC), stamped by
- ``notify.mark_operator_disconnect`` — disconnect in the app (reason
  "disconnected", 7 days);
- ``notify.mark_deletion_requested`` — user asked us to delete (reason
  "deletion_requested", 7 days);
- ``notify.mark_workspace_closed`` — workspace closed (reason
  "workspace_closed", 7 days);
- ``notify.mark_dead`` — a refresh that failed with ``invalid_grant``
  (revoked / expired at Google; reason "invalid_grant", 30 days), or any
  other EXPIRED/DISCONNECTED flip (P3b; reason "expired"/"disconnected",
  30 days);
- ``backfill_lost_access`` (each tick) — channels already dead before this
  existed: clock starts at the first tick after deploy (P3b, 30 days).
A reconnect (``notify.mark_connected``) clears it and cancels the purge.

The scheduler runs ``tick`` every 6 h. A channel is purged once
``access_lost_at + retention`` has been reached (7 or 30 calendar days
depending on the reason), so a tick at exactly that instant deletes the
data. Idempotent: ``youtube_data_purged_at`` >= ``access_lost_at`` means
already purged; re-running deletes nothing more.

Deleted (YouTube API data only):
- ``ChannelMetric`` rows (subscriber/view/video counts from channels.list);
- ``VideoMetric`` rows (YouTube Analytics, including ``traffic_json`` search terms);
- the stored OAuth credential (encrypted row and any legacy token files);
- ``Channel.yt_channel_title``, and ``Channel.name`` when it is still the
  YouTube title (reset to the slug).

S2 (PO CM / LegalOps 14/11): scripts and storyboards are derived from Google
data. When the loss reason is a deletion request or a workspace closure
(``DERIVED_PURGE_REASONS``, 7 days) the same purge also clears, for every video
of the channel, ``Video.script``, ``Video.creation_config`` (the storyboard
snapshot) and the render job's text files (``index.html`` = the storyboard,
``narration_words.json``, the ``script`` key of ``status.json``). A plain
disconnect keeps the draft (only the YouTube API data below goes).

PO CM 14/11 (round 3): for the same two reasons ``purge_media`` also deletes the
render media of NOT-published drafts (video output dir + render job dir: mp4,
narration audio, thumbnails, other artifacts). Published videos and YouTube are
never touched; safe path checks, plan logged first, ``MANAGER_PURGE_MEDIA_DRY_RUN``.

Kept (not API data the user can revoke, or not YouTube data at all): the
workspace, the channel row and its settings, topics, scripts (except as above), renders and
``Video`` rows (our own production records, including the ``yt_video_id`` of
videos CM uploaded), ``Playlist`` rows CM created, and ``yt_channel_id``
(the identity binding that stops a reconnect to the wrong channel).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlmodel import Session, select

from app.models import Channel, ChannelMetric, OAuthCredential, OAuthStatus, VideoMetric, utcnow

logger = logging.getLogger("manager.youtube_purge")

# LegalOps cláusula 7 v2 / YT III.D.2.3.a and III.E.4.g vs III.D.2.3.b.
RETENTION_DAYS_USER = 7
RETENTION_DAYS_GOOGLE = 30
RETENTION_DAYS = RETENTION_DAYS_GOOGLE  # backward-compatible alias (30-day path)
TICK_HOURS = 6


def retention_delta(reason: str | None) -> timedelta:
    """How long stored YouTube API data may be kept after this loss reason."""
    from app.services.notify import USER_PURGE_REASONS

    if reason in USER_PURGE_REASONS:
        return timedelta(days=RETENTION_DAYS_USER)
    return timedelta(days=RETENTION_DAYS_GOOGLE)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def is_due(channel: Channel, now: datetime) -> bool:
    """True when the channel's purge deadline has been reached and it is not connected."""
    if channel.access_lost_at is None or channel.oauth_status == OAuthStatus.CONNECTED:
        return False
    lost = _naive(channel.access_lost_at)
    if channel.youtube_data_purged_at is not None and _naive(channel.youtube_data_purged_at) >= lost:
        return False
    return lost + retention_delta(channel.access_lost_reason) <= _naive(now)


def due_channels(session: Session, now: datetime | None = None) -> list[Channel]:
    now = _naive(now or utcnow())
    rows = session.exec(
        select(Channel).where(
            Channel.access_lost_at != None,  # noqa: E711
            Channel.oauth_status != OAuthStatus.CONNECTED,
        )
    ).all()
    return [c for c in rows if is_due(c, now)]


def _derived_reasons() -> frozenset[str]:
    from app.services.notify import REASON_DELETION_REQUESTED, REASON_WORKSPACE_CLOSED
    return frozenset({REASON_DELETION_REQUESTED, REASON_WORKSPACE_CLOSED})


def _purge_job_text(handle: str | None) -> int:
    """Remove the script / storyboard text a render job left on disk."""
    import json
    from pathlib import Path

    from app.config import settings
    if not handle or "/" in handle or ".." in handle:
        return 0
    base = getattr(settings, "hyperframes_storage_dir", "") or ""
    if not base:
        return 0
    d = Path(base) / handle
    n = 0
    for name in ("index.html", "narration_words.json"):
        f = d / name
        if f.is_file():
            f.unlink()
            n += 1
    st = d / "status.json"
    if st.is_file():
        try:
            data = json.loads(st.read_text())
            if isinstance(data, dict) and "script" in data:
                data.pop("script", None)
                st.write_text(json.dumps(data))
                n += 1
        except (OSError, ValueError):
            pass
    return n


def purge_derived(session: Session, channel: Channel) -> dict:
    """Clear scripts + storyboards of the channel's videos (no commit)."""
    from app.models import Video
    n_v = n_f = 0
    for v in session.exec(select(Video).where(Video.channel_id == channel.id)).all():
        if v.script is not None or v.creation_config is not None:
            v.script = None
            v.creation_config = None
            session.add(v)
            n_v += 1
        try:
            n_f += _purge_job_text(v.mpt_task_id)
        except OSError as e:
            logger.warning("purge: job text for video id=%s not removed: %s", v.id, type(e).__name__)
    return {"derived_videos": n_v, "derived_files": n_f}


# ---- render media of NOT-published drafts (PO CM 14/11, round 3) -------------------
#
# On a deletion request / workspace closure the same 7-day purge also deletes the
# render artifacts of drafts that were never published: the video output dir
# ``<storage_dir>/videos/<video_id>/`` (video.mp4, thumb.jpg, provided thumbnail…),
# the render job dir ``<hyperframes_storage_dir>/<handle>/`` (final.mp4,
# narration.mp3, …) and the MPT task folder ``<mpt_storage_dir>/<task_id>/``. Published videos (status PUBLISHED, yt_video_id or
# published_at set) are never touched, nor anything on YouTube. Every planned
# deletion is logged first (``youtube_purge_media plan``); with
# ``MANAGER_PURGE_MEDIA_DRY_RUN=1`` nothing is deleted.

MEDIA_EVENT = "youtube_purge_media"
_HANDLE_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def is_published(video) -> bool:
    from app.models import VideoStatus
    return (video.status == VideoStatus.PUBLISHED or bool(video.yt_video_id)
            or video.published_at is not None)


def _safe_dir(root, name: str):
    """``root/name`` only if ``name`` is one plain path segment and the resolved
    directory is a direct child of the resolved root (no traversal, no symlink)."""
    from pathlib import Path
    if not root or not name or not _HANDLE_RE.match(name) or name in (".", ".."):
        return None
    r = Path(root)
    if not r.is_dir() or r.is_symlink():
        return None
    d = r / name
    if not d.exists():
        return None
    if d.is_symlink() or not d.is_dir():
        return None
    rr, dr = r.resolve(), d.resolve()
    if dr.parent != rr:
        return None
    return dr


def _media_files(d) -> list:
    """Entries under ``d`` to unlink: regular files inside ``d`` and symlinks
    (the link itself is removed, its target is never followed or touched)."""
    import os
    from pathlib import Path
    out = []
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        for x in list(dirnames):
            if (Path(dirpath) / x).is_symlink():      # a symlinked sub-dir: drop the link only
                out.append(Path(dirpath) / x)
                dirnames.remove(x)
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink():
                out.append(p)
                continue
            if not p.is_file() or d not in p.resolve().parents:
                continue
            out.append(p)
    return out


def _remove_tree(d, files: list) -> None:
    import os
    for p in files:
        p.unlink()
    for dirpath, _dirs, _files in os.walk(d, topdown=False, followlinks=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def media_targets(session: Session, channel: Channel, video) -> list:
    """Directories of ``video`` that the media purge may delete (empty if none / unsafe)."""
    from app.config import settings
    from app.models import Video
    if video.channel_id != channel.id or is_published(video) or video.id is None:
        return []
    dirs = []
    vd = _safe_dir(str(__import__("pathlib").Path(settings.storage_dir) / "videos"), str(int(video.id)))
    if vd is not None:
        dirs.append(vd)
    h = video.mpt_task_id
    if h:
        # a job dir shared with any other video (other channel / workspace) is never touched
        shared = session.exec(select(Video.id).where(Video.mpt_task_id == h, Video.id != video.id)).first()
        if shared is None:
            # HyperFrames job dir and (PO CM round 4) the MPT task folder, same guards.
            for root in (getattr(settings, "hyperframes_storage_dir", ""),
                         getattr(settings, "mpt_storage_dir", "")):
                jd = _safe_dir(root, h)
                if jd is not None and jd not in dirs:
                    dirs.append(jd)
    return dirs


def purge_media(session: Session, channel: Channel, *, dry_run: bool | None = None) -> dict:
    """Delete render media of the channel's NOT-published drafts (no commit).

    Only for ``DERIVED_PURGE_REASONS`` (the channel's own workspace asked for
    deletion or was closed); internal OS/RR channels get here only through that
    same request on their own workspace."""
    from app.config import settings
    from app.models import Video
    if channel.access_lost_reason not in _derived_reasons():
        return {"media_videos": 0, "media_files": 0, "media_dry_run": False}
    dry = bool(settings.purge_media_dry_run) if dry_run is None else bool(dry_run)
    n_v = n_f = kept = 0
    for v in session.exec(select(Video).where(Video.channel_id == channel.id)).all():
        if is_published(v):
            kept += 1
            continue
        dirs = media_targets(session, channel, v)
        files = [f for d in dirs for f in _media_files(d)]
        if not files and not dirs:
            continue
        logger.info("%s plan workspace_id=%s channel_id=%s video_id=%s files=%d dirs=%s dry_run=%s",
                    MEDIA_EVENT, channel.workspace_id, channel.id, v.id, len(files),
                    ",".join(str(d) for d in dirs), dry)
        for f in files:
            logger.info("%s %s %s", MEDIA_EVENT, "would_delete" if dry else "delete", f)
        if dry:
            n_f += len(files)
            n_v += 1
            continue
        try:
            for d in dirs:
                _remove_tree(d, [f for f in files if d in f.parents])
        except OSError as e:
            logger.warning("%s video_id=%s not fully removed: %s", MEDIA_EVENT, v.id, type(e).__name__)
        n_f += len(files)
        n_v += 1
        if v.video_path or v.thumb_path:
            v.video_path = None
            v.thumb_path = None
            session.add(v)
    return {"media_videos": n_v, "media_files": n_f, "media_published_kept": kept,
            "media_dry_run": dry}


def purge_channel(session: Session, channel: Channel, now: datetime | None = None) -> dict:
    """Delete one channel's YouTube API data and stamp youtube_data_purged_at.
    Commits. Returns row counts for the log line (no names, no tokens)."""
    from app.services import youtube

    now = _naive(now or utcnow())
    cid = channel.id
    n_cm = session.exec(delete(ChannelMetric).where(ChannelMetric.channel_id == cid)).rowcount
    n_vm = session.exec(delete(VideoMetric).where(VideoMetric.channel_id == cid)).rowcount
    cred = session.get(OAuthCredential, cid)
    if cred is not None:
        session.delete(cred)
    try:
        youtube.disconnect(channel.slug)          # legacy token.json / .bak / tmp
    except OSError as e:
        logger.warning("purge: token files for channel id=%s not removed: %s", cid, type(e).__name__)
    if channel.yt_channel_title and channel.name == channel.yt_channel_title:
        channel.name = channel.slug
    channel.yt_channel_title = None
    derived = None
    if channel.access_lost_reason in _derived_reasons():
        derived = purge_media(session, channel)          # media first (needs mpt_task_id)
        derived.update(purge_derived(session, channel))
    channel.youtube_data_purged_at = now
    channel.updated_at = now
    session.add(channel)
    session.commit()
    counts = {"channel_metrics": int(n_cm or 0), "video_metrics": int(n_vm or 0),
              "credential": cred is not None}
    if derived is not None:
        counts.update(derived)
    logger.info("purged YouTube data for channel id=%s workspace=%s (lost %s, reason=%s): %s",
                cid, channel.workspace_id, channel.access_lost_at, channel.access_lost_reason, counts)
    return counts


def backfill_lost_access(session: Session, now: datetime | None = None) -> int:
    """P3b: channels that were already dead before access_lost_at existed get a
    clock starting now (conservative: the full 30 days from this deploy).

    Covers EXPIRED channels, and DISCONNECTED channels that still hold YouTube
    metrics (a never-connected onboarding channel has none and is skipped).
    Idempotent: only rows with access_lost_at NULL are touched. Commits."""
    now = _naive(now or utcnow())
    rows = session.exec(
        select(Channel).where(
            Channel.access_lost_at == None,  # noqa: E711
            Channel.oauth_status.in_([OAuthStatus.EXPIRED, OAuthStatus.DISCONNECTED]),
        )
    ).all()
    n = 0
    for ch in rows:
        if ch.oauth_status == OAuthStatus.DISCONNECTED:
            has_data = (
                session.exec(select(ChannelMetric.id).where(ChannelMetric.channel_id == ch.id)).first()
                or session.exec(select(VideoMetric.id).where(VideoMetric.channel_id == ch.id)).first()
                or session.get(OAuthCredential, ch.id) is not None
            )
            if not has_data:
                continue
        ch.access_lost_at = now
        ch.access_lost_reason = f"{ch.oauth_status}_backfill"
        session.add(ch)
        n += 1
    if n:
        session.commit()
        logger.info("purge clock backfilled for %d already-dead channel(s)", n)
    return n


def tick(now: datetime | None = None) -> int:
    """Scheduler entry point. Returns how many channels were purged."""
    from app.db import session_scope

    n = 0
    with session_scope() as session:
        backfill_lost_access(session, now)
        for ch in due_channels(session, now):
            try:
                purge_channel(session, ch, now)
                n += 1
            except Exception:
                session.rollback()
                logger.exception("purge failed for channel id=%s", ch.id)
    return n
