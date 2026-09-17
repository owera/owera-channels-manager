"""Background music pool management."""

import random
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.config import settings
from app.services import music_gen

router = APIRouter(prefix="/api/music", tags=["music"])


class GenerateBody(BaseModel):
    count: int = 1
    style: str | None = None  # optional style description to filter presets

    @field_validator("count", mode="before")
    @classmethod
    def _reject_bool_count(cls, v):
        # Lax int coerces JSON false→0 / true→1 before the handler.
        # 0 then became a silent one-track generate via max(1, count).
        if isinstance(v, bool):
            raise ValueError("must be an integer >= 1, not a boolean")
        return v


def _require_int(fields: dict, key: str, minimum: int, hint: str) -> None:
    """Reject JSON null / bool / below-floor ints before they hit generate.

    Generate ``count<=0`` used to coerce 0 to 1 via ``max(1, body.count)``.
    JSON bools are rejected earlier by GenerateBody (lax int would
    coerce false→0 / true→1); the bool check here is defense in depth
    for non-HTTP callers.
    """
    if key not in fields:
        return
    v = fields[key]
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise HTTPException(400, hint)


def _style_pool(requested: str | None) -> list[dict]:
    """Presets to draw from. Unset/blank = all; else case-insensitive desc filter."""
    if requested is None or not requested.strip():
        return music_gen.TECHNO_STYLES
    needle = requested.strip().lower()
    matched = [s for s in music_gen.TECHNO_STYLES if needle in s["desc"].lower()]
    if not matched:
        raise HTTPException(400, f"unknown style {requested!r}")
    return matched


@router.get("")
def list_music():
    """List audio files in bgm_dir with name, size, and creation time."""
    bgm_dir = Path(settings.bgm_dir)
    tracks = music_gen.list_tracks(bgm_dir)
    return {"bgm_dir": str(bgm_dir), "count": len(tracks), "tracks": tracks}


@router.post("/generate")
def generate_music(body: GenerateBody):
    """Generate N new techno tracks via local procedural synthesis.

    Each track is ~30s of synthesised techno music saved as a WAV file in
    bgm_dir, where the render pipeline picks them up automatically.
    """
    # Empty/typed 0 (and a typed negative) used to silently generate one
    # track (the old floor coerced 0 up to 1, then capped at 20).
    # Growth-agent / curl still reach this path; the playbook hardcodes
    # a positive count.
    _require_int({"count": body.count}, "count", 1,
                 "count must be >= 1 "
                 "(0 was a silent one-track generate via max(1, count))")
    count = min(body.count, 20)
    bgm_dir = Path(settings.bgm_dir)
    pool = _style_pool(body.style)

    files = []
    errors = []
    for _ in range(count):
        style = random.choice(pool)
        try:
            out = music_gen.generate_and_save(style["desc"], bgm_dir)
            stat = out.stat()
            files.append({
                "name": out.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "style": style["desc"],
                "bpm": style["bpm"],
            })
        except Exception as e:
            errors.append(str(e))

    return {
        "generated": len(files),
        "files": files,
        "errors": errors,
        "bgm_dir": str(bgm_dir),
    }


@router.delete("/{filename}", status_code=204)
def delete_music(filename: str):
    """Remove a track from bgm_dir."""
    # Guard against path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    bgm_dir = Path(settings.bgm_dir)
    target = bgm_dir / filename
    # Resolve both and ensure target is inside bgm_dir
    try:
        target.resolve().relative_to(bgm_dir.resolve())
    except ValueError:
        raise HTTPException(400, "invalid filename")
    if not target.exists():
        raise HTTPException(404, "file not found")
    target.unlink()
