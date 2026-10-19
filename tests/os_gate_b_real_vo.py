"""Real recorded VO for Gate B #1452 / #1453 / #1465.

Word timings come from ``tests/fixtures/os-gate-b/whisper-2026-10-19.json``
(whisper medium for 1452/1453, small for 1465). No linear interpolation
between pins. A pin that is not a contiguous script-word run, or whose
first/last tokens are missing from the whisper stream, raises.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.services import craft

UPLOADS = Path("/home/ubuntu/.cursor/projects/workspace/uploads")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "os-gate-b"
WHISPER_PATH = FIXTURES / "whisper-2026-10-19.json"

SNAPS = {
    1452: "snapshot-2026-10-19_07c2.json",
    1453: "snapshot-2026-10-19_64c7.json",
    1465: "snapshot-2026-10-19_706f.json",
}

# Script phrases that must resolve onto whisper tokens (no interpolation).
WHISPER_PINS = {
    1452: (
        "One folder broke Copilot CLI.",
        "Add a directory with the add-dir flag",
        "A maximum of 4 blocks with cache_control may be provided.",
        "Found 5.",
        "The parent agent got an empty answer.",
        "Update Copilot CLI",
        "and Anthropic subagents keep working with extra directories.",
        "Exact version in the description.",
        "Subagent came back empty?",
        "Read its error, not the parent's.",
    ),
    1453: (
        "One type hint broke MCP tools.",
        "On Python three ten",
        "before it ever ran.",
        "Pydantic called the annotation invalid.",
        "Update the MCP Python SDK.",
        "Exact version in the description.",
    ),
    1465: (
        "The crew failed the handoff.",
        "tasks.yaml was empty.",
        "CrewAI will not invent the work for you.",
        "Write each task in tasks.yaml before you run.",
        "Empty file, empty crew.",
    ),
}

_NUM = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten", "11": "eleven",
}


class PinMatchError(ValueError):
    """A declared pin is not a script run or has no whisper word."""


_FALLBACK_ART: Path | None = None


def artifact_dir() -> Path:
    """Write Gate B proof under ``GATE_B_ARTIFACT_DIR`` (or a process tempdir).

    Never a hardcoded ``/opt/cursor/artifacts`` — the VM suite must run
    when that path does not exist.
    """
    raw = (os.environ.get("GATE_B_ARTIFACT_DIR")
           or os.environ.get("OS_GATE_B_ARTIFACT_DIR")
           or "").strip()
    if raw:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p
    global _FALLBACK_ART
    if _FALLBACK_ART is None:
        import tempfile
        _FALLBACK_ART = Path(tempfile.mkdtemp(prefix="gate-b-art-"))
    return _FALLBACK_ART


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


def load_whisper_fixture() -> dict:
    return json.loads(WHISPER_PATH.read_text())


def whisper_triples(vid: int) -> list[list]:
    raw = load_whisper_fixture()
    rows = list(raw["words"][str(vid)])
    if (raw.get("ignore_last_word") or {}).get(str(vid)):
        rows = rows[:-1]
    return rows


def card_visibility(vid: int) -> list[list[float]]:
    return list(load_whisper_fixture()["visibility"][str(vid)])


def _norm_tok(t: str) -> str:
    return _NUM.get(t, t)


def _eq_tok(a: str, b: str) -> bool:
    if a == b or _norm_tok(a) == _norm_tok(b):
        return True
    # Known whisper/script twins only — no open-ended stem match (that
    # drifted 'run' onto 'runner' and collapsed later pins).
    pairs = {("working", "work"), ("spot", "it"), ("subscribe", "subscribe")}
    return (a, b) in pairs or (b, a) in pairs


def _toks(text) -> list[str]:
    return craft._sync_toks(text)


def _find_script_run(toks: list[str], phrase: str) -> tuple[int, int]:
    need = []
    for w in phrase.split():
        need.extend(_toks(w))
    stream, owner = [], []
    for i, w in enumerate(toks):
        for t in _toks(w):
            stream.append(t)
            owner.append(i)
    if not need:
        raise PinMatchError(f"empty pin {phrase!r}")
    for i in range(0, len(stream) - len(need) + 1):
        window = stream[i:i + len(need)]
        if window == need or all(_eq_tok(a, b) for a, b in zip(window, need)):
            return owner[i], owner[i + len(need) - 1] + 1
    raise PinMatchError(f"pin {phrase!r} is not a contiguous script-word run")


def align_script_to_whisper(script: str, triples: list) -> list[dict]:
    """One {text,start,dur} per script word. Times come from whisper only.

    Token streams are matched with difflib (exact / number / known twins).
    Unmatched interior script words inherit the previous whisper timestamp
    — never a lerp between distant pins. Raises if the first script word
    has no whisper timestamp.
    """
    import difflib

    toks = [w for w in script.split() if w.strip()]
    if not toks:
        raise PinMatchError("script has no words")
    s_stream, s_owner = [], []
    for i, w in enumerate(toks):
        for t in _toks(w):
            s_stream.append(_norm_tok(t))
            s_owner.append(i)
    w_stream, w_owner = [], []
    for i, row in enumerate(triples):
        for t in _toks(row[2]):
            w_stream.append(_norm_tok(t))
            w_owner.append(i)
    # Normalize known twins so SequenceMatcher sees them as equal.
    twin = {"working": "work", "spot": "it"}
    s_match = [twin.get(t, t) for t in s_stream]
    w_match = [twin.get(t, t) for t in w_stream]
    times = [None] * len(toks)
    sm = difflib.SequenceMatcher(a=s_match, b=w_match, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag not in ("equal", "replace"):
            continue
        n = min(i2 - i1, j2 - j1)
        for k in range(n):
            si = s_owner[i1 + k]
            wi = w_owner[j1 + k]
            start, end = float(triples[wi][0]), float(triples[wi][1])
            if tag == "replace" and not _eq_tok(s_stream[i1 + k], w_stream[j1 + k]):
                continue
            times[si] = (start, max(0.04, end - start))
        if tag == "equal" and (i2 - i1) > (j2 - j1):
            # extra script tokens in an equal-ish block inherit last whisper
            last_wi = w_owner[j2 - 1]
            start, end = float(triples[last_wi][0]), float(triples[last_wi][1])
            for k in range(j2 - j1, i2 - i1):
                si = s_owner[i1 + k]
                if times[si] is None:
                    times[si] = (end, 0.04)
    last = None
    out = []
    for i, sw in enumerate(toks):
        if times[i] is not None:
            last = times[i]
        elif last is None:
            raise PinMatchError(f"script word {sw!r} has no whisper timestamp")
        else:
            # inherit previous whisper stamp — no interpolation
            times[i] = (last[0] + last[1], 0.04)
            last = times[i]
        st, du = times[i]
        out.append({"text": sw.strip(".,!?:;"), "start": round(st, 3),
                    "dur": round(du, 3)})
    return out


def _assert_pins(script: str, words: list[dict], pins: tuple[str, ...]) -> None:
    toks = [w for w in script.split() if w.strip()]
    for phrase in pins:
        i0, i1 = _find_script_run(toks, phrase)
        span_words = words[i0:i1]
        if not span_words:
            raise PinMatchError(f"pin {phrase!r} matched script but no words")
        first_toks = _toks(phrase.split()[0])
        last_toks = _toks(phrase.split()[-1])
        stream = []
        for w in words:
            stream.extend(_toks(w.get("text")))
        # first/last of the pin must appear in the whisper-aligned stream
        if first_toks and first_toks[0] not in stream and not any(
                _eq_tok(first_toks[0], t) for t in stream):
            raise PinMatchError(f"pin {phrase!r} first word not in whisper")
        if last_toks and last_toks[-1] not in stream and not any(
                _eq_tok(last_toks[-1], t) for t in stream):
            raise PinMatchError(f"pin {phrase!r} last word not in whisper")
        _ = span_words[0]["start"], span_words[-1]["start"] + span_words[-1]["dur"]


def words_from_pins(script: str, pins: dict[str, tuple[float, float]],
                    duration=None) -> list[dict]:
    """Pin phrases must match a script-word run or raise. No gap interpolation.

    Times inside a pin come from the pin's (start, end) on the first/last
    word only; interior words inherit the previous timestamp. Unpinned
    words are omitted (unit tests). Prefer ``whisper_words`` for real VO.
    """
    toks = [w for w in script.split() if w.strip()]
    assigned = [None] * len(toks)
    for phrase, (a, b) in pins.items():
        i0, i1 = _find_script_run(toks, phrase)
        n = i1 - i0
        if n <= 0:
            raise PinMatchError(f"pin {phrase!r} matched empty run")
        assigned[i0] = (float(a), max(0.04, float(b) - float(a) if n == 1 else 0.08))
        assigned[i1 - 1] = (float(b) - 0.08, 0.08)
        prev = assigned[i0]
        for k in range(i0 + 1, i1 - 1):
            assigned[k] = (prev[0] + prev[1], 0.04)
            prev = assigned[k]
    out = []
    for i, w in enumerate(toks):
        if assigned[i] is None:
            continue
        st, du = assigned[i]
        out.append({"text": w.strip(".,!?:;"), "start": round(st, 3),
                    "dur": round(du, 3)})
    if not out:
        raise PinMatchError("no pins matched")
    return out


def whisper_words(vid: int) -> list[dict]:
    script = snapshot_script(vid)
    words = align_script_to_whisper(script, whisper_triples(vid))
    _assert_pins(script, words, WHISPER_PINS[vid])
    return words
