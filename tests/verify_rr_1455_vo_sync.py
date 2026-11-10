"""RR #1455 card/VO sync (Gate B FAIL 2026-11-09) — RR on the VO-sentence board.

Run: PYTHONPATH=. .venv/bin/python tests/verify_rr_1455_vo_sync.py

Root cause (VM GATEB_2026-11-09, verified at tip 0eb81153):
  1. storyboard._compose_board took the deterministic one-card-per-sentence +
     snap-to-speech path only when brand == "os"; RR got the LLM template
     board (compare / term_define / list / isolated-term echoes) chained at
     fixed 2.80s holds, never snapped → cards drift into the PT ~0.94s pause
     (stale) and lead their speech (enters early).
  2. craft.SYNC_LEAD_MAX_S (1.1s) let the compose aligner call "synced" a board
     the gate fails (card_enters_early > CARD_ENTER_LEAD_MAX_S 0.3s;
     quote_before_speech: quotes 0 lead).
Fix: per-brand config settings.vo_sentence_card_brands (default os + rr;
other tenants' brands unchanged), gate-sourced sync lead (0.3s, quotes 0) on
those brands, VO-pause closing so the PT pause stays under blank / stale
budgets, and the RR hook join threshold aligned to the 2.5s first cut.
Fixture: tests/fixtures/rr1455/vo.json = #1455's real edge-tts pt-BR word
timings (creation_config.narration_words) + its pinned 15-sentence script.
"""
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

from app.config import settings
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


FX = json.loads((Path(__file__).parent / "fixtures" / "rr1455" / "vo.json").read_text())
S, W, D, T = FX["script"], FX["words"], FX["duration"], FX["title"]
TOL = craft.SYNC_TOL_S


def no_llm(*a, **k):
    raise AssertionError("RR VO-sentence path must not call the LLM")


def compose(words=W, duration=D, brand="rr", llm=no_llm):
    return storyboard.compose(subject=T, script=S, words=words, duration=duration,
                              resolution="portrait", width=1080, height=1920,
                              topic_id=FX["topic_id"], content_format="short",
                              language="Portuguese", llm=llm, brand=brand, title=T,
                              topic_name="IA")


def gate(beats, words, brand="rr"):
    return craft.video_maker_gate(
        beats, content_format="short", used_fallback=False,
        beat_timing=craft.BEAT_TIMING_CURRENT,
        hook_pace=craft.hook_pace_marker(beats, words, brand, "short"),
        card_sync=craft.card_sync_marker(beats, words, "short", script=S, brand=brand),
        card_text=craft.card_text_marker(beats, S, words, brand=brand),
        card_vo=craft.card_vo_marker(beats, S, brand=brand))


def sentences(script):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", script.strip()) if x.strip()]


# ---------------------------------------------------------------------------
print("config: per-brand VO-sentence cards (multi-tenant)")
ok(list(settings.vo_sentence_card_brands) == ["os", "rr"], "default vo_sentence_card_brands = [os, rr]")
ok(craft.vo_sentence_cards_applies("rr") and craft.vo_sentence_cards_applies("os"), "os + rr on")
ok(not craft.vo_sentence_cards_applies("owera") and not craft.vo_sentence_cards_applies("")
   and not craft.vo_sentence_cards_applies(None), "unconfigured brands keep the LLM board")
with patch.object(settings, "vo_sentence_card_brands", ["os", "acme"]):
    ok(craft.vo_sentence_cards_applies("acme") and not craft.vo_sentence_cards_applies("rr"),
       "brands come from settings (env MANAGER_VO_SENTENCE_CARD_BRANDS), not hardcoded")
with patch.object(craft, "OS_VO_SENTENCE_CARDS", False):
    ok(not craft.vo_sentence_cards_applies("rr") and not craft.vo_sentence_cards_applies("os"),
       "global kill switch OS_VO_SENTENCE_CARDS=False turns it off everywhere")

# ---------------------------------------------------------------------------
print("claim 2: sync lead sourced from the Gate B budget")
ok(craft.sync_lead_max() == craft.SYNC_LEAD_MAX_S == 1.1, "legacy lead 1.1s outside the gate-aligned block")
ok(craft.sync_lead_max({"type": "stat"}, aligned=True) == craft.CARD_ENTER_LEAD_MAX_S == 0.3,
   "gate-aligned: non-quote card lead ≤ CARD_ENTER_LEAD_MAX_S (0.3s)")
ok(craft.sync_lead_max({"type": "quote", "text": "x"}, aligned=True) == 0.0
   and craft.sync_lead_max("quote", aligned=True) == 0.0, "gate-aligned: quote card lead 0")
ok(craft.sync_lead_max({"type": "code", "lines": ["n = 1"]}, aligned=True) == 0.0,
   "quote-like literal (code) card lead 0 (quote_before_speech)")
with patch.object(craft, "CARD_ENTER_LEAD_MAX_S", 0.25):
    ok(craft.sync_lead_max({"type": "stat"}, aligned=True) == 0.25,
       "lead follows the gate constant (craft and gate can't drift)")
with craft.gate_aligned_sync(True):
    ok(craft.sync_lead_max({"type": "stat"}) == 0.3, "gate_aligned_sync block → 0.3s")
ok(craft.sync_lead_max({"type": "stat"}) == 1.1, "block exit restores legacy")
_b = [{"type": "hook", "start": 0.0, "dur": 2.0, "text": "Pular a spec não é engenharia."},
      {"type": "stat", "start": 2.5, "dur": 2.0, "cue": "Você pede", "value": "1", "unit": "", "label": "x"},
      {"type": "quote", "start": 5.86, "dur": 2.4, "text": "Ele devolve oitenta linhas de código."},
      {"type": "cta", "start": 32.45, "dur": 3.0, "text": "Se inscreve · IA"}]
_rr = craft.card_sync_marker(_b, W, "short", script=S, brand="rr")
_lg = craft.card_sync_marker(_b, W, "short", script=S, brand="owera")
ok(_rr["gate_aligned"] and _rr["lead_max"] == 0.3 and "gate_aligned" not in _lg and _lg["lead_max"] == 1.1,
   "card_sync marker: RR gate-aligned (0.3s), other brands legacy (1.1s)")
_rh = craft.card_sync_hits(_rr)
ok(any("card 1" in h and "max 0.3s" in h for h in _rh), f"RR: stat 0.50s early → hit ({_rh})")
ok(any("card 2" in h and "max 0.0s" in h for h in _rh), "RR: quote 0.10s early → hit (quote lead 0)")
ok(craft.card_sync_hits(_lg) == [], "legacy brand: same board within 1.1s → no hit (unchanged)")

# ---------------------------------------------------------------------------
print("claim 1: RR #1455 composes the snapped VO-sentence board (real TTS timings)")
html = compose()
ok(bool(html), "RR compose returns HTML without calling the LLM")
beats = craft.beats_from_html(html)
types = [b.get("type") for b in beats]
mids = beats[1:-1]
ok(types[0] == "hook" and types[-1] == "cta", "hook first, CTA last")
ok(not ({"compare", "term_define", "list", "stat"} & set(types)), f"no LLM template cards ({types})")
ok(not any(craft.is_term_card(b) for b in beats), "no isolated-term / echo card")
want = [s for s in sentences(S)[1:] if not craft.is_endcard_vo_sentence(s)
        and not craft.contains_subscribe_cta(s)]
ok([b.get("text") for b in mids] == want,
   f"one mid card per VO sentence, verbatim, in order ({len(mids)} cards)")
for i, b in enumerate(mids, start=1):
    sp = craft.spoken_span(b.get("text"), W)
    st, end = float(b["start"]), float(b["start"]) + float(b["dur"])
    ok(sp and abs(st - sp[0]) <= TOL + 1e-6, f"card {i} enters on its first word ({st:.2f} vs {sp[0]:.2f})")
    ok(end >= sp[1] + craft.CARD_PAD_FLOOR_S - 1e-6 and end <= sp[1] + craft.STALE_CARD_MAX_S + 1e-6,
       f"card {i} exits in [last word + pad, last word + stale] ({end - sp[1]:.2f}s after)")
    ok(float(b["dur"]) <= craft.MID_BEAT_MAX_S + 1e-6, f"card {i} hold ≤ {craft.MID_BEAT_MAX_S}s")
ok(craft.card_timing_hits(beats, W, S, duration=D) == [],
   "card timing: no stale / quote_before_speech / enters / exits early")
ok(craft.blank_gap_hits(beats, D) == [], "blank gap: the PT 0.94s pause stays ≤ budget")
ok(craft.card_sync_hits(craft.card_sync_marker(beats, W, "short", script=S, brand="rr")) == [],
   "card sync (gate-aligned 0.3s / quotes 0): no hit")
print("RR hook + CTA preserved")
ok(beats[0]["text"] == "Pular a spec não é engenharia." and beats[0]["start"] == 0.0, "hook = title claim")
ok('data-split="1"' in html and re.search(r"Pular a spec.*✕.*não é engenharia", re.sub(r"<[^>]+>", " ", html), re.S),
   "two-box hook 'Pular a spec ✕ não é engenharia.' (frame0 split)")
ok(beats[-1]["text"] == "Se inscreve · IA" and not beats[-1].get("sub"), "CTA chip 'Se inscreve · IA', no EN micro")
g = gate(beats, W)
ok(g["checks"]["A"] == "PASS" and g["checks"]["C"] == "PASS", "Gate A + C PASS")
ok([r for r in g["reasons"] if "hook pace" not in r] == [],
   f"un-joined TTS: only the RR first cut remains ({g['reasons']})")

print("RR hook join: PT pause after the claim → TTS join (worker), first cut by 2.5s")
ok(craft.hook_join_next_by("rr") == craft.HOOK_FIRST_CUT_BY_S, "RR join threshold = 2.5s first cut (quote lead 0)")
ok(craft.needs_hook_join(S, W, "rr"), "#1455: next sentence at 3.00s → worker re-TTSes with the hook join")
ok(not craft.needs_hook_join(S, W, "os"), "OS never joins (unchanged)")
nxt = craft.next_speech_start(craft.first_sentence(S), W)
end = craft.claim_spoken_end(beats[0]["text"], W)
shift = nxt - (end + 0.28)   # ';' pause ≈0.28s (craft HOOK_JOIN note)
WJ = [dict(w, start=round(float(w["start"]) - shift, 3)) if float(w["start"]) >= nxt - 0.01 else w
      for w in W]
bj = craft.beats_from_html(compose(words=WJ, duration=round(D - shift, 2)))
ok(float(bj[1]["start"]) <= craft.HOOK_FIRST_CUT_BY_S + 1e-6, f"joined: first cut {bj[1]['start']:.2f}s ≤ 2.5s")
gj = gate(bj, WJ)
ok(gj["result"] == "PASS", f"joined TTS: craft gate A/B/C PASS ({gj['reasons']})")
ok(craft.card_timing_hits(bj, WJ, S, duration=round(D - shift, 2)) == [], "joined: card timing clean")

# ---------------------------------------------------------------------------
print("kill switch: RR off settings → LLM template path (old behaviour)")
calls = []
with patch.object(settings, "vo_sentence_card_brands", ["os"]):
    compose(llm=lambda *a, **k: (calls.append(1) or json.dumps({"beats": [
        {"type": "hook", "cue": "Pular a spec", "text": "Pular a spec não é engenharia."},
        {"type": "quote", "cue": "Você pede", "text": "Você pede uma função simples."},
        {"type": "cta", "cue": "Se inscreve", "text": "Se inscreve · IA"}]})))
    legacy_join = craft.hook_join_next_by("rr")
ok(calls, "RR dropped from vo_sentence_card_brands → compose calls the LLM")
ok(legacy_join == craft.HOOK_JOIN_NEXT_BY_S, "and hook join is back on the legacy 3.6s")

print(f"\nverify_rr_1455_vo_sync: {_checks} checks OK")
