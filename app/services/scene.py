"""scene/v1 — versioned scene schema, aspects, safe areas, CTA legibility and
the ``terminal`` scene (asciinema ``.cast`` rendered by asciinema-agg).

Render rethink, phase 1 (channels-render-rethink/PROPOSAL.md §2.1, §3, §4).

* Every existing storyboard card type maps 1:1 onto a ``card`` scene
  (``beat_to_scene`` / ``scene_to_beat`` round-trip byte-identically), so
  existing boards and gates do not change.
* ``terminal`` is implemented: a recorded ``.cast`` file is the ONLY source
  of on-screen terminal text. Its commands/output must pass the strict raw
  CLI allowlist rules (``craft.cli_card_strict_literal``, VM PR #138) and the
  cast sha256 is stored in the gate snapshot. No agg binary → fail closed
  (``AggMissingError``); there is no fake-typing fallback.
* ``animation`` / ``media`` / ``caption`` are declared in the schema but NOT
  implemented: validating such a scene fails closed.

Everything here is per-profile / per-instance data (aspect, network, safe-area
override, theme colours, cast paths, prompt); nothing is tied to one operation.
Zero new Python dependencies; ``agg`` (and ``ffmpeg``, already used by the
worker mux) are the only external binaries, and only for terminal scenes.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

SCHEMA = "scene/v1"

# --------------------------------------------------------------------------- schema

SCENE_TYPES = ("card", "terminal", "animation", "media", "caption")
IMPLEMENTED_SCENE_TYPES = frozenset({"card", "terminal"})
# Existing storyboard renderers (storyboard._RENDERERS) — the ``card`` kinds.
CARD_KINDS = ("hook", "statement", "stat", "compare", "list", "term_define",
              "quote", "cta", "code", "command", "diagram", "endcard")
# Board beat types that are not cards but scenes of their own.
BEAT_SCENE_TYPES = {"terminal": "terminal"}
# Timing / renderer keys the board owns; everything else is card content.
_TIMING_KEYS = ("start", "dur", "cue")


class SceneError(ValueError):
    """Invalid or unsupported scene (fail closed)."""


class UnknownAspectError(SceneError):
    """video_aspect not in ASPECTS (no silent 9:16 fallback)."""


class AggMissingError(RuntimeError):
    """asciinema-agg binary not found — terminal scenes cannot render."""


def beat_to_scene(beat: dict, i: int = 0) -> dict:
    """Map a storyboard beat onto a scene/v1 dict (no information lost)."""
    if not isinstance(beat, dict):
        raise SceneError(f"beat {i} is not an object")
    typ = beat.get("type") or ""
    stype = BEAT_SCENE_TYPES.get(typ, "card")
    if stype == "card" and typ not in CARD_KINDS:
        raise SceneError(f"beat {i}: unknown card kind {typ!r}")
    scene = {"schema": SCHEMA, "id": f"s{i}", "type": stype}
    if stype == "card":
        scene["kind"] = typ
    for k in _TIMING_KEYS:
        if k in beat:
            scene[k] = beat[k]
    scene["content"] = {k: v for k, v in beat.items()
                        if k != "type" and k not in _TIMING_KEYS}
    scene["safe_area"] = "auto"
    return scene


def scene_to_beat(scene: dict) -> dict:
    """Inverse of ``beat_to_scene`` (validates first)."""
    validate_scene(scene)
    typ = scene["kind"] if scene["type"] == "card" else scene["type"]
    beat = {"type": typ}
    content = scene.get("content") or {}
    # Preserve the original key order: type, content keys…, timing keys.
    beat.update(content)
    for k in _TIMING_KEYS:
        if k in scene:
            beat[k] = scene[k]
    return beat


def validate_scene(scene: dict) -> dict:
    if not isinstance(scene, dict):
        raise SceneError("scene is not an object")
    if scene.get("schema") != SCHEMA:
        raise SceneError(f"unsupported scene schema {scene.get('schema')!r} (want {SCHEMA})")
    stype = scene.get("type")
    if stype not in SCENE_TYPES:
        raise SceneError(f"unknown scene type {stype!r}")
    if stype not in IMPLEMENTED_SCENE_TYPES:
        raise SceneError(f"scene type {stype!r} is declared in {SCHEMA} but not implemented")
    if stype == "card" and scene.get("kind") not in CARD_KINDS:
        raise SceneError(f"unknown card kind {scene.get('kind')!r}")
    if stype == "terminal":
        c = scene.get("content") or {}
        if not isinstance(c.get("cast"), str) or not c["cast"].strip():
            raise SceneError("terminal scene needs content.cast (path to a .cast recording)")
    return scene


def board_to_scenes(beats) -> list[dict]:
    return [beat_to_scene(b, i) for i, b in enumerate(beats or [])]


def scenes_to_board(scenes) -> list[dict]:
    return [scene_to_beat(s) for s in scenes or []]


# --------------------------------------------------------------------------- aspects

# video_aspect -> (hyperframes --resolution preset, width, height)
ASPECTS = {
    "9:16": ("portrait", 1080, 1920),
    "16:9": ("landscape", 1920, 1080),
    "1:1": ("square", 1080, 1080),
    # LinkedIn / feed. HyperFrames reads the pixel size from the composition;
    # "custom" is not one of its presets (verify on claw0 before relying on it).
    "4:5": ("custom", 1080, 1350),
}


def resolve_aspect(aspect: str | None) -> tuple[str, int, int]:
    """(preset, width, height). Missing → profile default 9:16; unknown → raise."""
    key = aspect or "9:16"
    if key not in ASPECTS:
        raise UnknownAspectError(
            f"unknown video_aspect {aspect!r}; supported: {sorted(ASPECTS)}")
    return ASPECTS[key]


def aspect_of(width: int, height: int) -> str | None:
    for k, (_r, w, h) in ASPECTS.items():
        if w * height == h * width:
            return k
    return None


# --------------------------------------------------------------------------- safe areas

# Fractions of the frame (top, bottom, left, right). PROPOSAL §3 (to be
# validated per network). Profiles may override with ``safe_area`` = dict.
SAFE_AREAS = {
    ("youtube", "9:16"): {"top": 0.08, "bottom": 0.20, "left": 0.06, "right": 0.11},
    ("instagram", "9:16"): {"top": 0.12, "bottom": 0.22, "left": 0.06, "right": 0.12},
    ("linkedin", "1:1"): {"top": 0.06, "bottom": 0.10, "left": 0.06, "right": 0.06},
    ("linkedin", "4:5"): {"top": 0.06, "bottom": 0.10, "left": 0.06, "right": 0.06},
    ("x", "16:9"): {"top": 0.05, "bottom": 0.12, "left": 0.05, "right": 0.05},
    ("youtube", "16:9"): {"top": 0.05, "bottom": 0.12, "left": 0.05, "right": 0.05},
}
DEFAULT_NETWORK = {"9:16": "youtube", "16:9": "youtube", "1:1": "linkedin", "4:5": "linkedin"}

# The craft gate's split-card box (top/bottom edges, left/right padding as
# fractions). 9:16 is the historical 1080×1920 box, unchanged.
SPLIT_BOXES = {
    "9:16": {"top": 0.09, "bottom": 0.72, "left": 0.08, "right": 0.11},
    "4:5": {"top": 0.08, "bottom": 0.84, "left": 0.08, "right": 0.08},
    "1:1": {"top": 0.08, "bottom": 0.86, "left": 0.08, "right": 0.08},
    "16:9": {"top": 0.08, "bottom": 0.84, "left": 0.06, "right": 0.06},
}
_SA_KEYS = ("top", "bottom", "left", "right")


def _check_fracs(d: dict, what: str) -> dict:
    out = {}
    for k in _SA_KEYS:
        v = d.get(k)
        if not isinstance(v, (int, float)) or not 0 <= v < 0.5:
            raise SceneError(f"{what}.{k} must be a fraction in [0, 0.5), got {v!r}")
        out[k] = float(v)
    return out


def safe_area(aspect: str, network: str | None = None, override: dict | None = None) -> dict:
    resolve_aspect(aspect)
    if override:
        return _check_fracs(override, "safe_area")
    net = network or DEFAULT_NETWORK[aspect]
    sa = SAFE_AREAS.get((net, aspect))
    if sa is None:
        raise SceneError(f"no safe area for network {net!r} at {aspect}")
    return dict(sa)


def split_box_fracs(width: int, height: int, override: dict | None = None) -> dict:
    a = aspect_of(width, height)
    if override:
        o = dict(override)
        if not all(isinstance(o.get(k), (int, float)) for k in _SA_KEYS) \
                or not 0 <= o["top"] < o["bottom"] <= 1:
            raise SceneError(f"split_box override invalid: {override!r}")
        return {k: float(o[k]) for k in _SA_KEYS}
    if a is None:
        # Non-preset size: historical box (same as before this table existed).
        a = "9:16"
    return dict(SPLIT_BOXES[a])


# --------------------------------------------------------------------------- CTA legibility

# Absolute px floors on the final frame (PROPOSAL §4).
CTA_MIN_PX = {
    "9:16": {"main": 64, "sub": 40},
    "4:5": {"main": 56, "sub": 36},
    "1:1": {"main": 56, "sub": 36},
    "16:9": {"main": 56, "sub": 34},
}
CTA_MAIN_CONTRAST = 7.0
CTA_SUB_CONTRAST = 4.5


def _rel_lum(hexcol: str) -> float:
    h = (hexcol or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise SceneError(f"not a #rrggbb colour: {hexcol!r}")
    ch = []
    for k in (0, 2, 4):
        c = int(h[k:k + 2], 16) / 255
        ch.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def contrast(fg: str, bg: str) -> float:
    a, b = _rel_lum(fg), _rel_lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _floors(width: int, height: int) -> dict:
    return CTA_MIN_PX.get(aspect_of(width, height) or "9:16", CTA_MIN_PX["9:16"])


def cta_sizes(width: int, height: int, fs: int, body_fs: int, *, long: bool = False) -> dict:
    """max(floor, width-proportional size) for the main CTA and its sub line."""
    f = _floors(width, height)
    main = int(fs * 1.05) if long else int(fs * 0.72)
    sub = int(body_fs) if long else int(body_fs * 0.8)
    return {"main": max(f["main"], main), "sub": max(f["sub"], sub)}


def _theme_bgs(th: dict) -> list[str]:
    return [c for c in (th.get("bg_base"), th.get("bg_deep")) if c]


def cta_colors(th: dict) -> dict:
    """Main CTA = fg; sub = fg_dim only if it clears 4.5:1 on every bg, else fg."""
    bgs = _theme_bgs(th)
    fg, dim = th.get("fg") or "#ffffff", th.get("fg_dim")
    sub = fg
    if dim and bgs and all(contrast(dim, b) >= CTA_SUB_CONTRAST for b in bgs):
        sub = dim
    return {"main": fg, "sub": sub}


def cta_legibility_marker(beats, th: dict, width: int, height: int,
                          fs: int | None = None, body_fs: int | None = None) -> dict | None:
    """Compose-time numbers the cta_legibility gate check verifies."""
    if not any(isinstance(b, dict) and (b.get("type") or "") in ("cta", "endcard")
               for b in beats or []):
        return None
    fs = fs if fs is not None else max(32, int(width * 0.065))
    body_fs = body_fs if body_fs is not None else max(28, int(width * 0.045))
    sizes = cta_sizes(width, height, fs, body_fs)
    cols = cta_colors(th)
    bgs = _theme_bgs(th)
    return {
        "v": 1, "aspect": aspect_of(width, height), "width": width, "height": height,
        "main_px": sizes["main"], "sub_px": sizes["sub"],
        "main_contrast": round(min(contrast(cols["main"], b) for b in bgs), 2) if bgs else None,
        "sub_contrast": round(min(contrast(cols["sub"], b) for b in bgs), 2) if bgs else None,
    }


def cta_legibility_hits(marker) -> list[dict]:
    """Fail closed: a malformed marker, an unknown aspect, a size under the
    aspect floor or a contrast under 7:1 (main) / 4.5:1 (sub) is a hit."""
    if marker is None:
        return []
    def hit(d):
        return [{"i": None, "check": "cta_legibility", "detail": d}]
    if not isinstance(marker, dict):
        return hit("cta_legibility marker is not an object")
    a = marker.get("aspect")
    if a not in CTA_MIN_PX:
        return hit(f"unknown aspect {a!r} for CTA floors")
    f = CTA_MIN_PX[a]
    out = []
    for key, floor, ckey, cmin in (("main_px", f["main"], "main_contrast", CTA_MAIN_CONTRAST),
                                   ("sub_px", f["sub"], "sub_contrast", CTA_SUB_CONTRAST)):
        px, cr = marker.get(key), marker.get(ckey)
        if not isinstance(px, (int, float)) or px < floor:
            out += hit(f"{key}={px!r} below {floor}px floor at {a}")
        if not isinstance(cr, (int, float)) or cr + 1e-9 < cmin:
            out += hit(f"{ckey}={cr!r} below {cmin}:1")
    return out


# --------------------------------------------------------------------------- terminal (.cast)

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_cast(path: Path) -> tuple[dict, list]:
    """asciicast v2: header line + [time, code, data] events. Raises SceneError."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        events = [json.loads(ln) for ln in lines[1:] if ln.strip()]
    except (OSError, IndexError, ValueError, UnicodeDecodeError) as e:
        raise SceneError(f"unreadable .cast {path}: {type(e).__name__}: {e}")
    if not isinstance(header, dict) or header.get("version") != 2:
        raise SceneError(f"{path}: only asciicast v2 is supported")
    return header, events


def cast_screen_lines(path: Path) -> list[str]:
    """Output stream with ANSI/OSC stripped, CR handled, as text lines.
    Trailing terminal padding (spaces) is dropped; nothing else is
    normalised, so the strict allowlist comparison stays byte-exact."""
    _h, events = read_cast(path)
    raw = "".join(e[2] for e in events
                  if isinstance(e, list) and len(e) >= 3 and e[1] == "o" and isinstance(e[2], str))
    raw = _ANSI_RE.sub("", raw).replace("\r\n", "\n")
    out = []
    for ln in raw.split("\n"):
        if "\r" in ln:            # carriage-return redraw: last segment wins
            ln = ln.split("\r")[-1]
        out.append(ln.rstrip())
    while out and not out[-1]:
        out.pop()
    return out


def cast_transcript(path: Path, prompt: str = "$ ") -> list[dict]:
    """Split screen lines into {"command", "output": [...]} blocks by prompt.
    A trailing bare prompt (shell waiting) is dropped."""
    blocks: list[dict] = []
    pre: list[str] = []
    for ln in cast_screen_lines(path):
        if prompt and ln == prompt.rstrip():
            continue              # bare prompt (shell idle) — no text
        if prompt and ln.startswith(prompt):
            cmd = ln[len(prompt):]
            blocks.append({"command": cmd, "output": []})
        elif blocks:
            if ln:
                blocks[-1]["output"].append(ln)
        elif ln:
            pre.append(ln)
    blocks = [b for b in blocks if b["command"].strip() or b["output"]]
    if pre:
        blocks.insert(0, {"command": None, "output": pre})
    return blocks


def _cast_path(ref: str, cast_root: Path | None) -> Path:
    p = Path(ref)
    if not p.is_absolute():
        if cast_root is None:
            raise SceneError(f"relative cast path {ref!r} needs a cast_root")
        root = Path(cast_root).resolve()
        p = (root / p).resolve()
        if root not in p.parents and p != root:
            raise SceneError(f"cast path {ref!r} escapes cast_root")
    return p


def terminal_check(beat: dict, sources, cast_root: Path | None = None) -> dict:
    """{"cast", "sha256", "hits": [...]} for one terminal beat. Fail closed."""
    from app.services import craft
    ref = (beat or {}).get("cast") or ""
    res = {"cast": ref, "sha256": None, "hits": []}

    def hit(check, detail):
        res["hits"].append({"check": check, "detail": detail})
        return res
    try:
        path = _cast_path(ref, cast_root)
    except SceneError as e:
        return hit("terminal_cast_missing", str(e))
    if not path.is_file():
        return hit("terminal_cast_missing", f"cast {ref!r} not found")
    sha = sha256_file(path)
    res["sha256"] = sha
    want = beat.get("cast_sha256")
    if want and want != sha:
        return hit("terminal_cast_sha_mismatch", f"{ref}: sha256 {sha} != pinned {want}")
    try:
        blocks = cast_transcript(path, beat.get("prompt") or "$ ")
    except SceneError as e:
        return hit("terminal_cast_unreadable", str(e))
    if not blocks:
        return hit("terminal_not_literal", f"{ref}: recording shows no text")
    if sources is None:
        return hit("terminal_not_literal", f"{ref}: no on-screen allowlist (fail closed)")
    for k, b in enumerate(blocks):
        if b["command"] is None:
            ok = craft.cli_card_strict_literal({"type": "code", "lines": b["output"]}, sources)
        else:
            ok = craft.cli_card_strict_literal(
                {"type": "command", "command": b["command"], "output": b["output"]}, sources)
        if not ok:
            shown = b["command"] if b["command"] is not None else (b["output"] or [""])[0]
            hit("terminal_not_literal",
                f"{ref} block {k} {shown[:60]!r} not byte-exact in the allowlist")
    return res


def terminal_check_marker(beats, sources, cast_root: Path | None = None) -> dict | None:
    terms = [(i, b) for i, b in enumerate(beats or [])
             if isinstance(b, dict) and b.get("type") == "terminal"]
    if not terms:
        return None
    casts, hits = [], []
    for i, b in terms:
        r = terminal_check(b, sources, cast_root)
        casts.append({"i": i, "cast": r["cast"], "sha256": r["sha256"]})
        hits += [{"i": i, **h} for h in r["hits"]]
    return {"v": 1, "casts": casts, "hits": hits}


def terminal_check_hits(marker) -> list[str]:
    if marker is None:
        return []
    if not isinstance(marker, dict) or not isinstance(marker.get("hits"), list):
        return ["terminal_check: malformed marker (fail closed)"]
    out = [f"beat[{h.get('i')}] {h.get('check')}: {h.get('detail')}" for h in marker["hits"]]
    for c in marker.get("casts") or []:
        if not c.get("sha256"):
            if not any(f"beat[{c.get('i')}]" in x for x in out):
                out.append(f"beat[{c.get('i')}] terminal_cast_missing: no sha256 recorded")
    return out


def agg_binary(agg_bin: str | None = None) -> str:
    found = shutil.which(agg_bin or "agg")
    if not found:
        raise AggMissingError(
            f"asciinema-agg binary {agg_bin or 'agg'!r} not found on PATH — terminal "
            "scenes cannot render (no fake-typing fallback). Install agg on the render host.")
    return found


def render_terminal_clip(beat: dict, cast_root: Path | None, out_dir: Path, i: int, *,
                         width: int, agg_bin: str | None = None,
                         font_size: int | None = None, theme_name: str = "monokai",
                         run=subprocess.run) -> Path:
    """.cast → agg GIF → mp4 (ffmpeg). Returns the mp4 path. Raises on any failure."""
    agg = agg_binary(agg_bin)
    src = _cast_path(beat.get("cast") or "", cast_root)
    if not src.is_file():
        raise SceneError(f"cast {beat.get('cast')!r} not found")
    out_dir = Path(out_dir)
    gif = out_dir / f"term_{i}.gif"
    mp4 = out_dir / f"term_{i}.mp4"
    fsz = font_size or max(16, int(width * 0.028))
    try:
        run([agg, "--font-size", str(fsz), "--theme", theme_name, "--idle-time-limit", "2",
             str(src), str(gif)], check=True, capture_output=True, text=True, timeout=300)
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(gif), "-movflags", "faststart",
             "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(mp4)],
            check=True, capture_output=True, text=True, timeout=300)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"terminal render failed: {(e.stderr or e.stdout or '')[-400:]}")
    if not mp4.exists():
        raise RuntimeError("terminal render produced no clip")
    return mp4
