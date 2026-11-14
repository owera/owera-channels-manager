import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.config import MPT_DIR, settings
from app.db import get_session
from app.models import Channel, RenderProfile, Workspace, utcnow
from app.schemas import ProfileCreate, ProfileUpdate
from app.services.mpt_client import DEFAULT_PARAMS, mpt
from app.workspace import (current_workspace, is_admin_workspace, require_channel,
                           require_profile)

router = APIRouter(prefix="/api", tags=["profiles"])

_VOICES_JSON = MPT_DIR / "app" / "services" / "data" / "azure_voices.json"
_FONTS_DIR = MPT_DIR / "resource" / "fonts"


@router.get("/profiles")
def list_profiles(channel_id: int | None = None, session: Session = Depends(get_session),
                  ws: Workspace = Depends(current_workspace)):
    q = select(RenderProfile)
    if channel_id is not None:
        require_channel(session, ws, channel_id)   # S4: foreign channel → 404
        q = q.where((RenderProfile.channel_id == channel_id) | (RenderProfile.channel_id == None))  # noqa: E711
    else:
        # S4: shared (channel-less) profiles + this workspace's channels only.
        own = select(Channel.id).where(Channel.workspace_id == ws.id)
        q = q.where((RenderProfile.channel_id == None) | (RenderProfile.channel_id.in_(own)))  # noqa: E711
    return session.exec(q.order_by(RenderProfile.id)).all()


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileCreate, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace)):
    if body.channel_id is not None:
        require_channel(session, ws, body.channel_id)   # S4: foreign channel → 404
    elif not is_admin_workspace(ws):
        # Shared profiles show up in every workspace: only the admin creates them.
        raise HTTPException(400, "channel_id is required")
    p = RenderProfile(name=body.name, channel_id=body.channel_id,
                      engine=body.engine or "mpt",
                      params_json=json.dumps(body.params or {}))
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: int, session: Session = Depends(get_session),
                ws: Workspace = Depends(current_workspace)):
    p = require_profile(session, ws, profile_id)
    return p


@router.patch("/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileUpdate, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace)):
    p = require_profile(session, ws, profile_id, write=True)
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
def delete_profile(profile_id: int, session: Session = Depends(get_session),
                   ws: Workspace = Depends(current_workspace)):
    # S4: missing / foreign / shared-for-a-buyer all answer the same 404
    # (was: missing → silent 204).
    p = require_profile(session, ws, profile_id, write=True)
    session.delete(p)
    session.commit()


@router.get("/params/font/{name}")
def get_font(name: str):
    """Serve a font file so the profile editor can preview it (WYSIWYG)."""
    safe = os.path.basename(name)
    p = _FONTS_DIR / safe
    if not p.exists():
        raise HTTPException(404, "font not found")
    ext = p.suffix.lower()
    media = {"ttf": "font/ttf", "otf": "font/otf", "ttc": "font/collection"}.get(ext[1:], "application/octet-stream")
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
    if _FONTS_DIR.exists():
        fonts = sorted(p.name for p in _FONTS_DIR.iterdir()
                       if p.suffix.lower() in (".ttf", ".ttc", ".otf"))

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
