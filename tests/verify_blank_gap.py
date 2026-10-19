"""Gate B blank-gap check (CMO: 0.5s of blank beyond the 0.12s fade).

Run: PYTHONPATH=. .venv/bin/python tests/verify_blank_gap.py

A card owns its visual hold + BEAT_GAP_S (the fade it plays out on). On the
final timed board, gap = next.start − (prev.start + prev.dur). FAIL when
gap − BEAT_GAP_S > BLANK_GAP_MAX_S (0.5). Same extra budget for a leading
hole (first beat after t=0) and a trailing hole after the endcard vs
creation_config["duration"] (the video_duration / END_TAIL_S figure).
OS and RR. Normal aligner boards produce zero hits.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

from app.services import craft
from app.services.engines import worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def os_cards():
    return [
        {"type": "hook", "text": "Your RAG reads junk", "object": "receipt", "cue": "Your RAG"},
        {"type": "code", "lines": ["rerank(q)"], "cue": "rerank the pile"},
        {"type": "stat", "value": "40", "unit": "%", "cue": "forty percent"},
        {"type": "cta", "text": "Subscribe · Agent traps", "cue": "Subscribe"},
    ]


def rr_cards():
    return [
        {"type": "hook", "text": "Chat sem fim não é engenharia.", "object": "terminal",
         "cue": "Chat sem fim"},
        {"type": "stat", "value": "30", "unit": "mensagem", "label": "decisão errada",
         "cue": "mensagem 30", "unit_first": True},
        {"type": "quote", "text": "Eu fecho o fio.", "cue": "Eu fecho"},
        {"type": "cta", "text": "Se inscreve · IA", "cue": "Se inscreve"},
    ]


def timed(cards, *, extra=0.0, first=0.0, holds=None):
    """Place cards with inter-beat gap = BEAT_GAP_S + extra. first = t0 of beat 0."""
    out, t = [], float(first)
    for i, c in enumerate(cards):
        b = dict(c)
        hold = (holds[i] if holds is not None else
                (3.5 if (b.get("type") or "") in craft.CTA_TYPES else 2.0))
        b["start"] = round(t, 3)
        b["dur"] = round(hold, 3)
        out.append(b)
        t = t + hold + craft.BEAT_GAP_S + extra
    return out


def last_end(board):
    b = board[-1]
    return float(b["start"]) + float(b["dur"])


print("constants")
ok(craft.BEAT_GAP_S == 0.12, "BEAT_GAP_S is the 0.12s fade")
ok(craft.BLANK_GAP_MAX_S == 0.5, "BLANK_GAP_MAX_S = 0.5 (CMO)")
ok(craft.END_TAIL_S == craft.BLANK_GAP_MAX_S,
   "END_TAIL_S matches the blank budget (video_duration tail)")

print("gap exactly at fade passes; fade+0.5 passes; fade+0.51 fails")
for brand, cards in (("OS", os_cards()), ("RR", rr_cards())):
    exact = timed(cards, extra=0.0)
    at = timed(cards, extra=craft.BLANK_GAP_MAX_S)
    over = timed(cards, extra=craft.BLANK_GAP_MAX_S + 0.01)
    ok(craft.blank_gap_hits(exact) == [], f"{brand}: exact fade → no hits")
    ok(craft.blank_gap_hits(at) == [], f"{brand}: fade+0.50 extra → pass")
    h = craft.blank_gap_hits(over)
    ok(h and all("hole at" in x and "0.51s beyond" in x for x in h)
       and len(h) == 3, f"{brand}: fade+0.51 → 3 inter-beat hits ({h})")
    g = craft.video_maker_gate(over)
    ok(g["checks"]["B"] == "FAIL" and g["result"] == "FAIL"
       and any(r.startswith("[B] Blank gap: FAIL") and "beat[0]→beat[1]" in r
               for r in g["reasons"]),
       f"{brand}: Gate B FAIL with blank_gap reason")
    kinds = [k for k in g.get("kinds") or [] if k.get("check") == "blank_gap"]
    ok(len(kinds) == 3 and all(k["kind"] == "real" for k in kinds)
       and "0.51s beyond" in kinds[0]["detail"],
       f"{brand}: kinds check=blank_gap kind=real detail has time/size")
    ok(craft.video_maker_gate(exact)["checks"]["B"] == "PASS"
       and craft.video_maker_gate(at)["checks"]["B"] == "PASS",
       f"{brand}: exact / fade+0.50 boards stay Gate B PASS")

print("leading hole")
for brand, cards in (("OS", os_cards()), ("RR", rr_cards())):
    lead_ok = timed(cards, first=craft.BLANK_GAP_MAX_S)
    lead_bad = timed(cards, first=craft.BLANK_GAP_MAX_S + 0.01)
    ok(craft.blank_gap_hits(lead_ok) == [], f"{brand}: first beat at 0.50s → pass")
    h = craft.blank_gap_hits(lead_bad)
    ok(len(h) == 1 and h[0].startswith("leading hole") and "0.51s" in h[0],
       f"{brand}: first beat at 0.51s → leading hit ({h})")
    g = craft.video_maker_gate(lead_bad)
    ok(g["checks"]["B"] == "FAIL" and any("leading hole" in r for r in g["reasons"])
       and any(k["check"] == "blank_gap" and "leading hole" in k["detail"]
               for k in g.get("kinds") or []),
       f"{brand}: leading hole is a Gate B blank_gap hit")

print("trailing hole")
for brand, cards in (("OS", os_cards()), ("RR", rr_cards())):
    board = timed(cards)
    end = last_end(board)
    ok(craft.blank_gap_hits(board, duration=end) == [],
       f"{brand}: endcard ends at video end → no trailing hit")
    ok(craft.blank_gap_hits(board, duration=end + craft.BLANK_GAP_MAX_S) == [],
       f"{brand}: endcard 0.50s before video end → pass")
    h = craft.blank_gap_hits(board, duration=end + craft.BLANK_GAP_MAX_S + 0.01)
    ok(len(h) == 1 and h[0].startswith("trailing hole") and "endcard" in h[0]
       and "0.51s blank" in h[0] and f"{end:.2f}s" in h[0],
       f"{brand}: endcard 0.51s before video end → trailing hit ({h})")
    g = craft.video_maker_gate(board, duration=end + 0.51)
    ok(g["checks"]["B"] == "FAIL" and any("trailing hole" in r for r in g["reasons"]),
       f"{brand}: trailing hole is a Gate B FAIL")
    ok(craft.blank_gap_hits(board) == [],
       f"{brand}: no duration → trailing check skipped (not a false hit)")

print("duration threading (creation_config / worker)")
trail = timed(os_cards())
end = last_end(trail)
cc = {"beats": trail, "beat_timing": craft.BEAT_TIMING_CURRENT,
      "duration": end + 0.51}
r = craft.video_maker_gate_reason(cc, "short")
ok(r is not None and "Blank gap" in r and "trailing hole" in r,
   "video_maker_gate_reason reads creation_config.duration")
gof = craft.video_maker_gate_of(cc, "short")
ok(gof["checks"]["B"] == "FAIL" and any(k["check"] == "blank_gap" for k in gof.get("kinds") or []),
   "video_maker_gate_of recomputes blank_gap from stored duration")
ok(craft.video_maker_gate_reason({"beats": trail, "beat_timing": craft.BEAT_TIMING_CURRENT},
                                 "short") is None,
   "no duration on a tight board → review gate stays open")
ok(craft.video_maker_gate(trail, content_format="long", duration=end + 2.0)["result"] == "PASS",
   "longs stay exempt")
with patch.object(craft, "beats_from_html", return_value=trail):
    cc_w = worker._creation_config(
        "Your RAG reads junk. · Agent traps 1", {"content_format": "short"},
        "<html>", "Your RAG reads junk. Subscribe — next agent trap.",
        end + 0.51, "portrait", None, False, brand="os")
ok(cc_w["duration"] == end + 0.51
   and cc_w["craft_gate"]["checks"]["B"] == "FAIL"
   and any("Blank gap" in x for x in cc_w["craft_gate"]["reasons"]),
   "worker render-time gate gets duration and FAILs a trailing hole")
ok("duration=duration" in (worker._creation_config.__code__.co_varnames
                           and "")  # keep lints quiet; real pin is the source
   or "duration=duration" in __import__("inspect").getsource(worker._creation_config),
   "worker._creation_config passes duration into video_maker_gate")

print("untimed / snapshot boards: card owns hold + fade → no false hits")
untimed = [
    {"type": "hook", "start": 0.0, "dur": 3.0, "text": "Hook", "object": "bill"},
    {"type": "statement", "text": "A whole spoken sentence."},
    {"type": "quote", "text": "Another whole spoken sentence."},
    {"type": "endcard", "start": 20.0, "dur": 3.0, "text": "Subscribe · Agent traps"},
]
ok(craft.blank_gap_hits(untimed) == [],
   "mids without dur infer hold via _cue_span (own the 0.12s fade)")
ok(craft.blank_gap_hits([]) == [] and craft.blank_gap_hits(None) == [],
   "empty board → no hits")

print("existing fixtures / currently-passing boards")
ROOT = Path(__file__).parent
newly = []

a9 = json.loads((ROOT / "fixtures" / "rr1443" / "a9_render.json").read_text())
a9_board = a9.get("beats") or a9.get("board_a9") or a9.get("board") or []
a9_hits = craft.blank_gap_hits(a9_board, a9["duration"])
ok(a9_hits == [], f"RR #1443 a9addc4 timed board: no blank_gap ({a9_hits})")
ok(craft.video_maker_gate(a9_board, duration=a9["duration"],
                          beat_timing=craft.BEAT_TIMING_CURRENT)["checks"]["B"] != "n/a",
   "a9 board is judged (B is PASS or already-FAIL for other reasons)")
if a9_hits:
    newly.append(("rr1443/a9_render.json", a9_hits))

# Golden Gate B short from verify_craft — already PASS A/B/C.
golden = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Your RAG reads junk",
     "object": "receipt", "cue": "Your RAG"},
    {"type": "code", "start": 2.0, "dur": 2.4, "lines": ["rerank(q)"],
     "cue": "rerank the pile"},
    {"type": "stat", "start": 4.5, "dur": 2.4, "value": "40", "unit": "%",
     "cue": "forty percent"},
    {"type": "cta", "start": 7.0, "dur": 3.5, "text": "Rerank first",
     "cue": "rerank first"},
]
ok(craft.blank_gap_hits(golden) == [], "verify_craft _pass_beats: no blank_gap")
ok(craft.video_maker_gate(golden)["result"] == "PASS",
   "verify_craft golden short still full PASS")

# OS 06/10 fixtures have timing stripped — compose is covered by
# verify_os_card_whole_sentence; here we only pin they don't invent holes
# when re-timed at the fade (the shape the aligner writes).
os_fx = json.loads((ROOT / "fixtures" / "os1319" / "os_boards.json").read_text())
for vid in ("1319", "1328", "1318"):
    raw = os_fx[vid].get("llm_board") or os_fx[vid].get("beats") or []
    board = [b for b in raw if isinstance(b, dict)]
    if not board:
        continue
    # timing-stripped → _cue_span owns the fade; must stay clean
    h = craft.blank_gap_hits(board, os_fx[vid].get("duration"))
    if h:
        newly.append((f"os1319 #{vid} (timing-stripped)", h))
    ok(h == [], f"OS #{vid} timing-stripped fixture: no blank_gap ({h})")

if newly:
    print("NEWLY FAILING FIXTURES:")
    for name, hits in newly:
        print(f"  {name}: {hits}")
else:
    print("  no existing fixture newly fails blank_gap")

print()
print(f"ALL {_checks} CHECKS PASSED")
