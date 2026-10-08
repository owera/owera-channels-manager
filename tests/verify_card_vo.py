"""Card ⊂ VO craft_review check (Video Maker P0, council 06/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_card_vo.py

Every text card (statement/quote) must show one WHOLE sentence the VO says
(punctuation and case normalised), and each spoken sentence gets one card:
no card spanning two sentences, no sentence with two cards, no card text
absent from the VO. Continuation (CoS 06/10, on by default): a long sentence
may run over several text cards only when each further card shows the
literal NEXT words of that sentence; echo, skipped words, loose fragments,
chains that stop early and unspoken quotes still fail. No OCR runs in the pipeline, so the check reads the
rendered card source text against the VO script (edge-tts speaks it
verbatim). New short renders carry creation_config["card_vo"] (CARD_VO_V1)
and the Video Maker gate (craft_review / publish gate) fails Gate B on it.
Boards below are modelled on the 06/10 Gate B FAILs (#1328, #1401, #1423).
"""
import inspect
import json
import sys
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


def checks(hits):
    return [h["check"] for h in hits]


# #1328 "The agent called undo on a tool with no undo." (Gate B FAIL 06/10).
S1328 = ("The agent called undo on a tool with no undo. That tool never had an undo, and the model "
         "invented one. Agents copy editor habits onto deletes, sends, and deploys. The call returns "
         "success. The previous state is gone. No undo in the tool, no unsupervised call. "
         "Subscribe — next agent trap.")
HOOK = {"type": "hook", "start": 0.0, "dur": 3.0, "text": "The agent called undo on a tool with no undo."}
END = {"type": "endcard", "start": 20.0, "dur": 3.0, "text": "Subscribe · Agent traps", "cue": "Subscribe"}


def board(*mid):
    return [HOOK, *mid, END]


def stmt(text, cue=None):
    return {"type": "statement", "text": text, **({"cue": cue} if cue else {})}


def quote(text):
    return {"type": "quote", "text": text}


GOOD = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200", "label": "OK"},
    stmt("The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)

print("clean board")
ok(craft.card_vo_hits(GOOD, S1328) == [], "one whole-sentence card per spoken sentence → no hits")

print("normalisation")
n = board(
    stmt("THAT TOOL NEVER HAD AN UNDO — AND THE MODEL INVENTED ONE"),
    quote("“agents copy editor habits onto deletes sends and deploys”"),
    {"type": "stat", "cue": "the call returns success", "value": "200"},
    stmt("the previous state is gone"),
    quote("No undo in the tool... no unsupervised call!"),
)
ok(craft.card_vo_hits(n, S1328) == [], "case, quotes, dashes and missing/extra punctuation are ignored")
pt = ("Ignorar o gráfico não é engenharia. Quando você extrai o texto, o gráfico não vem. "
      "Subscribe — next IA trap.")
ptb = [{"type": "hook", "text": "Ignorar o gráfico não é engenharia."},
       stmt("quando voce extrai o texto o grafico nao vem")]
ok(craft.card_vo_hits(ptb, pt) == [], "PT-BR accents fold (você = voce, não = nao)")

print("fragments (#1328 cards 3, 9, 10)")
frag = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("No undo"),
)
h = craft.card_vo_hits(frag, S1328)
ok(checks(h) == ["card_fragment", "card_fragment"] and [x["i"] for x in h] == [2, 5],
   "tail-of-sentence quote and half-sentence quote → card_fragment (cards 2 and 5)")
ok("Agents copy editor habits" in h[0]["detail"], "fragment detail names the whole spoken sentence")
two = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("No undo"),
    quote("no unsupervised call."),
)
h = craft.card_vo_hits(two, S1328)
ok(checks(h) == ["card_fragment", "card_fragment"] and sorted(x["i"] for x in h) == [5, 6]
   and "skips words after card 5" in h[0]["detail"] and "stops before" in h[1]["detail"],
   "#1328 #9/#10: 'No undo' + 'no unsupervised call.' skip 'in the tool' → both fragments")
h = craft.card_vo_hits(two, S1328, allow_continuation=False)
ok(checks(h) == ["card_fragment", "card_fragment", "sentence_two_cards"]
   and h[-1]["i"] == 6 and "cards 5, 6" in h[-1]["detail"],
   "strict mode: 2 fragments + sentence_two_cards")

print("card spanning two sentences")
span = board(
    stmt("That tool never had an undo, and the model invented one. Agents copy editor habits "
         "onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
ok(checks(craft.card_vo_hits(span, S1328)) == ["card_spans_sentences"],
   "two whole sentences on one card → card_spans_sentences")
span2 = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    stmt("The call returns success. The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
ok(checks(craft.card_vo_hits(span2, S1328)) == ["card_spans_sentences"],
   "short sentences joined on one card still span two sentences")
tail = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("success. The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
ok(checks(craft.card_vo_hits(tail, S1328)) == ["card_spans_sentences"],
   "a card straddling a sentence boundary is spanning, not a fragment")
ok(checks(craft.card_vo_hits(tail, S1328, allow_continuation=False)) == ["card_spans_sentences", "sentence_two_cards"],
   "strict mode: …and it doubles the stat's sentence")

print("card text absent from the VO (#1401 'Done cut owns the calendar', #1423 card 10)")
absent = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("Done cut owns the calendar."),
)
h = craft.card_vo_hits(absent, S1328)
ok(checks(h)[0] == "card_not_in_vo" and h[0]["i"] == 5, "unspoken card text → card_not_in_vo")
ok(checks(h)[1:] == ["sentence_no_card"],
   "…and its sentence is left without a card (informational note)")
para = board(
    stmt("The tool never had undo; the model made one up."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
ok(checks(craft.card_vo_hits(para, S1328))[:1] == ["card_not_in_vo"],
   "a paraphrase of a spoken sentence is not in the VO")
order = board(stmt("one the invented model and"))
ok("card_not_in_vo" in checks(craft.card_vo_hits(order, S1328)), "spoken words in the wrong order → not in VO")

print("one card per sentence")
dbl = board(
    stmt("That tool never had an undo, and the model invented one."),
    {"type": "diagram", "cue": "That tool never had an undo", "title": "Undo?"},
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
h = craft.card_vo_hits(dbl, S1328)
ok(checks(h) == ["sentence_two_cards"] and h[0]["i"] == 2,
   "text card + object card on the same sentence → sentence_two_cards on the second card")
echo = board(
    stmt("That tool never had an undo, and the model invented one."),
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    {"type": "stat", "cue": "The call returns success", "value": "200"},
    stmt("The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
ok(checks(craft.card_vo_hits(echo, S1328)) == ["echo_card"],
   "the same sentence carded twice → echo_card")
ok(checks(craft.card_vo_hits(echo, S1328, allow_continuation=False)) == ["sentence_two_cards"],
   "strict mode: the same sentence carded twice → sentence_two_cards")
ok(craft.CARD_VO_ALLOW_CONTINUATION is True, "continuation is on by default (CoS 06/10)")
hook_dup = [HOOK, stmt("The agent called undo on a tool with no undo."), *GOOD[1:]]
ok(checks(craft.card_vo_hits(hook_dup, S1328)) == ["echo_card"],
   "a mid card repeating the hook sentence echoes the hook")
gap = board(
    stmt("That tool never had an undo, and the model invented one."),
    quote("Agents copy editor habits onto deletes, sends, and deploys."),
    stmt("The previous state is gone."),
    quote("No undo in the tool, no unsupervised call."),
)
h = craft.card_vo_hits(gap, S1328)
ok(checks(h) == ["sentence_no_card"] and h[0]["i"] is None and "returns success" in h[0]["detail"],
   "a spoken sentence with no card → sentence_no_card")
ok(craft.card_vo_check_hits(craft.card_vo_marker(gap, S1328)) == [],
   "sentence_no_card is informational: not gated (object cards anchor by cue only)")

print("continuation: literal next words only (CoS 06/10)")
SL = ("The agent called undo on a tool with no undo. Agents copy editor habits onto deletes, sends, "
      "and deploys, and nobody checks the schema first. Subscribe — next agent trap.")
A = "Agents copy editor habits onto deletes,"
B = "sends, and deploys,"
C = "and nobody checks the schema first."


def lb(*mid):
    return [HOOK, *mid, END]


ok(craft.card_vo_hits(lb(stmt(A), quote(B), stmt(C)), SL) == [],
   "valid continuation: 3 cards, each the literal next words, reaching the sentence end → pass")
ok(craft.card_vo_hits(lb(stmt("agents copy editor habits onto deletes sends and deploys"),
                         quote("“And nobody checks the schema first!”")), SL) == [],
   "valid 2-card continuation with punctuation/case/quote marks normalised → pass")
ok(craft.card_vo_hits(lb(stmt(A + " " + B + " " + C)), SL) == [], "the whole long sentence on one card → pass")
h = craft.card_vo_hits(lb(stmt(A), quote("onto deletes, sends, and deploys,"), stmt(C)), SL)
ok(h[0]["check"] == "echo_card" and h[0]["i"] == 2 and "repeats words card 1" in h[0]["detail"]
   and all(x["check"] in craft.CARD_VO_GATED for x in h),
   "echo: the next card repeats earlier words ('onto deletes') → echo_card")
h = craft.card_vo_hits(lb(stmt(A), stmt(A + " " + B), stmt(C)), SL)
ok("echo_card" in checks(h), "echo: the next card re-quotes the sentence from its first word (#1423 #3) → echo_card")
h = craft.card_vo_hits(lb(stmt("Agents copy editor habits"), quote(B), stmt(C)), SL)
ok("card_fragment" in checks(h) and any(x["i"] == 2 and "skips words" in x["detail"] for x in h),
   "skipped words: 'onto deletes' never shown between cards → card_fragment (not the next words)")
h = craft.card_vo_hits(lb(stmt(A), quote("sends and nobody checks"), stmt(C)), SL)
ok("card_not_in_vo" in checks(h) and all(x["check"] in craft.CARD_VO_GATED for x in h if x["i"] == 2),
   "non-contiguous fragment ('sends … nobody checks'): its words are not spoken in that order → gated")
h = craft.card_vo_hits(lb(stmt("editor habits onto deletes, sends,")), SL)
ok(checks(h) == ["card_fragment"] and "does not start" in h[0]["detail"],
   "any substring is not enough: a mid-sentence piece on its own → card_fragment")
h = craft.card_vo_hits(lb(quote(B + " " + C)), SL)
ok(checks(h) == ["card_fragment"], "a loose tail (does not start the sentence) → card_fragment")
h = craft.card_vo_hits(lb(stmt(A), quote(B)), SL)
ok(checks(h) == ["card_fragment"] and h[0]["i"] == 2 and "stops before" in h[0]["detail"],
   "a chain that stops before the sentence ends → card_fragment on its last card")
h = craft.card_vo_hits(lb(stmt(A), quote(B), quote("and nobody reads the docs.")), SL)
ok(checks(h)[0] == "card_not_in_vo" and h[0]["i"] == 3,
   "unspoken quote in the continuation slot → card_not_in_vo")
ok("card_fragment" in checks(h), "…and the chain before it no longer reaches the sentence end")
h = craft.card_vo_hits(lb(stmt(A), {"type": "stat", "cue": "sends, and deploys", "value": "3"}, stmt(C)), SL)
ok("card_fragment" in checks(h),
   "a text card after an object card mid-sentence is a loose part, not a continuation")
rich = lb({"type": "stat", "cue": "Agents copy editor habits", "value": "3", "label": "habits"},
          {"type": "diagram", "cue": "and nobody checks the schema", "title": "Schema"})
ok(craft.card_vo_hits(rich, SL) == [],
   "object cards later on the same sentence keep the #70 rule (anchor later, not a near-duplicate) → pass")
ok(checks(craft.card_vo_hits(lb(stmt(A), quote(B), stmt(C)), SL, allow_continuation=False))
   == ["card_fragment", "card_fragment", "card_fragment", "sentence_two_cards"],
   "strict mode (allow_continuation=False): the same valid chain fails")
m_echo = craft.card_vo_marker(lb(stmt(A), quote("onto deletes, sends, and deploys,"), stmt(C)), SL)
ok(m_echo["continuation"] is True and any(x.startswith("echo_card") for x in craft.card_vo_check_hits(m_echo)),
   "marker records continuation=True; echo_card is gated")
g = craft.video_maker_gate(lb(stmt(A), quote(B), stmt(C)), card_vo=craft.card_vo_marker(lb(stmt(A), quote(B), stmt(C)), SL))
ok(not any("Card ⊂ VO" in r for r in g["reasons"]), "gate: a valid continuation chain passes")

print("skips")
ok(craft.card_vo_hits([], S1328) == [] and craft.card_vo_hits(GOOD, "") == []
   and craft.card_vo_hits(None, None) == [], "empty board / no script → no hits")
ok(all(h["check"] != "sentence_no_card" or "Subscribe" not in h["detail"]
       for h in craft.card_vo_hits(gap, S1328)), "the endcard/Subscribe sentence is never required to have a card")
ok(craft.card_vo_hits(board(*GOOD[1:], {"type": "cta", "text": "Follow for more"}), S1328) == [],
   "cta/endcard cards are skipped")

print("marker + gate")
m_bad = craft.card_vo_marker(frag, S1328)
ok(m_bad["version"] == craft.CARD_VO_V1 and m_bad["source"] == "card_source_text+vo_script"
   and checks(m_bad["hits"]) == ["card_fragment", "card_fragment"], "marker records version, source and hits")
ok(craft.card_vo_marker(frag, S1328, content_format="long") is None, "longs get no marker")
g = craft.video_maker_gate(frag, card_vo=m_bad)
ok(g["result"] == "FAIL" and g["checks"]["B"] == "FAIL"
   and any(r.startswith("[B] Card ⊂ VO: FAIL") and "sends, and deploys." in r for r in g["reasons"]),
   "gate: Gate B FAIL with the Card ⊂ VO reason")
ok([k["check"] for k in g.get("kinds", []) if k["check"] == "card_vo"] == ["card_vo", "card_vo"]
   and all(k["kind"] == "real" for k in g["kinds"] if k["check"] == "card_vo"),
   "Card ⊂ VO hits are kind=real (craft defects, never measurement)")
g_ok = craft.video_maker_gate(GOOD, card_vo=craft.card_vo_marker(GOOD, S1328))
ok(not any("Card ⊂ VO" in r for r in g_ok["reasons"]), "clean board: no Card ⊂ VO reason")
ok(craft.card_vo_check_hits({"version": "other", "hits": m_bad["hits"]}) == []
   and craft.card_vo_check_hits(None) == [], "unknown/absent marker is not judged")

cc_new = {"beats": frag, "beat_timing": craft.BEAT_TIMING_CURRENT, "card_vo": m_bad}
cc_old = {"beats": frag, "beat_timing": craft.BEAT_TIMING_CURRENT}
r_new = craft.video_maker_gate_reason(json.dumps(cc_new), "short")
r_old = craft.video_maker_gate_reason(json.dumps(cc_old), "short")
ok(r_new is not None and "Card ⊂ VO" in r_new, "craft_review gate reason fails a marked render")
ok(r_old is None or "Card ⊂ VO" not in r_old, "older renders without the marker are not re-judged")
ok(craft.video_maker_gate_reason(json.dumps(cc_new), "long") is None, "longs exempt from the gate")
pr = craft.publish_craft_block_reason(title="The agent called undo on a tool with no undo. · Agent traps 3",
                                     script=S1328, creation_config=json.dumps(cc_new),
                                     content_format="short", require_vo_beats=False, check_audio=False)
ok(pr is not None and "Card ⊂ VO" in pr, "publish_craft_block_reason (approve/publish/auto-approve) blocks it")

print("render records the marker")
with patch.object(craft, "beats_from_html", return_value=frag):
    cc_r = worker._creation_config("The agent called undo on a tool with no undo. · Agent traps 3",
                                   {"content_format": "short"}, "<html>", S1328, 23.0, "portrait",
                                   None, False, brand="os")
    cc_fb = worker._creation_config("s", {"content_format": "short"}, "<html>", S1328, 23.0,
                                    "portrait", None, True, brand="os")
ok(cc_r.get("card_vo", {}).get("version") == craft.CARD_VO_V1
   and checks(cc_r["card_vo"]["hits"]) == ["card_fragment", "card_fragment"]
   and any(r.startswith("[B] Card ⊂ VO: FAIL") for r in cc_r["craft_gate"]["reasons"]),
   "new short render stores card_vo and its craft_gate fails the fragments")
ok("card_vo" not in cc_fb, "fallback render: no card_vo marker")
with patch.object(craft, "beats_from_html", return_value=GOOD):
    cc_g = worker._creation_config("s", {"content_format": "short"}, "<html>", S1328, 23.0,
                                   "portrait", None, False, brand="os")
ok(cc_g["card_vo"]["hits"] == [] and not any("Card ⊂ VO" in r for r in cc_g["craft_gate"]["reasons"]),
   "clean render: empty card_vo hits, no Card ⊂ VO reason")
ok("card_vo=cvo" in inspect.getsource(worker._creation_config)
   and "card_vo=cvo" in inspect.getsource(craft.video_maker_gate_reason),
   "render gate and stored-gate recompute both pass card_vo")

print()
print(f"ALL {_checks} CHECKS PASSED")
