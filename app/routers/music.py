"""Background music pool management."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services import music_gen

router = APIRouter(prefix="/api/music", tags=["music"])


class GenerateBody(BaseModel):
    count: int = 1
    prompt: str | None = None


@router.get("")
def list_music():
    """List audio files in bgm_dir with name, size, and creation time."""
    bgm_dir = Path(settings.bgm_dir)
    tracks = music_gen.list_tracks(bgm_dir)
    return {"bgm_dir": str(bgm_dir), "count": len(tracks), "tracks": tracks}


@router.post("/generate")
def generate_music(body: GenerateBody):
    """Generate N new techno tracks via HuggingFace MusicGen.

    Each track is ~30s of procedurally-prompted techno music saved as a WAV
    file in bgm_dir, where the render pipeline picks them up automatically.
    """
    if not music_gen._token():
        raise HTTPException(503, "HF token not configured — set MANAGER_HF_TOKEN or HF_TOKEN")
    count = min(max(1, body.count), 20)
    bgm_dir = Path(settings.bgm_dir)

    import random
    files = []
    errors = []
    for _ in range(count):
        prompt = body.prompt or random.choice(music_gen.TECHNO_PROMPTS)
        try:
            out = music_gen.generate_and_save(prompt, bgm_dir)
            stat = out.stat()
            files.append({
                "name": out.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "prompt": prompt,
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
