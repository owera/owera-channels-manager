"""Card ⊂ VO craft_review check (Video Maker P0, council 06/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_card_vo.py

Every text card (statement/quote) must show one WHOLE sentence the VO says
(punctuation and case normalised), and each spoken sentence gets exactly one
card: no card spanning two sentences, no sentence with two cards, no card
text absent from the VO. No OCR runs in the pipeline, so the check reads the
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
ok(checks(h) == ["card_fragment", "card_fragment", "sentence_two_cards"]
   and h[-1]["i"] == 6 and "cards 5, 6" in h[-1]["detail"],
   "one sentence split over two quote cards → 2 fragments + sentence_two_cards")

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
ok(checks(craft.card_vo_hits(tail, S1328)) == ["card_spans_sentences", "sentence_two_cards"],
   "a card straddling a sentence boundary is spanning (not a fragment) and doubles the stat's sentence")

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
ok(checks(craft.card_vo_hits(echo, S1328)) == ["sentence_two_cards"],
   "the same sentence carded twice (echo) → sentence_two_cards")
ok(craft.CARD_VO_ALLOW_CONTINUATION is False,
   "continuation cards are not exempt by default (VM rule is strict; product switch)")
hook_dup = [HOOK, stmt("The agent called undo on a tool with no undo."), *GOOD[1:]]
ok(checks(craft.card_vo_hits(hook_dup, S1328)) == ["sentence_two_cards"],
   "a mid card repeating the hook sentence is a second card on sentence 0")
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
