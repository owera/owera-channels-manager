"""Topic-47 source-card unspoken exemption + CTA on the spoken closer.

Run: PYTHONPATH=. .venv/bin/python tests/verify_t47_source_cards.py

(1) unspoken_card: a code/command card whose on-screen text is
byte-identical to an on_screen_allow entry (kind error/log/command/code)
maps onto the cue's spoken span. 1-char drift, unspoken cue, or a visual
hold over the 3.0s cap (tail+fade included) still FAIL.
(2) CTA card_sync: compose sits the chip on the spoken CTA line. Tested
on the #1453 PATCH board and the AFTER #1452/#1453/#1465 boards.
"""
import json
import sys
from app.services import craft
from app.services.engines import storyboard
from tests.os_gate_b_real_vo import (
    FIXTURES, snapshot_duration, snapshot_script, whisper_words,
)

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ERR = (
    "pydantic.errors.PydanticForbiddenQualifier: The annotation 'NotRequired[int]' "
    "contains the 'typing.NotRequired' type qualifier, which is invalid in the "
    "context it is defined."
)
CUE = "Pydantic called the annotation invalid"
CMD = "uv run --python 3.10 --frozen --all-extras --dev python repro.py"
CUE_CMD = "Test registration there"
HOOK = "One type hint broke MCP tools."
SUB = "Subscribe"
ALLOW = [
    {"kind": "error", "text": ERR, "source": "https://example.test/e"},
    {"kind": "command", "text": CMD, "source": "https://example.test/e"},
]
ALLOW1452 = [
    {"kind": "quote",
     "text": "A maximum of 4 blocks with cache_control may be provided.",
     "source": "https://github.com/github/copilot-cli/issues/4185"},
    {"kind": "log", "text": "Found 5.",
     "source": "https://github.com/github/copilot-cli/issues/4185"},
]


def stamp(text, t0, step=0.25, dur=0.2):
    out, t = [], float(t0)
    for w in text.split():
        out.append({"text": w.strip(".,!?:;"), "start": round(t, 3), "dur": dur})
        t += step
    return out, t


def fixture_words():
    w, t = stamp(HOOK, 0.0)
    more, t = stamp(CUE, 2.0)
    w += more
    more, t = stamp(CUE_CMD, t + 0.2)
    w += more
    more, t = stamp(SUB, t + 0.2)
    w += more
    return w


def code_board(*, lines, cue, start=2.0, dur=1.55):
    return [
        {"type": "hook", "start": 0.0, "dur": 1.88, "text": HOOK,
         "object": "terminal"},
        {"type": "code", "start": start, "dur": dur, "cue": cue, "lines": list(lines)},
        {"type": "cta", "start": start + dur + 0.12, "dur": 2.0,
         "text": "Subscribe · Agent traps", "cue": SUB},
    ]


def unspoken_of(hits):
    return [h for h in hits if h.get("check") == "unspoken_card"]


def gate(beats, words, sources, duration=8.0):
    ctime = craft.card_timing_marker(beats, words, HOOK + " " + CUE, duration=duration,
                                    sources=sources)
    sync = craft.card_sync_marker(beats, words, "short", script=HOOK + " " + SUB)
    return craft.video_maker_gate(
        beats, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT,
        duration=duration, card_timing=ctime, card_sync=sync,
        brand="os", script=HOOK + " " + CUE + " " + SUB, words=words,
        sources=sources)


print("source-card allowlist match (byte-identical)")
ok(craft.is_allowlisted_source_card(
    {"type": "code", "lines": [ERR], "cue": CUE}, ALLOW),
   "error line byte-equal → source card")
ok(not craft.is_allowlisted_source_card(
    {"type": "code", "lines": [ERR + "."], "cue": CUE}, ALLOW),
   "1-char suffix is not byte-equal")
ok(craft.is_allowlisted_source_card(
    {"type": "command", "command": CMD, "output": [], "cue": CUE_CMD}, ALLOW),
   "command byte-equal → source card")
ok(craft.is_allowlisted_source_card(
    {"type": "code", "lines": [ERR], "cue": CUE},
    [{"kind": "code", "text": ERR, "source": "https://example.test/e"}]),
   "kind=code on the raw allowlist is accepted")
ok(craft._card_spoken_text(
    {"type": "code", "lines": [ERR], "cue": CUE}, ALLOW) == CUE,
   "allowlisted source card speaks the cue")
ok(craft._card_spoken_text(
    {"type": "code", "lines": [ERR[:-1] + "X"], "cue": CUE}, ALLOW) == ERR[:-1] + "X",
   "1-char drift speaks the screen text, not the cue")


print("unspoken_card branches")
W = fixture_words()
cue_span = craft.spoken_span(CUE, W)
ok(cue_span and abs(cue_span[0] - 2.0) < 1e-9, f"cue span starts at 2.0 ({cue_span})")

good = code_board(lines=[ERR], cue=CUE, start=2.0, dur=1.55)
hits = craft.card_timing_hits(good, W, sources=ALLOW, duration=8.0)
ok(not unspoken_of(hits), f"byte-equal + spoken cue → no unspoken ({hits})")
g = gate(good, W, ALLOW)
ok(not any(k.get("check") == "unspoken_card" for k in (g.get("kinds") or [])
           if isinstance(k, dict)),
   "byte-equal + spoken cue is not a Gate B unspoken_card")

drift = code_board(lines=[ERR + "x"], cue=CUE, start=2.0, dur=1.55)
d_hits = craft.card_timing_hits(drift, W, sources=ALLOW, duration=8.0)
ok(any(h.get("i") == 1 and h.get("check") == "unspoken_card" for h in d_hits),
   f"1-char diff → unspoken_card FAIL ({d_hits})")

bad_cue = code_board(lines=[ERR], cue="This cue is never spoken here", start=2.0, dur=1.55)
c_hits = craft.card_timing_hits(bad_cue, W, sources=ALLOW, duration=8.0)
ok(any(h.get("i") == 1 and h.get("check") == "unspoken_card" for h in c_hits),
   f"unspoken cue → unspoken_card FAIL ({c_hits})")

# Visual cap: hold 3.10s + 0.20 fade = 3.30s > MID_CARD_MAX_S 3.0.
over = code_board(lines=[ERR], cue=CUE, start=2.0, dur=3.10)
o_hits = craft.card_timing_hits(over, W, sources=ALLOW, duration=10.0)
ok(not unspoken_of(o_hits),
   f"over-cap source card is not unspoken ({unspoken_of(o_hits)})")
og = craft.video_maker_gate(
    over, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT,
    duration=10.0, brand="os", words=W, sources=ALLOW,
    card_timing=craft.card_timing_marker(over, W, sources=ALLOW, duration=10.0))
ok(og["checks"]["B"] == "FAIL"
   and any("beat[" in r and "3." in r for r in (og.get("reasons") or [])),
   f"over-cap source card still FAILs the 3.0s visual cap ({og.get('reasons')})")


print("#1453 PATCH compose + AFTER CTA on spoken line")
TTS1453 = json.loads((FIXTURES / "tts-1453-revised.json").read_text())
SCRIPT = TTS1453["script"]
DUR = float(TTS1453["duration"])
WORDS = TTS1453["words"]
PATCH = {
    "on_screen_allow": [
        {"kind": "error", "text": ERR,
         "source": "https://github.com/modelcontextprotocol/python-sdk/issues/3227"},
        {"kind": "command", "text": CMD,
         "source": "https://github.com/modelcontextprotocol/python-sdk/issues/3227"},
        {"kind": "quote", "text": "Python three eleven and up were fine.", "source": "vo"},
        {"kind": "quote", "text": "Registration works.", "source": "vo"},
        {"kind": "quote", "text": "Optional keys are left out instead of sent as null.",
         "source": "vo"},
        {"kind": "quote", "text": "Exact version in the description.", "source": "vo"},
    ],
    "on_screen": [
        {"type": "code", "cue": CUE, "lang": "", "lines": [ERR], "_lit": True},
        {"type": "command", "cue": CUE_CMD, "prompt": "$", "command": CMD,
         "output": [], "_lit": True},
    ],
}


def boom(*a, **k):
    raise AssertionError("LLM must not run")


html = storyboard.compose(
    subject="One type hint broke MCP tools. · Agent traps 14",
    script=SCRIPT, words=WORDS, duration=DUR,
    resolution="portrait", width=1080, height=1920, topic_id=47,
    content_format="short", language="English", llm=boom, brand="os",
    topic_name="Agent traps",
    on_screen_allow=PATCH["on_screen_allow"],
    on_screen=PATCH["on_screen"],
)
ok(bool(html), "#1453 PATCH compose returns HTML")
beats = craft.beats_from_html(html)
ctime = craft.card_timing_marker(beats, WORDS, SCRIPT, duration=DUR,
                                sources=PATCH["on_screen_allow"])
sync = craft.card_sync_marker(beats, WORDS, "short", script=SCRIPT)
g1453 = craft.video_maker_gate(
    beats, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT,
    duration=DUR, card_timing=ctime, card_sync=sync, brand="os",
    script=SCRIPT, words=WORDS, sources=PATCH["on_screen_allow"],
    allowlist_check=craft.allowlist_required_marker(47, PATCH["on_screen_allow"]))
ok(not any(h.get("check") == "unspoken_card" for h in (ctime or {}).get("hits") or []),
   f"#1453 PATCH has no unspoken_card ({(ctime or {}).get('hits')})")
cta_hits = craft.card_sync_hits(sync)
ok(not any("cta" in h and "AFTER" in h for h in cta_hits),
   f"#1453 PATCH CTA is not late ({cta_hits})")
sub = craft.spoken_span(beats[-1].get("cue") or "", WORDS)
ok(sub and abs(float(beats[-1]["start"]) - float(sub[0])) <= craft.SYNC_TOL_S + 1e-9,
   f"#1453 PATCH CTA start {beats[-1]['start']} sits on Subscribe {sub}")
kinds = [k.get("check") for k in (g1453.get("kinds") or []) if isinstance(k, dict)]
ok("unspoken_card" not in kinds,
   f"#1453 PATCH remaining kinds {kinds} (no unspoken_card)")


def after_cta(vid, topic, subject, name, sources=None):
    words = whisper_words(vid)
    script = snapshot_script(vid)
    dur = snapshot_duration(vid)
    html = storyboard.compose(
        subject=subject, script=script, words=words, duration=dur,
        resolution="portrait", width=1080, height=1920, topic_id=topic,
        content_format="short", language="English", llm=boom, brand="os",
        topic_name=name, on_screen_allow=sources)
    ok(bool(html), f"#{vid} AFTER compose returns HTML")
    board = craft.beats_from_html(html)
    sync = craft.card_sync_marker(board, words, "short", script=script)
    hits = craft.card_sync_hits(sync)
    cta = board[-1]
    note = next(n for n in (sync or {}).get("notes") or [] if n.get("i") == len(board) - 1)
    sub = craft.spoken_span(cta.get("cue") or "", words)
    ok(not any("cta" in h and "AFTER" in h for h in hits),
       f"#{vid} AFTER CTA is not late ({hits})")
    ok(sub and abs(float(cta["start"]) - float(sub[0])) <= craft.SYNC_TOL_S + 1e-9,
       f"#{vid} AFTER CTA {cta['start']} sits on Subscribe {sub[0] if sub else None} "
       f"(note {note})")
    return board, sync


after_cta(1452, 47, "One folder broke Copilot CLI. · Agent traps 12",
          "Agent traps", ALLOW1452)
after_cta(1453, 47, "One type hint broke MCP tools. · Agent traps 14",
          "Agent traps", None)
after_cta(1465, 7, "The crew failed the handoff. tasks.yaml was empty. · CrewAI 21",
          "CrewAI", None)

print()
print(f"ALL {_checks} CHECKS PASSED")
