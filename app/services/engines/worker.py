"""The HyperFrames render pipeline, run on a daemon thread (see hyperframes.py).

Pinned against hyperframes@0.6.97. The composition format and `render` invocation
were validated empirically:
  * `npx hyperframes render <dir> -o <out> --quality <q> --quiet` renders <dir>/index.html
  * index.html must register a *paused* GSAP timeline on window.__timelines["master"];
    timed elements need class="clip" + data-start/duration/track-index; the root element
    carries data-composition-id/width/height/duration and sets the output size + length.
  * the rendered MP4 has no audio — narration + BGM are muxed in afterwards.

The thread never touches the ORM/DB; it only writes files and status.json, which the
render loop polls.
"""

import asyncio
import hashlib
import os
import re
import subprocess
from pathlib import Path

from app.config import REPO_DIR, settings
from app.services.engines.base import STATE_COMPLETE, STATE_FAILED

_ASSETS = Path(__file__).resolve().parent / "assets"
_RENDER_TIMEOUT = 1800           # 30 min hard cap on the CLI subprocess

# video_aspect -> (hyperframes --resolution preset, width, height)
_ASPECTS = {
    "9:16": ("portrait", 1080, 1920),
    "16:9": ("landscape", 1920, 1080),
    "1:1": ("square", 1080, 1080),
}


def _status(handle: str, **fields) -> None:
    # Imported lazily to avoid a cycle (hyperframes.py imports this module).
    from app.services.engines.hyperframes import write_status

    write_status(handle, **fields)


# --------------------------------------------------------------------------- pipeline

def run_job(handle: str, job_dir: Path, subject: str, params: dict) -> None:
    try:
        aspect = params.get("video_aspect") or "9:16"
        resolution, width, height = _ASPECTS.get(aspect, _ASPECTS["9:16"])

        # 1. Narration script
        _status(handle, progress=5)
        script = _generate_script(subject, params)
        _status(handle, script=script, progress=12)

        # 2. Voiceover (edge-tts) -> narration.mp3
        narration = job_dir / "narration.mp3"
        _tts(script, _voice(params), narration)
        narr_secs = _probe_duration(narration) or 12.0
        duration = max(4.0, round(narr_secs + 0.6, 2))   # small tail so visuals don't cut early
        _status(handle, progress=25)

        # 3. Composition (LLM, with a deterministic fallback) -> index.html (+ gsap)
        (job_dir / "gsap.min.js").write_bytes((_ASSETS / "gsap.min.js").read_bytes())
        html = _generate_composition(subject, script, resolution, width, height, duration)
        if not _looks_valid(html):
            html = _fallback_composition(subject, script, resolution, width, height, duration)
        (job_dir / "index.html").write_text(html)
        _status(handle, progress=45)

        # 4. Render the silent video. Retry once with the safe fallback template.
        silent = job_dir / "render.mp4"
        try:
            _render(job_dir, silent)
        except Exception as e:
            (job_dir / "render-error.txt").write_text(str(e))
            html = _fallback_composition(subject, script, resolution, width, height, duration)
            (job_dir / "index.html").write_text(html)
            _render(job_dir, silent)
        _status(handle, progress=80)

        # 5. Mux narration + BGM under the video -> final.mp4
        _mux(silent, narration, _pick_bgm(params, handle),
             float(params.get("bgm_volume") or 0.2), job_dir / "final.mp4")
        _status(handle, progress=100, state=STATE_COMPLETE)
    except Exception as e:  # any failure -> the render loop sees STATE_FAILED
        _status(handle, state=STATE_FAILED, error=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- LLM steps

def _llm(prompt: str, system: str | None = None, max_tokens: int = 2000) -> str:
    import litellm

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = litellm.completion(model=settings.litellm_model, messages=messages,
                              max_tokens=max_tokens, drop_params=True)
    return resp.choices[0].message.content or ""


def _generate_script(subject: str, params: dict) -> str:
    n = int(params.get("paragraph_number") or 2)
    if (params.get("content_format") or "short") == "long":
        prompt = (
            f"Write an engaging, well-structured voiceover script for an in-depth "
            f"YouTube video titled \"{subject}\". About 450-700 words across "
            f"{max(6, n)} paragraphs: a hook, several substantive sections that explain "
            "with concrete detail and examples, then a short wrap-up. Conversational and "
            "authoritative, no filler, no headings, no stage directions, no emojis. "
            "Return ONLY the spoken words."
        )
        max_tokens = 1500
    else:
        prompt = (
            f"Write a punchy voiceover script for a vertical short-form video titled "
            f"\"{subject}\". About {max(60, n * 35)}-{max(90, n * 50)} words, {n} short "
            "paragraphs. Conversational, concrete, no filler, no headings, no stage "
            "directions, no emojis. Return ONLY the spoken words."
        )
        max_tokens = 600
    text = _llm(prompt, max_tokens=max_tokens).strip()
    # Strip accidental markdown/quote wrapping.
    return re.sub(r"^[\"'`]+|[\"'`]+$", "", text).strip()


_COMPOSITION_SYSTEM = """You are an expert HyperFrames composition author. HyperFrames renders an HTML \
file to MP4 by seeking a paused GSAP timeline frame by frame. Output a SINGLE \
self-contained index.html and NOTHING else (no markdown fences, no prose).

HARD RULES — a violation makes the render fail:
1. <html lang="en" data-resolution="{RES}"> ... </html>.
2. Load GSAP from the local file: <script src="gsap.min.js"></script>. No CDNs, no other <script src>.
3. The root element MUST be:
   <div id="root" data-composition-id="master" data-width="{W}" data-height="{H}" data-start="0" data-duration="{DUR}"> ... </div>
4. Every animated/timed element MUST have class="clip" and data-start, data-duration, data-track-index (unique integer per element).
5. Register a PAUSED timeline at the end of <body>:
   <script> window.__timelines = window.__timelines || {}; const tl = gsap.timeline({paused:true}); /* tweens */ window.__timelines["master"] = tl; </script>
6. Deterministic only: NO Math.random, NO Date.now, NO fetch/network, NO external images/fonts/video.
7. All timing must fit within 0..{DUR} seconds. Keep text inside safe margins (>=80px from edges).
8. ONE TEXT BLOCK AT A TIME — never render text on top of other visible text. The title must
   fade to opacity 0 BEFORE the first phrase appears, and each phrase must fade to opacity 0
   before the next phrase appears. So every text element you fade in MUST have a matching
   fade-to-0 tween that completes at or before the next text element's data-start (only the
   final element may stay). Do not leave earlier text on screen — give each its own time slot.

STYLE: modern, bold, high-contrast motion graphics. Reveal ONE item at a time in a single \
centered focal area: show the title, fade it out, then bring in each short phrase and fade it \
out before the next — a clean replacement, never a cluttered or overlapping frame. System \
sans-serif, big readable type, subtle GSAP entrances (fade/slide/scale). Tasteful dark or vivid background."""


def _generate_composition(subject: str, script: str, resolution: str,
                          width: int, height: int, duration: float) -> str:
    system = (_COMPOSITION_SYSTEM
              .replace("{RES}", resolution).replace("{W}", str(width))
              .replace("{H}", str(height)).replace("{DUR}", str(duration)))
    prompt = (
        f"Topic/title: {subject}\n\nNarration (visuals should reinforce this, do not just "
        f"dump it verbatim):\n{script}\n\nThe video is {duration} seconds at {width}x{height} "
        f"({resolution}). Author the index.html now."
    )
    html = _llm(prompt, system=system, max_tokens=4000).strip()
    return _strip_fences(html)


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:html)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _looks_valid(html: str) -> bool:
    h = html.lower()
    return ("<html" in h and 'data-composition-id="master"' in h
            and "window.__timelines" in h and "gsap.timeline" in h)


def _fallback_composition(subject: str, script: str, resolution: str,
                          width: int, height: int, duration: float) -> str:
    """A guaranteed-valid, NON-OVERLAPPING composition: one centered focal area that
    shows the title, then one key line at a time. Each block fades fully out before the
    next fades in, so two text blocks are never on screen together. Used when the LLM
    output is malformed or its render fails."""
    k = max(4, min(8, int(duration // 18)))            # more reveals for longer videos
    lines = _key_lines(script, k=k)
    pad = max(60, int(width * 0.08))
    segments = [("seg-title", _esc(subject))] + [("seg-line", _esc(l)) for l in lines]
    n = len(segments)
    fade = 0.5
    seg_len = max(1.8, round((duration - 0.3) / n, 3))

    els, tweens = [], []
    for i, (cls, text) in enumerate(segments):
        start = round(i * seg_len, 2)
        last = i == n - 1
        # window covers entrance + hold; last block holds to the end
        dur = round(duration - start, 2) if last else round(seg_len, 2)
        eid = f"seg{i}"
        els.append(
            f'<div id="{eid}" class="clip {cls}" data-start="{start}" '
            f'data-duration="{dur}" data-track-index="{i}">{text}</div>'
        )
        tweens.append(
            f'tl.fromTo("#{eid}",{{opacity:0,y:34}},{{opacity:1,y:0,duration:{fade},'
            f'ease:"power2.out"}},{start});'
        )
        if not last:  # fade fully out exactly as the next block starts -> no overlap
            tweens.append(
                f'tl.to("#{eid}",{{opacity:0,duration:{fade},ease:"power2.in"}},'
                f'{round(start + seg_len - fade, 2)});'
            )

    return f"""<!doctype html>
<html lang="en" data-resolution="{resolution}">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
  html,body{{margin:0;padding:0;width:{width}px;height:{height}px;background:#0b0b16;
    overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
  #root{{width:{width}px;height:{height}px;position:relative}}
  /* every segment fills the same centered focal box; only one is ever visible (opacity) */
  .clip{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    padding:0 {pad}px;box-sizing:border-box;text-align:center;color:#fff;opacity:0}}
  .seg-title{{font-size:{int(width*0.07)}px;font-weight:800;line-height:1.08;letter-spacing:-1px}}
  .seg-line{{font-size:{int(width*0.05)}px;font-weight:600;color:#c9d2ff;line-height:1.2}}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="{width}" data-height="{height}"
       data-start="0" data-duration="{duration}">
    {''.join(els)}
  </div>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
    {''.join(tweens)}
    window.__timelines["master"] = tl;
  </script>
</body></html>
"""


def _key_lines(script: str, k: int = 4) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", script) if p.strip()]
    out = []
    for p in parts:
        words = p.split()
        out.append(" ".join(words[:7]) + ("…" if len(words) > 7 else ""))
        if len(out) >= k:
            break
    return out or ["Watch to the end"]


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------- media steps

def _voice(params: dict) -> str:
    v = params.get("voice_name") or "en-US-AndrewNeural-Male"
    return re.sub(r"-(Male|Female)$", "", v)        # edge-tts wants the bare voice id


def _tts(text: str, voice: str, out_path: Path) -> None:
    import edge_tts

    async def _gen() -> None:
        await edge_tts.Communicate(text, voice).save(str(out_path))

    asyncio.run(_gen())
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"edge-tts produced no audio for voice {voice}")


def _probe_duration(path: Path) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def _render(job_dir: Path, out_path: Path) -> None:
    env = {**os.environ, "npm_config_yes": "true", "HYPERFRAMES_TELEMETRY": "0",
           "CI": "1"}
    cmd = ["npx", "--yes", f"hyperframes@{settings.hyperframes_version}", "render",
           str(job_dir), "-o", str(out_path), "--quality",
           settings.hyperframes_render_quality, "--quiet"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       timeout=_RENDER_TIMEOUT, env=env)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or e.stdout or "")[-800:]
        raise RuntimeError(f"hyperframes render failed (exit {e.returncode}): {tail}")
    if not out_path.exists():
        raise RuntimeError("hyperframes render reported success but produced no file")


def _pick_bgm(params: dict, handle: str) -> Path | None:
    bgm_type = params.get("bgm_type")
    if bgm_type == "":                       # explicitly disabled
        return None
    bgm_dir = Path(settings.bgm_dir)
    if not bgm_dir.exists():
        bgm_dir = REPO_DIR / "channel" / "music"
    if not bgm_dir.exists():
        return None
    tracks = sorted(p for p in bgm_dir.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav"))
    if not tracks:
        return None
    if isinstance(bgm_type, str) and bgm_type not in ("", "random"):
        named = bgm_dir / bgm_type
        if named.exists():
            return named
    # Deterministic pick (no Math.random equivalent needed): hash the handle.
    idx = int(hashlib.sha1(handle.encode()).hexdigest(), 16) % len(tracks)
    return tracks[idx]


def _mux(video: Path, narration: Path, bgm: Path | None, bgm_volume: float,
         out_path: Path) -> None:
    dur = _probe_duration(video) or _probe_duration(narration) or 12.0
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(narration)]
    if bgm is not None:
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
        flt = (f"[1:a]apad,atrim=0:{dur}[n];"
               f"[2:a]volume={bgm_volume},atrim=0:{dur}[b];"
               f"[n][b]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        flt = f"[1:a]apad,atrim=0:{dur}[a]"
    cmd += ["-filter_complex", flt, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg mux failed: {(e.stderr or '')[-500:]}")
