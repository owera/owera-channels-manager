import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.config import MPT_DIR, settings
from app.db import get_session
from app.models import RenderProfile, utcnow
from app.schemas import ProfileCreate, ProfileUpdate
from app.services.mpt_client import DEFAULT_PARAMS, mpt

router = APIRouter(prefix="/api", tags=["profiles"])

_VOICES_JSON = MPT_DIR / "app" / "services" / "data" / "azure_voices.json"
_FONTS_DIR = MPT_DIR / "resource" / "fonts"
_FONT_SUFFIXES = (".ttf", ".ttc", ".otf")


@router.get("/profiles")
def list_profiles(channel_id: int | None = None, session: Session = Depends(get_session)):
    q = select(RenderProfile)
    if channel_id is not None:
        q = q.where((RenderProfile.channel_id == channel_id) | (RenderProfile.channel_id == None))  # noqa: E711
    return session.exec(q.order_by(RenderProfile.id)).all()


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileCreate, session: Session = Depends(get_session)):
    p = RenderProfile(name=body.name, channel_id=body.channel_id,
                      engine=body.engine or "mpt",
                      params_json=json.dumps(body.params or {}))
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, session: Session = Depends(get_session)):
    p = session.get(RenderProfile, profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    return p


@router.patch("/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileUpdate, session: Session = Depends(get_session)):
    p = session.get(RenderProfile, profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    if body.name is not None:
        p.name = body.name
    if body.engine is not None:
        p.engine = body.engine
    if body.params is not None:
        p.params_json = json.dumps(body.params)
    p.updated_at = utcnow()
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int, session: Session = Depends(get_session)):
    p = session.get(RenderProfile, profile_id)
    if p:
        session.delete(p)
        session.commit()


def _contained_font(name: str) -> Path | None:
    """A readable font file inside `_FONTS_DIR`, or None.

    `basename` + `exists()` handed `FileResponse` a directory (it raises
    `RuntimeError: ... is not a file`) and followed a symlink out of the
    fonts dir. Resolve the root and the candidate, then require a regular
    file that stays inside. The substring `..` is a legal font name
    (`foo..bar.ttf`); only a path that leaves the dir is refused.
    Containment is by path, so a hardlink inside the dir to an outside
    file still matches — creating that link already takes write access here.
    """
    if (not isinstance(name, str) or not name or name in (".", "..")
            or name != Path(name).name
            or "/" in name or "\\" in name or "\x00" in name):
        return None
    if Path(name).suffix.lower() not in _FONT_SUFFIXES:
        return None
    try:
        # Resolved root: on macOS /tmp → /private/tmp, and an unresolved
        # root rejects every file in a temp fonts dir.
        root = _FONTS_DIR.resolve()
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        if candidate.suffix.lower() not in _FONT_SUFFIXES:
            return None
        return candidate if os.access(candidate, os.R_OK) else None
    except (OSError, ValueError, RuntimeError):
        # An over-long segment or a symlink loop. A NUL is rejected above.
        return None


@router.get("/params/font/{name}")
def get_font(name: str):
    """Serve one font file so the profile editor can preview it."""
    p = _contained_font(name)
    if p is None:
        raise HTTPException(404, "font not found")
    ext = p.suffix.lower()
    media = {"ttf": "font/ttf", "otf": "font/otf", "ttc": "font/collection"}.get(
        ext[1:], "application/octet-stream")
    return FileResponse(p, media_type=media)


@router.get("/params/options")
def params_options():
    """Everything the Render Profile editor needs to populate its controls."""
    voices = []
    try:
        for v in json.loads(_VOICES_JSON.read_text()):
            voices.append(f"{v['name']}-{v['gender']}")
    except Exception:
        voices = ["en-US-AndrewNeural-Male", "en-US-AvaNeural-Female"]

    fonts = []
    try:
        if _FONTS_DIR.exists():
            # Same predicate as the preview, so a listed name is servable.
            fonts = sorted(
                p.name for p in _FONTS_DIR.iterdir()
                if _contained_font(p.name) is not None
            )
    except OSError:
        fonts = []

    # Read BGM files directly from the musicgen pool (same source as the render worker).
    bgm_dir = Path(settings.bgm_dir)
    bgm = sorted(
        p.name for p in bgm_dir.glob("*")
        if p.suffix.lower() in (".mp3", ".m4a", ".wav")
    ) if bgm_dir.exists() else []

    return {
        "defaults": DEFAULT_PARAMS,
        "video_aspect": ["9:16", "16:9", "1:1"],
        "video_concat_mode": ["random", "sequential"],
        "video_transition_mode": [None, "Shuffle", "FadeIn", "FadeOut", "SlideIn", "SlideOut"],
        "video_source": ["pexels", "pixabay", "coverr", "local"],
        "subtitle_position": ["bottom", "top", "center", "custom"],
        "bgm_type": ["random", ""],          # random = auto-pick from pool; "" = silence
        "voices": voices,
        "fonts": fonts or ["STHeitiMedium.ttc"],
        "bgm_files": bgm,
        "privacy": ["public", "unlisted", "private"],
        # The editable field surface (key -> control hint) for the form.
        "fields": {
            "video_language": "text", "video_source": "select",
            "video_aspect": "select", "video_concat_mode": "select",
            "video_transition_mode": "select", "video_clip_duration": "int",
            "paragraph_number": "int", "voice_name": "voice", "voice_rate": "float",
            "voice_volume": "float", "bgm_type": "select", "bgm_file": "bgm",
            "bgm_volume": "float", "subtitle_enabled": "bool",
            "subtitle_position": "select", "custom_position": "float",
            "font_name": "font", "font_size": "int", "text_fore_color": "color",
            "stroke_color": "color", "stroke_width": "float",
            "video_script_prompt": "textarea", "custom_system_prompt": "textarea",
        },
    }
