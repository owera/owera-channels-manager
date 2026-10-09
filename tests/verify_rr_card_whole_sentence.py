"""RR whole-sentence cards (VM Gate B #1443 F1, re-render on a9addc4 08/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_rr_card_whole_sentence.py

On RR (PT series) an on-screen text card (statement/quote) shows EITHER a
whole VO sentence (bounded by . ! ? or a strong clause boundary ; : — –) OR
an isolated term (1–3 spoken words, a noun phrase or number) — never a piece
of a sentence. The #78 continuation allowance no longer lets a sentence run
over two text cards unless each card is itself whole.

  (a) Gate B: card_vo_hits(whole=True) / card_vo_marker(brand="rr").
  (b) Composer: whole-sentence board build (strong-clause units, isolated-term
      units, rich card after the sentence card) + _guard_whole_cards (rewrite
      to the whole sentence or drop) with generator slips (#88 mechanism).
  WARNs: a code card the VO never speaks is invented (RR PT); a bare stat
      number spoken as a label ("da mensagem 30") is drawn "mensagem 30",
      static (no 0→30 count-up).
OS (EN) got the same rule on 09/10 (tests/verify_os_card_whole_sentence.py); other brands keep #78.
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


FX = json.loads((Path(__file__).parent / "fixtures" / "rr1443" / "a9_render.json").read_text())
S = FX["script"]
WORDS = FX["words"]
A9 = FX["board_a9"]
ALLOWED = ["hook", "statement", "stat", "compare", "list", "term_define", "quote", "code",
           "command", "cta"]
SUBJECT = "Chat sem fim não é engenharia. · IA 233"


def checks(hits):
    return [(h["i"], h["check"]) for h in hits]


# ---------------------------------------------------------------------------
print("Fixtures: the #1443 fragments are not whole sentences nor isolated terms")
FRAGS = ["entre uma resposta e outra.", "Depois abro um chat novo", "só com esse resumo.",
         "Quando não cabe", "errada da mensagem", "puxando a resposta."]
for t in FRAGS:
    ok(craft.card_piece_reason(t, S) is not None, f"{t!r} → piece of a sentence")
for t in ["O modelo não lembra de nada entre uma resposta e outra.",
          "Depois abro um chat novo só com esse resumo.", "Eu fecho o fio."]:
    ok(craft.card_piece_reason(t, S) is None, f"{t!r} → whole sentence")
for t in ["mensagem 30", "chat novo", "Fio", "resumo", "dez linhas"]:
    ok(craft.card_piece_reason(t, S) is None, f"{t!r} → isolated term")
for t in ["você", "esse", "nada", "Peço", "O modelo", "decisão errada da mensagem 30"]:
    ok(craft.isolated_term_reason(t, S) is not None, f"{t!r} is not a term (pronoun/verb/edge/long)")
SC = "Eu fecho o fio; depois abro outro chat. Pronto."
ok(craft.is_whole_unit("Eu fecho o fio;", SC) and craft.is_whole_unit("depois abro outro chat.", SC),
   "each strong clause (;) is a whole unit")
ok(not craft.is_whole_unit("Eu fecho", SC), "a clause piece is not")

# ---------------------------------------------------------------------------
print("(a) Gate B: card_vo whole rule on the a9addc4 board")
h = craft.card_vo_hits(A9, S, whole=True)
frag_i = sorted(i for i, c in checks(h) if c == "card_fragment")
ok(frag_i == [2, 4, 7, 8, 11, 12], f"card_fragment on #2 #4 #7 #8 #11 #12 (got {frag_i})")
ok(any(x["i"] == 11 and "piece of the sentence" in x["detail"] for x in h),
   "#11 'Depois abro um chat novo' + #12 split: now FAIL (was a #78 continuation)")
h78 = craft.card_vo_hits(A9, S)
ok(not any(x["i"] in (11, 12) for x in h78), "other series (#78 rule) unchanged: #11+#12 continuation passes")
m = craft.card_vo_marker(A9, S, brand="rr")
ok(m.get("whole_sentence") is True and any(x["i"] == 12 for x in m["hits"]), "marker brand rr → whole rule")
ok("whole_sentence" not in craft.card_vo_marker(A9, S, brand="owera"), "marker other brand → #78 rule")
ok(any(x.startswith("card_fragment") for x in craft.card_vo_check_hits(m)), "the hit is gated (Gate B FAIL)")

H = {"type": "hook", "cue": "Eu fecho o fio", "text": "Eu fecho o fio;"}
END = {"type": "cta", "cue": "Pronto", "text": "Se inscreve · IA"}
two = [H, {"type": "quote", "cue": "depois abro", "text": "depois abro outro chat."}, END]
ok(not [c for c in checks(craft.card_vo_hits(two, SC, whole=True)) if c[1] == "card_fragment"],
   "a sentence split at ';' over two cards passes (each card is whole)")
mid = [{"type": "hook", "cue": "x", "text": "Pronto."},
       {"type": "quote", "cue": "Eu fecho o fio", "text": "Eu fecho o fio;"},
       {"type": "quote", "cue": "depois abro outro", "text": "depois abro outro chat."}, END]
ok(not [c for c in checks(craft.card_vo_hits(mid, SC, whole=True)) if c[1] == "card_fragment"],
   "two whole clause cards in a row pass")
cut = [H, {"type": "quote", "cue": "depois abro", "text": "depois abro"},
       {"type": "quote", "cue": "outro chat", "text": "outro chat."}, END]
ok(sorted(i for i, c in checks(craft.card_vo_hits(cut, SC, whole=True)) if c == "card_fragment") == [1, 2],
   "a clause split into 'depois abro' + 'outro chat.' FAILs on both pieces (#78 allowed it)")
ok(not [c for c in checks(craft.card_vo_hits(cut, SC)) if c[1] == "card_fragment"],
   "… which the #78 rule (other series) still accepts as a continuation")

# isolated term after the sentence card / rich card after it
SS = ("Chat sem fim não é engenharia. A decisão errada da mensagem 30 continua lá, puxando a "
      "resposta. Se inscreve.")
tb = [{"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia."},
      {"type": "quote", "cue": "A decisão errada", "text": "A decisão errada da mensagem 30 continua lá, puxando a resposta."},
      {"type": "quote", "cue": "mensagem 30", "text": "mensagem 30", "plain": True, "isolated": True},
      {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA"}]
ok(not [c for c in checks(craft.card_vo_hits(tb, SS, whole=True)) if c[1] in craft.CARD_VO_GATED],
   "sentence card + isolated term 'mensagem 30' → no gated card_vo hit")
ok(not craft.card_text_hits(tb, SS, rr=True), "… and no card_text echo / near_duplicate")
ok(not craft.repeated_card_hits(tb), "… and no partial-echo repeated card")
tb_plain = [dict(b) for b in tb]
tb_plain[2].pop("isolated")
ok(craft.repeated_card_hits(tb_plain), "the same term WITHOUT the isolated flag stays a partial echo")
rb = [tb[0], tb[1], {"type": "stat", "cue": "mensagem 30", "value": "30", "unit": "mensagem",
                     "unit_first": True, "label": ""}, tb[3]]
ok(not [c for c in checks(craft.card_vo_hits(rb, SS, whole=True)) if c[1] in craft.CARD_VO_GATED]
   and not craft.card_text_hits(rb, SS, rr=True),
   "RR: a rich card illustrating the sentence card (anchored later) passes")
ok([c for c in checks(craft.card_vo_hits(rb, SS)) if c[1] == "sentence_two_cards"],
   "other series: the same pair stays sentence_two_cards (#78 unchanged)")

# ---------------------------------------------------------------------------
print("(b) Composer guard: rewrite to the whole sentence or drop, with slips")
tok = storyboard._WHOLE_CARDS.set(True)
try:
    g = [{"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia."},
         {"type": "quote", "cue": "Depois abro um chat novo", "text": "Depois abro um chat novo"},
         {"type": "quote", "cue": "só com esse resumo.", "text": "só com esse resumo."},
         {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA"}]
    slips = []
    storyboard._guard_whole_cards(g, S, None, slips)
    ok([b.get("text") for b in g[1:-1]] == ["Depois abro um chat novo só com esse resumo."],
       "split sentence → one whole-sentence card (the later piece goes)")
    ok([x["kind"] for x in slips] == ["fragment_to_sentence", "fragment_dropped"], "both repairs are slips")
    g = [{"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia."},
         {"type": "stat", "cue": "O modelo não lembra", "value": "0", "label": "x"},
         {"type": "quote", "cue": "entre uma resposta e outra.", "text": "entre uma resposta e outra."},
         {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA"}]
    slips = []
    storyboard._guard_whole_cards(g, S, None, slips)
    ok([b["type"] for b in g] == ["hook", "stat", "cta"] and slips[0]["kind"] == "fragment_dropped",
       "a tail piece on a sentence another card carries is dropped")
    # timed: a dropped card's time goes to the previous mid card within the cap
    g = [{"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia.", "start": 0.0, "dur": 2.38},
         {"type": "stat", "cue": "O modelo não lembra", "value": "0", "label": "x", "start": 2.5, "dur": 1.2},
         {"type": "quote", "cue": "entre uma", "text": "entre uma resposta e outra.", "start": 3.82, "dur": 1.2},
         {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA", "start": 5.14, "dur": 3.0}]
    slips = []
    storyboard._guard_whole_cards(g, S, WORDS, slips, timed=True)
    ok(len(g) == 3 and abs(g[1]["dur"] - 2.52) < 1e-6, "timed drop: previous card holds until the next one")
    g[1]["dur"] = 1.2
    g.insert(2, {"type": "quote", "cue": "entre uma", "text": "entre uma resposta e outra.", "start": 3.82, "dur": 2.6})
    slips = []
    storyboard._guard_whole_cards(g, S, WORDS, slips, timed=True)
    ok(len(g) == 4 and slips[0]["kind"] == "fragment_unrepaired",
       "timed, no hold-safe repair → kept + slip (Gate B blocks it)")
finally:
    storyboard._WHOLE_CARDS.reset(tok)
ok(storyboard._WHOLE_CARDS.get() is False, "whole mode is off outside an RR compose")
plain = [{"type": "hook", "cue": "Chat", "text": "Chat sem fim não é engenharia."},
         {"type": "quote", "cue": "Depois abro um chat novo", "text": "Depois abro um chat novo"},
         {"type": "cta", "cue": "Se inscreve", "text": "x"}]
storyboard._guard_whole_cards(plain, S, None, [])
ok(plain[1]["text"] == "Depois abro um chat novo", "guard is a no-op outside RR")
ok(storyboard._split_point("Quando não cabe mais, corta ou resume, e você não vê".split()) is not None,
   "other series: clause splits at commas/conjunctions as before")
tok = storyboard._WHOLE_CARDS.set(True)
ok(storyboard._split_point("Quando não cabe mais, corta ou resume, e você não vê".split()) is None,
   "RR: no comma/conjunction split point (only ; : — –)")
ok(storyboard._split_point("Eu fecho o fio; depois abro outro chat novo".split()) == 4, "RR: split at ';'")
storyboard._WHOLE_CARDS.reset(tok)

# ---------------------------------------------------------------------------
print("WARN: code card the VO never speaks (RR PT)")
code = A9[3]
ok(code["type"] == "code" and not craft.code_card_spoken(code, S), "#1443 #3 snippet is not spoken")
ok(craft.invented_output(code, S) == [], "how it got through: code lines were 'illustrative' (no check)")
ok(len(craft.invented_output(code, S, spoken_code=True)) == 3, "RR PT now: every unspoken line is invented")
ok(craft.code_card_spoken({"type": "code", "lines": ["n_batch = 512"]}, "O n batch fica em 512."),
   "identifiers the VO says pass")
ct = craft.card_text_hits(A9, S, rr=True)
ok(any(x["i"] == 3 and x["check"] == "invented_output" for x in ct), "Gate B card_text flags it on RR")
EN = "The app resends the whole chat on every message. Subscribe."
ok(not [x for x in craft.card_text_hits(
    [{"type": "hook", "cue": "x", "text": "x"}, dict(code, cue="The app resends"),
     {"type": "cta", "cue": "Subscribe", "text": "Subscribe"}], EN, rr=True)
    if x["check"] == "invented_output"], "EN RR script: illustrative snippet rule unchanged")

print("WARN: the 0→30 counter (bare number spoken as a label)")
ok(craft.stat_ordinal_unit(A9[6], S) == "mensagem", "'da mensagem 30' → label noun 'mensagem'")
ok(craft.stat_ordinal_unit({"type": "stat", "value": "30"}, "Depois de 30 mensagens o contexto estoura.") == "",
   "'de 30 mensagens' is a quantity")
ok(craft.stat_ordinal_unit({"type": "stat", "value": "10", "unit": "linhas"}, S) == "", "a stat with a unit is untouched")
ok(any(x["i"] == 6 and x["check"] == "stat_word_order" for x in ct), "Gate B card_text flags the counter")
gb = [{"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia."},
      {"type": "stat", "cue": "A decisão errada", "value": "30", "unit": "", "label": ""},
      {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA"}]
storyboard._enforce_card_text_rules(gb, S, None, "rr")
ok(gb[1].get("unit") == "mensagem" and gb[1].get("unit_first") is True, "composer: drawn 'mensagem 30'")
html, tw = storyboard.render_stat(dict(gb[1], start=0.0, dur=2.8),
                                 {"i": 0, "start": 0.0, "dur": 2.8, "is_last": False,
                                  "width": 1080, "height": 1920, "duration": 35.0})
ok(">30<" in html and not any("var o={v:0}" in t for t in tw),
   "static number, no count-up from 0")

# ---------------------------------------------------------------------------
print("End to end: RR compose of #1443 (a9addc4 draft) → Gate B clean")
LLM_BOARD = {"beats": [
    {"type": "hook", "cue": "Chat sem fim não é engenharia", "text": "Chat sem fim não é engenharia."},
    {"type": "stat", "cue": "O modelo não lembra", "value": "0", "unit": "", "label": "não lembra de nada"},
    {"type": "quote", "cue": "entre uma resposta e outra.", "text": "entre uma resposta e outra."},
    {"type": "code", "cue": "O app reenvia", "lines": code["lines"]},
    {"type": "quote", "cue": "Quando não cabe", "text": "Quando não cabe"},
    {"type": "compare", "cue": "corta ou resume", "title": "Não cabe mais",
     "left": {"title": "Corta", "items": ["o que saiu"]}, "right": {"title": "Resume", "items": ["você não vê"]}},
    {"type": "stat", "cue": "A decisão errada", "value": "30", "unit": "", "label": "puxando a resposta"},
    {"type": "quote", "cue": "errada da mensagem", "text": "errada da mensagem"},
    {"type": "term_define", "cue": "Eu fecho o fio", "term": "Fio", "definition": "a conversa que eu fecho"},
    {"type": "stat", "cue": "dez linhas", "value": "10", "unit": "linhas", "label": "com as decisões"},
    {"type": "quote", "cue": "Depois abro um chat novo", "text": "Depois abro um chat novo"},
    {"type": "cta", "cue": "Se inscreve. Próxima armadilha de IA.", "text": "Se inscreve · IA", "sub": ""}]}


def compose(brand):
    return storyboard.compose(subject=SUBJECT, script=S, words=WORDS, duration=FX["duration"],
                              resolution="1080x1920", width=1080, height=1920, topic_id=None,
                              content_format="short", allowed_types=ALLOWED, language="pt",
                              llm=lambda *a, **k: json.dumps(LLM_BOARD), brand=brand)


html = compose("rr")
beats = craft.beats_from_html(html)
texts = [b.get("text") for b in beats if b.get("type") in craft.TEXT_CARD_TYPES]
ok(all(craft.card_piece_reason(t, S) is None for t in texts),
   f"every text card is a whole sentence or an isolated term: {texts}")
ok(not any(t in texts for t in FRAGS), "none of the #1443 fragments is on the board")
ok(not [b for b in beats if b.get("type") == "code"], "no invented code card")
st30 = [b for b in beats if b.get("type") == "stat" and b.get("value") == "30"]
ok(st30 and st30[0].get("unit") == "mensagem" and st30[0].get("unit_first"), "counter drawn 'mensagem 30'")
vo = craft.card_vo_marker(beats, S, brand="rr")
ok(vo["whole_sentence"] and not craft.card_vo_check_hits(vo), f"card_vo whole: no gated hit {vo['hits']}")
ok(not craft.card_text_hits(beats, S, WORDS, rr=True), "card_text: no hit")
ok(not craft.card_sync_hits(craft.card_sync_marker(beats, WORDS, "short", script=S)), "card sync: PASS")
ok(not craft.repeated_card_hits(beats), "no repeated card")
ok(beats[0]["dur"] <= 2.38 + 1e-6, f"first cut ≤2.5s kept (hook {beats[0]['dur']}s)")
ok(all(b["dur"] <= craft.MID_BEAT_MAX_S + 1e-6 for b in beats[1:-1]), "mid holds ≤2.80s")
sl = craft.generator_slips_from_html(html)
kinds = [x["kind"] for x in sl or []]
ok("unspoken_code" in kinds and "fragment_dropped" in kinds,
   f"repairs logged as generator slips (#88 mechanism): {kinds}")
gate = craft.attach_generator_slips(craft.video_maker_gate(
    beats, content_format="short", used_fallback=False, beat_timing=craft.BEAT_TIMING_CURRENT,
    hook_pace=craft.hook_pace_marker(beats, WORDS, "rr", "short"),
    card_sync=craft.card_sync_marker(beats, WORDS, "short", script=S),
    card_text=craft.card_text_marker(beats, S, WORDS, brand="rr"), card_vo=vo), sl)
ok(gate["checks"].get("B") == "PASS", f"craft gate B PASS ({gate.get('reasons')})")
ok(gate.get("generator_slips") == len(sl), "slips surface in craft_gate.generator_slips")

html_o = compose("owera")
ok(craft.generator_slips_from_html(html_o) is None, "non-RR brand: no whole-card slips (board path unchanged)")

print(f"\nverify_rr_card_whole_sentence: {_checks} checks OK")
