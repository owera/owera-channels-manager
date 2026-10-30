"""OS/literal_cards Gate B + generator — VM_FAIL #1452/#1453/#1465.

Run: PYTHONPATH=. .venv/bin/python tests/verify_os_literal_cards.py

Reproductions use the visible-card tables and spoken-span anchors from
VM_FAIL_21fe.md (#1452), VM_FAIL_c638.md (#1453), VM_FAIL_0465.md (#1465).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from app.services import craft, topic_flags
from app.services.engines import storyboard
from tests.os_gate_b_real_vo import (
    PinMatchError, artifact_dir, card_visibility, load_whisper_fixture,
    snapshot_description, snapshot_duration, snapshot_script, whisper_triples,
    whisper_words, words_from_pins,
)

_checks = 0
UPLOADS = Path("/home/ubuntu/.cursor/projects/workspace/uploads")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "os-gate-b"


def _vm_fail(name: str) -> Path:
    for p in (FIXTURES / name, UPLOADS / name):
        if p.is_file():
            return p
    return FIXTURES / name


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def checks_of(hits) -> set[str]:
    return {h.get("check") for h in (hits or []) if isinstance(h, dict)}


# --- scripts = snapshot VO (the recorded audio), not rewritten copies ------
S1452 = snapshot_script(1452)
S1453 = snapshot_script(1453)
S1465 = snapshot_script(1465)
D1452 = snapshot_description(1452)
D1453 = snapshot_description(1453)
D1465 = snapshot_description(1465)

ALLOW1452 = [
    {"kind": "quote",
     "text": "A maximum of 4 blocks with cache_control may be provided.",
     "source": "https://github.com/github/copilot-cli/issues/4185"},
    {"kind": "log", "text": "Found 5.",
     "source": "https://github.com/github/copilot-cli/issues/4185"},
]


def _beat(typ, start, end, **kw):
    b = {"type": typ, "start": float(start), "dur": round(float(end) - float(start), 3)}
    b.update(kw)
    return b


def _apply_visibility(beats, vis):
    """Pin board start/end to BEFORE-mp4 visibility (30fps)."""
    if len(beats) != len(vis):
        raise AssertionError(f"visibility {len(vis)} vs board {len(beats)}")
    out = []
    for b, (a, c) in zip(beats, vis):
        nb = dict(b)
        nb["start"] = float(a)
        nb["dur"] = round(float(c) - float(a), 3)
        out.append(nb)
    return out


# Visible cards from VM_FAIL.md; start/end overwritten from the BEFORE mp4
# visibility list in whisper-2026-10-19.json.
B1452 = [
    _beat("hook", 0.00, 2.33, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("term_define", 2.53, 4.93, term="add-dir",
          definition="flag that adds a directory", cue="add-dir"),
    _beat("compare", 5.17, 6.73, title="Claude subagent",
          left={"title": "Claude subagent", "items": ["every one"]},
          right={"title": "On the spot", "items": ["failed"]}),
    _beat("quote", 7.07, 7.83,
          text="A maximum of 4 blocks with cache_control may be provided.",
          literal_quote=True),
    _beat("term_define", 8.07, 9.37, term="cache_control",
          definition="maximum of 4 blocks"),
    _beat("quote", 9.70, 11.73, text="cache_control", isolated=True, plain=True),
    _beat("compare", 11.93, 14.33, title="Over the limit",
          left={"title": "Found", "items": ["5"]},
          right={"title": "Limit", "items": ["one block over"]}),
    _beat("quote", 14.60, 16.43, text="The parent agent got an empty answer.",
          plain=True),
    _beat("term_define", 16.70, 19.10, term="Copilot CLI",
          definition="update for Anthropic subagents"),
    _beat("compare", 19.30, 21.70,
          left={"title": "Directories", "items": ["extra directories"]},
          right={"title": "Subagents", "items": ["keep working"]}),
    _beat("term_define", 21.90, 24.27, term="version",
          definition="exact version in the description"),
    _beat("quote", 24.60, 25.53, text="Subagent came back empty?", plain=True),
    _beat("compare", 25.77, 28.13,
          left={"title": "Subagent", "items": ["its error"]},
          right={"title": "Parent", "items": ["not the parent's"]}),
    _beat("cta", 28.37, 30.60, text="Subscribe · Agent traps"),
]

B1453 = [
    _beat("hook", 0.00, 2.33, text="One type hint broke MCP tools.", object="terminal"),
    _beat("term_define", 2.53, 4.93, term="Python three ten",
          definition="runtime where the tool failed"),
    _beat("term_define", 5.13, 6.63, term="NotRequired",
          definition="optional key on the TypedDict"),
    _beat("quote", 6.97, 9.23, text="registration", isolated=True, plain=True),
    _beat("term_define", 9.47, 11.83, term="Pydantic",
          definition="called the annotation invalid"),
    _beat("compare", 12.07, 14.43, title="Python runtime",
          left={"title": "three ten", "items": ["tool failed"]},
          right={"title": "three eleven", "items": ["and up were fine"]}),
    _beat("term_define", 14.67, 17.03, term="MCP Python SDK",
          definition="update it and registration works"),
    _beat("quote", 17.37, 18.53,
          text="Registration works, and optional keys are left out instead of sent as null.",
          literal_quote=True),
    _beat("term_define", 18.73, 20.47, term="Optional keys",
          definition="NotRequired fields on the return"),
    _beat("compare", 20.67, 22.97, title="Instead of",
          left={"title": "Left out", "items": ["not as null"]},
          right={"title": "Sent as null", "items": ["null on the wire"]}),
    _beat("quote", 23.30, 24.40, text="Exact version in the description.", plain=True),
    _beat("term_define", 24.60, 25.93, term="Old Python",
          definition="test registration in prod"),
    _beat("quote", 26.23, 27.90, text="prod", isolated=True, plain=True),
    _beat("cta", 28.13, 30.77, text="Subscribe · Agent traps"),
]

B1465 = [
    _beat("hook", 0.00, 7.43, text="The crew failed the handoff. tasks.yaml was empty.",
          object="terminal"),
    _beat("term_define", 7.63, 10.03, term="CrewAI",
          definition="will not invent the work for you"),
    _beat("term_define", 10.23, 12.63, term="crew runner",
          definition="only moves what that file declares"),
    _beat("term_define", 12.83, 15.23, term="task",
          definition="needs a description, an expected output"),
    _beat("compare", 15.47, 16.77,
          left={"title": "Needs", "items": ["an agent"]},
          right={"title": "Handoff", "items": ["never starts"]}),
    _beat("quote", 17.07, 19.10, text="handoff", isolated=True, plain=True),
    _beat("compare", 19.33, 21.70,
          left={"title": "Crew logs", "items": ["will not fill"]},
          right={"title": "Blank file", "items": ["a blank file"]}),
    _beat("term_define", 21.93, 23.23, term="before you run",
          definition="write each task in tasks.yaml"),
    _beat("quote", 23.57, 24.43, text="tasks yaml", isolated=True, plain=True),
    _beat("quote", 24.77, 27.03, text="Empty file, empty crew.", plain=True),
    _beat("cta", 27.27, 30.13, text="Subscribe · CrewAI"),
]
B1452 = _apply_visibility(B1452, card_visibility(1452))
B1453 = _apply_visibility(B1453, card_visibility(1453))
B1465 = _apply_visibility(B1465, card_visibility(1465))


def _norm_toks(words) -> list[str]:
    return [t.strip(".,!?:;") for t in words]


def words_anchor(script, anchors: dict[str, tuple[float, float]], duration=None,
                 rest_step=0.28):
    """Word timings: pin VM_FAIL phrases to (t0, t1); leftover words fill
    the time between pins (and the tail up to duration − END_TAIL_S)."""
    toks = script.split()
    pins = []
    used = set()
    for phrase, (a, b) in anchors.items():
        need = _norm_toks(phrase.split())
        for i in range(0, len(toks) - len(need) + 1):
            if i in used:
                continue
            if _norm_toks(toks[i:i + len(need)]) == need:
                pins.append((i, i + len(need), float(a), float(b)))
                used.update(range(i, i + len(need)))
                break
    pins.sort()
    usable = (float(duration) - craft.END_TAIL_S) if duration else None
    assigned = [None] * len(toks)
    for i0, i1, a, b in pins:
        step = (b - a) / max(i1 - i0, 1)
        for k in range(i1 - i0):
            assigned[i0 + k] = (a + k * step, step * 0.85)
    edges = [(0, 0, 0.08, 0.08)] + pins
    if usable is not None:
        edges.append((len(toks), len(toks), usable, usable))
    else:
        last_t = pins[-1][3] if pins else 0.08
        edges.append((len(toks), len(toks), last_t + rest_step * 2, last_t + rest_step * 2))
    for n, (i0, i1, a, b) in enumerate(edges[:-1]):
        j0, j1, c, d = edges[n + 1]
        lo, hi = i1, j0
        if lo >= hi:
            continue
        t0, t1 = b, c
        span = max(0.2, t1 - t0)
        step = span / (hi - lo)
        for k in range(hi - lo):
            assigned[lo + k] = (t0 + k * step, min(0.32, step * 0.8))
    out = []
    t = 0.08
    for i, w in enumerate(toks):
        if assigned[i]:
            st, du = assigned[i]
        else:
            st, du = t, 0.22
        out.append({"text": w.strip(".,!?:;"), "start": round(st, 3), "dur": round(du, 3)})
        t = st + max(du, rest_step)
    return out


print("VM_FAIL.md files are attached")
ok(_vm_fail("VM_FAIL_21fe.md").is_file(), "VM_FAIL #1452 attached")
ok(_vm_fail("VM_FAIL_c638.md").is_file(), "VM_FAIL #1453 attached")
ok(_vm_fail("VM_FAIL_0465.md").is_file(), "VM_FAIL #1465 attached")
ok(topic_flags.has(topic_flags.LITERAL_CARDS, 47)
   and topic_flags.has(topic_flags.ALLOWLIST_REQUIRED, 47),
   "topic 47 is literal_cards + allowlist_required")


print("#1452 BEFORE (VM_FAIL_21fe) — Gate B FAILs")
w1452 = whisper_words(1452)
h1452 = craft.os_card_hits(B1452, S1452, ALLOW1452, words=w1452)
c1452 = checks_of(h1452)
ok("vs_card_not_allowed" in c1452, f"#1452 vs_card_not_allowed ({c1452})")
ok("single_term_card" in c1452, f"#1452 single_term_card ({c1452})")
ok("card_text_not_literal" in c1452 or "vs_card_not_allowed" in c1452,
   "#1452 invented compare copy is a fail")
# cache_control isolated after the quote that already showed it
ok(any(h.get("check") == "echo_card" and h.get("i") == 5 for h in h1452),
   f"#1452 isolated cache_control after the quote → echo_card ({h1452})")
qh1452 = craft.quote_before_speech_hits(B1452, w1452)
ok(qh1452 and qh1452[0]["check"] == "quote_before_speech" and qh1452[0]["i"] == 3,
   f"#1452 quote at 7.07 before speech 8.04 → quote_before_speech ({qh1452})")
g1452 = craft.video_maker_gate(B1452, brand="os", script=S1452, words=w1452,
                              sources=ALLOW1452, duration=snapshot_duration(1452),
                              description=D1452)
ok(g1452["checks"]["B"] == "FAIL", f"#1452 Gate B FAIL ({g1452['reasons'][:2]})")


print("#1453 BEFORE (VM_FAIL_c638) — Gate B FAILs")
w1453 = whisper_words(1453)
h1453 = craft.os_card_hits(B1453, S1453, None, words=w1453)
c1453 = checks_of(h1453)
ok("single_term_card" in c1453, f"#1453 isolated registration/prod → single_term_card ({c1453})")
ok("vs_card_not_allowed" in c1453, f"#1453 VS templates ({c1453})")
ok(any("null on the wire" in (h.get("detail") or "") or h.get("check") == "vs_card_not_allowed"
       for h in h1453),
   "#1453 invented 'null on the wire' compare fails")
qh1453 = craft.quote_before_speech_hits(B1453, w1453)
ok(qh1453 and any(h.get("i") == 7 for h in qh1453),
   f"#1453 quote at 17.37 before speech 18.42 → quote_before_speech ({qh1453})")
g1453 = craft.video_maker_gate(B1453, brand="os", script=S1453, words=w1453,
                              duration=snapshot_duration(1453), description=D1453)
ok(g1453["checks"]["B"] == "FAIL", "#1453 Gate B FAIL")


print("#1465 BEFORE (VM_FAIL_0465) — Gate B FAILs")
w1465 = whisper_words(1465)
h1465 = craft.os_card_hits(B1465, S1465, None, words=w1465)
c1465 = checks_of(h1465)
ok("single_term_card" in c1465, f"#1465 isolated handoff / tasks yaml ({c1465})")
ok("vs_card_not_allowed" in c1465, f"#1465 jumble VS ({c1465})")
sh1465 = craft.stale_card_hits(B1465, w1465, S1465)
ok(sh1465 and sh1465[0]["i"] == 0, f"#1465 frozen hook 7.43s → stale_card ({sh1465})")
g1465 = craft.video_maker_gate(B1465, brand="os", script=S1465, words=w1465,
                              duration=snapshot_duration(1465), description=D1465)
ok(g1465["checks"]["B"] == "FAIL", "#1465 Gate B FAIL")
ok(any(h.get("check") == "repeated_vo_sentence"
       for h in craft.repeated_vo_sentence_hits(S1465)),
   "#1465 snapshot VO repeats 'tasks.yaml was empty'")
ok(any(h.get("check") == "description_missing_source"
       for h in craft.description_missing_source_hits(D1465, brand="os")),
   "#1465 description has no public source link")


print("fail-closed + hook skip + whisper pins")
ok(craft.card_timing_marker([], None, fail_closed=True)["hits"][0]["check"]
   == craft.MISSING_WORDS_CHECK,
   "missing word timings → missing_word_timings (fail closed)")
ok(craft.card_timing_marker([], None, fail_closed=False) is None,
   "RR fail-open: no words → no marker")
bare = [
    _beat("hook", 0.0, 1.5, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 2.0, 3.5, text="this card is not in the VO at all", plain=True),
    _beat("cta", 4.0, 6.5, text="Subscribe · Agent traps"),
]
ok(any(h["check"] == craft.UNSPOKEN_CARD_CHECK
       for h in craft.stale_card_hits(bare, w1452, S1452, fail_closed=True)),
   "card with no spoken span → unspoken_card (fail closed)")
ok(not craft.stale_card_hits(bare, w1452, S1452, fail_closed=False),
   "RR fail-open: unspoken card is skipped")
hook_two = [
    _beat("hook", 0.0, 3.5,
          text="The crew failed the handoff. tasks.yaml was empty.",
          object="terminal"),
    _beat("quote", 3.66, 5.0, text="CrewAI will not invent the work for you.",
          plain=True),
    _beat("cta", 7.0, 9.5, text="Subscribe · CrewAI"),
]
ok("card_text_not_literal" not in checks_of(
    craft.os_card_hits(hook_two, S1465, None, words=w1465)),
   "two-sentence hook is not an OS mid-card literal FAIL")
span8 = craft.spoken_span(
    "and Anthropic subagents keep working with extra directories.", w1452)
ok(span8 and (span8[1] - span8[0]) > 3.0,
   f"#1452 card 8 spoken {span8} > 3.0s")
frozen8 = [
    _beat("hook", 0.0, 2.3, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 19.82, 22.26,
          text="and Anthropic subagents keep working with extra directories.",
          plain=True),
    _beat("cta", 24.0, 26.5, text="Subscribe · Agent traps"),
]
ok(any(h["check"] == "sentence_too_long" and h["i"] == 1
       for h in craft.sentence_too_long_hits(frozen8, w1452)),
   "#1452 card 8 spoken 18.36–22.08 → sentence_too_long")
span6 = craft.spoken_span("Write each task in tasks.yaml before you run.", w1465)
ok(span6 and abs(span6[1] - 25.28) < 0.02,
   f"#1465 'run.' ends at {span6}")
frozen6 = [
    _beat("hook", 0.0, 3.5,
          text="The crew failed the handoff. tasks.yaml was empty.",
          object="terminal"),
    _beat("quote", 22.46, 24.76,
          text="Write each task in tasks.yaml before you run.", plain=True),
    _beat("cta", 27.0, 30.0, text="Subscribe · CrewAI"),
]
ok(any(h["check"] == "card_exits_early" and h["i"] == 1
       for h in craft.card_exits_early_hits(frozen6, w1465)),
   "#1465 card 6 ends 24.76, last word run. at 25.28 → card_exits_early")
try:
    words_from_pins(S1452, {"this phrase is not spoken": (0.0, 1.0)})
    raise AssertionError("unmatched pin must raise")
except PinMatchError:
    pass
ok(True, "unmatched pin raises PinMatchError (no interpolation fallback)")
_prev_art = os.environ.get("GATE_B_ARTIFACT_DIR")
with tempfile.TemporaryDirectory() as tmp:
    os.environ["GATE_B_ARTIFACT_DIR"] = tmp
    try:
        p = artifact_dir()
        ok(p.resolve() == Path(tmp).resolve(),
           f"GATE_B_ARTIFACT_DIR wins ({p})")
    finally:
        if _prev_art is None:
            os.environ.pop("GATE_B_ARTIFACT_DIR", None)
        else:
            os.environ["GATE_B_ARTIFACT_DIR"] = _prev_art


print("whisper fixture + BEFORE mp4 visibility × visual hold")
fx = load_whisper_fixture()
ok(fx["models"]["1452"] == "whisper-medium"
   and fx["models"]["1453"] == "whisper-medium"
   and fx["models"]["1465"] == "whisper-small",
   "1452/1453=medium, 1465=small")
ok(fx["ignore_last_word"].get("1453") is True, "1453 last whisper word ignored")
ok(fx["words"]["1452"][0] == [0.0, 0.34, "One"]
   and fx["words"]["1452"][47] == [18.36, 18.78, "and"]
   and abs(fx["words"]["1465"][-10][1] - 25.28) < 1e-9
   and fx["words"]["1465"][-10][2] == "run.",
   "fixture words are the 2026-10-19 whisper triples")
ok(len(whisper_triples(1453)) == len(fx["words"]["1453"]) - 1,
   "1453 artifact last word dropped from the stream")
ok(fx["visibility"]["1452"][3] == [7.07, 7.83]
   and fx["visibility"]["1453"][7] == [17.37, 18.53]
   and fx["visibility"]["1465"][4] == [15.47, 16.77],
   "BEFORE mp4 visibility matches the 30fps list")


def _hold_reasons(board):
    g = craft.video_maker_gate(board, beat_timing=craft.BEAT_TIMING_CURRENT,
                               brand="os")
    return g, [r for r in (g.get("reasons") or []) if "mid card" in r]


for vid, board in ((1452, B1452), (1453, B1453), (1465, B1465)):
    vis = card_visibility(vid)
    ok(len(vis) == len(board), f"#{vid} visibility cards = BEFORE board")
    long_mids = []
    for i, b in enumerate(board):
        a, c = vis[i]
        ok(abs(float(b["start"]) - a) < 1e-9
           and abs(float(b["start"]) + float(b["dur"]) - c) < 1e-9,
           f"#{vid} card {i} start/end = visibility {a}–{c}")
        if (b.get("type") or "") in ("hook", "cta", "endcard"):
            continue
        nxt = vis[i + 1][0] if i + 1 < len(vis) else c
        visible = c - a
        incl_fade = nxt - a
        if visible > 3.0 + 1e-6 or incl_fade > 3.0 + 1e-6:
            long_mids.append((i, visible, incl_fade))
    g, mid = _hold_reasons(board)
    if long_mids:
        ok(g["checks"]["B"] == "FAIL" and mid,
           f"#{vid} BEFORE mid visible >3.0s incl fade → beat_hold "
           f"({long_mids} {g.get('reasons')})")
    else:
        ok(not mid,
           f"#{vid} BEFORE mp4 mids are ≤3.0s incl fade (no beat_hold) "
           f"({[round(vis[i+1][0]-vis[i][0], 2) if i+1 < len(vis) else round(vis[i][1]-vis[i][0], 2) for i, b in enumerate(board) if b.get('type') not in ('hook', 'cta', 'endcard')]})")

# VM examples that passed on 56dc5e9 (quote hold exemption): must FAIL on OS.
for vid, hold, text in (
    (1452, 3.31, "A maximum of 4 blocks with cache_control may be provided."),
    (1453, 3.71, "Registration works, and optional keys are left out instead of sent as null."),
    (1465, 5.87, "A task needs a description, an expected output, and an agent, or the handoff never starts."),
):
    board = [
        {"type": "hook", "start": 0.0, "dur": 1.5,
         "text": "One folder broke Copilot CLI.", "object": "terminal"},
        {"type": "quote", "start": 2.0, "dur": hold, "text": text,
         "literal_quote": True},
        {"type": "cta", "start": round(2.0 + hold + 0.12, 3), "dur": 2.0,
         "text": "Subscribe · Agent traps"},
    ]
    g, mid = _hold_reasons(board)
    ok(g["checks"]["B"] == "FAIL" and any("beat[1]" in r for r in mid),
       f"#{vid} mid visible {hold:.2f}s (quote) → OS beat_hold FAIL "
       f"({g.get('reasons')})")
    scr = snapshot_script(vid)
    g_rr = craft.video_maker_gate(
        board, beat_timing=craft.BEAT_TIMING_CURRENT, brand="rr",
        sources=ALLOW1452, script=scr, words=whisper_words(vid))
    ok(not any("beat[1]" in r and "mid card" in r
               for r in (g_rr.get("reasons") or [])),
       f"#{vid} RR keeps the quote hold exemption")


print("per-field literal (positive + negative)")
ok(craft.card_is_literal(
    {"type": "quote", "text": "The parent agent got an empty answer.", "plain": True},
    S1452), "whole VO sentence is literal")
ok(not craft.card_is_literal(
    {"type": "term_define", "term": "add-dir",
     "definition": "flag that adds a directory"}, S1452),
   "invented term_define subtitle is not literal")
ok(not craft.card_is_literal(
    {"type": "compare", "left": {"title": "Left out", "items": ["not as null"]},
     "right": {"title": "Sent as null", "items": ["null on the wire"]}}, S1453),
   "invented compare items are not literal")
ok(craft.card_is_literal(
    {"type": "quote", "text": "Found 5.", "plain": True}, S1452, ALLOW1452)
   or craft.source_literal("Found 5.", ALLOW1452, {"log"}),
   "allowlist 'Found 5.' is literal")
ok(not craft.literal_field_hits(
    [{"type": "hook", "text": "One folder broke Copilot CLI."},
     {"type": "quote", "text": "The parent agent got an empty answer.", "plain": True},
     {"type": "cta", "text": "Subscribe · Agent traps"}], S1452),
   "literal_field_hits empty on a clean board")
ok(any(h["check"] == "card_text_not_literal" for h in craft.literal_field_hits(
    [{"type": "hook", "text": "One folder broke Copilot CLI."},
     {"type": "statement", "text": "Invented slogan on screen."},
     {"type": "cta", "text": "Subscribe"}], S1452)),
   "invented statement → card_text_not_literal")


print("isolated term banned on OS even with allowlist")
term_allow = [{"kind": "quote", "text": "cache_control", "source": "https://x.test"}]
clean = [
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 2.12, 4.12, text="cache_control", isolated=True, plain=True),
    _beat("cta", 4.24, 7.0, text="Subscribe · Agent traps"),
]
ok("single_term_card" in checks_of(craft.os_card_hits(clean, S1452, term_allow)),
   "allowlisted isolated term still → single_term_card")
ok("single_term_card" in checks_of(craft.os_card_hits(clean, S1452, None)),
   "same term without allowlist → single_term_card")


print("CMO quote hold: first word → last word + 0.3; #1452 CAPIError 8.04–11.4")
qtext = "A maximum of 4 blocks with cache_control may be provided."
qt = []
t0 = 8.04
for w in qtext.split():
    qt.append({"text": w.strip(".,"), "start": round(t0, 3), "dur": 0.28})
    t0 += 0.28
# pin last word so spoken end is 11.1 (CMO: card 8.04–11.4)
n = len(qtext.split())
qt[n - 1]["start"] = 10.82
qt[n - 1]["dur"] = 0.28  # end 11.10
qb = [
    _beat("hook", 0.0, 2.3, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 7.07, 7.83, text=qtext, literal_quote=True),
    _beat("cta", 14.0, 16.5, text="Subscribe · Agent traps"),
]
storyboard._time_literal_quotes(qb, qt, ALLOW1452)
ok(abs(qb[1]["start"] - 8.04) < 1e-6, f"quote start = first word ({qb[1]['start']})")
ok(abs((qb[1]["start"] + qb[1]["dur"]) - 11.4) < 0.05,
   f"#1452 CAPIError holds ~8.04–11.4 (end {qb[1]['start'] + qb[1]['dur']:.2f})")
ok(qb[1]["dur"] > craft.MID_BEAT_MAX_S, "literal quote exceeds the mid cap")
ok(not craft.quote_before_speech_hits(qb, qt), "timed quote is not before speech")
gg = craft.video_maker_gate(qb, beat_timing=craft.BEAT_TIMING_CURRENT,
                            sources=ALLOW1452, brand="os")
ok(gg["checks"]["B"] == "FAIL"
   and any("mid card" in r and "beat[1]" in r for r in (gg.get("reasons") or [])),
   f"OS quote visible >3.0s incl fade → beat_hold FAIL ({gg.get('reasons')})")
gg_rr = craft.video_maker_gate(qb, beat_timing=craft.BEAT_TIMING_CURRENT,
                               sources=ALLOW1452, brand="rr", words=qt)
ok(not any("mid card" in r and "beat[1]" in r for r in (gg_rr.get("reasons") or [])),
   "RR keeps the spoken-span quote hold exemption")


print("generator post-filter (kill switch off) converts #1452 templates")
craft.OS_VO_SENTENCE_CARDS = False
try:
    w, dur = [], 0.1
    for tok in S1452.split():
        w.append({"text": tok.strip(".,!?:;"), "start": round(dur, 3), "dur": 0.28})
        dur += 0.32
    dur = round(dur + 2.5, 2)
    llm_board = {
        "beats": [
            {"type": "hook", "cue": "One folder", "text": "One folder broke Copilot CLI."},
            {"type": "compare", "cue": "Add a directory",
             "left": {"title": "Found", "items": ["5"]},
             "right": {"title": "Limit", "items": ["one block over"]}},
            {"type": "term_define", "cue": "cache_control", "term": "cache_control",
             "definition": "maximum of 4 blocks"},
            {"type": "quote", "cue": "A maximum of 4",
             "text": "A maximum of 4 blocks with cache_control may be provided."},
            {"type": "cta", "cue": "Subscribe", "text": "Subscribe · Agent traps"},
        ]
    }
    html = storyboard.compose(
        subject="One folder broke Copilot CLI. · Agent traps 12",
        script=S1452, words=w, duration=dur, resolution="portrait",
        width=1080, height=1920, topic_id=47, content_format="short",
        language="English",
        llm=lambda *a, **k: json.dumps(llm_board),
        brand="os", topic_name="Agent traps", on_screen_allow=ALLOW1452)
finally:
    craft.OS_VO_SENTENCE_CARDS = True
ok(html, "LLM-path compose returns HTML")
after = craft.beats_from_html(html)
types = [b.get("type") for b in after]
ok("compare" not in types, f"post-filter dropped compare ({types})")
ok("term_define" not in types, f"post-filter dropped term_define ({types})")
ok(not any(craft.is_term_card(b) for b in after), "no isolated-term leftover")
g_after = craft.video_maker_gate(
    after, brand="os", script=S1452, words=w, sources=ALLOW1452, duration=dur,
    beat_timing=craft.BEAT_TIMING_CURRENT)
ok("vs_card_not_allowed" not in str(g_after.get("reasons")),
   f"AFTER has no vs_card_not_allowed ({g_after.get('reasons')})")
ok("single_term_card" not in str(g_after.get("reasons")),
   f"AFTER has no single_term_card ({g_after.get('reasons')})")

print("convert-in-place keeps the slot (no blank_gap from a drop)")
slot = [
    _beat("hook", 0.00, 2.33, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 2.53, 4.93, text="Add a directory with the add-dir flag, and every Claude subagent failed on the spot.",
          plain=True),
    _beat("compare", 5.17, 6.73, title="Over the limit", cue="Found 5",
          left={"title": "Found", "items": ["5"]},
          right={"title": "Limit", "items": ["one block over"]}),
    _beat("quote", 6.93, 8.93, text="The parent agent got an empty answer.", plain=True),
    _beat("cta", 9.13, 12.00, text="Subscribe · Agent traps"),
]
slips_c = []
storyboard._plain_os_literal_mids(slot, S1452, ALLOW1452, os=True, literal=True,
                                  slips=slips_c)
ok(slot[2].get("type") != "compare", f"compare became {slot[2].get('type')}")
ok(abs(float(slot[2].get("start") or 0) - 5.17) < 1e-6
   and abs(float(slot[2].get("dur") or 0) - 1.56) < 1e-6,
   f"converted card kept the slot ({slot[2].get('start')}+{slot[2].get('dur')})")
ok(any(s.get("kind") == "vs_card_not_allowed" for s in slips_c),
   f"slip logged ({slips_c})")
ok(not craft.blank_gap_hits(slot), "in-place convert opened no hole")


print("OS prompt forbids VS / isolated term")
sp = storyboard._system_prompt(
    ["hook", "statement", "stat", "compare", "list", "term_define", "quote", "cta"],
    literal=True, os=True)
ok("OS CARDS" in sp and "NEVER emit a compare" in sp, "OS prompt has the VS ban")
ok("not even when the term is in the allowlist" in sp, "OS prompt bans isolated terms")
ok("Your RAG pulls junk" in sp and "Fixed-size chunking" not in sp,
   "OS example is whole-sentence cards, not term_define")
sp0 = storyboard._system_prompt(
    ["hook", "statement", "stat", "compare", "list", "term_define", "quote", "cta"])
ok("Fixed-size chunking" in sp0 and "OS CARDS" not in sp0,
   "non-OS prompt still shows the compare/term example")


print("fill hole with a whole-sentence VO card")
hole = [
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 5.5, 7.5, text="The parent agent got an empty answer.", plain=True),
    _beat("cta", 7.7, 10.5, text="Subscribe · Agent traps"),
]
slips = []
w_fill, t = [], 0.1
for tok in S1452.split():
    w_fill.append({"text": tok.strip(".,!?:;"), "start": round(t, 3), "dur": 0.22})
    t += 0.26
storyboard._fill_blank_gaps(hole, S1452, w_fill, slips)
ok(len(hole) >= 4, f"fill inserted a card ({[b.get('type') for b in hole]})")
ok(any(s.get("kind") == "blank_gap_filled" for s in slips) or not craft.blank_gap_hits(hole),
   f"hole closed ({slips})")
ok(not craft.blank_gap_hits(hole), "filled board has no blank_gap")


print("new Gate B FAILs (isolated)")
tiny = [
    _beat("hook", 0.0, 1.5, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("code", 1.6, 2.1, lines=["Found 5."], lang="log"),
    _beat("quote", 2.2, 3.0, text="The parent agent got an empty answer.", plain=True),
    _beat("cta", 4.0, 8.0, text="Subscribe · Agent traps"),
]
ok(any(h["check"] == "code_read_too_short"
       for h in craft.code_read_too_short_hits(tiny)),
   "Found 5. at 0.50s → code_read_too_short (floor 1.2s)")
ok(craft.CODE_READ_MIN_S == 1.2, "CODE_READ_MIN_S = 1.2s")
ok(any(h["check"] == "card_overlap" for h in craft.card_time_overlap_hits([
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 1.5, 3.5, text="Found 5.", plain=True),
    _beat("cta", 3.6, 6.0, text="Subscribe"),
])), "overlapping visual holds → card_overlap")
early = [
    _beat("hook", 0.0, 1.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 1.0, 2.5, text="Found 5.", plain=True),
    _beat("cta", 2.6, 5.0, text="Subscribe"),
]
w_early = words_from_pins(S1452, {"Found 5.": (2.0, 2.5),
                                  "One folder broke Copilot CLI.": (0.10, 1.00)})
ok(any(h["check"] == "card_enters_early" and h["i"] == 1
       for h in craft.card_enters_early_hits(early, w_early)),
   "card enters 1.0s before speech 2.0s → card_enters_early")
exit_b = [
    _beat("hook", 0.0, 1.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 8.04, 9.00, text="A maximum of 4 blocks with cache_control may be provided.",
          literal_quote=True),
    _beat("cta", 11.5, 14.0, text="Subscribe"),
]
ok(any(h["check"] == "card_exits_early"
       for h in craft.card_exits_early_hits(exit_b, w1452)),
   "CAPIError card ending 9.00 before last word 11.1 → card_exits_early")
ok(any(h["check"] == "cta_after_video_end"
       for h in craft.cta_after_video_end_hits(tiny, duration=6.0)),
   "CTA ending 8.0s on a 6.0s video → cta_after_video_end")
ok(not craft.description_missing_source_hits(D1452, brand="os")
   and not craft.description_missing_source_hits(D1453, brand="os"),
   "#1452/#1453 descriptions have public source links")
ok(not craft.text_in_script(S1452, "Found 5."),
   "text_in_script rejects inverted containment (script ⊂ card)")
ok(not craft.text_in_script(S1452, S1452),
   "whole-script echo is not text_in_script of itself")
ok(craft.text_in_script("Found 5.", S1452),
   "contiguous card ⊂ script still passes")
enum = "A task needs a description, an expected output, and an agent."
ok(craft.is_enumeration_clause_split(enum),
   "enumeration is detected")
ok(not craft.is_enumeration_clause_split(
    "Add a directory with the add-dir flag, and every Claude subagent failed on the spot."),
   "coordinated clauses with a verb are not an enumeration")
ok(storyboard._os_sentence_units(enum, force=True) == [enum],
   "generator does not fragment an enumeration")
ok(any(h["check"] == "enumeration_needs_rewrite"
       for h in craft.enumeration_hits(
           [{"type": "quote", "text": "an expected output", "plain": True}],
           enum + " Subscribe — next CrewAI trap.")),
   "enumeration fragment on screen → enumeration_needs_rewrite")


print("AFTER compose (deterministic, no LLM) + committed before/after tables")


def _gate(beats, *, duration, script, words, topic_id, sources, description=None):
    ctext = craft.card_text_marker(beats, script, words, brand="os", sources=sources)
    cvo = craft.card_vo_marker(beats, script, brand="os")
    ctime = craft.card_timing_marker(beats, words, script, duration=duration,
                                    sources=sources)
    al = craft.allowlist_required_marker(topic_id, sources)
    sync = craft.card_sync_marker(beats, words, "short", script=script)
    return craft.video_maker_gate(
        beats, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT,
        card_text=ctext, card_vo=cvo, duration=duration, card_timing=ctime,
        card_sync=sync, allowlist_check=al, brand="os", script=script, words=words,
        sources=sources, description=description)


def _kinds(g: dict) -> list[str]:
    out = []
    for k in g.get("kinds") or []:
        chk = k.get("check") if isinstance(k, dict) else None
        if chk and chk not in out:
            out.append(chk)
    for r in g.get("reasons") or []:
        for name in ("blank_gap", "stale_card", "quote_before_speech",
                     "vs_card_not_allowed", "single_term_card",
                     "card_text_not_literal", "echo_card", "allowlist_required",
                     "sentence_too_long", "card_exits_early", "card_enters_early",
                     "card_overlap", "cta_after_video_end", "repeated_vo_sentence",
                     "code_read_too_short", "description_missing_source",
                     "enumeration_needs_rewrite", "beat_hold",
                     "missing_word_timings", "unspoken_card"):
            if name in r and name not in out:
                out.append(name)
    return out


def _md_board(beats, g, label):
    lines = [f"### {label}", "",
             "| # | type | start | dur | end | text |",
             "|---|------|------:|----:|----:|------|"]
    for i, b in enumerate(beats):
        st = float(b.get("start") or 0)
        du = float(b.get("dur") or 0)
        text = (craft.beat_screen_text(b) or b.get("text") or "")[:90]
        text = text.replace("|", "\\|")
        lines.append(f"| {i} | {b.get('type')} | {st:.2f} | {du:.2f} | {st + du:.2f} | {text} |")
    kinds = _kinds(g)
    lines += ["", f"**Gate B:** `{g['checks'].get('B')}` / result `{g.get('result')}`",
              "", f"**kinds:** {', '.join(kinds) or '(none)'}", "",
              "**reasons:**", ""]
    for h in (g.get("reasons") or ["(none)"]):
        lines.append(f"- {h}")
    lines.append("")
    return lines


CASES = [
    dict(vid=1452, topic=47, dur=snapshot_duration(1452), script=S1452, before=B1452,
         words=w1452, sources=ALLOW1452, description=D1452,
         subject="One folder broke Copilot CLI. · Agent traps 12",
         name="Agent traps", fail_file="VM_FAIL_21fe.md"),
    dict(vid=1453, topic=47, dur=snapshot_duration(1453), script=S1453, before=B1453,
         words=w1453, sources=None, description=D1453,
         subject="One type hint broke MCP tools. · Agent traps 14",
         name="Agent traps", fail_file="VM_FAIL_c638.md"),
    dict(vid=1465, topic=7, dur=snapshot_duration(1465), script=S1465, before=B1465,
         words=w1465, sources=None, description=D1465,
         subject="The crew failed the handoff. tasks.yaml was empty. · CrewAI 21",
         name="CrewAI", fail_file="VM_FAIL_0465.md"),
]

DOCS = Path("docs")
ART = artifact_dir()
summaries = []
per_md = []
for c in CASES:
    gb = _gate(c["before"], duration=c["dur"], script=c["script"], words=c["words"],
               topic_id=c["topic"], sources=c["sources"], description=c.get("description"))
    html = storyboard.compose(
        subject=c["subject"], script=c["script"], words=c["words"], duration=c["dur"],
        resolution="portrait", width=1080, height=1920, topic_id=c["topic"],
        content_format="short", language="English",
        llm=lambda *a, **k: (_ for _ in ()).throw(AssertionError("AFTER must not call LLM")),
        brand="os", topic_name=c["name"], on_screen_allow=c["sources"])
    ok(html, f"#{c['vid']} AFTER compose returns HTML")
    after = craft.beats_from_html(html)
    types = [b.get("type") for b in after]
    ok(not ({"compare", "term_define", "list"} & set(types)),
       f"#{c['vid']} AFTER has no banned templates ({types})")
    ok(not any(craft.is_term_card(b) for b in after),
       f"#{c['vid']} AFTER has no isolated-term card")
    ga = _gate(after, duration=c["dur"], script=c["script"], words=c["words"],
               topic_id=c["topic"], sources=c["sources"],
               description=c.get("description"))
    ok("vs_card_not_allowed" not in _kinds(ga)
       and "single_term_card" not in _kinds(ga),
       f"#{c['vid']} AFTER Gate B dropped VS/term ({_kinds(ga)})")
    note = (f"BEFORE = board visível do `{c['fail_file']}` (main `c35e110`). "
            "AFTER = `storyboard.compose` no script do snapshot + âncoras "
            "whisper/card_sync do áudio gravado (sem S1453/S1465 reescrito, "
            "sem timing sintético uniforme).")
    body = [f"# #{c['vid']} (topic {c['topic']}, duration {c['dur']:.2f}s)", "",
            note, ""]
    body += _md_board(c["before"], gb, "BEFORE (main c35e110 / VM_FAIL)")
    body += _md_board(after, ga, "AFTER (este branch, 1 frase da VO)")
    md = "\n".join(body)
    per_md.append(md)
    (ART / f"gate-b-{c['vid']}-before-after.md").write_text(md)
    summaries.append({
        "id": c["vid"], "topic": c["topic"],
        "before_B": gb["checks"]["B"], "after_B": ga["checks"]["B"],
        "before_kinds": _kinds(gb), "after_kinds": _kinds(ga),
        "hook_before": float(c["before"][0].get("dur") or 0),
        "hook_after": float(after[0].get("dur") or 0),
        "after": after, "ga": ga,
    })

s1452 = next(s for s in summaries if s["id"] == 1452)
s1453 = next(s for s in summaries if s["id"] == 1453)
s1465 = next(s for s in summaries if s["id"] == 1465)
q = next((b for b in s1452["after"]
          if "maximum of 4 blocks" in (b.get("text") or "")), None)
ok(q is not None, "#1452 AFTER has the CAPIError quote")
q_sp = craft.spoken_span(craft._card_spoken_text(q, None), w1452)
ok(q_sp and float(q["start"]) + 1e-6 >= float(q_sp[0]) - craft.SYNC_TOL_S,
   f"#1452 quote start {q['start']} not before first word {q_sp[0] if q_sp else None}")
ok(q_sp and float(q["start"]) + float(q["dur"]) + 1e-6
   >= float(q_sp[1]) + craft.CARD_PAD_FLOOR_S - craft.SYNC_TOL_S,
   f"#1452 CAPIError holds through last word + pad "
   f"(end {float(q['start']) + float(q['dur']):.2f}, last {q_sp[1]})")
ok(float(q["dur"]) > craft.MID_BEAT_MAX_S, "literal quote exceeds mid cap")
ok("beat_hold" in s1452["after_kinds"]
   or any("mid card" in r for r in (s1452["ga"].get("reasons") or [])),
   f"#1452 AFTER OS quote >3.0s incl fade → beat_hold ({s1452['after_kinds']})")
ok(s1452["ga"]["checks"]["C"] == "PASS",
   f"#1452 AFTER Gate C PASS ({s1452['ga'].get('reasons')})")
ok("blank_gap" not in s1452["after_kinds"] and "slide_spam" not in s1452["after_kinds"],
   f"#1452 AFTER no blank_gap/slide_spam ({s1452['after_kinds']})")
ok(s1452["ga"]["checks"]["B"] == "FAIL"
   and "sentence_too_long" in s1452["after_kinds"],
   f"#1452 AFTER Gate B FAIL sentence_too_long "
   f"({s1452['after_kinds']} {s1452['ga'].get('reasons')})")
ok(any("CAPIError" in (h.get("detail") or "")
       or "maximum of 4 blocks" in (h.get("detail") or "")
       for h in craft.sentence_too_long_hits(s1452["after"], w1452, ALLOW1452)),
   "#1452 CAPIError quote spoken ~3.06s (8.04–11.1) is sentence_too_long")
ok(s1465["hook_before"] > 5, f"#1465 BEFORE hook frozen ({s1465['hook_before']}s)")
ok(s1465["hook_after"] <= 4.1, f"#1465 AFTER hook ≤ ~4s ({s1465['hook_after']})")
ok("stale_card" not in s1465["after_kinds"] or s1465["hook_after"] <= 4.1,
   "#1465 AFTER hook is not the stale hit")
ok(s1465["ga"]["checks"]["B"] == "FAIL",
   f"#1465 AFTER Gate B FAIL on real VO ({s1465['after_kinds']})")
ok("repeated_vo_sentence" in s1465["after_kinds"],
   f"#1465 AFTER repeated_vo_sentence ({s1465['after_kinds']})")
ok("description_missing_source" in s1465["after_kinds"],
   f"#1465 AFTER description_missing_source ({s1465['after_kinds']})")
ok("enumeration_needs_rewrite" in s1465["after_kinds"],
   f"#1465 AFTER enumeration_needs_rewrite ({s1465['after_kinds']})")
ok("slide_spam" not in s1465["after_kinds"],
   f"#1465 AFTER no slide_spam ({s1465['after_kinds']})")
ok(not any(h.get("i") == 0 and h.get("check") == "card_text_not_literal"
           for h in craft.os_card_hits(s1465["after"], S1465, None, w1465)),
   f"#1465 AFTER two-sentence hook is not card_text_not_literal ({s1465['after_kinds']})")
ok(any(h.get("i") == 8 or "Anthropic" in (h.get("detail") or "")
       for h in craft.sentence_too_long_hits(s1452["after"], w1452, ALLOW1452)),
   "#1452 AFTER card 8 Anthropic clause spoken >3.0s")
ok("beat_hold" in s1465["after_kinds"]
   or any("mid card" in r for r in (s1465["ga"].get("reasons") or [])),
   f"#1465 AFTER card 4 5.87s quote → beat_hold ({s1465['after_kinds']})")
ok("allowlist_required" in s1453["before_kinds"] or s1453["before_B"] == "FAIL",
   "#1453 topic 47 without allowlist is a Gate B FAIL")
ok(s1453["ga"]["checks"]["B"] == "FAIL"
   and "allowlist_required" in s1453["after_kinds"]
   and "sentence_too_long" in s1453["after_kinds"],
   f"#1453 AFTER allowlist_required + sentence_too_long "
   f"({s1453['after_kinds']} {s1453['ga'].get('reasons')})")
ok(s1453["ga"]["checks"]["C"] == "PASS", "#1453 AFTER Gate C PASS")
ok(any(h.get("check") == "sentence_too_long"
       for h in craft.sentence_too_long_hits(s1453["after"], w1453)),
   f"#1453 AFTER sentence_too_long on snapshot VO "
   f"({craft.sentence_too_long_hits(s1453['after'], w1453)})")

index = [
    "# Gate B before/after — OS #1452, #1453, #1465",
    "",
    "BEFORE = board visível dos `VM_FAIL_*.md` (main `c35e110`).",
    "AFTER = gerador determinístico deste branch no **script do snapshot** "
    "(VO gravada) + âncoras whisper/`card_sync` do áudio real. Sem script "
    "S1453/S1465 reescrito e sem timing sintético uniforme. O check literal "
    "compara o card com as palavras faladas/renderizadas.",
    "",
    "`SENTENCE_SPOKEN_MAX_S = 3.0` vale para **todo** mid card, quote "
    "literal incluída (sem isenção de span). Se nenhum split de cláusula "
    "cabe em ≤3,0s — ou o split de vírgula quebraria uma enumeração — "
    "Gate B FALLA e o script é que se parte.",
    "",
    "Checks novos: `card_exits_early`, `card_enters_early` (lead > 0,3s), "
    "`card_overlap`, `cta_after_video_end`, `repeated_vo_sentence`, "
    "`code_read_too_short` (piso 1,2s em code/command), "
    "`description_missing_source`, `enumeration_needs_rewrite`.",
    "",
    "`slide_spam` é o check Gate C existente (`STATEMENT_MAX_SHORTS = 1`).",
    "",
    "Âncoras: timestamps whisper reais + visibilidade BEFORE (mp4, 30fps) em "
    "`tests/fixtures/os-gate-b/whisper-2026-10-19.json` (sem interpolação; "
    "pin que não casa uma palavra falha). Hold visual cruza com essa lista.",
    "",
    "| vídeo | topic | BEFORE B | AFTER B | BEFORE kinds | AFTER kinds | hook BEFORE | hook AFTER |",
    "|------:|------:|:--------:|:-------:|--------------|-------------|------------:|-----------:|",
]
for s in summaries:
    index.append(
        f"| #{s['id']} | {s['topic']} | {s['before_B']} | {s['after_B']} | "
        f"{', '.join(s['before_kinds']) or '—'} | {', '.join(s['after_kinds']) or '—'} | "
        f"{s['hook_before']:.2f}s | {s['hook_after']:.2f}s |")
index += ["", "Tabelas por vídeo abaixo.", ""]
full = "\n".join(index) + "\n\n" + "\n---\n\n".join(per_md) + "\n"
(DOCS / "gate-b-os-before-after.md").write_text(full)
(ART / "gate-b-before-after.md").write_text(full)
print(f"  wrote {DOCS / 'gate-b-os-before-after.md'}")

print()
print(f"ALL {_checks} CHECKS PASSED")
