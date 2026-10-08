"""Tight mid cards on topic_flags.TIGHT_MID_CARDS topics (CMO 08/10, PREFLIGHT
#1451 Q3): mid card hold ≤ 2.48s (≈2.60s as the VM measures it, hold + 0.12s
gap) instead of 2.80 (≈2.92 measured; #1450 measured 2.88–2.94).

Run: PYTHONPATH=. .venv/bin/python tests/verify_tight_mid_cards.py

Pins:
  * flag: topic 47 by default (shared topic_flags structure);
  * craft.mid_hold_cap: scoped (context manager), restored after compose /
    marker computation, nested-safe; continuation threshold follows the cap;
  * compose on topic 47: every mid hold ≤ 2.48s; the same board on another
    topic keeps the 2.80 cap (some holds > 2.48) — nothing else changes;
  * Gate B hold check unchanged (2.80) for every board; topic-47 board passes;
  * worker markers computed under the same cap (source pin).
"""
import inspect
import json
import sys

from app.config import Settings
from app.services import craft, topic_flags
from app.services.engines import storyboard, worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("flag + cap scope")
ok(Settings().topic_flags.get("tight_mid_cards") == [47]
   and topic_flags.has(topic_flags.TIGHT_MID_CARDS, 47)
   and not topic_flags.has(topic_flags.TIGHT_MID_CARDS, 1), "tight_mid_cards default [47]")
ok(craft.mid_cap_for_topic(47) == craft.LITERAL_MID_BEAT_MAX_S == 2.48
   and craft.mid_cap_for_topic(1) is None, "topic 47 → 2.48, others → default")
ok(craft.mid_beat_max() == craft.MID_BEAT_MAX_S == storyboard._MID_MAX == 2.80
   and storyboard._mid_max() == 2.80, "default cap 2.80 (craft == storyboard)")
ok(craft.card_cont_min_span() == craft.CARD_CONT_MIN_SPAN_S == 2.92, "default continuation span 2.92")
with craft.mid_hold_cap(2.48):
    ok(craft.mid_beat_max() == 2.48 and storyboard._mid_max() == 2.48
       and craft.card_cont_min_span() == 2.6, "inside the cap: hold 2.48, continuation span 2.60")
    with craft.mid_hold_cap(None):
        ok(craft.mid_beat_max() == 2.80, "nested None → default")
    ok(craft.mid_beat_max() == 2.48, "nested exit restores 2.48")
ok(craft.mid_beat_max() == 2.80, "exit restores 2.80")
try:
    with craft.mid_hold_cap(2.48):
        raise RuntimeError("x")
except RuntimeError:
    pass
ok(craft.mid_beat_max() == 2.80, "restored even when the body raises")
ok(craft.sentence_card_cap(2.7) == 1, "2.7s sentence: one card at the default cap")
with craft.mid_hold_cap(2.48):
    ok(craft.sentence_card_cap(2.7) == 2, "…two cards (hold + continuation) at 2.48")

SCRIPT = ("Cursor timed out on French Windows. Every Agent request failed right away with "
          "Agent Execution Timed Out. The real error was in the dev tools: a PowerShell "
          "parser error, because a quote broke on non-English Windows. The timeout message "
          "pointed at the wrong place. The fix: update Cursor to the latest patch. Cursor "
          "support said so on the forum, and the reporter confirmed it works. Agent times "
          "out instantly? Check the logs before the network. Subscribe — next agent trap.")
BOARD = [
    {"type": "hook", "cue": "Cursor timed out on French Windows",
     "text": "Cursor timed out on French Windows."},
    {"type": "statement", "cue": "Every Agent request failed", "text": "Every request failed"},
    {"type": "term_define", "cue": "The real error was in", "term": "ParserError",
     "definition": "PowerShell could not parse the command"},
    {"type": "compare", "cue": "The timeout message pointed",
     "left": {"title": "Message", "items": ["timeout"]},
     "right": {"title": "Cause", "items": ["parser error"]}},
    {"type": "statement", "cue": "The fix: update Cursor", "text": "Update Cursor"},
    {"type": "statement", "cue": "Check the logs before", "text": "Check the logs first"},
    {"type": "cta", "cue": "Subscribe next agent trap", "text": "Subscribe · Agent traps"},
]


def llm(user, system=None, max_tokens=None):
    return json.dumps({"beats": BOARD})


words, t = [], 0.1
for w in SCRIPT.split():
    words.append({"text": w.strip(".,!?:"), "start": round(t, 3), "dur": 0.3})
    t += 0.36
DUR = round(t + 0.6, 2)


def holds(topic_id):
    html = storyboard.compose(subject="Cursor timed out on French Windows. · Agent traps 9",
                              script=SCRIPT, words=words, duration=DUR, resolution="portrait",
                              width=1080, height=1920, topic_id=topic_id, content_format="short",
                              language="English", llm=llm, brand="os", topic_name="Agent traps",
                              allowed_types=["hook", "statement", "stat", "compare", "list",
                                             "term_define", "quote", "cta"])
    fb = craft.beats_from_html(html)
    mids = [float(b["dur"]) for b in fb[1:] if (b.get("type") or "") not in craft.CTA_TYPES]
    return fb, mids


print("compose")
fb47, m47 = holds(47)
ok(craft.mid_beat_max() == 2.80, "cap restored after compose")
ok(m47 and max(m47) <= 2.48 + 1e-6, f"topic 47: every mid hold ≤ 2.48s (max {max(m47):.2f}s)")
fb1, m1 = holds(1)
ok(max(m1) > 2.48 + 1e-6 and max(m1) <= 2.80 + 1e-6,
   f"topic 1: default cap (max hold {max(m1):.2f}s ≤ 2.80) — unchanged elsewhere")
gate = craft.video_maker_gate(fb47, content_format="short", beat_timing=craft.BEAT_TIMING_CURRENT)
ok(gate["checks"]["B"] == "PASS", f"Gate B (2.80 hold check, unchanged) passes the topic-47 board ({gate.get('reasons')})")
with craft.mid_hold_cap(2.48):
    hits = craft.card_text_hits(fb47, SCRIPT, words)
ok(not [h for h in hits if h["check"] == "echo_card"],
   "continuation cards of the tighter board are not echoes under the same cap")

print("worker")
src = inspect.getsource(worker._creation_config)
ok("mid_hold_cap(_craft_cap.mid_cap_for_topic(params.get(\"topic_id\")))" in src
   and src.index("mid_hold_cap") < src.index("card_text_marker"),
   "render markers computed under the topic's cap")
ok("_compose_board(topic_id=topic_id" in inspect.getsource(storyboard.compose),
   "compose runs the board under craft.mid_hold_cap(topic cap)")

print()
print(f"ALL {_checks} CHECKS PASSED")
