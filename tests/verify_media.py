"""Regression checks for the Review/Board video+thumb media routes.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_media.py

``GET /api/videos/{id}/video`` is what the Review player and Board modal
``<video>`` tags hit. Browsers always send ``Range`` for seeking. A hand-rolled
parser 500'd on malformed ranges (``bytes=abc`` → ``int()``) and treated
suffix ranges (``bytes=-N``) as a prefix starting at 0, so a seek could 500
the player or return the wrong bytes. Starlette's ``FileResponse`` already
implements RFC 7233 (206 / 416 / 400); these checks pin that the route
delegates to it instead of re-parsing.

Uses an in-memory SQLite DB, a throwaway media dir, and FastAPI's TestClient
(no real manager.db, no network, no storage/). Exits non-zero on the first
failed assertion.
"""
from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Video, VideoStatus
from app.routers import media as media_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# Isolated-copy batteries pin this so a stale pyc / wrong PYTHONPATH cannot
# silently test a different checkout (08-01 lesson).
ok(Path(media_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "media module loaded from this tree")

_TMP = Path(tempfile.mkdtemp(prefix="verify-media-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
_PAYLOAD = bytes(range(256))  # unique bytes so a wrong slice is obvious
_VIDEO = _TMP / "video.mp4"
_BIN = _TMP / "clip.bin"  # no .mp4 suffix — FileResponse's guess would not be video/mp4
_THUMB = _TMP / "thumb.jpg"
_VIDEO.write_bytes(_PAYLOAD)
_BIN.write_bytes(_PAYLOAD)
_THUMB.write_bytes(b"\xff\xd8fake-jpeg")  # not a real JPEG; we only serve bytes

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Video(channel_id=1, topic_id=1, subject="has-media",
                status=VideoStatus.REVIEW,
                video_path=str(_VIDEO), thumb_path=str(_THUMB)))          # id 1
    s.add(Video(channel_id=1, topic_id=1, subject="no-files",
                status=VideoStatus.REVIEW,
                video_path=None, thumb_path=None))                        # id 2
    s.add(Video(channel_id=1, topic_id=1, subject="missing-files",
                status=VideoStatus.REVIEW,
                video_path=str(_TMP / "gone.mp4"),
                thumb_path=str(_TMP / "gone.jpg")))                       # id 3
    s.add(Video(channel_id=1, topic_id=1, subject="bin-suffix",
                status=VideoStatus.REVIEW,
                video_path=str(_BIN), thumb_path=None))                   # id 4
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")


def get_video(vid, **kw):
    return client.get(f"/api/videos/{vid}/video", auth=auth, **kw)


def get_thumb(vid, **kw):
    return client.get(f"/api/videos/{vid}/thumb", auth=auth, **kw)


try:
    print("auth + 404s")
    r = client.get("/api/videos/1/video")
    ok(r.status_code == 401, "video stream still requires auth")
    r = client.get("/api/videos/1/thumb")
    ok(r.status_code == 401, "thumb still requires auth")

    r = get_video(999)
    ok(r.status_code == 404, "unknown video id is 404")
    r = get_video(2)
    ok(r.status_code == 404, "video with no video_path is 404")
    r = get_video(3)
    ok(r.status_code == 404, "video_path pointing at a missing file is 404")

    r = get_thumb(999)
    ok(r.status_code == 404, "unknown id thumb is 404")
    r = get_thumb(2)
    ok(r.status_code == 404, "video with no thumb_path is 404")
    r = get_thumb(3)
    ok(r.status_code == 404, "thumb_path pointing at a missing file is 404")

    print("full-file GET (no Range)")
    r = get_video(1)
    ok(r.status_code == 200, "no Range returns 200")
    ok(r.content == _PAYLOAD, "no Range body is the whole file")
    ok(r.headers.get("content-type", "").startswith("video/mp4"),
       "no Range content-type is video/mp4")
    ok(r.headers.get("accept-ranges", "").lower() == "bytes",
       "no Range advertises Accept-Ranges: bytes (so the player can seek)")
    ok(int(r.headers.get("content-length", "-1")) == len(_PAYLOAD),
       "no Range Content-Length is the file size")

    print("valid Range → 206")
    r = get_video(1, headers={"Range": "bytes=0-15"})
    ok(r.status_code == 206, "bytes=0-15 is 206 Partial Content")
    ok(r.content == _PAYLOAD[0:16], "bytes=0-15 body is the first 16 bytes")
    ok("bytes 0-15/256" in r.headers.get("content-range", ""),
       "Content-Range is bytes 0-15/256")
    ok(int(r.headers.get("content-length", "-1")) == 16,
       "partial Content-Length is 16")

    r = get_video(1, headers={"Range": "bytes=240-"})
    ok(r.status_code == 206, "bytes=240- (open end) is 206")
    ok(r.content == _PAYLOAD[240:], "bytes=240- body is the tail")
    ok("bytes 240-255/256" in r.headers.get("content-range", ""),
       "open-end Content-Range is bytes 240-255/256")

    print("suffix Range (bytes=-N = last N)")
    r = get_video(1, headers={"Range": "bytes=-16"})
    ok(r.content == _PAYLOAD[-16:],
       "bytes=-16 body is the LAST 16 bytes (pre-fix treated empty-start as 0)")
    ok("bytes 240-255/256" in r.headers.get("content-range", ""),
       "suffix Content-Range is bytes 240-255/256")

    print("malformed / unsatisfiable Range must not 500")
    r = get_video(1, headers={"Range": "bytes=abc"})
    ok(r.status_code == 400,
       "malformed bytes=abc is 400, not 500 (pre-fix int() ValueError)")

    r = get_video(1, headers={"Range": "not-a-range"})
    ok(r.status_code == 400, "Range without bytes= unit is 400, not 500")

    r = get_video(1, headers={"Range": "bytes=9999-"})
    ok(r.status_code == 416,
       "unsatisfiable bytes=9999- is 416 Range Not Satisfiable (pre-fix 206)")
    cr = r.headers.get("content-range", "")
    ok("256" in cr and "*" in cr,
       "416 Content-Range is bytes */256 so the player can recover")

    r = get_video(1, headers={"Range": "bytes=200-10"})
    ok(r.status_code == 400,
       "start>end Range is 400 malformed, not a negative-length 206")

    print("explicit video/mp4 (not guessed from suffix)")
    r = get_video(4)
    ok(r.status_code == 200, "non-.mp4 video_path still 200")
    ok(r.content == _PAYLOAD, "non-.mp4 video_path still serves the file")
    ok(r.headers.get("content-type", "").startswith("video/mp4"),
       "content-type stays video/mp4 even when video_path is clip.bin "
       "(FileResponse's suffix guess would not)")

    print("thumb")
    r = get_thumb(1)
    ok(r.status_code == 200, "present thumb is 200")
    ok(r.content == b"\xff\xd8fake-jpeg", "thumb body is the file bytes")
    ok(r.headers.get("content-type", "").startswith("image/jpeg"),
       "thumb content-type is image/jpeg")

    # Wiring pin: a spa()-class regression that never calls FileResponse
    # (hand-rolled StreamingResponse for every Range) 500s on bytes=abc.
    # Re-read the source so a reimplementation that keeps the int() parse
    # dies even if a future Starlette change made TestClient swallow it.
    src = Path(media_router.__file__).read_text()
    ok("StreamingResponse" not in src,
       "media.py no longer hand-rolls StreamingResponse (FileResponse owns Range)")
    ok("int(start_str)" not in src and "replace(\"bytes=\"" not in src,
       "media.py no longer parses the Range header itself")
    ok("Request" not in src and "range_header" not in src,
       "stream_video no longer takes Request / reads a range_header (pre-fix did both)")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
