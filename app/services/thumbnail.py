"""Generate a custom YouTube thumbnail PNG for a published video.

Shorts (YPP move 1): native 9:16 card — concrete OBJECT above the spoken hook
type. Long-form stays 16:9 with the object beside the type. HyperFrames renders
the card (bundled chromium) and ffmpeg extracts a still. Best-effort: every
entry point returns None on failure so a publish is never blocked.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

from app.config import settings
from app.services.engines.theme import PALETTE, resolve
from app.services.engines import theme as theme_mod
from app.services.engines.worker import _ASSETS, _esc, _llm

logger = logging.getLogger("manager.thumbnail")

# Shorts default = native 9:16 (Designer lock). Long-form uses _canvas("long").
_W, _H = 1080, 1920
_OUT_W, _OUT_H = 720, 1280
_RENDER_TIMEOUT = 240            # a static card renders fast; never stall a publish

# Accent palette keyed by topic_id (same topic = same brand color). Canonical home is
# theme.PALETTE — make_thumbnail_png routes through theme.resolve so a video's motion
# accent matches its thumbnail, including the zero-is-missing gate (topic_id 0/None
# hashes the subject instead of pinning palette[0] blue).
_THUMB_PALETTE = PALETTE


def _canvas(content_format: str = "short") -> tuple[int, int, int, int]:
    """render_w, render_h, out_w, out_h. Shorts = 9:16; long = 16:9."""
    if (content_format or "short") == "long":
        return 1920, 1080, 1280, 720
    return 1080, 1920, 720, 1280


def _hook_text(subject: str, title: str | None,
               content_format: str = "short") -> str:
    """Frame-0 / thumb copy: the spoken title hook, not a curiosity gap.

    Decolar lock — MUST equal the first spoken sentence (the title before `·`)
    or a faithful ≤8-word compression of that same claim. Repeating the title
    is required. A drifted LLM slogan falls back to the compressed claim.
    Typography of the hook STAYS (P1) — the object widget proves the angle.
    """
    from app.services import craft
    spoken = craft.spoken_hook_source(title, None, subject)
    fallback = craft.compress_claim(spoken, 8) or "Watch This"
    try:
        fmt_hint = ("long-form YouTube video" if content_format == "long"
                    else "short-form vertical video")
        system = (
            "You compress YouTube title hooks for the thumbnail. DECOLAR LOCK: "
            "the thumbnail MUST equal the first spoken sentence (the title hook) "
            "or a faithful compression of that SAME claim, at most 8 words. "
            "Repeating the title is REQUIRED — do not invent a curiosity gap, "
            "a second slogan, or a different angle. Do NOT withhold the claim. "
            "Do NOT tell the viewer something the title does not already say. "
            "No emojis, no hashtags, no quotes, no trailing punctuation. "
            "Keep the title's language and distinctive words (the tool, the number, "
            "the object of the claim: receipt, terminal, bill). "
            "Prefer naming the object of the angle (receipt, terminal, invoice) "
            "when it is already in the title; never swap in a generic 💸 emoji punch. "
            "No emoji-first hook, no emoji soup. "
            "Return ONLY the compressed hook."
        )
        prompt = (
            f"Video title: {spoken or (title or subject or '')}\n"
            f"Format: {fmt_hint}\n\n"
            "Compress THIS claim into ≤8 words — same claim, not a new hook."
        )
        out = _llm(prompt, system=system, max_tokens=100).strip()
        out = re.sub(r'^["\'`]+|["\'`]+$', "", out).splitlines()[0].strip()
        words = out.split()
        if (2 <= len(words) <= 8 and len(out) <= 60 and craft.claim_aligned(out, spoken)
                and not craft.emoji_first(out) and not craft.emoji_soup(out)):
            return out
    except Exception as e:
        logger.info("thumbnail hook LLM failed, using spoken claim: %s", e)
    return fallback


def _thumbnail_html(hook: str, accent: str = "#5b8cff",
                    bg_deep: str = "#1b2a6b", brand: str | None = None,
                    th: dict | None = None,
                    obj: dict | str | None = None,
                    content_format: str = "short") -> str:
    """Object-above-type 9:16 card (shorts) or object-beside (long).

    Object proves the spoken angle (bill / receipt / GPU / app / terminal).
    Hook typography stays. Widgets are themed by YPP2 brand tokens:
    os = cold gray stroke + O crop; rr = burgundy #C41E5A stroke, no mark;
    else legacy neon (accent bar kept so unbranded tests stay pinned).
    """
    from app.services import craft
    if brand and not th:
        tokens = theme_mod.resolve(None, hook, brand=brand)
    else:
        tokens = th or {}
    spec = craft.coerce_object(obj) if obj is not None else craft.opening_object(hook)
    rw, rh, _, _ = _canvas(content_format)
    portrait = rh >= rw
    pad = int(rw * 0.08)
    font = int(rw * (0.072 if portrait else 0.068))
    fg = tokens.get("fg") or "#ffffff"
    bg_base = tokens.get("bg_base") or "#000"
    glow = tokens.get("glow") or bg_deep
    glow2 = tokens.get("glow2") or bg_base
    stroke = tokens.get("stroke") or accent
    logo = tokens.get("logo") if brand == "os" else ""
    inset = theme_mod.mark_inset_px(rw, rh)
    logo_h = theme_mod.logo_height_px(rh)
    resolution = "portrait" if portrait else "landscape"
    obj_accent = stroke if brand in ("os", "rr") else accent

    if brand == "os":
        bg = f"radial-gradient(120% 90% at 18% 0%,{glow} 0%,{bg_base} 64%)"
        bar_html = ""
        mark_html = (f'<img id="brand-mark" src="{_esc(logo)}" alt="" />'
                     if logo else "")
        mark_css = (f"#brand-mark{{position:absolute;left:{inset}px;bottom:{inset}px;"
                    f"height:{logo_h}px;width:auto;opacity:.9;z-index:5;pointer-events:none}}")
    elif brand == "rr":
        bg = (f"radial-gradient(110% 80% at 82% 0%,{glow} 0%,{glow2} 28%,{bg_base} 62%)")
        bar_html = ""
        mark_html = ""
        mark_css = ""
    else:
        bg = f"radial-gradient(120% 120% at 20% 0%,{bg_deep} 0%,#000 62%)"
        bar_html = ""
        mark_html = ""
        mark_css = ""
        fg = "#ffffff"

    if portrait:
        layout = (
            f"#stage{{position:absolute;inset:0;display:flex;flex-direction:column;"
            f"align-items:center;justify-content:flex-end;gap:{int(rh * 0.03)}px;"
            f"padding:{int(rh * 0.10)}px {pad}px {int(rh * 0.12)}px;box-sizing:border-box}}"
            f"#hobj{{flex:0 0 auto;width:78%;max-height:34%;--obj-accent:{obj_accent};"
            f"--obj-mono:ui-monospace,Menlo,Consolas,monospace}}"
            f"#hook{{flex:0 0 auto;width:100%;text-align:center;color:{fg};"
            f"font-size:{font}px;font-weight:800;line-height:1.08;letter-spacing:-2px;"
            f"text-shadow:0 6px 28px rgba(0,0,0,.55);z-index:2}}"
        )
    else:
        layout = (
            f"#stage{{position:absolute;inset:0;display:flex;flex-direction:row;"
            f"align-items:center;justify-content:center;gap:{int(rw * 0.04)}px;"
            f"padding:0 {pad}px;box-sizing:border-box}}"
            f"#hobj{{flex:0 0 38%;max-height:70%;--obj-accent:{obj_accent};"
            f"--obj-mono:ui-monospace,Menlo,Consolas,monospace}}"
            f"#hook{{flex:1 1 auto;text-align:left;color:{fg};"
            f"font-size:{font}px;font-weight:800;line-height:1.08;letter-spacing:-2px;"
            f"text-shadow:0 6px 28px rgba(0,0,0,.55);z-index:2}}"
        )
    brand_attr = f' data-brand="{brand}"' if brand else ""
    return f"""<!doctype html>
<html lang="en" data-resolution="{resolution}"{brand_attr}>
<head><meta charset="UTF-8"/>
<script src="gsap.min.js"></script>
<style>
  html,body{{margin:0;padding:0;width:{rw}px;height:{rh}px;overflow:hidden;
    font-family:{'-apple-system,Segoe UI,Helvetica,Arial,sans-serif'}}}
  #root{{width:{rw}px;height:{rh}px;position:relative;background:{bg}}}
  {mark_css}
  {layout}
  {craft.OBJECT_CSS}
</style></head>
<body>
  <div id="root" data-composition-id="master" data-width="{rw}" data-height="{rh}"
       data-start="0" data-duration="1">
    {bar_html}
    {mark_html}
    <div id="stage" class="clip" data-start="0" data-duration="1" data-track-index="0">
      <div id="hobj">{craft.object_markup(spec)}</div>
      <div id="hook" class="clip" data-start="0" data-duration="1" data-track-index="1">{_esc(hook)}</div>
    </div>
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


def _extract_frame(mp4: Path, out_png: Path,
                   content_format: str = "short") -> None:
    _, _, out_w, out_h = _canvas(content_format)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.4", "-i", str(mp4),
           "-frames:v", "1", "-vf", f"scale={out_w}:{out_h}", str(out_png)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not out_png.exists():
        raise RuntimeError(f"ffmpeg thumbnail extract failed: {(r.stderr or '')[-300:]}")


def make_thumbnail_png(subject: str, title: str | None, out_png: Path,
                       topic_id: int | None = None,
                       content_format: str = "short",
                       brand: str | None = None) -> Path | None:
    """Build a custom thumbnail PNG at `out_png`. Returns the path, or None on any
    failure (caller treats thumbnails as best-effort)."""
    out_png = Path(out_png)
    work = out_png.parent / ".thumb_work"
    try:
        tokens = resolve(topic_id, subject, brand=brand)
        accent, bg_deep = tokens["accent"], tokens["bg_deep"]
        work.mkdir(parents=True, exist_ok=True)
        (work / "gsap.min.js").write_bytes((_ASSETS / "gsap.min.js").read_bytes())
        theme_mod.stage_brand_assets(work, brand)
        from app.services import craft
        hook = _hook_text(subject, title, content_format=content_format)
        spoken = craft.spoken_hook_source(title, None, subject)
        obj = craft.opening_object(spoken or hook)
        (work / "index.html").write_text(
            _thumbnail_html(hook, accent=accent, bg_deep=bg_deep, brand=brand,
                            th=tokens, obj=obj, content_format=content_format))
        _render(work, work / "thumb.mp4")
        _extract_frame(work / "thumb.mp4", out_png, content_format=content_format)
        return out_png
    except Exception as e:
        logger.info("custom thumbnail generation failed for %r: %s", subject, e)
        return None
