"""Render-and-judge harness — the growth agent's engagement quality gate.

Renders a small GOLDEN SET of canonical subjects through the REAL generation pipeline
(script -> edge-tts word timings -> typed storyboard -> HyperFrames render), extracts a
frame per beat, generates the title + thumbnail hook, and prints a per-subject manifest.
The agent then READS the frames (vision) + the script/title/hook and scores each subject
against run/engagement-rubric.md.

Usage (from repo root):
    PYTHONPATH=. .venv/bin/python run/rubric_review.py --label before
    # ...edit a generation prompt...
    PYTHONPATH=. .venv/bin/python run/rubric_review.py --label after
    # then Read frames from both labels and compare the rubric score.

Options:
    --label NAME   output subdir under run/.rubric_review/ (default: "sample")
    --only ID      render just one golden-set entry by id (e.g. ch1-code)
    --quality Q    draft | standard | high (default: draft — fast, for the gate)
    --list         print the golden set and exit

IMPORTANT (headless runs): run this in the FOREGROUND as a single blocking command with a
long timeout (the golden set takes a few minutes). Do NOT background it — a headless
`claude -p` run exits when the turn yields, abandoning a background render. For a quick
before/after gate on one lever, use `--only <id>` (one subject ≈ 30-60s at draft quality).

Output: run/.rubric_review/<label>/<id>/  (index.html, render.mp4, b*_<type>.png,
        plus manifest.json summarizing every subject). This dir is gitignored.

The harness intentionally reuses the shipped pipeline so what it judges is exactly what
ships: worker._generate_script / _tts / _render / _has_visible_frames, storyboard.compose
with settings.composition_beat_types, metadata.generate, thumbnail._hook_text.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from app.config import load_dotenv_into_env, settings

load_dotenv_into_env()

from app.services import metadata, thumbnail
from app.services.engines import storyboard, worker

# 4 canonical subjects: both channels (EN/PT), code-heavy + concept-heavy, all short-form
# (the dominant format; keeps the daily gate fast). topic_id fixes the palette for R10.
GOLDEN = [
    {"id": "ch1-code", "lang": "EN", "topic_id": 1, "format": "short",
     "voice": "en-US-AndrewNeural-Male",
     "subject": "Fix Your Slow RAG: Add a Reranking Step in Five Lines"},
    {"id": "ch1-concept", "lang": "EN", "topic_id": 2, "format": "short",
     "voice": "en-US-AndrewNeural-Male",
     "subject": "Why Your AI Agent Forgets Everything Between Chats"},
    {"id": "ch2-code", "lang": "PT", "topic_id": 4, "format": "short",
     "voice": "pt-BR-AntonioNeural",
     "subject": "Pare de Escrever Integracoes: Um Servidor MCP Para Tudo"},
    {"id": "ch2-concept", "lang": "PT", "topic_id": 5, "format": "short",
     "voice": "pt-BR-AntonioNeural",
     "subject": "Por Que Sua RAG Busca Lixo: O Erro de Chunking"},
]

# Anchored to this script's own dir (run/), not config.REPO_DIR — which is the app's
# PARENT under the repo's monorepo-style layout, not the repo root.
OUT_ROOT = Path(__file__).resolve().parent / ".rubric_review"


def _render_subject(entry: dict, out_dir: Path) -> dict:
    """Run one golden subject through the full pipeline; return a manifest dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    subject, fmt = entry["subject"], entry["format"]
    params = {"content_format": fmt, "paragraph_number": 2, "voice_name": entry["voice"]}

    script = worker._generate_script(subject, params)
    words = worker._tts(script, worker._voice(params), out_dir / "narration.mp3")
    dur = max(4.0, round((worker._probe_duration(out_dir / "narration.mp3") or 12.0) + 0.6, 2))

    html = storyboard.compose(
        subject=subject, script=script, words=words, duration=dur,
        resolution="portrait", width=1080, height=1920, topic_id=entry["topic_id"],
        content_format=fmt, allowed_types=settings.composition_beat_types, llm=worker._llm,
    )
    used_fallback = not html
    if used_fallback:
        html = worker._fallback_composition(subject, script, "portrait", 1080, 1920, dur)
    (out_dir / "index.html").write_text(html)
    (out_dir / "gsap.min.js").write_bytes((worker._ASSETS / "gsap.min.js").read_bytes())

    out_mp4 = out_dir / "render.mp4"
    worker._render(out_dir, out_mp4)
    visible = worker._has_visible_frames(out_mp4)

    # one frame per beat, at the beat's midpoint
    beats = re.findall(r'class="beat ([a-z_]+)" id="b\d+" data-start="([0-9.]+)"', html)
    beat_types = [t for t, _ in beats]
    starts = [float(s) for _, s in beats]
    frames = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else dur
        t = (s + end) / 2
        fp = out_dir / f"b{i}_{beat_types[i]}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                        "-i", str(out_mp4), "-frames:v", "1", str(fp)], check=False)
        if fp.exists():
            frames.append(str(fp))

    # R8: title + thumbnail hook (best-effort — never fail the harness on these)
    title = thumb_hook = None
    try:
        meta = metadata.generate(subject, script, fmt)
        title = meta.get("title")
    except Exception as e:
        title = f"(metadata failed: {e})"
    try:
        thumb_hook = thumbnail._hook_text(subject, title, content_format=fmt)
    except Exception as e:
        thumb_hook = f"(thumb hook failed: {e})"

    return {
        "id": entry["id"], "lang": entry["lang"], "format": fmt, "subject": subject,
        "title": title, "thumbnail_hook": thumb_hook,
        "script_words": len(script.split()), "script": script,
        "duration": dur, "beats": beat_types, "used_fallback": used_fallback,
        "visible": visible, "frames": frames, "dir": str(out_dir),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="sample")
    ap.add_argument("--only", default=None)
    ap.add_argument("--quality", default="draft", choices=["draft", "standard", "high"])
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    # Render fast by default — the gate judges hook/variety/clarity/layout, not final polish.
    # This is a separate process from the running manager, so overriding here is harmless.
    settings.hyperframes_render_quality = args.quality

    if args.list:
        for e in GOLDEN:
            print(f"  {e['id']:12} [{e['lang']}/{e['format']}] {e['subject']}")
        return 0

    entries = [e for e in GOLDEN if (not args.only or e["id"] == args.only)]
    if not entries:
        print(f"no golden entry matches --only {args.only!r}", file=sys.stderr)
        return 2

    label_dir = OUT_ROOT / args.label
    manifest = []
    for e in entries:
        print(f"\n=== [{e['id']}] {e['subject']}", flush=True)
        try:
            m = _render_subject(e, label_dir / e["id"])
        except Exception as ex:
            print(f"  ERROR: {type(ex).__name__}: {ex}", flush=True)
            manifest.append({"id": e["id"], "error": f"{type(ex).__name__}: {ex}"})
            continue
        manifest.append(m)
        flag = " (FELL BACK)" if m["used_fallback"] else ""
        print(f"  title:  {m['title']}")
        print(f"  hook:   {m['thumbnail_hook']}")
        print(f"  script: {m['script_words']}w | dur {m['duration']}s | visible={m['visible']}{flag}")
        print(f"  beats:  {', '.join(m['beats'])}")
        print(f"  frames: {m['dir']}")

    (label_dir).mkdir(parents=True, exist_ok=True)
    (label_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    ok = [m for m in manifest if not m.get("error") and m.get("visible") and not m.get("used_fallback")]
    print(f"\n{len(ok)}/{len(manifest)} clean renders. Manifest: {label_dir / 'manifest.json'}")
    print("Now READ the frames + script/title/hook above and score each subject against "
          "run/engagement-rubric.md (2=strong, 1=weak, 0=broken per lever).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
