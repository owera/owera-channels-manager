"""Serve rendered video + thumbnail for the Review player (with Range support)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlmodel import Session

from app.db import get_session
from app.models import Video

router = APIRouter(prefix="/api/videos", tags=["media"])

_CHUNK = 1024 * 1024


@router.get("/{video_id}/video")
def stream_video(video_id: int, request: Request, session: Session = Depends(get_session)):
    t = session.get(Video, video_id)
    if not t or not t.video_path or not Path(t.video_path).exists():
        raise HTTPException(404, "video not found")
    path = Path(t.video_path)
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if range_header is None:
        return FileResponse(path, media_type="video/mp4")

    # Partial content for seeking.
    start_str = range_header.replace("bytes=", "").split("-")[0]
    end_str = range_header.replace("bytes=", "").split("-")[1] if "-" in range_header else ""
    start = int(start_str) if start_str else 0
    end = int(end_str) if end_str else file_size - 1
    end = min(end, file_size - 1)
    length = end - start + 1

    def iterfile():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(iterfile(), status_code=206, headers=headers, media_type="video/mp4")


@router.get("/{video_id}/thumb")
def thumb(video_id: int, session: Session = Depends(get_session)):
    t = session.get(Video, video_id)
    if not t or not t.thumb_path or not Path(t.thumb_path).exists():
        return Response(status_code=404)
    return FileResponse(t.thumb_path, media_type="image/jpeg")
