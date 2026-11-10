"""Per-channel sentence pace (PO CM cut 2026-11-10; CMO steering): RR 2.2s / 2.5s.

Run: PYTHONPATH=. .venv/bin/python tests/verify_sentence_pace.py

settings.sentence_pace = {"rr": {"target_s": 2.2, "cap_s": 2.5}} (key order:
"channel:<id>" → channel slug → brand; no entry → generator unchanged, e.g. OS).
_generate_script adds the pace rule to the prompt, asks one rewrite for
over-cap sentences, then app.services.sentence_pace.enforce: filler drop
first, split only into two clausal halves (no "E…" half, no echo), hook and
closer never touched, unsplittable sentences reported (never mangled).
Fixtures: tests/fixtures/rr_pace/stub_llm_1464_style.txt (stub LLM output on
the #1464 topic); sample_vo.json = real edge-tts pt-BR words of the post-pass
sample (the Gate B evidence in the PR).
"""
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.services import craft
from app.services import sentence_pace as sp
from app.services.engines import storyboard, worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


FXD = Path(__file__).parent / "fixtures" / "rr_pace"
STUB = (FXD / "stub_llm_1464_style.txt").read_text().strip()
TITLE = "Pedir resumo não é engenharia. · IA 235"
HOOK = "Pedir resumo não é engenharia."
CLOSER = "Se inscreve. Próxima armadilha de IA."
RR = {"content_format": "short", "brand": "rr", "voice_name": "pt-BR-AntonioNeural-Male"}
OS = {"content_format": "short", "brand": "os", "voice_name": "en-US-AndrewNeural-Male"}


def sents(s):
    return [x for x in sp._SENT_SPLIT_RE.split(s or "") if x.strip()]


def toks(s):
    return [sp._fold(w) for w in re.findall(r"[\wÀ-ÿ]+", s or "")]


def lost_words(before, after):
    """Multiset of words in `before` missing from `after`."""
    a = toks(after)
    out = []
    for w in toks(before):
        if w in a:
            a.remove(w)
        else:
            out.append(w)
    return out


def gen(subject, params, reply, cfg=None):
    calls = []

    def llm(prompt, system=None, max_tokens=2000):
        calls.append(prompt)
        return reply(len(calls)) if callable(reply) else reply

    ctx = patch.object(settings, "sentence_pace", cfg) if cfg is not None else patch.object(
        settings, "sentence_pace", settings.sentence_pace)
    with ctx, patch.object(worker, "_llm", side_effect=llm):
        out = worker._generate_script(subject, params)
    return out, calls


print("config / lookup")
ok(settings.sentence_pace.get("rr") == {"target_s": 2.2, "cap_s": 2.5}, "default RR = 2.2s target / 2.5s cap")
ok("os" not in settings.sentence_pace, "OS has no entry (unchanged)")
ok(sp.pace_for(RR) == {"target_s": 2.2, "cap_s": 2.5, "key": "rr"}, "RR short → pace")
ok(sp.pace_for(OS) is None and sp.pace_for({"brand": "acme"}) is None, "OS / other tenants → None")
ok(sp.pace_for({**RR, "content_format": "long"}) is None, "long-form → None")
cfg = {"rr": {"target_s": 2.2, "cap_s": 2.5}, "channel:7": {"target_s": 2.0, "cap_s": 2.3},
       "rr-pt": {"target_s": 2.1, "cap_s": 2.4}, "bad": {"target_s": 3, "cap_s": 2}}
with patch.object(settings, "sentence_pace", cfg):
    ok(sp.pace_for({**RR, "channel_id": 7, "channel_slug": "rr-pt"})["key"] == "channel:7", "channel id beats slug/brand")
    ok(sp.pace_for({**RR, "channel_id": 9, "channel_slug": "rr-pt"})["key"] == "rr-pt", "slug beats brand")
    ok(sp.pace_for({"brand": "bad"}) is None, "cap < target is ignored")

print("estimator (fit to 87 real edge-tts pt-BR sentences: MAE 0.135s, LOSO-CV MAE 0.143s)")
plain = sp.estimate_spoken_s("O modelo lê a página toda.")
ok(sp.estimate_spoken_s("O modelo lê a página 14.") > sp.estimate_spoken_s("O modelo lê a página X."), "numbers weigh more than a short word")
ok(sp.estimate_spoken_s("Volta em JSON.") > sp.estimate_spoken_s("Volta em mim."), "'JSON' heavier than a normal word")
ok(sp.estimate_spoken_s("Próxima armadilha de IA.") > sp.estimate_spoken_s("Próxima armadilha de lá."), "'IA' heavier than a monosyllable")
ok(1.2 < plain < 2.5, f"6-word sentence ≈ {plain}s")
# real #1455 timings (fixture) vs estimate: every sentence within 0.6s
fx = json.loads((Path(__file__).parent / "fixtures" / "rr1455" / "vo.json").read_text())
spans = craft.sentence_spans(fx["script"], fx["words"])
errs = [abs(sp.estimate_spoken_s(s) - (spans[i][1] - spans[i][0] if isinstance(spans[i], (list, tuple)) else spans[i]))
        for i, s in enumerate(sents(fx["script"])) if i in spans]
ok(errs and max(errs) < 0.6, f"#1455 real timings: max |err| {max(errs):.2f}s, mean {sum(errs)/len(errs):.2f}s")

print("post-pass rules")
P = {"target_s": 2.2, "cap_s": 2.5}
out, rep = sp.enforce(STUB, P)
ss = sents(out)
ok(ss[0] == HOOK, "hook sentence untouched")
ok(out.endswith(CLOSER), "closer untouched")
ok(all(sp.estimate_spoken_s(s) <= 2.5 + 1e-9 for s in ss[1:-2]), "every non-protected sentence ≤ 2.5s estimated")
lost = lost_words(STUB, out)
ok(all(w in sp.FILLER_WORDS | sp.SPLIT_DROP_WORDS for w in lost), f"no content words lost (dropped only {sorted(set(lost))})")
ok(rep["splits"] >= 3 and not rep["unsplittable"], f"{rep['splits']} splits, none unsplittable")
for c in rep["changes"]:
    if c["kind"] != "split":
        continue
    a, b = c["to"][0], c["to"][-1]
    ok(sp.looks_clausal(a) and sp.looks_clausal(b), f"complete halves: {a!r} | {b!r}")
    ok(not toks(b)[0] == "e" and not toks(a)[0] == "e", "no half starts with 'E'")
    ok(not sp.is_echo(a, b), "second half does not repeat the first's verb/noun")
ok(all(not s.startswith(("E ", "e ")) for s in ss), "no sentence in the output starts with 'E'")
# hook / closer stay even when long
long_hook = "Pedir resumo pro modelo sem especificar os campos não é engenharia de verdade. " \
            "O resto cabe. " + CLOSER
o2, _ = sp.enforce(long_hook, P)
ok(sents(o2)[0] == sents(long_hook)[0] and o2.endswith(CLOSER), "over-cap hook is never split or shortened")
# filler first
o3, r3 = sp.enforce(HOOK + " O modelo realmente escolhe sozinho o que importa. " + CLOSER, P)
ok("realmente" not in o3 and r3["splits"] == 0 and "O modelo escolhe sozinho o que importa." in o3,
   "shorten (filler) before splitting")
# unsplittable: reported, never mangled
one = HOOK + " A decisão errada que o modelo tomou ontem continua lá, puxando a resposta. " + CLOSER
o4, r4 = sp.enforce(one, P)
ok(o4 == one and r4["unsplittable"], "one-idea over-cap sentence is reported, text unchanged")
# list enumerations are not split into fragments
lst = HOOK + " Eu pergunto por campo: valor, prazo, multa, e a página de cada um. " + CLOSER
o5, _ = sp.enforce(lst, P)
ok("valor, prazo, multa" in o5 and not any(s.lower().startswith(("valor", "prazo", "multa", "a página")) for s in sents(o5)),
   "enumeration kept whole (no fragment halves)")
ok(sp.enforce(STUB, P)[0] == sp.enforce(out, P)[0], "idempotent")
ok(not sp.looks_clausal("E o detalhe.") and not sp.looks_clausal("Puxando a resposta.")
   and sp.looks_clausal("Ninguém percebe.") and not sp.looks_clausal("Quando não cabe."),
   "clause check: no 'E…' stub, no gerund fragment, no bare subordinate")

print("stubbed-LLM RR generation")
out, calls = gen(TITLE, RR, STUB)
ok("SENTENCE PACE (hard rule)" in calls[0] and "2.2s" in calls[0] and "2.5s" in calls[0], "RR prompt carries the 2.2s/2.5s rule")
ok("~3.9s spoken" not in calls[0], "legacy 3.9s soft line replaced for RR")
ok(len(calls) == 2 and "2.5s" in calls[1], "one rewrite ask for over-cap sentences")
ok(sents(out)[0] == HOOK and out.endswith(CLOSER), "generated RR: hook + PT closer intact")
ok(not sp.over_cap(out, 2.5), "generated RR: every sentence ≤ 2.5s estimated")
ok(all(w in sp.FILLER_WORDS | sp.SPLIT_DROP_WORDS for w in lost_words(STUB, out)), "generated RR: content preserved")
out_ok, c2 = gen(TITLE, RR, lambda n: STUB if n == 1 else sp.enforce(STUB, P)[0])
ok(not sp.over_cap(out_ok, 2.5), "rewrite reply within cap is used")

print("OS unchanged")
os_reply = ("Skipping the spec is not engineering. The model guesses what you meant. "
            "You ship the guess, and nobody notices for a very long time until the bill arrives. Follow for more.")
o_os, c_os = gen("Skipping the spec is not engineering · AI 12", OS, os_reply)
o_os0, c_os0 = gen("Skipping the spec is not engineering · AI 12", OS, os_reply, cfg={})
ok(o_os == o_os0 and c_os == c_os0, "OS prompt + output identical with or without sentence_pace config")
ok(all("SENTENCE PACE" not in c and "2.5s" not in c for c in c_os), "OS: no pace rule, no pace rewrite ask")

print("Gate B on the real edge-tts sample (#201 check)")
FX = json.loads((FXD / "sample_vo.json").read_text())
S, W, D = FX["script"], FX["words"], FX["duration"]
ok(S == out, "fixture script = this generator's output")
html = storyboard.compose(subject=FX["title"], script=S, words=W, duration=D, resolution="portrait",
                          width=1080, height=1920, topic_id=None, content_format="short",
                          language="Portuguese", llm=lambda *a, **k: (_ for _ in ()).throw(AssertionError("llm")),
                          brand="rr", title=FX["title"], topic_name="IA")
b = craft.beats_from_html(html)
g = craft.video_maker_gate(b, content_format="short", used_fallback=False, beat_timing=craft.BEAT_TIMING_CURRENT,
                           hook_pace=craft.hook_pace_marker(b, W, "rr", "short"),
                           card_sync=craft.card_sync_marker(b, W, "short", script=S, brand="rr"),
                           card_text=craft.card_text_marker(b, S, W, brand="rr"),
                           card_vo=craft.card_vo_marker(b, S, brand="rr"))
ok(g["result"] == "PASS", f"Gate B {g['result']} {g['checks']} {g.get('reasons')}")
ok(not craft.card_timing_hits(b, W, S, duration=D), "card_timing clean")
ok(not craft.blank_gap_hits(b, D), "blank_gap clean")
sp_real = craft.sentence_spans(S, W)
mx = max((v[1] - v[0]) if isinstance(v, (list, tuple)) else v for v in sp_real.values())
ok(mx <= 2.5 + 0.01, f"real TTS: longest sentence {mx:.2f}s ≤ 2.5s")
cta = [x for x in b if x["type"] in craft.CTA_TYPES][0]
ok(b[0]["text"] == HOOK and "Se inscreve" in (cta.get("text") or ""), "board: hook card + 'Se inscreve · IA' chip")

print(f"ALL {_checks} CHECKS PASSED")
