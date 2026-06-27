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
import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("manager.worker")

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

# Accent colors cycled per subject hash — gives each video a consistent color identity.
_ACCENTS = ["#5b8cff", "#00c9a7", "#ff6b35", "#9b5fe0",
            "#ff3b5c", "#2ec4b6", "#ff85a1", "#f9c74f"]

# Five visually distinct composition templates. Placeholders:
#   __RES__    resolution preset   __W__ / __H__   pixel dimensions
#   __DUR__    duration (float)    __ACCENT__       hex accent color
#   __PAD__    horizontal padding  __FS__           primary font-size px
#   __CLIPS__  rendered clip <div> elements
# The universal GSAP timeline loop is embedded in each template — it animates every
# .clip from its data-start/data-duration attributes, no per-clip tweens needed.
_TEMPLATES = {
    # 1. Near-black bg, gradient top bar, glow bloom bg, words stagger up.
    "bold_dark": """\
<!doctype html>
<html lang="en" data-resolution="__RES__">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
html,body{margin:0;padding:0;width:__W__px;height:__H__px;background:#0b0b16;
  overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}
#root{width:__W__px;height:__H__px;position:relative}
#top-bar{position:absolute;top:0;left:0;width:100%;height:8px;
  background:linear-gradient(90deg,__ACCENT__,#a36bff)}
#bg-motion{position:absolute;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse at 80% 15%,__ACCENT__44,transparent 55%)}
.clip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:0 __PAD__px;box-sizing:border-box;text-align:center;color:#fff;opacity:0;
  font-size:__FS__px;font-weight:800;line-height:1.35;letter-spacing:-1px;
  text-shadow:0 4px 24px rgba(0,0,0,.7);z-index:1}
.word{display:inline-block}
.clip-text{display:block;width:100%;text-align:center}
.clip-emoji{display:block;font-size:calc(__FS__px * 0.85);line-height:1;margin-bottom:0.15em;opacity:0;transform:scale(0.5)}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="__W__" data-height="__H__"
       data-start="0" data-duration="__DUR__">
    <div id="top-bar"></div>
    <div id="bg-motion" style="opacity:0"></div>
    __CLIPS__
  </div>
  <script>
  window.__timelines = window.__timelines || {};
  const tl = gsap.timeline({paused:true});
  tl.fromTo('#bg-motion',{opacity:0},{opacity:0.6,duration:__DUR__,ease:"sine.inOut"},0);
  document.querySelectorAll('.clip').forEach(el => {
    const s = parseFloat(el.dataset.start), d = parseFloat(el.dataset.duration);
    const f = el.dataset.w === "3" ? 0.85 : 0.6;
    const words = el.querySelectorAll('.word');
    tl.set(el,{opacity:1,immediateRender:false},s);
    const em0=el.querySelector('.clip-emoji');if(em0)tl.fromTo(em0,{opacity:0,scale:0.4},{opacity:1,scale:1,duration:0.18,ease:"back.out(2)"},Math.max(0,s-0.12));
    tl.fromTo(words.length ? words : el,{opacity:0,y:28},{opacity:1,y:0,stagger:0.04,duration:0.28,ease:"power2.out"},s);
    if (el.dataset.w === "3") { tl.to(el,{scale:1.04,duration:0.15,ease:"power1.in"},s+0.35).to(el,{scale:1,duration:0.15,ease:"power1.out"}); }
    if (s+d < __DUR__-0.1) tl.to(el,{opacity:0,duration:f,ease:"power2.in"},s+d-f);
  });
  window.__timelines["master"] = tl;
  </script>
</body></html>""",

    # 2. Off-white bg, dark text, floating accent dots, words stagger up.
    "light_minimal": """\
<!doctype html>
<html lang="en" data-resolution="__RES__">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
html,body{margin:0;padding:0;width:__W__px;height:__H__px;background:#f7f7fa;
  overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}
#root{width:__W__px;height:__H__px;position:relative}
#bottom-bar{position:absolute;bottom:0;left:0;width:100%;height:6px;background:__ACCENT__}
#bg-motion{position:absolute;inset:0;z-index:0;pointer-events:none}
.dot{position:absolute;width:5px;height:5px;border-radius:50%;background:__ACCENT__;opacity:0}
.clip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:0 __PAD__px;box-sizing:border-box;text-align:center;color:#111;opacity:0;
  font-size:__FS__px;font-weight:700;line-height:1.35;letter-spacing:-0.5px;z-index:1}
.word{display:inline-block}
.clip-text{display:block;width:100%;text-align:center}
.clip-emoji{display:block;font-size:calc(__FS__px * 0.85);line-height:1;margin-bottom:0.15em;opacity:0;transform:scale(0.5)}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="__W__" data-height="__H__"
       data-start="0" data-duration="__DUR__">
    <div id="bottom-bar"></div>
    <div id="bg-motion">
      <span class="dot" style="top:18%;left:12%"></span>
      <span class="dot" style="top:72%;left:78%"></span>
      <span class="dot" style="top:44%;left:91%"></span>
      <span class="dot" style="top:85%;left:23%"></span>
    </div>
    __CLIPS__
  </div>
  <script>
  window.__timelines = window.__timelines || {};
  const tl = gsap.timeline({paused:true});
  tl.fromTo('#bg-motion .dot',{opacity:0,y:10},{opacity:0.3,y:0,stagger:__DUR__*0.25,duration:1.2,ease:"sine.out"},0);
  document.querySelectorAll('.clip').forEach(el => {
    const s = parseFloat(el.dataset.start), d = parseFloat(el.dataset.duration);
    const f = el.dataset.w === "3" ? 0.85 : 0.5;
    const words = el.querySelectorAll('.word');
    tl.set(el,{opacity:1,immediateRender:false},s);
    const em1=el.querySelector('.clip-emoji');if(em1)tl.fromTo(em1,{opacity:0,scale:0.4},{opacity:1,scale:1,duration:0.18,ease:"back.out(2)"},Math.max(0,s-0.12));
    tl.fromTo(words.length ? words : el,{opacity:0,y:20},{opacity:1,y:0,stagger:0.04,duration:0.28,ease:"power1.out"},s);
    if (el.dataset.w === "3") { tl.to(el,{scale:1.04,duration:0.15,ease:"power1.in"},s+0.35).to(el,{scale:1,duration:0.15,ease:"power1.out"}); }
    if (s+d < __DUR__-0.1) tl.to(el,{opacity:0,duration:f,ease:"power1.in"},s+d-f);
  });
  window.__timelines["master"] = tl;
  </script>
</body></html>""",

    # 3. Vivid radial gradient bg, breathing overlay, words stagger up — energetic.
    "gradient_kinetic": """\
<!doctype html>
<html lang="en" data-resolution="__RES__">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
html,body{margin:0;padding:0;width:__W__px;height:__H__px;
  background:radial-gradient(ellipse at 35% 25%,__ACCENT__ 0%,#0b0b16 68%);
  overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}
#root{width:__W__px;height:__H__px;position:relative}
#bg-motion{position:absolute;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse at 35% 25%,__ACCENT__ 0%,transparent 55%)}
.clip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:0 __PAD__px;box-sizing:border-box;text-align:center;color:#fff;opacity:0;
  font-size:__FS__px;font-weight:900;line-height:1.35;letter-spacing:-1.5px;
  text-shadow:0 2px 32px rgba(0,0,0,.75);z-index:1}
.word{display:inline-block}
.clip-text{display:block;width:100%;text-align:center}
.clip-emoji{display:block;font-size:calc(__FS__px * 0.85);line-height:1;margin-bottom:0.15em;opacity:0;transform:scale(0.5)}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="__W__" data-height="__H__"
       data-start="0" data-duration="__DUR__">
    <div id="bg-motion" style="opacity:0.3"></div>
    __CLIPS__
  </div>
  <script>
  window.__timelines = window.__timelines || {};
  const tl = gsap.timeline({paused:true});
  tl.fromTo('#bg-motion',{opacity:0.3,scale:1},{opacity:0.7,scale:1.1,duration:__DUR__,ease:"sine.inOut"},0);
  document.querySelectorAll('.clip').forEach(el => {
    const s = parseFloat(el.dataset.start), d = parseFloat(el.dataset.duration);
    const f = el.dataset.w === "3" ? 0.85 : 0.7;
    const words = el.querySelectorAll('.word');
    tl.set(el,{opacity:1,immediateRender:false},s);
    const em2=el.querySelector('.clip-emoji');if(em2)tl.fromTo(em2,{opacity:0,scale:0.4},{opacity:1,scale:1,duration:0.18,ease:"back.out(2)"},Math.max(0,s-0.12));
    tl.fromTo(words.length ? words : el,{opacity:0,y:20},{opacity:1,y:0,stagger:0.04,duration:0.28,ease:"power2.out"},s);
    if (el.dataset.w === "3") { tl.to(el,{scale:1.04,duration:0.15,ease:"power1.in"},s+0.35).to(el,{scale:1,duration:0.15,ease:"power1.out"}); }
    if (s+d < __DUR__-0.1) tl.to(el,{opacity:0,scale:1.05,duration:f,ease:"power2.in"},s+d-f);
  });
  window.__timelines["master"] = tl;
  </script>
</body></html>""",

    # 4. Very dark bg, neon glow, scanlines sweep across, words stagger from left.
    "neon_accent": """\
<!doctype html>
<html lang="en" data-resolution="__RES__">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
html,body{margin:0;padding:0;width:__W__px;height:__H__px;background:#050508;
  overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}
#root{width:__W__px;height:__H__px;position:relative}
#bg-motion{position:absolute;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.scan{position:absolute;left:0;width:100%;height:1px;background:__ACCENT__;opacity:0.2}
.clip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:0 __PAD__px;box-sizing:border-box;text-align:center;color:#fff;opacity:0;
  font-size:__FS__px;font-weight:800;line-height:1.35;
  border-bottom:4px solid __ACCENT__;
  text-shadow:0 0 48px __ACCENT__;z-index:1}
.word{display:inline-block}
.clip-text{display:block;width:100%;text-align:center}
.clip-emoji{display:block;font-size:calc(__FS__px * 0.85);line-height:1;margin-bottom:0.15em;opacity:0;transform:scale(0.5)}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="__W__" data-height="__H__"
       data-start="0" data-duration="__DUR__">
    <div id="bg-motion">
      <div class="scan" style="top:30%"></div>
      <div class="scan" style="top:60%"></div>
      <div class="scan" style="top:90%"></div>
    </div>
    __CLIPS__
  </div>
  <script>
  window.__timelines = window.__timelines || {};
  const tl = gsap.timeline({paused:true});
  tl.fromTo('#bg-motion .scan',{x:'-100%'},{x:'0%',stagger:__DUR__/4,duration:1.5,ease:"power2.inOut"},0);
  document.querySelectorAll('.clip').forEach(el => {
    const s = parseFloat(el.dataset.start), d = parseFloat(el.dataset.duration);
    const f = el.dataset.w === "3" ? 0.85 : 0.5;
    const words = el.querySelectorAll('.word');
    tl.set(el,{opacity:1,immediateRender:false},s);
    const em3=el.querySelector('.clip-emoji');if(em3)tl.fromTo(em3,{opacity:0,scale:0.4},{opacity:1,scale:1,duration:0.18,ease:"back.out(2)"},Math.max(0,s-0.12));
    tl.fromTo(words.length ? words : el,{opacity:0,x:-32},{opacity:1,x:0,stagger:0.04,duration:0.28,ease:"power3.out"},s);
    if (el.dataset.w === "3") { tl.to(el,{scale:1.04,duration:0.15,ease:"power1.in"},s+0.35).to(el,{scale:1,duration:0.15,ease:"power1.out"}); }
    if (s+d < __DUR__-0.1) tl.to(el,{opacity:0,x:24,duration:f,ease:"power2.in"},s+d-f);
  });
  window.__timelines["master"] = tl;
  </script>
</body></html>""",

    # 5. Accent fills entire bg, overlay pulses, words stagger up — vivid, social-native.
    "vivid_color": """\
<!doctype html>
<html lang="en" data-resolution="__RES__">
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
html,body{margin:0;padding:0;width:__W__px;height:__H__px;overflow:hidden;
  font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}
#root{width:__W__px;height:__H__px;position:relative;
  background:linear-gradient(145deg,__ACCENT__ 0%,rgba(0,0,0,.42) 100%)}
#bg-motion{position:absolute;inset:0;z-index:0;pointer-events:none;
  background:__ACCENT__;mix-blend-mode:overlay}
.clip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:0 __PAD__px;box-sizing:border-box;text-align:center;color:#fff;opacity:0;
  font-size:__FS__px;font-weight:900;line-height:1.35;
  text-shadow:0 3px 20px rgba(0,0,0,.5);z-index:1}
.word{display:inline-block}
.clip-text{display:block;width:100%;text-align:center}
.clip-emoji{display:block;font-size:calc(__FS__px * 0.85);line-height:1;margin-bottom:0.15em;opacity:0;transform:scale(0.5)}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="__W__" data-height="__H__"
       data-start="0" data-duration="__DUR__">
    <div id="bg-motion" style="opacity:0.4"></div>
    __CLIPS__
  </div>
  <script>
  window.__timelines = window.__timelines || {};
  const tl = gsap.timeline({paused:true});
  tl.fromTo('#bg-motion',{opacity:0.4},{opacity:0.72,duration:__DUR__*0.5,ease:"sine.inOut"},0);
  tl.fromTo('#bg-motion',{opacity:0.72},{opacity:0.4,duration:__DUR__*0.5,ease:"sine.inOut"},__DUR__*0.5);
  document.querySelectorAll('.clip').forEach(el => {
    const s = parseFloat(el.dataset.start), d = parseFloat(el.dataset.duration);
    const f = el.dataset.w === "3" ? 0.85 : 0.55;
    const words = el.querySelectorAll('.word');
    tl.set(el,{opacity:1,immediateRender:false},s);
    const em4=el.querySelector('.clip-emoji');if(em4)tl.fromTo(em4,{opacity:0,scale:0.4},{opacity:1,scale:1,duration:0.18,ease:"back.out(2)"},Math.max(0,s-0.12));
    tl.fromTo(words.length ? words : el,{opacity:0,y:20},{opacity:1,y:0,stagger:0.04,duration:0.28,ease:"power2.out"},s);
    if (el.dataset.w === "3") { tl.to(el,{scale:1.04,duration:0.15,ease:"power1.in"},s+0.35).to(el,{scale:1,duration:0.15,ease:"power1.out"}); }
    if (s+d < __DUR__-0.1) tl.to(el,{opacity:0,scale:0.95,duration:f,ease:"power2.in"},s+d-f);
  });
  window.__timelines["master"] = tl;
  </script>
</body></html>""",
}
_TEMPLATE_KEYS = list(_TEMPLATES.keys())


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


def _word_count_bounds(params: dict) -> tuple[int, int]:
    """Acceptable word count range for a generated script.  Scripts outside this band
    on the first try get one retry with an explicit word count constraint."""
    n = int(params.get("paragraph_number") or 2)
    if (params.get("content_format") or "short") == "long":
        target = max(400, min(700, 500 + (n - 6) * 50))
        return int(target * 0.65), int(target * 1.35)
    return 50, 140


def _generate_script(subject: str, params: dict) -> str:
    n = int(params.get("paragraph_number") or 2)
    if (params.get("content_format") or "short") == "long":
        prompt = (
            f"Write an engaging, well-structured voiceover script for an in-depth "
            f"YouTube video titled \"{subject}\". About 450-700 words across "
            f"{max(6, n)} paragraphs. Structure: open with a direct, punchy hook (2-3 "
            "sentences) that immediately addresses the tension or question in the title — "
            "do NOT start with 'In this video', 'Today we will', or 'Welcome back'; instead "
            "open with the core question, surprising claim, or the pain point the viewer "
            "already feels. Follow with substantive sections explaining with concrete detail "
            "and examples, then a short wrap-up. Conversational and authoritative, no filler, "
            "no headings, no stage directions, no emojis. "
            "Return ONLY the spoken words."
        )
        max_tokens = 1500
    else:
        prompt = (
            f"Write a punchy voiceover script for a vertical short-form video titled "
            f"\"{subject}\". About {max(60, n * 35)}-{max(90, n * 50)} words, {n} short "
            "paragraphs. Open with ONE sentence that immediately voices the tension, doubt, "
            "or question implied by the title — the viewer should feel 'yes, that's exactly "
            "my problem' within the first three seconds. Forbidden openers: 'In this video', "
            "'Today', 'Welcome', 'Here's how'. After that hook, give the honest verdict or "
            "concrete insight — take a clear position, don't hedge. Close with one tight, "
            "memorable line that crystallises the lesson in a sentence the viewer will quote. "
            "Conversational, concrete, no filler, no headings, no stage directions, no emojis. "
            "Return ONLY the spoken words."
        )
        max_tokens = 600
    text = _llm(prompt, max_tokens=max_tokens).strip()
    text = re.sub(r"^[\"'`]+|[\"'`]+$", "", text).strip()

    # Word-count guard: if far outside the target range, retry once with an explicit hint.
    lo, hi = _word_count_bounds(params)
    wc = len(text.split())
    if not (lo <= wc <= hi):
        logger.debug("script word count %d outside [%d,%d] for %r; retrying", wc, lo, hi, subject)
        retry = _llm(
            prompt + f"\n\nIMPORTANT: Your response MUST be between {lo} and {hi} words. "
            "Count carefully before responding.",
            max_tokens=max_tokens,
        ).strip()
        retry = re.sub(r"^[\"'`]+|[\"'`]+$", "", retry).strip()
        if retry:
            text = retry

    return text


def _pick_template(subject: str) -> tuple[str, str]:
    """Deterministic (subject-hash) template name + accent color — same title always
    gets the same look, but different titles get visually distinct compositions."""
    h = int(hashlib.sha1(subject.encode()).hexdigest(), 16)
    name = _TEMPLATE_KEYS[h % len(_TEMPLATE_KEYS)]
    accent = _ACCENTS[h % len(_ACCENTS)]
    return name, accent


def _clips_from_json(raw: str, duration: float) -> list[dict] | None:
    """Parse the LLM's JSON clip array. Returns None on any parse/schema error."""
    try:
        # The model sometimes wraps the array in prose or a ```json fence.
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        data = json.loads(m.group(0) if m else raw)
        if not isinstance(data, list) or len(data) < 3:
            return None
        clips = []
        for c in data:
            if not all(k in c for k in ("text", "start", "duration")):
                return None
            clips.append({
                "text": str(c["text"])[:120].strip(),
                "start": round(float(c["start"]), 3),
                "duration": round(float(c["duration"]), 3),
                "w": max(1, min(3, int(c.get("w", 1)))),
                "emoji": str(c.get("emoji", ""))[:2].strip(),
            })
        return clips
    except Exception:
        return None


def _validate_clips(clips: list[dict], duration: float) -> bool:
    """Verify: no overlapping windows, positive durations, all within video length."""
    intervals = sorted((c["start"], c["start"] + c["duration"]) for c in clips)
    for i, (s, e) in enumerate(intervals):
        if s < -0.05 or e > duration + 0.6 or e - s < 0.5:
            return False
        if i > 0 and s < intervals[i - 1][1] - 0.12:   # 120ms tolerance for float rounding
            return False
    return True


def _assemble_composition(clips: list[dict], template_name: str, accent: str,
                          resolution: str, width: int, height: int, duration: float) -> str:
    """Inject validated clips into a template and return the final index.html."""
    pad = max(60, int(width * 0.08))
    fs = max(32, int(width * 0.065))
    clip_els = []
    for i, c in enumerate(clips):
        w = c.get("w", 1)
        if w == 3:
            if template_name == "light_minimal":
                style = f' style="font-size:{int(fs*1.3)}px;font-weight:900"'
            else:
                style = f' style="font-size:{int(fs*1.3)}px;color:{accent}"'
        elif w == 2:
            style = f' style="font-size:{int(fs*1.15)}px"'
        else:
            style = ""
        words_html = " ".join(
            f'<span class="word">{word}</span>' for word in _esc(c["text"]).split()
        )
        emoji = c.get("emoji", "")
        emoji_html = f'<div class="clip-emoji">{emoji}</div>' if emoji else ""
        clip_els.append(
            f'<div class="clip" data-start="{c["start"]}" data-duration="{c["duration"]}" '
            f'data-track-index="{i}" data-w="{w}"{style}>'
            f'{emoji_html}<span class="clip-text">{words_html}</span></div>'
        )
    return (_TEMPLATES[template_name]
            .replace("__RES__", resolution)
            .replace("__W__", str(width))
            .replace("__H__", str(height))
            .replace("__DUR__", str(duration))
            .replace("__ACCENT__", accent)
            .replace("__PAD__", str(pad))
            .replace("__FS__", str(fs))
            .replace("__CLIPS__", "\n    ".join(clip_els)))


def _generate_composition(subject: str, script: str, resolution: str,
                          width: int, height: int, duration: float) -> str:
    """Generate a composition by asking the LLM for clip content only, then injecting
    into a pre-authored template.  Falls back to empty string on failure (caller will
    use _fallback_composition).  Token usage is ~90% lower than the old full-HTML approach."""
    template_name, accent = _pick_template(subject)
    n_clips = max(6, min(12, int(duration / 4)))
    spacing = round(duration / n_clips, 2)

    system = (
        "You extract phrase clips from a video narration for a motion-graphics composition. "
        "Respond with ONLY a valid JSON array — no prose, no markdown, no code fences. "
        f"Rules: {n_clips} clips (±2 is fine); each text ≤ 8 words; "
        f"no overlapping time windows; cover 0..{duration}s; "
        "clips must be in chronological order. "
        "Add \"w\": 1|2|3 to each clip: exactly one gets w=3 (the single core reveal or punchline); "
        "2-4 get w=2 (supporting highlights); all others w=1. "
        "Also add \"emoji\": one single emoji character that visually represents each clip's topic "
        "(e.g. \"💸\" for cost, \"⚠️\" for warning, \"🔥\" for urgency, \"🧠\" for AI/cognition). "
        "No multi-emoji strings — exactly one character per clip."
    )
    prompt = (
        f"Video title: {subject}\n"
        f"Duration: {duration}s\n"
        f"Narration:\n{script}\n\n"
        f'Return {n_clips} clips as: [{{"text":"...","start":0.0,"duration":{spacing},"w":1}}, ...]'
    )
    raw = _llm(prompt, system=system, max_tokens=900).strip()
    clips = _clips_from_json(raw, duration)
    if clips and _validate_clips(clips, duration):
        return _assemble_composition(clips, template_name, accent,
                                     resolution, width, height, duration)

    # Retry with rigid evenly-spaced timing so the model only writes the text.
    retry_prompt = (
        f"Video title: {subject}\n"
        f"Duration: {duration}s\n"
        f"Extract exactly {n_clips} short phrases (≤8 words each) from this narration "
        f"and space them {spacing}s apart starting at 0.0:\n{script}\n\n"
        f"Return ONLY: [{{'\"text\":\"...\",\"start\":0.0,\"duration\":{spacing}}},"
        f" {{\"text\":\"...\",\"start\":{spacing},\"duration\":{spacing}}}, ...]"
    )
    raw2 = _llm(retry_prompt, system=system, max_tokens=900).strip()
    clips2 = _clips_from_json(raw2, duration)
    if clips2 and _validate_clips(clips2, duration):
        return _assemble_composition(clips2, template_name, accent,
                                     resolution, width, height, duration)

    return ""   # signals to run_job to use _fallback_composition


def _looks_valid(html: str) -> bool:
    h = html.lower()
    if not ("<html" in h and 'data-composition-id="master"' in h and "gsap.timeline" in h):
        return False
    # Reject truncated output (LLM hit the token limit): the timeline must be registered
    # AND actually animate clips. Without tweens, every .clip stays at its CSS opacity:0
    # and the video renders blank — so fall back to the deterministic composition instead.
    closed = re.search(r'__timelines\s*\[\s*["\']master["\']\s*\]\s*=', h) is not None
    has_tween = re.search(r'\.(fromto|to|from|set)\s*\(', h) is not None
    return closed and has_tween


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
    wav_tracks = sorted(p for p in bgm_dir.glob("techno_*.wav"))
    all_tracks = sorted(p for p in bgm_dir.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav"))
    tracks = wav_tracks or all_tracks
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
