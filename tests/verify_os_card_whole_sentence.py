"""OS (EN) whole-sentence cards (CoS 09/10: OS #1319 / #1328 fail the same
card_fragment as RR #1443; #93 only covered brand rr).

Run: PYTHONPATH=. .venv/bin/python tests/verify_os_card_whole_sentence.py

On OS (brand "os", EN shorts) an on-screen text card (statement/quote) now
shows EITHER a whole VO sentence / strong-clause run OR an isolated term —
never a piece of a sentence (craft.CARD_VO_WHOLE_BRANDS = rr + os).
  (a) Gate B: card_vo_marker(brand="os") → whole_sentence + card_fragment.
  (b) Composer: same storyboard _WHOLE_CARDS build + _guard_whole_cards,
      repairs logged as generator slips (#88 mechanism).
  EN term heuristics: English nouns are often verb bases ("answer key",
      "hard stop", "recall"), list items follow commas ("base URL"), an
      article after a word makes it a verb ("Strip | the key").
  RR PT-only parts (unspoken code → narration, PT counter, foreign terms)
      stay on RR: card_text_marker(brand="os")["rr"] is False.
"""
import json
import sys
from pathlib import Path

from app.services import craft
from app.services.engines import storyboard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


FX = json.loads((Path(__file__).parent / "fixtures" / "os1319" / "os_boards.json").read_text())
ALLOWED = ["hook", "statement", "stat", "compare", "list", "term_define", "quote", "code",
           "command", "cta"]

print("brand scope")
ok(craft.card_vo_whole_applies("os") and craft.card_vo_whole_applies("rr"), "rr + os get the rule")
ok(not craft.card_vo_whole_applies("owera") and not craft.card_vo_whole_applies(None),
   "other brands keep the #78 rule")

# ---------------------------------------------------------------------------
print("Gate B: the 06/10 fragments are card_fragment on OS")
S28 = FX["1328"]["script"]
B28 = FX["1328"]["board_0610"]
vo = craft.card_vo_marker(B28, S28, brand="os")
ok(vo.get("whole_sentence") is True, "card_vo marker carries whole_sentence on OS")
frag_txt = {B28[h["i"]].get("text") for h in vo["hits"] if h["check"] == "card_fragment"}
for t in FX["gate_b_0610"]["1328"]:
    ok(t in frag_txt, f"#1328 {t!r} → card_fragment")
ok(craft.card_vo_check_hits(vo), "#1328 06/10 board FAILS Gate B card_vo")
ok("whole_sentence" not in craft.card_vo_marker(B28, S28, brand="owera"), "non-OS marker unchanged")
S19 = FX["1319"]["script"]
b19 = [{"type": "hook", "cue": "Your eval passed", "text": S19.split(". ")[0] + "."},
       {"type": "quote", "cue": "That score is leakage.", "text": "That score is leakage."},
       {"type": "quote", "cue": "eval only counts", "text": "eval only counts"},
       {"type": "quote", "cue": "once the answer is gone.", "text": "once the answer is gone."},
       {"type": "cta", "cue": "Subscribe — next", "text": "Subscribe · Agent traps"}]
h19 = craft.card_vo_marker(b19, S19, brand="os")["hits"]
ok({h["i"] for h in h19 if h["check"] == "card_fragment"} >= {2, 3},
   "#1319 (#78 compose) 'eval only counts' | 'once the answer is gone.' → card_fragment")

# ---------------------------------------------------------------------------
print("EN isolated-term heuristics")
S18 = FX["1318"]["script"]
for t, s in (("answer key", S19), ("leakage", S19), ("solved trace", S19), ("recall", S19),
             ("expected output", S19), ("tool result", S19), ("green bar", S19),
             ("hard stop", S28), ("schema", S28), ("success", S28), ("previous state", S28),
             ("base URL", S18), ("production write", S18), ("Credentials", S18)):
    ok(craft.card_piece_reason(t, s) is None, f"term: {t!r}")
for t, s, why in (("Strip", S19, "verb + its object"), ("contaminated", S19, "verb"),
                  ("sends, and deploys.", S28, "comma/conjunction"),
                  ("deletes, sends", S28, "comma/conjunction"),
                  ("No undo", S28, "function word"), ("no unsupervised call.", S28, "function word"),
                  ("secrets actually", S18, "adverb"), ("database behind", S18, "function word"),
                  ("Missing undo", S28, "verb")):
    r = craft.card_piece_reason(t, s)
    ok(r is not None and why in r, f"not a term: {t!r} ({r})")
ok(craft.card_piece_reason("The previous state is gone.", S28) is None, "a whole sentence passes")
ok(craft.card_piece_reason("No undo in the tool, no unsupervised call.", S28) is None,
   "the merged quote the VM asked for passes")
# PT unchanged (the EN branch keys on the script language)
PT = "O modelo não lembra de nada entre uma resposta e outra."
ok(craft.card_piece_reason("entre uma resposta e outra.", PT) is not None, "PT fragment still fails")
ok("has a verb" in (craft.card_piece_reason("não lembra", PT) or ""), "PT verb rule unchanged")

# ---------------------------------------------------------------------------
print("RR-only checks stay RR")
code = {"type": "code", "cue": "The agent called undo", "lines": ["agent.call(\"undo\")"]}
bb = [{"type": "hook", "cue": "x", "text": "x"}, code, {"type": "cta", "cue": "Subscribe", "text": "Subscribe"}]
ct = craft.card_text_marker(bb, S28, brand="os")
ok(ct["rr"] is False and not [h for h in ct["hits"] if h["check"] in ("invented_output", "stat_word_order")],
   "OS card_text: no RR invented-output / PT counter checks")

# ---------------------------------------------------------------------------
print("Composer guard on OS: fragment → whole sentence / dropped, logged as slips")
tok = storyboard._WHOLE_CARDS.set(True)
try:
    g = [dict(b) for b in B28]
    slips: list = []
    storyboard._guard_whole_cards(g, S28, None, slips)
finally:
    storyboard._WHOLE_CARDS.reset(tok)
gt = [b.get("text") for b in g if b.get("type") in craft.TEXT_CARD_TYPES]
ok(all(craft.card_piece_reason(t, S28) is None for t in gt), f"no piece left: {gt}")
ok({s["kind"] for s in slips} <= {"fragment_to_sentence", "fragment_dropped"} and len(slips) == 3,
   f"3 repairs logged: {[s['kind'] for s in slips]}")


def compose(vid, brand):
    f = FX[vid]
    return storyboard.compose(subject=f["subject"], script=f["script"], words=f["words"],
                              duration=f["duration"], resolution="1080x1920", width=1080,
                              height=1920, topic_id=f["topic_id"], content_format="short",
                              allowed_types=ALLOWED, language="English",
                              llm=lambda *a, **k: json.dumps({"beats": f["llm_board"]}), brand=brand)


print("End to end: OS compose (06/10 LLM boards, real VO timing) → Gate B card_vo clean")
for vid in ("1319", "1328", "1318"):
    f = FX[vid]
    html = compose(vid, "os")
    beats = craft.beats_from_html(html)
    S, W = f["script"], f["words"]
    texts = [b.get("text") for b in beats if b.get("type") in craft.TEXT_CARD_TYPES]
    ok(all(craft.card_piece_reason(t, S) is None for t in texts), f"#{vid} whole/term only: {texts}")
    ok(not any(t in texts for t in FX["gate_b_0610"]["1328"]), f"#{vid} no 06/10 fragment")
    vo = craft.card_vo_marker(beats, S, brand="os")
    ok(not craft.card_vo_check_hits(vo), f"#{vid} card_vo whole: no gated hit")
    ok(not craft.card_text_check_hits(craft.card_text_marker(beats, S, W, brand="os")),
       f"#{vid} card_text: no hit")
    ok(not craft.card_sync_hits(craft.card_sync_marker(beats, W, "short", script=S)), f"#{vid} card sync PASS")
    kinds = [x["kind"] for x in craft.generator_slips_from_html(html) or []]
    ok("whole_cards_infeasible" not in kinds and "fragment_unrepaired" not in kinds,
       f"#{vid} no fallback / unrepaired slip ({kinds})")
    if vid == "1328":
        old = craft.beats_from_html(compose(vid, "owera"))
        ok(any(craft.card_piece_reason(b.get("text"), S) is not None
               for b in old if b.get("type") in craft.TEXT_CARD_TYPES),
           "#1328 other brand: #78 compose still cuts the sentence (rule is OS-scoped)")

print()
print(f"ALL {_checks} CHECKS PASSED")
