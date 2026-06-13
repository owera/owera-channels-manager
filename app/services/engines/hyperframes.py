"""HyperFrames engine adapter — local HTML/CSS -> MP4 motion-graphics renders.

HyperFrames (https://github.com/heygen-com/hyperframes) is a local CLI, not a task
queue, and it generates no content or audio of its own. So this adapter reproduces
MPT's async shape and supplies the missing pieces itself:

  submit()  spawns a daemon thread and returns immediately (the scheduler tick must
            never block); the thread runs the full pipeline and writes progress to a
            ``status.json`` the render loop polls.

  pipeline  subject --LLM--> narration script
                     --edge-tts--> narration.mp3 (+ duration)
                     --LLM--> index.html  (a HyperFrames composition, GSAP timeline)
                     --hyperframes render--> render.mp4 (silent)
                     --ffmpeg--> final.mp4 (narration + BGM muxed under the video)

The render command/format was pinned against hyperframes@0.6.97; see worker.py.
"""

import json
import threading
from pathlib import Path

from app.config import settings
from app.services.engines.base import STATE_PROCESSING
from app.services.engines.worker import run_job


def _job_dir(handle: str) -> Path:
    return Path(settings.hyperframes_storage_dir) / handle


def _status_path(handle: str) -> Path:
    return _job_dir(handle) / "status.json"


def write_status(handle: str, **fields) -> None:
    """Merge ``fields`` into a job's status.json (the poll contract surface)."""
    path = _status_path(handle)
    cur: dict = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text())
        except json.JSONDecodeError:
            cur = {}
    cur.update(fields)
    path.write_text(json.dumps(cur))


class HyperFramesEngine:
    name = "hyperframes"

    def submit(self, video, params: dict) -> str:
        # uuid is fine here (a plain adapter, unlike a deterministic Workflow script).
        from uuid import uuid4

        handle = uuid4().hex
        job_dir = _job_dir(handle)
        job_dir.mkdir(parents=True, exist_ok=True)
        write_status(handle, state=STATE_PROCESSING, progress=0, script=None, error=None)

        # Capture plain values; the worker thread must not touch the ORM/session.
        subject = video.subject
        t = threading.Thread(
            target=run_job,
            args=(handle, job_dir, subject, dict(params)),
            name=f"hyperframes-{handle[:8]}",
            daemon=True,
        )
        t.start()
        return handle

    def poll(self, handle: str) -> dict:
        path = _status_path(handle)
        if not path.exists():
            return {"state": STATE_PROCESSING, "progress": 0, "script": None}
        try:
            s = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {"state": STATE_PROCESSING, "progress": 0, "script": None}
        return {
            "state": s.get("state", STATE_PROCESSING),
            "progress": int(s.get("progress") or 0),
            "script": s.get("script"),
            "error": s.get("error"),
        }

    def final_path(self, handle: str) -> Path:
        return _job_dir(handle) / "final.mp4"
