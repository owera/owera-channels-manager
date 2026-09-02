"""Serve rendered video + thumbnail for the Review player (with Range support)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlmodel import Session

from app.db import get_session
from app.models import Video

router = APIRouter(prefix="/api/videos", tags=["media"])


@router.get("/{video_id}/video")
def stream_video(video_id: int, session: Session = Depends(get_session)):
    t = session.get(Video, video_id)
    if not t or not t.video_path or not Path(t.video_path).exists():
        raise HTTPException(404, "video not found")
    # FileResponse owns Range (RFC 7233); do not re-parse the header.
    return FileResponse(t.video_path, media_type="video/mp4")


@router.get("/{video_id}/thumb")
def thumb(video_id: int, session: Session = Depends(get_session)):
    t = session.get(Video, video_id)
    if not t or not t.thumb_path or not Path(t.thumb_path).exists():
        return Response(status_code=404)
    return FileResponse(t.thumb_path, media_type="image/jpeg")
