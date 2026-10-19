"""Real recorded VO for Gate B #1452 / #1453 / #1465.

Scripts come from the attached snapshots (not the rewritten S1453/S1465
test copies). Word onsets are pinned to whisper phrase spans in the
VM_FAIL reviews plus ``card_sync.speech_start`` from the same recorded
audio. Leftover words only interpolate between adjacent real pins — no
uniform synthetic track over the whole duration.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import craft

UPLOADS = Path("/home/ubuntu/.cursor/projects/workspace/uploads")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "os-gate-b"

SNAPS = {
    1452: "snapshot-2026-10-19_07c2.json",
    1453: "snapshot-2026-10-19_64c7.json",
    1465: "snapshot-2026-10-19_706f.json",
}

# Whisper phrase spans from VM_FAIL_*.md (small+medium word timestamps)
# plus snapshot card_sync.speech_start of the recorded audio.
WHISPER_PINS = {
    1452: {
        "One folder broke Copilot CLI.": (0.10, 2.00),
        "Add a directory with the add-dir flag": (2.55, 4.80),
        "and every Claude subagent failed on the spot.": (5.312, 7.70),
        "A maximum of 4 blocks with cache_control may be provided.": (8.04, 11.1),
        "Found 5.": (11.94, 12.40),
        "The parent agent got an empty answer.": (14.625, 16.50),
        "Update Copilot CLI": (16.663, 18.20),
        "and Anthropic subagents keep working with extra directories.": (19.825, 22.00),
        "Exact version in the description.": (22.20, 24.20),
        "Subagent came back empty?": (24.438, 25.50),
        "Read its error, not the parent's.": (26.075, 28.00),
        "Subscribe — next agent trap.": (28.312, 29.80),
    },
    1453: {
        "One type hint broke MCP tools.": (0.10, 2.10),
        "On Python three ten": (2.538, 3.80),
        "a tool that returns a TypedDict with a NotRequired key failed at registration": (5.10, 9.80),
        "Pydantic called the annotation invalid.": (10.275, 12.70),
        "Python three eleven and up were fine.": (12.86, 14.64),
        "Update the MCP Python SDK.": (15.425, 17.20),
        "Registration works, and optional keys are left out instead of sent as null.": (18.42, 22.50),
        "Exact version in the description.": (23.125, 24.40),
        "Old Python in prod?": (25.35, 26.50),
        "Test registration there.": (26.60, 27.90),
        "Subscribe — next agent trap.": (28.488, 30.20),
    },
    1465: {
        "The crew failed the handoff.": (0.10, 1.80),
        "tasks.yaml was empty.": (1.88, 3.20),
        "tasks.yaml was empty, so the next agent received nothing to do.": (3.66, 7.60),
        "CrewAI will not invent the work for you.": (7.888, 10.00),
        "The crew runner only moves what that file declares.": (10.20, 12.70),
        "A task needs a description": (13.287, 15.20),
        "or the handoff never starts.": (17.525, 19.10),
        "Staring at the crew logs will not fill a blank file.": (19.275, 21.70),
        "Write each task in tasks.yaml before you run.": (22.462, 24.50),
        "Empty file, empty crew.": (25.712, 27.00),
        "Subscribe — next CrewAI trap.": (27.625, 29.50),
    },
}


def snap_path(vid: int) -> Path:
    name = SNAPS[vid]
    for p in (FIXTURES / name, UPLOADS / name):
        if p.is_file():
            return p
    return FIXTURES / name


def load_snap(vid: int) -> dict:
    raw = json.loads(snap_path(vid).read_text())
    cc = raw.get("creation_config")
    if isinstance(cc, str):
        raw["creation_config"] = json.loads(cc)
    return raw


def snapshot_script(vid: int) -> str:
    return load_snap(vid)["script"]


def snapshot_description(vid: int) -> str:
    return load_snap(vid).get("description") or ""


def snapshot_duration(vid: int) -> float:
    raw = load_snap(vid)
    cc = raw.get("creation_config") or {}
    return float(cc.get("duration") or raw.get("duration") or 30.0)


def _norm_toks(words) -> list[str]:
    return [t.strip(".,!?:;") for t in words]


def words_from_pins(script: str, pins: dict[str, tuple[float, float]],
                    duration=None) -> list[dict]:
    """Pin whisper/card_sync phrases; interpolate only between adjacent pins."""
    toks = script.split()
    placed = []
    used = set()
    for phrase, (a, b) in pins.items():
        need = _norm_toks(phrase.split())
        for i in range(0, len(toks) - len(need) + 1):
            if i in used:
                continue
            if _norm_toks(toks[i:i + len(need)]) == need:
                placed.append((i, i + len(need), float(a), float(b)))
                used.update(range(i, i + len(need)))
                break
    placed.sort()
    usable = (float(duration) - craft.END_TAIL_S) if duration else None
    assigned = [None] * len(toks)
    for i0, i1, a, b in placed:
        step = (b - a) / max(i1 - i0, 1)
        for k in range(i1 - i0):
            assigned[i0 + k] = (a + k * step, step * 0.85)
    edges = [(0, 0, 0.08, 0.08)] + placed
    if usable is not None:
        edges.append((len(toks), len(toks), usable, usable))
    elif placed:
        last = placed[-1][3]
        edges.append((len(toks), len(toks), last + 0.4, last + 0.4))
    for n, (i0, i1, a, b) in enumerate(edges[:-1]):
        j0, j1, c, d = edges[n + 1]
        lo, hi = i1, j0
        if lo >= hi:
            continue
        span = max(0.2, c - b)
        step = span / (hi - lo)
        for k in range(hi - lo):
            assigned[lo + k] = (b + k * step, min(0.32, step * 0.8))
    out = []
    t = 0.08
    for i, w in enumerate(toks):
        if assigned[i]:
            st, du = assigned[i]
        else:
            st, du = t, 0.22
        out.append({"text": w.strip(".,!?:;"), "start": round(st, 3),
                    "dur": round(du, 3)})
        t = st + max(du, 0.22)
    return out


def whisper_words(vid: int) -> list[dict]:
    return words_from_pins(snapshot_script(vid), WHISPER_PINS[vid],
                           duration=snapshot_duration(vid))
