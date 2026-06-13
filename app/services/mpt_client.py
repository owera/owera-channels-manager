"""HTTP client for the MoneyPrinterTurbo engine + VideoParams builder.

We drive MPT over its REST API (never import its internals) so the manager stays
decoupled and upgrade-safe, and gets MPT's task queue + progress polling for free.
"""

from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings

# MPT task states (app/models/const.py)
STATE_FAILED = -1
STATE_COMPLETE = 1
STATE_PROCESSING = 4

# Defaults that mirror the proven channel/produce.py settings. A render profile or
# per-topic override can replace any of these; subject always wins last.
DEFAULT_PARAMS: dict[str, Any] = {
    "video_language": "en-US",
    "video_source": "pexels",
    "video_aspect": "9:16",
    "video_count": 1,
    "voice_name": "en-US-AndrewNeural-Male",
    "subtitle_enabled": True,
    "paragraph_number": 2,
    "bgm_type": "random",          # user's own SoundCloud tracks in resource/songs/
    "bgm_volume": 0.2,
}


def build_video_params(subject: str, *param_layers: Optional[dict]) -> dict:
    """Merge default -> profile -> topic-overrides (left to right), then force subject."""
    merged: dict[str, Any] = dict(DEFAULT_PARAMS)
    for layer in param_layers:
        if layer:
            merged.update({k: v for k, v in layer.items() if v is not None})
    merged["video_subject"] = subject
    return merged


class MPTError(Exception):
    pass


class MPTClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        self.base_url = (base_url or settings.mpt_base_url).rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.api, timeout=self._timeout)

    def ping(self) -> bool:
        # MPT has no /ping route registered; the tasks list is a cheap liveness probe.
        try:
            with httpx.Client(base_url=self.api, timeout=5.0) as c:
                r = c.get("/tasks", params={"page": 1, "page_size": 1})
                return r.status_code == 200
        except Exception:
            return False

    def submit(self, params: dict) -> str:
        """POST /videos -> task_id."""
        with self._client() as c:
            r = c.post("/videos", json=params)
            r.raise_for_status()
            data = r.json().get("data") or {}
            task_id = data.get("task_id")
            if not task_id:
                raise MPTError(f"no task_id in response: {r.text[:300]}")
            return task_id

    def poll(self, task_id: str) -> dict:
        """GET /tasks/{id} -> task dict (state, progress, videos, script, ...)."""
        with self._client() as c:
            r = c.get(f"/tasks/{task_id}")
            r.raise_for_status()
            return r.json().get("data") or {}

    def social_metadata(self, subject: str, script: str, platform: str = "youtube_shorts",
                        language: str = "en-US") -> Optional[dict]:
        """POST /social-metadata -> {title, caption, hashtags} or None on failure."""
        try:
            with self._client() as c:
                r = c.post("/social-metadata", json={
                    "video_subject": subject,
                    "video_script": script,
                    "language": language,
                    "platform": platform,
                })
                r.raise_for_status()
                return r.json().get("data") or None
        except Exception:
            return None

    def local_final_path(self, task_id: str, index: int = 1) -> Path:
        """Final rendered file on disk — read directly, don't depend on returned URLs."""
        return Path(settings.mpt_storage_dir) / task_id / f"final-{index}.mp4"

    def list_musics(self) -> list[dict]:
        try:
            with self._client() as c:
                r = c.get("/musics")
                r.raise_for_status()
                return (r.json().get("data") or {}).get("files", [])
        except Exception:
            return []


mpt = MPTClient()
