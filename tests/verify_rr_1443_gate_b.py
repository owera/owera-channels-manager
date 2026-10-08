"""RR #1443 Gate B (08/10) fixes — scoped to RR / PT-BR, OS and Shipping unchanged.

Run: PYTHONPATH=. .venv/bin/python tests/verify_rr_1443_gate_b.py

F4  stat "30 mensagem" while the VO says "da mensagem 30": RR PT stat cards
    whose unit is spoken before the number are drawn unit-first ("mensagem
    30", static, no count-up) and card_text flags stat_word_order otherwise.
F5  English micro "same series" on the RR IA endcard: PT series get no micro.
F6  Spoken CTA still "Subscribe — next IA trap." (TTS "trép"), chip already
    "Se inscreve · IA": a provided/pre-composed script with the stale EN closer
    on a PT series gets the decided PT closer "Se inscreve. Próxima armadilha
    de IA." (edit endcard_pt_swapped). EN series keep their closer.
"""
import json
import sys

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


PHASE_A = ["hook", "statement", "stat", "compare", "list", "term_define", "quote", "cta"]
CTX = {"i": 0, "start": 0.0, "dur": 2.8, "is_last": False,
       "width": 1080, "height": 1920, "duration": 35.0}

# #1443 "Chat sem fim não é engenharia. · IA 233" (VO_TRANSCRIPT body).
S1443 = ("Chat sem fim não é engenharia. O modelo não lembra de nada entre uma resposta e outra. "
         "O app reenvia a conversa a cada mensagem. Quando não cabe mais, corta ou resume, "
         "e você não vê o que saiu. A decisão errada da mensagem 30 continua lá, puxando a "
         "resposta. Eu fecho o fio. Peço um resumo de dez linhas com as decisões e confiro. "
         "Depois abro um chat novo só com esse resumo. Se inscreve. Próxima armadilha de IA.")
T1443 = "Chat sem fim não é engenharia. · IA 233"


def stat(value, unit="", label="decisão errada", cue="A decisão errada da mensagem 30"):
    return {"type": "stat", "cue": cue, "value": value, "unit": unit, "label": label}


def board(*mid):
    return [{"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia."},
            {"type": "statement", "cue": "O app reenvia", "text": "O app reenvia a conversa a cada mensagem."},
            *mid,
            {"type": "statement", "cue": "Eu fecho o fio", "text": "Eu fecho o fio."},
            {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA", "sub": ""}]


# ---------------------------------------------------------------------------
print("F4: stat word order follows the VO (RR PT)")
ok(craft.stat_unit_first(stat("30", "mensagem"), S1443),
   "'da mensagem 30' → unit spoken before the number")
ok(craft.stat_unit_first(stat("30 mensagem"), S1443),
   "value written '30 mensagem' with no unit is caught too")
ok(craft.stat_value_unit(stat("30 mensagem")) == ("30", "mensagem"),
   "'30 mensagem' splits into value 30 / unit mensagem")
ok(not craft.stat_unit_first(stat("30", "mensagens"), "Depois de 30 mensagens o contexto estoura."),
   "'30 mensagens' (a quantity) keeps number-first")
ok(not craft.stat_unit_first(stat("10", "linhas"), S1443),
   "'resumo de dez linhas' (no 'linhas 10') keeps number-first")
ok(not craft.stat_unit_first(stat("300", "ms"), "The call takes 300 ms. Then ms 300 again."),
   "VO saying both orders keeps number-first")
ok(not craft.stat_unit_first({"type": "statement", "text": "mensagem 30"}, S1443),
   "only stat cards")

g = storyboard.parse_storyboard(json.dumps({"beats": board(stat("30", "mensagem"))}), PHASE_A)
storyboard._enforce_card_text_rules(g, S1443, None, "rr")
st = [b for b in g if b.get("type") == "stat"][0]
ok(st.get("unit_first") is True and st["value"] == "30" and st["unit"] == "mensagem",
   "RR PT generator marks the stat unit_first")
ok(all(h["check"] != "stat_word_order" for h in craft.card_text_hits(g, S1443, rr=True)),
   "generator output carries no stat_word_order hit")
ok(any("unit_first" in x for x in craft.snapshot_beats(g)), "unit_first survives the HTML beat snapshot")

g2 = storyboard.parse_storyboard(json.dumps({"beats": board(stat("30 mensagem"))}), PHASE_A)
storyboard._enforce_card_text_rules(g2, S1443, None, "rr")
st2 = [b for b in g2 if b.get("type") == "stat"][0]
ok(st2["value"] == "30" and st2["unit"] == "mensagem" and st2.get("unit_first"),
   "'30 mensagem' value is split and marked unit_first")

html, tw = storyboard.render_stat(dict(st, start=0.0, dur=2.8), dict(CTX))
row = html.split('class="stat-row"', 1)[1]
ok(row.find("stat-unit") < row.find("stat-num"), "card draws the unit before the number")
ok('<span class="stat-unit">mensagem</span><span class="stat-num">30</span>' in html,
   "card reads 'mensagem 30' (real value, not a 0 placeholder)")
ok(not any("var o={v:0}" in t for t in tw), "ordinal stat has no count-up from 0")

raw = board(stat("30", "mensagem"))
hits = craft.card_text_hits(raw, S1443, rr=True)
ok(any(h["check"] == "stat_word_order" for h in hits),
   "Gate B card_text flags a '30 mensagem' board that skipped the generator")
ok(not any(h["check"] == "stat_word_order" for h in craft.card_text_hits(raw, S1443, rr=False)),
   "stat_word_order is RR-only (rr=False → no hit)")

print("F4: OS / EN unchanged")
EN = "The agent retried call 30 and burned the budget. Subscribe — next agent trap."
en_b = [{"type": "hook", "cue": "The agent retried", "text": "The agent retried call 30."},
        {"type": "statement", "cue": "burned the budget", "text": "It burned the budget."},
        stat("30", "call", label="retry", cue="call 30"),
        {"type": "statement", "cue": "next", "text": "Check the retries."},
        {"type": "cta", "cue": "Subscribe", "text": "Subscribe · Agent traps"}]
en_parsed = storyboard.parse_storyboard(json.dumps({"beats": en_b}), PHASE_A)
storyboard._enforce_card_text_rules(en_parsed, EN, None, "os")
ok(not any(b.get("unit_first") for b in en_parsed), "OS generator never marks unit_first")
storyboard._enforce_card_text_rules(g_pt_os := storyboard.parse_storyboard(
    json.dumps({"beats": board(stat("30", "mensagem"))}), PHASE_A), S1443, None, "os")
ok(not any(b.get("unit_first") for b in g_pt_os), "non-RR brand on PT text: untouched")
ok(not any(h["check"] == "stat_word_order" for h in craft.card_text_hits(en_b, EN, rr=True)),
   "EN text (not PT) never gets stat_word_order")
os_html, os_tw = storyboard.render_stat({"value": "42", "unit": "ms", "label": "per call",
                                         "start": 0.0, "dur": 3}, dict(CTX))
ok('<span class="stat-num">0</span><span class="stat-unit">ms</span>' in os_html
   and any("var o={v:0}" in t and "42" in t for t in os_tw),
   "default stat render unchanged (number-first, count-up)")

# ---------------------------------------------------------------------------
print("F5: no English micro on the PT endcard")
rr = craft.series_endcard(T1443, S1443, brand="rr")
ok(rr["chip"] == "Se inscreve · IA" and rr["micro"] == "", "RR IA: chip PT, no 'same series'")
ok(craft.endcard_clean(rr), "RR IA endcard still clean")
cta = board()
storyboard._sanitize_cta(cta, S1443, subject=T1443, brand="rr")
ok(cta[-1]["text"] == "Se inscreve · IA" and cta[-1]["sub"] == "", "compose card: sub is empty")
ok(craft.series_endcard_micro("IA") == "", "series_endcard_micro('IA') == ''")
for series, title, brand in (("Copilot Credits", "Copilot billed $27. · Copilot Credits 75", "os"),
                             ("Agent memory", "x · Agent memory 3", "os"),
                             ("Shipping", "x · Shipping 12", "os"),
                             ("Local", "Gemma 12B cabe em 8 gigas. · Local 12", "rr")):
    c = craft.series_endcard(title, brand=brand)
    ok(c["series"] == series and c["micro"] == "same series" and c["chip"] == f"Subscribe · {series}",
       f"{series}: EN chip + 'same series' micro unchanged")

# ---------------------------------------------------------------------------
print("F6: stale EN closer on a PT series → PT closer")
stale = S1443.rsplit("Se inscreve.", 1)[0] + "Subscribe — next IA trap."
txt, edits = craft.prepare_provided_script(stale, T1443, brand="rr")
ok(txt.endswith("Eu fecho o fio. Peço um resumo de dez linhas com as decisões e confiro. "
                "Depois abro um chat novo só com esse resumo. Se inscreve. Próxima armadilha de IA."),
   "'Subscribe — next IA trap.' replaced by 'Se inscreve. Próxima armadilha de IA.'")
ok("Subscribe" not in txt and "trap" not in txt, "no EN closer / 'trap' left to mispronounce")
ok(edits == ["endcard_pt_swapped"], "edit recorded as endcard_pt_swapped")
ok(txt.startswith(stale.rsplit("Subscribe", 1)[0].rstrip()), "body before the closer untouched")
ok(craft.prepare_provided_script(txt, T1443, brand="rr") == (txt, []), "idempotent")
ok(craft.prepare_provided_script(S1443, T1443, brand="rr") == (S1443, []),
   "a PT closer is kept as written")
ok(craft.prepare_provided_script("Chat sem fim não é engenharia. Subscribe — next IA receipt.",
                                 "Chat sem fim não é engenharia.", brand="rr")[0]
   == "Chat sem fim não é engenharia. Se inscreve. Próxima armadilha de IA.",
   "RR default series (IA) with no title suffix, any EN noun → PT closer")
ok(storyboard._spoken_endcard_cue(txt) == "Se inscreve. Próxima armadilha de IA.",
   "endcard cue follows the new spoken PT closer (#83)")

print("F6: EN series keep their closer (OS / Shipping / RR Local)")
for script, subject, brand in (
        ("Gemma 12B cabe em 8 gigas. Subscribe — next Local receipt.",
         "Gemma 12B cabe em 8 gigas. · Local 12", "rr"),
        ("The deploy shipped twice. Subscribe — next Shipping trap.", "The deploy shipped twice. · Shipping 9", "os"),
        ("Copilot billed $27. Subscribe — next Copilot Credits trap.",
         "Copilot billed $27. · Copilot Credits 75", "os"),
        ("The agent forgot the plan. Subscribe — next Agent memory trap.", "The agent forgot the plan.", "os"),
        # EN closer naming IA but the video's series is EN → untouched
        ("Chat sem fim. Subscribe — next IA trap.", "Chat sem fim. · Shipping 4", "os"),
        # RR title says Local but the closer names IA → series is EN, untouched
        ("Gemma cabe. Subscribe — next IA trap.", "Gemma cabe. · Local 3", "rr")):
    out, ed = craft.prepare_provided_script(script, subject, brand=brand)
    ok(out == script and ed == [], f"kept as written: {script[-40:]!r}")
ok(not craft.pt_series_needs_pt_closer("Subscribe — next Local trap.", "x", brand="rr"),
   "closer naming an EN series is never swapped")
ok(not craft.pt_series_needs_pt_closer("Se inscreve. Próxima armadilha de IA.", T1443, brand="rr"),
   "PT closer needs no swap")

print(f"\nverify_rr_1443_gate_b: {_checks} checks passed")
