"""Generate techno background music via HuggingFace MusicGen.

Calls facebook/musicgen-small on the HF Inference API with randomised techno
prompts and saves the resulting WAV files into bgm_dir, where the existing
_pick_bgm() logic in worker.py can find and use them.

Token: set MANAGER_HF_TOKEN (or HF_TOKEN) in the environment / .env file.
If no token is configured, generation is skipped and a warning is logged.
"""

import logging
import os
import random
import time
from pathlib import Path

import requests

from app.config import settings
from app.db import session_scope
from app.services import quota

logger = logging.getLogger("manager.music_gen")

# Varied prompts covering different techno sub-genres and BPMs so the generated
# pool has sonic diversity. Keep this list long enough that repeated random picks
# rarely land on the same prompt.
TECHNO_PROMPTS = [
    "driving techno music 128 bpm, dark underground, four-on-the-floor kick drum",
    "minimal techno 130 bpm, hypnotic, Berlin warehouse style, stripped-back percussion",
    "acid techno 135 bpm, squelchy 303 bassline, industrial, relentless hi-hats",
    "deep techno 126 bpm, atmospheric pads, Detroit influence, slow-building tension",
    "hard techno 140 bpm, distorted kick, aggressive, festival main-stage energy",
    "dark techno 132 bpm, heavy bass rumble, industrial noise, dystopian atmosphere",
    "melodic techno 128 bpm, euphoric synth arpeggios, emotional build, festival anthem",
    "industrial techno 136 bpm, metallic percussion, harsh noise textures, relentless groove",
    "dub techno 124 bpm, spacious reverb, cavernous delays, hypnotic sub bass",
    "peak-time techno 138 bpm, pumping kick, stabs, relentless drive, peak floor energy",
    "minimal techno 125 bpm, late night, sparse kick, evolving filter sweeps",
    "techno 130 bpm, pulsing bass, analogue warmth, classic 909 drums",
    "EBM influenced techno 140 bpm, sequenced bass, harsh industrial drums",
    "progressive techno 128 bpm, slow filter opening, building tension, euphoric climax",
    "atmospheric techno 126 bpm, cinematic pads, distant vocals, emotional depth",
    "rave techno 138 bpm, early 90s style, raw energy, acidic synth lines",
    "rolling techno 132 bpm, looped groove, hypnotic percussion, subtle variations",
    "techno 133 bpm, reverbed claps, dark melody, underground club feel",
    "trance-influenced techno 138 bpm, soaring pads, driving kick, euphoric breakdown",
    "drone techno 120 bpm, deep low-end, minimal, meditative, slow evolving textures",
]

_HF_API_URL = "https://router.huggingface.co/hf-inference/models/facebook/musicgen-small"
_AUDIO_EXTS = {".mp3", ".m4a", ".wav"}


def _token() -> str:
    """Return HF token from settings (MANAGER_HF_TOKEN) or HF_TOKEN env fallback."""
    return settings.hf_token or os.getenv("HF_TOKEN", "")


def pool_count(bgm_dir: Path) -> int:
    """Count audio files in bgm_dir (same extensions as worker._pick_bgm)."""
    if not bgm_dir.exists():
        return 0
    return sum(1 for p in bgm_dir.iterdir() if p.suffix.lower() in _AUDIO_EXTS)


def list_tracks(bgm_dir: Path) -> list[dict]:
    """Return metadata for each audio file in bgm_dir, sorted by name."""
    if not bgm_dir.exists():
        return []
    result = []
    for p in sorted(bgm_dir.iterdir()):
        if p.suffix.lower() not in _AUDIO_EXTS:
            continue
        stat = p.stat()
        result.append({
            "name": p.name,
            "size_kb": round(stat.st_size / 1024, 1),
            "created": stat.st_ctime,
        })
    return result


def generate_track(prompt: str, duration_s: int = 30) -> bytes:
    """Call HF MusicGen and return raw WAV bytes.

    Raises RuntimeError if token is missing or the API call fails.
    """
    token = _token()
    if not token:
        raise RuntimeError(
            "HF token not configured — set MANAGER_HF_TOKEN or HF_TOKEN env var"
        )
    resp = requests.post(
        _HF_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"inputs": prompt, "parameters": {"duration": duration_s}},
        timeout=180,    # model cold-start can take 60–90s; generation another 30s
    )
    if resp.status_code == 503:
        # Model loading — wait and retry once
        logger.info("MusicGen model loading (503), waiting 30s then retrying")
        time.sleep(30)
        resp = requests.post(
            _HF_API_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"inputs": prompt, "parameters": {"duration": duration_s}},
            timeout=180,
        )
    if not resp.ok:
        raise RuntimeError(f"HF API error {resp.status_code}: {resp.text[:200]}")
    return resp.content


def generate_and_save(prompt: str, bgm_dir: Path, duration_s: int = 30) -> Path:
    """Generate one track and save it to bgm_dir. Returns the saved path."""
    bgm_dir.mkdir(parents=True, exist_ok=True)
    wav_bytes = generate_track(prompt, duration_s)
    slug = str(int(time.time() * 1000))
    out = bgm_dir / f"techno_{slug}.wav"
    out.write_bytes(wav_bytes)
    logger.info("saved generated track: %s (%d KB)", out.name, len(wav_bytes) // 1024)
    return out


def replenish(target: int | None = None) -> int:
    """Generate tracks until bgm_dir has `target` files.

    Returns the number of tracks generated. Skips gracefully if no token is set.
    """
    token = _token()
    if not token:
        logger.warning("music_gen: no HF token configured — skipping replenish")
        return 0

    bgm_dir = Path(settings.bgm_dir)
    target = target or settings.bgm_pool_target
    current = pool_count(bgm_dir)
    if current >= target:
        return 0

    need = target - current
    logger.info("BGM pool has %d tracks, target %d — generating %d", current, target, need)

    generated = 0
    for _ in range(need):
        prompt = random.choice(TECHNO_PROMPTS)
        try:
            out = generate_and_save(prompt, bgm_dir)
            with session_scope() as session:
                quota.log(session, kind="music_gen", status="success",
                          detail=f"generated {out.name}: {prompt[:80]}")
            generated += 1
        except Exception as e:
            logger.error("music_gen: track generation failed: %s", e)
            with session_scope() as session:
                quota.log(session, kind="music_gen", status="error",
                          detail=str(e)[:200])

    return generated
