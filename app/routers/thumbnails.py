"""Operator-provided thumbnail per video (Designer file replaces the template).

Settable until the upload starts (publishing/published → 409; not re-set on
YouTube). Stored as <storage_dir>/videos/<id>/thumb_provided.<png|jpg>; render
finalize never replaces it and publish uploads it instead of generating the
template card. See app/services/provided_thumb.py and docs/CRAFT.md.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app.db import get_session
from app.models import Topic, Video, VideoStatus, utcnow
from app.services import quota

router = APIRouter(prefix="/api/videos", tags=["thumbnails"])

_THUMB_LOCKED = {VideoStatus.PUBLISHING, VideoStatus.PUBLISHED}


def _thumb_target(session: Session, video_id: int) -> Video:
    v = session.get(Video, video_id)
    if not v:
        raise HTTPException(404, "video not found")
    if v.status in _THUMB_LOCKED:
        raise HTTPException(409, f"cannot change the thumbnail from status '{v.status}' "
                                 "(already uploading/published)")
    return v


@router.post("/{video_id}/thumbnail")
async def set_provided_thumbnail(video_id: int, request: Request,
                                 session: Session = Depends(get_session)):
    """Set an operator-provided thumbnail.

    - multipart/form-data with a `file` field (PNG or JPEG), or
    - JSON `{"path": "<file under the manager storage dir>"}`.
    """
    from app.services import provided_thumb as pt
    v = _thumb_target(session, video_id)
    ctype = (request.headers.get("content-type") or "").lower()
    source = ""
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            f = form.get("file")
            if f is None or not hasattr(f, "read"):
                raise pt.ThumbError("multipart upload needs a 'file' field")
            data = await f.read(pt.MAX_INPUT_BYTES + 1)
            source = f"upload {getattr(f, 'filename', '') or 'file'}"
        elif ctype.startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                raise pt.ThumbError("invalid JSON body")
            if not isinstance(body, dict) or "path" not in body:
                raise pt.ThumbError('JSON body must be {"path": "..."}')
            src = pt.resolve_media_path(body["path"])
            if src.stat().st_size > pt.MAX_INPUT_BYTES:
                raise pt.ThumbError("file too large", status=413)
            data = src.read_bytes()
            source = f"path {src}"
        else:
            raise pt.ThumbError("send multipart/form-data (field 'file') or "
                                'JSON {"path": ...}', status=415)
        topic = session.get(Topic, v.topic_id)
        fmt = "long" if topic and topic.content_format == "long" else "short"
        info = pt.validate(data, fmt)
    except pt.ThumbError as e:
        raise HTTPException(e.status, str(e))
    dest = pt.store(v, info)
    v.updated_at = utcnow()
    session.add(v)
    detail = (f"provided thumbnail set via API from {source}: {info['format']} "
              f"{info['width']}x{info['height']}, {info['stored_bytes']} bytes"
              + (f" ({info['conversion']} from {info['original_bytes']} bytes)"
                 if info["converted"] else "")
              + (f"; warnings: {'; '.join(info['warnings'])}" if info["warnings"] else ""))
    quota.log(session, kind="thumbnail_set", status="success", video_id=v.id,
              channel_id=v.channel_id, detail=detail)
    session.commit()
    session.refresh(v)
    return {"id": v.id, "status": v.status, "thumb_path": v.thumb_path,
            "thumb_source": pt.THUMB_SOURCE_PROVIDED, "format": info["format"],
            "width": info["width"], "height": info["height"],
            "bytes": info["stored_bytes"], "original_bytes": info["original_bytes"],
            "converted": info["converted"], "conversion": info["conversion"],
            "warnings": info["warnings"], "stored_at": str(dest)}


@router.delete("/{video_id}/thumbnail")
def clear_provided_thumbnail(video_id: int, session: Session = Depends(get_session)):
    """Clear the provided thumbnail; publish goes back to the template."""
    from app.services import provided_thumb as pt
    v = _thumb_target(session, video_id)
    had = pt.clear(v)
    v.updated_at = utcnow()
    session.add(v)
    quota.log(session, kind="thumbnail_set", status="success", video_id=v.id,
              channel_id=v.channel_id,
              detail=("provided thumbnail cleared via API" if had
                      else "clear provided thumbnail via API: none was set"))
    session.commit()
    session.refresh(v)
    return {"id": v.id, "status": v.status, "thumb_path": v.thumb_path,
            "thumb_source": None, "cleared": had}
