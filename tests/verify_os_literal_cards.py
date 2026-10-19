"""OS/literal_cards Gate B + generator — VM_FAIL #1452/#1453/#1465.

Run: PYTHONPATH=. .venv/bin/python tests/verify_os_literal_cards.py

Reproductions use the visible-card tables and spoken-span anchors from
VM_FAIL_21fe.md (#1452), VM_FAIL_c638.md (#1453), VM_FAIL_0465.md (#1465).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.services import craft, topic_flags
from app.services.engines import storyboard

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


# --- scripts (VO as reviewed) ---------------------------------------------
S1452 = (
    "One folder broke Copilot CLI. Add a directory with the add-dir flag, and every "
    "Claude subagent failed on the spot. A maximum of 4 blocks with cache_control may "
    "be provided. Found 5. One block over the limit. The parent agent got an empty "
    "answer. Update Copilot CLI, and Anthropic subagents keep working with extra "
    "directories. Exact version in the description. Subagent came back empty? Read "
    "its error, not the parent's. Subscribe — next agent trap."
)
S1453 = (
    "One type hint broke MCP tools. On Python three ten the tool failed. NotRequired "
    "is the optional key on the TypedDict. Clean registration. Pydantic called the "
    "annotation invalid. Python three eleven and up were fine. Update the MCP Python "
    "SDK and registration works. Registration works, and optional keys are left out "
    "instead of sent as null. Exact version in the description. Old Python in prod? "
    "Test registration there. Subscribe — next agent trap."
)
S1465 = (
    "The crew failed the handoff. tasks.yaml was empty. So the next agent received "
    "nothing to do. CrewAI will not invent the work for you. The crew runner only "
    "moves what that file declares. A task needs a description, an expected output, "
    "and an agent. The handoff never starts. The crew logs will not fill a blank file. "
    "Before you run, write each task in tasks.yaml. Empty file, empty crew. "
    "Subscribe — next CrewAI trap."
)

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


# Visible cards from VM_FAIL.md (interval = start–end).
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
w1452 = words_anchor(S1452, {
    "A maximum of 4 blocks with cache_control may be provided.": (8.04, 11.1),
    "Found 5.": (11.94, 12.40),
    "One folder broke Copilot CLI.": (0.10, 2.00),
}, duration=30.60)
h1452 = craft.os_card_hits(B1452, S1452, ALLOW1452)
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
                              sources=ALLOW1452, duration=30.60)
ok(g1452["checks"]["B"] == "FAIL", f"#1452 Gate B FAIL ({g1452['reasons'][:2]})")


print("#1453 BEFORE (VM_FAIL_c638) — Gate B FAILs")
w1453 = words_anchor(S1453, {
    "Registration works, and optional keys are left out instead of sent as null.": (18.42, 22.5),
    "One type hint broke MCP tools.": (0.10, 2.10),
}, duration=30.77)
h1453 = craft.os_card_hits(B1453, S1453, None)
c1453 = checks_of(h1453)
ok("single_term_card" in c1453, f"#1453 isolated registration/prod → single_term_card ({c1453})")
ok("vs_card_not_allowed" in c1453, f"#1453 VS templates ({c1453})")
ok(any("null on the wire" in (h.get("detail") or "") or h.get("check") == "vs_card_not_allowed"
       for h in h1453),
   "#1453 invented 'null on the wire' compare fails")
qh1453 = craft.quote_before_speech_hits(B1453, w1453)
ok(qh1453 and any(h.get("i") == 7 for h in qh1453),
   f"#1453 quote at 17.37 before speech 18.42 → quote_before_speech ({qh1453})")
g1453 = craft.video_maker_gate(B1453, brand="os", script=S1453, words=w1453, duration=30.77)
ok(g1453["checks"]["B"] == "FAIL", "#1453 Gate B FAIL")


print("#1465 BEFORE (VM_FAIL_0465) — Gate B FAILs")
w1465 = words_anchor(S1465, {
    "The crew failed the handoff.": (0.10, 1.80),
    "tasks.yaml was empty.": (1.88, 3.20),
}, duration=30.13)
h1465 = craft.os_card_hits(B1465, S1465, None)
c1465 = checks_of(h1465)
ok("single_term_card" in c1465, f"#1465 isolated handoff / tasks yaml ({c1465})")
ok("vs_card_not_allowed" in c1465, f"#1465 jumble VS ({c1465})")
sh1465 = craft.stale_card_hits(B1465, w1465, S1465)
ok(sh1465 and sh1465[0]["i"] == 0, f"#1465 frozen hook 7.43s → stale_card ({sh1465})")
g1465 = craft.video_maker_gate(B1465, brand="os", script=S1465, words=w1465, duration=30.13)
ok(g1465["checks"]["B"] == "FAIL", "#1465 Gate B FAIL")


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
gg = craft.video_maker_gate(qb, beat_timing=craft.BEAT_TIMING_CURRENT, sources=ALLOW1452)
ok(not any("mid card" in r and "beat[1]" in r for r in (gg.get("reasons") or [])),
   f"Gate B does not FAIL the literal quote for the cap ({gg.get('reasons')})")


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
storyboard._fill_blank_gaps(hole, S1452, w1452, slips)
ok(len(hole) >= 4, f"fill inserted a card ({[b.get('type') for b in hole]})")
ok(any(s.get("kind") == "blank_gap_filled" for s in slips) or not craft.blank_gap_hits(hole),
   f"hole closed ({slips})")
ok(not craft.blank_gap_hits(hole), "filled board has no blank_gap")


print("AFTER compose (deterministic, no LLM) + committed before/after tables")


def _gate(beats, *, duration, script, words, topic_id, sources):
    ctext = craft.card_text_marker(beats, script, words, brand="os", sources=sources)
    cvo = craft.card_vo_marker(beats, script, brand="os")
    ctime = craft.card_timing_marker(beats, words, script)
    al = craft.allowlist_required_marker(topic_id, sources)
    return craft.video_maker_gate(
        beats, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT,
        card_text=ctext, card_vo=cvo, duration=duration, card_timing=ctime,
        allowlist_check=al, brand="os", script=script, words=words, sources=sources)


def _kinds(g: dict) -> list[str]:
    out = []
    for k in g.get("kinds") or []:
        chk = k.get("check") if isinstance(k, dict) else None
        if chk and chk not in out:
            out.append(chk)
    for r in g.get("reasons") or []:
        for name in ("blank_gap", "stale_card", "quote_before_speech",
                     "vs_card_not_allowed", "single_term_card",
                     "card_text_not_literal", "echo_card", "allowlist_required"):
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
    dict(vid=1452, topic=47, dur=30.60, script=S1452, before=B1452, words=w1452,
         sources=ALLOW1452, subject="One folder broke Copilot CLI. · Agent traps 12",
         name="Agent traps", fail_file="VM_FAIL_21fe.md"),
    dict(vid=1453, topic=47, dur=30.77, script=S1453, before=B1453, words=w1453,
         sources=None, subject="One type hint broke MCP tools. · Agent traps 14",
         name="Agent traps", fail_file="VM_FAIL_c638.md"),
    dict(vid=1465, topic=7, dur=30.13, script=S1465, before=B1465, words=w1465,
         sources=None, subject="The crew failed the handoff. tasks.yaml was empty. · CrewAI 21",
         name="CrewAI", fail_file="VM_FAIL_0465.md"),
]

DOCS = Path("docs")
ART = Path("/opt/cursor/artifacts")
ART.mkdir(parents=True, exist_ok=True)
summaries = []
per_md = []
for c in CASES:
    gb = _gate(c["before"], duration=c["dur"], script=c["script"], words=c["words"],
               topic_id=c["topic"], sources=c["sources"])
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
               topic_id=c["topic"], sources=c["sources"])
    ok("vs_card_not_allowed" not in _kinds(ga)
       and "single_term_card" not in _kinds(ga),
       f"#{c['vid']} AFTER Gate B dropped VS/term ({_kinds(ga)})")
    note = (f"BEFORE = board visível do `{c['fail_file']}` (main `c35e110`). "
            "AFTER = `storyboard.compose` determinístico neste branch "
            "(sem LLM; âncoras de fala do VM_FAIL; resto das palavras "
            "preenche o vão entre âncoras).")
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
ok(abs(float(q["start"]) - 8.04) < 0.05, f"#1452 quote start {q['start']}")
ok(abs(float(q["start"]) + float(q["dur"]) - 11.4) < 0.15,
   f"#1452 CAPIError ~8.04–11.4 (end {float(q['start']) + float(q['dur']):.2f})")
ok(float(q["dur"]) > craft.MID_BEAT_MAX_S, "literal quote exceeds mid cap")
ok("stale_card" not in s1452["after_kinds"] or not any(
    "beat[2]" in r and "mid card" in r for r in (s1452["ga"].get("reasons") or [])),
   "Gate B does not FAIL the CAPIError quote for the cap")
ok(s1452["ga"]["checks"]["B"] == "PASS" and s1452["ga"]["checks"]["C"] == "PASS"
   and s1452["ga"]["result"] == "PASS",
   f"#1452 AFTER Gate B+C PASS ({s1452['ga'].get('reasons')})")
ok("blank_gap" not in s1452["after_kinds"] and "slide_spam" not in s1452["after_kinds"],
   f"#1452 AFTER no blank_gap/slide_spam ({s1452['after_kinds']})")
ok(s1465["hook_before"] > 5, f"#1465 BEFORE hook frozen ({s1465['hook_before']}s)")
ok(s1465["hook_after"] <= 4.1, f"#1465 AFTER hook ≤ ~4s ({s1465['hook_after']})")
ok("stale_card" not in s1465["after_kinds"] or s1465["hook_after"] <= 4.1,
   "#1465 AFTER hook is not the stale hit")
ok(s1465["ga"]["result"] == "PASS" and s1465["ga"]["checks"]["B"] == "PASS",
   f"#1465 AFTER Gate B PASS ({s1465['ga'].get('reasons')})")
ok("blank_gap" not in s1465["after_kinds"],
   f"#1465 AFTER no blank_gap ({s1465['after_kinds']})")
ok("allowlist_required" in s1453["before_kinds"] or s1453["before_B"] == "FAIL",
   "#1453 topic 47 without allowlist is a Gate B FAIL")
ok(s1453["ga"]["checks"]["B"] == "FAIL"
   and s1453["after_kinds"] == ["allowlist_required"],
   f"#1453 AFTER only allowlist_required ({s1453['after_kinds']} {s1453['ga'].get('reasons')})")
ok(s1453["ga"]["checks"]["C"] == "PASS", "#1453 AFTER Gate C PASS")

index = [
    "# Gate B before/after — OS #1452, #1453, #1465",
    "",
    "BEFORE = board visível dos `VM_FAIL_*.md` (main `c35e110`).",
    "AFTER = gerador determinístico deste branch (1 frase da VO; quote literal "
    "segura o span inteiro + 0,3s; sem compare/term/list/termo isolado).",
    "",
    "Quando o span falado de uma frase passa do cap mid e deixaria um buraco "
    "> 0,5s além do fade, a ordem é: (1) cláusulas fortes #96 (`; : — –`); "
    "(2) vírgula / conjunção coordenativa, cada peça um span contíguo da VO "
    "sem eco do card vizinho; (3) último recurso `cap_extended_spoken` — o "
    "card plain segura até a última palavra falada + 0,3s (nunca na frase "
    "seguinte). Gate B isenta esse hold do cap mid (não é `stale_card` nem "
    "`blank_gap`).",
    "",
    "`slide_spam` é o check Gate C existente (`STATEMENT_MAX_SHORTS = 1`): "
    "statement/list que só reexibe a narração sem tipo rico "
    "(code/command/diagram/compare/stat). O gerador OS não emite statement "
    "(allowlist log fica `code`) e junta peças curtas demais da mesma frase.",
    "",
    "Âncoras de fala dos VM_FAIL (whisper). Resto das palavras preenche o vão "
    "entre âncoras até `duration − END_TAIL_S`. Sem render/TTS novo.",
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
