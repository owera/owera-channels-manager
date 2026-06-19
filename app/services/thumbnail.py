"""Generate a custom YouTube thumbnail (1280x720 PNG) for a published video.

The single biggest unused CTR lever: the pipeline rendered preview frames locally
but never uploaded a designed thumbnail. This builds a bold, high-contrast "hook
card" through the existing HyperFrames render path (bundled chromium — zero new
dependencies) and extracts a still frame with ffmpeg. The hook copy comes from a
small LLM call, falling back to the video's title.

YouTube thumbnails are always 16:9 regardless of the video's aspect, so this always
produces 1280x720 (well under YouTube's 2MB limit). Best-effort by contract: every
entry point returns None on failure so a publish is never blocked.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

from app.config import settings
from app.services.engines.worker import _ASSETS, _esc, _llm

logger = logging.getLogger("manager.thumbnail")

# Render at the proven landscape preset size, then downscale to YouTube's spec.
_W, _H = 1920, 1080
_OUT_W, _OUT_H = 1280, 720
_RENDER_TIMEOUT = 240            # a static card renders fast; never stall a publish


def _hook_text(subject: str, title: str | None) -> str:
    """A punchy 3–6 word thumbnail hook. LLM with a deterministic fallback."""
    base = (title or subject or "").strip()
    try:
        system = (
            "You write YouTube thumbnail hooks. Return ONLY a single punchy hook of "
            "3 to 6 words that creates curiosity — no quotes, no emojis, no hashtags, "
            "no trailing punctuation. Prefer concrete, high-contrast words. "
            "Always respond in the same language as the video title."
        )
        out = _llm(f"Video title: {base}\n\nWrite the thumbnail hook.",
                   system=system, max_tokens=40).strip()
        out = re.sub(r'^["\'`]+|["\'`]+$', "", out).splitlines()[0].strip()
        words = out.split()
        if 2 <= len(words) <= 8 and len(out) <= 48:
            return out.upper()
    except Exception as e:
        logger.info("thumbnail hook LLM failed, using title: %s", e)
    # Fallback: first ~5 words of the title.
    return " ".join(base.split()[:5]).upper() or "WATCH THIS"


def _thumbnail_html(hook: str) -> str:
    """A deterministic, guaranteed-valid static hook card. One clip, fully visible
    for the whole (tiny) duration; the timeline is non-empty so HyperFrames seeks it
    cleanly, and every extracted frame shows the text."""
    pad = int(_W * 0.07)
    font = int(_W * 0.085)
    return f"""<!doctype html>
<html lang="en" data-resolution="landscape">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
  html,body{{margin:0;padding:0;width:{_W}px;height:{_H}px;overflow:hidden;
    font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
  #root{{width:{_W}px;height:{_H}px;position:relative;
    background:radial-gradient(120% 120% at 20% 0%,#1b2a6b 0%,#0b0b16 60%)}}
  #accent{{position:absolute;left:0;top:0;height:18px;width:100%;
    background:linear-gradient(90deg,#5b8cff,#a36bff,#ff5bb0)}}
  #hook{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    padding:0 {pad}px;box-sizing:border-box;text-align:center;color:#fff;opacity:1;
    font-size:{font}px;font-weight:800;line-height:1.04;letter-spacing:-2px;
    text-shadow:0 6px 28px rgba(0,0,0,.55)}}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="{_W}" data-height="{_H}"
       data-start="0" data-duration="1">
    <div id="accent"></div>
    <div id="hook" class="clip" data-start="0" data-duration="1" data-track-index="0">{_esc(hook)}</div>
  </div>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
    tl.fromTo("#hook", {{opacity:1}}, {{opacity:1, duration:0.5}}, 0);
    window.__timelines["master"] = tl;
  </script>
</body></html>
"""


def _render(job_dir: Path, out_mp4: Path) -> None:
    """Render the static card to MP4 via the pinned HyperFrames CLI (short timeout)."""
    env = {**os.environ, "npm_config_yes": "true", "HYPERFRAMES_TELEMETRY": "0", "CI": "1"}
    cmd = ["npx", "--yes", f"hyperframes@{settings.hyperframes_version}", "render",
           str(job_dir), "-o", str(out_mp4), "--quality",
           settings.hyperframes_render_quality, "--quiet"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=_RENDER_TIMEOUT, env=env)
    if r.returncode != 0 or not out_mp4.exists():
        tail = (r.stderr or r.stdout or "")[-500:]
        raise RuntimeError(f"hyperframes thumbnail render failed: {tail}")


def _extract_frame(mp4: Path, out_png: Path) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.4", "-i", str(mp4),
           "-frames:v", "1", "-vf", f"scale={_OUT_W}:{_OUT_H}", str(out_png)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not out_png.exists():
        raise RuntimeError(f"ffmpeg thumbnail extract failed: {(r.stderr or '')[-300:]}")


def make_thumbnail_png(subject: str, title: str | None, out_png: Path) -> Path | None:
    """Build a custom thumbnail PNG at `out_png`. Returns the path, or None on any
    failure (caller treats thumbnails as best-effort)."""
    out_png = Path(out_png)
    work = out_png.parent / ".thumb_work"
    try:
        work.mkdir(parents=True, exist_ok=True)
        (work / "gsap.min.js").write_bytes((_ASSETS / "gsap.min.js").read_bytes())
        (work / "index.html").write_text(_thumbnail_html(_hook_text(subject, title)))
        _render(work, work / "thumb.mp4")
        _extract_frame(work / "thumb.mp4", out_png)
        return out_png
    except Exception as e:
        logger.info("custom thumbnail generation failed for %r: %s", subject, e)
        return None
