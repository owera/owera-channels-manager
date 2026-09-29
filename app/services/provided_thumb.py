"""Operator-provided thumbnail per video.

A designer's thumbnail replaces the auto one for a single video:

- it is stored next to the render in `<storage_dir>/videos/<id>/thumb_provided.<png|jpg>`
  and `Video.thumb_path` points at it. The file name is the marker of record
  (`is_provided`), so it survives anything that rewrites `creation_config`.
  `creation_config.thumb_source="provided"` + `thumb_provided_path` are also written
  for visibility and carried forward by finalize.
- render finalize never replaces it with the 1s ffmpeg still.
- publish uploads it with thumbnails.set and skips template generation.

Validation (YouTube thumbnails.set: image/jpeg or image/png, 2 MB max):
- PNG or JPEG only, detected from the file bytes (not the name or content-type).
- long side >= 640 px and short side >= 360 px.
- over 2 MB -> re-encoded to JPEG with ffmpeg (quality steps, then scaled so the
  long side is 1280 px). Still over 2 MB -> rejected. Under 2 MB is stored as-is.
- Portrait (e.g. 1080x1920) is accepted as-is: the template thumbnails this
  app already uploads for Shorts are 720x1280 portrait PNGs, and thumbnails.set
  has accepted every one on both channels. An aspect that doesn't match the
  video's format only produces a warning.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings

PROVIDED_STEM = "thumb_provided"
THUMB_SOURCE_PROVIDED = "provided"
YT_THUMB_MAX_BYTES = 2 * 1024 * 1024          # thumbnails.set limit
MAX_INPUT_BYTES = 25 * 1024 * 1024            # refuse absurd uploads before decoding
MIN_LONG_SIDE = 640
MIN_SHORT_SIDE = 360
_JPEG_QUALITIES = (2, 4, 6, 9, 12)            # ffmpeg -q:v (lower = better)
_SCALED_LONG_SIDE = 1280

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class ThumbError(ValueError):
    """Validation failure → HTTP 400/413 at the API layer."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def video_dir(video_id: int) -> Path:
    return Path(settings.storage_dir) / "videos" / str(video_id)


def media_root() -> Path:
    return Path(settings.storage_dir).resolve()


def is_provided(video) -> bool:
    """True when the video's thumb_path is an operator-provided file."""
    tp = getattr(video, "thumb_path", None)
    return bool(tp) and Path(tp).name.startswith(PROVIDED_STEM + ".")


def provided_path(video) -> Path | None:
    """The provided thumbnail file, if set and still on disk."""
    if not is_provided(video):
        return None
    p = Path(video.thumb_path)
    return p if p.is_file() else None


def sniff(data: bytes) -> tuple[str, int, int]:
    """("png"|"jpeg", width, height) from the bytes. Raises ThumbError."""
    if data[:8] == _PNG_SIG:
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ThumbError("corrupt PNG (no IHDR)")
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return "png", w, h
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 3 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:              # fill byte
                i += 1
                continue
            if marker in (0x01,) or 0xD0 <= marker <= 0xD9:
                i += 2                      # standalone marker, no length
                continue
            seglen = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in _SOF_MARKERS and i + 9 <= n:
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return "jpeg", w, h
            if seglen < 2:
                break
            i += 2 + seglen
        raise ThumbError("corrupt JPEG (no frame header)")
    raise ThumbError("unsupported image type — PNG or JPEG only")


def _ffmpeg_jpeg(src: Path, out: Path, q: int, long_side: int | None, portrait: bool) -> bool:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if long_side:
        cmd += ["-vf", f"scale=-2:{long_side}" if portrait else f"scale={long_side}:-2"]
    cmd += ["-frames:v", "1", "-q:v", str(q), str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
    except Exception:
        return False
    return out.is_file()


def _compress(data: bytes, w: int, h: int) -> tuple[bytes, str]:
    """Re-encode an over-limit image to JPEG ≤ 2 MB. Returns (bytes, how)."""
    if not shutil.which("ffmpeg"):
        raise ThumbError("image is over YouTube's 2 MB thumbnail limit and ffmpeg "
                         "is not available to compress it — upload a smaller file")
    portrait = h > w
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.img"
        src.write_bytes(data)
        steps = [(q, None) for q in _JPEG_QUALITIES]
        if max(w, h) > _SCALED_LONG_SIDE:
            steps += [(q, _SCALED_LONG_SIDE) for q in _JPEG_QUALITIES]
        for q, long_side in steps:
            out = Path(td) / f"out_{q}_{long_side or 0}.jpg"
            if _ffmpeg_jpeg(src, out, q, long_side, portrait):
                blob = out.read_bytes()
                if len(blob) <= YT_THUMB_MAX_BYTES:
                    how = f"re-encoded to JPEG (ffmpeg -q:v {q})"
                    if long_side:
                        how += f", scaled to long side {long_side}px"
                    return blob, how
    raise ThumbError("could not compress the image under YouTube's 2 MB thumbnail limit "
                     "— upload a smaller file")


def validate(data: bytes, content_format: str = "short") -> dict:
    """Validate (and compress if needed). Returns a dict with the bytes to store."""
    if not data:
        raise ThumbError("empty file")
    if len(data) > MAX_INPUT_BYTES:
        raise ThumbError(f"file too large ({len(data)} bytes; max input "
                         f"{MAX_INPUT_BYTES} bytes)", status=413)
    fmt, w, h = sniff(data)
    if max(w, h) < MIN_LONG_SIDE or min(w, h) < MIN_SHORT_SIDE:
        raise ThumbError(f"image too small ({w}x{h}); need long side >= {MIN_LONG_SIDE}px "
                         f"and short side >= {MIN_SHORT_SIDE}px")
    warnings = []
    portrait = h > w
    if content_format == "long" and portrait:
        warnings.append(f"portrait thumbnail ({w}x{h}) on a long-form (16:9) video")
    if content_format != "long" and w > h:
        warnings.append(f"landscape thumbnail ({w}x{h}) on a Short (9:16) video")
    conversion = None
    out = data
    if len(data) > YT_THUMB_MAX_BYTES:
        out, conversion = _compress(data, w, h)
        fmt, w, h = sniff(out)
    return {"bytes": out, "format": fmt, "width": w, "height": h,
            "original_bytes": len(data), "stored_bytes": len(out),
            "converted": conversion is not None, "conversion": conversion,
            "warnings": warnings}


def resolve_media_path(raw: str) -> Path:
    """A JSON-form path must be an existing file inside the manager media dir."""
    if not isinstance(raw, str) or not raw.strip():
        raise ThumbError("path must be a non-empty string")
    root = media_root()
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        rp = p.resolve()
    except (OSError, RuntimeError, ValueError):
        raise ThumbError("invalid path")
    if not rp.is_relative_to(root):
        raise ThumbError(f"path must be inside the manager media dir ({root})")
    if not rp.is_file():
        raise ThumbError("path is not a readable file")
    return rp


def _clear_files(vdir: Path) -> None:
    for old in vdir.glob(PROVIDED_STEM + ".*"):
        try:
            old.unlink()
        except OSError:
            pass


def _cc(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def carry_marker(video, new_cc):
    """Re-apply the provided-thumb marker onto a creation_config about to be
    written (finalize / PATCH craft replace the whole blob). Returns the same
    type it was given (dict → dict, str → str, None → None unless provided)."""
    if not is_provided(video):
        return new_cc
    was_str = isinstance(new_cc, str)
    cc = _cc(new_cc)
    cc["thumb_source"] = THUMB_SOURCE_PROVIDED
    cc["thumb_provided_path"] = video.thumb_path
    return json.dumps(cc) if (was_str or new_cc is None) else cc


def store(video, info: dict) -> Path:
    """Write the validated image as the video's provided thumbnail + mark it."""
    vdir = video_dir(video.id)
    vdir.mkdir(parents=True, exist_ok=True)
    _clear_files(vdir)
    ext = "png" if info["format"] == "png" else "jpg"
    dest = vdir / f"{PROVIDED_STEM}.{ext}"
    tmp = vdir / f".{PROVIDED_STEM}.tmp"
    tmp.write_bytes(info["bytes"])
    tmp.replace(dest)
    video.thumb_path = str(dest)
    cc = _cc(video.creation_config)
    cc["thumb_source"] = THUMB_SOURCE_PROVIDED
    cc["thumb_provided_path"] = str(dest)
    video.creation_config = json.dumps(cc)
    return dest


def clear(video) -> bool:
    """Remove the provided thumbnail. thumb_path falls back to the render still
    (thumb.jpg) when present, else None. Returns True if one was set."""
    had = is_provided(video)
    vdir = video_dir(video.id)
    _clear_files(vdir)
    if had:
        still = vdir / "thumb.jpg"
        video.thumb_path = str(still) if still.is_file() else None
    cc = _cc(video.creation_config)
    if "thumb_source" in cc or "thumb_provided_path" in cc:
        cc.pop("thumb_source", None)
        cc.pop("thumb_provided_path", None)
        video.creation_config = json.dumps(cc) if cc else None
    return had


def mime_for(path: str | Path) -> str:
    return "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
