"""Closed-list OS publish grandfather for Gate B PR #122.

The list is generated at deploy by ``scripts/gate_grandfather_snapshot.py``.
Only IDs on that list use the stored (old) craft_gate at publish, and only
when ``vm_pass`` matches the current master. Missing marker is not enough.
#1328 is always excluded. Expected list at this deploy: empty.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ALWAYS_EXCLUDE = frozenset({1328})
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "gate_grandfather.json"


def grandfather_path() -> Path:
    env = (os.environ.get("GATE_GRANDFATHER_PATH") or "").strip()
    return Path(env) if env else DEFAULT_PATH


def load_grandfather(path: Path | None = None) -> dict:
    p = path or grandfather_path()
    if not p.is_file():
        return {"ids": [], "excluded": sorted(ALWAYS_EXCLUDE), "generated_at": None}
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ids": [], "excluded": sorted(ALWAYS_EXCLUDE), "generated_at": None}
    return raw if isinstance(raw, dict) else {"ids": [], "excluded": sorted(ALWAYS_EXCLUDE)}


def grandfather_ids(path: Path | None = None) -> set[int]:
    raw = load_grandfather(path)
    out = set()
    for x in raw.get("ids") or []:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out - ALWAYS_EXCLUDE


def vm_pass_matches_master(video=None, *, creation_config=None,
                           video_path: str | None = None) -> bool:
    """True when vm_pass is a PASS bound to the **current render artifact**.

    ``vm_pass`` is not a git SHA. ``review_guard.vm_pass_of`` / ``set_vm_pass``
    bind it to ``video_path`` plus ``artifact`` = ``size:mtime_ns`` of that
    file (#108). A PASS recorded for another master (e.g. #1328 on sha
    ``adcdc31f…`` with a VM_FAIL) does not match and must not publish via
    the old gate. No path / no fingerprint proof → not grandfathered.
    """
    if video is not None:
        from app.services.review_guard import vm_pass_of
        return vm_pass_of(video) is not None
    cc = creation_config if isinstance(creation_config, dict) else {}
    rec = cc.get("vm_pass")
    if not isinstance(rec, dict) or rec.get("result") != "PASS":
        return False
    path_now = video_path
    if not path_now or rec.get("video_path") != path_now:
        return False
    if "artifact" in rec:
        from app.services.review_guard import artifact_fingerprint
        if rec.get("artifact") != artifact_fingerprint(path_now):
            return False
    return True


def park_os_held(video, reason) -> None:
    """Fail-closed OS: ``status=review`` + ``Video.held``. Never ``failed``."""
    from app.models import VideoStatus, utcnow

    video.status = VideoStatus.REVIEW
    video.approved_at = None
    video.held = True
    if getattr(video, "held_at", None) is None:
        video.held_at = utcnow()
    msg = str(reason or "held")
    video.error = msg if msg.startswith("held:") else f"held: {msg}"


def is_grandfathered(video_id, *, video=None, creation_config=None,
                     video_path: str | None = None,
                     path: Path | None = None) -> bool:
    """Closed list + current-master vm_pass. #1328 never qualifies."""
    try:
        vid = int(video_id)
    except (TypeError, ValueError):
        return False
    if vid in ALWAYS_EXCLUDE or vid not in grandfather_ids(path):
        return False
    cc = creation_config
    if cc is None and video is not None:
        raw = getattr(video, "creation_config", None)
        if isinstance(raw, dict):
            cc = raw
        elif isinstance(raw, str) and raw:
            try:
                cc = json.loads(raw)
            except json.JSONDecodeError:
                cc = {}
    path_now = video_path or (getattr(video, "video_path", None) if video is not None else None)
    return vm_pass_matches_master(video, creation_config=cc if isinstance(cc, dict) else {},
                                  video_path=path_now)
