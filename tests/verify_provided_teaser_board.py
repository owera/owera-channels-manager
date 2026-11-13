"""OS #1477: a provided (verbatim) script on a Shipping teaser gets the
deterministic VO-sentence board — whole-sentence quotes, no list/slide card,
cards timed on the measured (edge-tts WordBoundary) word times.

    PYTHONPATH=. .venv/bin/python tests/verify_provided_teaser_board.py

Fixture: tests/fixtures/os-1477/snapshot-2026-11-12.json (the prod render
that FAILed Gate B/C: list card for sentence 2, 3 sentences without a card,
quotes up to 1.10s before speech / 1.58s stale). No LLM, no TTS, no DB.
"""
import json
import sys
from pathlib import Path

from app.services import craft
from app.services.engines import storyboard, worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


FX = json.loads((Path(__file__).resolve().parent / "fixtures" / "os-1477"
                 / "snapshot-2026-11-12.json").read_text())
SCRIPT, WORDS, DUR = FX["script"], FX["narration_words"], FX["duration"]


class _NoLLM:
    calls = 0

    def __call__(self, *a, **k):
        _NoLLM.calls += 1
        raise RuntimeError("LLM must not be called for a provided teaser board")


def _compose(provided, llm=None, script=SCRIPT, words=WORDS, duration=DUR):
    return storyboard.compose(
        subject=FX["subject"], script=script, words=words, duration=duration,
        resolution="portrait", width=1080, height=1920, topic_id=FX["topic_id"],
        content_format="short", language=None, llm=llm or _NoLLM(), brand="os",
        provided_thumb=True, topic_name=FX["topic_name"], on_screen_allow=None,
        on_screen=None, title=FX["title"], provided=provided)


def _params():
    return {"brand": "os", "channel_id": 1, "topic_id": FX["topic_id"],
            "topic_name": FX["topic_name"], "content_format": "short",
            "description": FX["description"], "title": FX["title"],
            "thumb_source": "provided"}


# ---------------------------------------------------------------------------
print("baseline: the stored #1477 LLM board FAILs B and C (repro)")
stored_cc = {"beats": FX["stored_beats"], "beat_timing": FX["stored_beat_timing"],
             "script": SCRIPT, "narration_words": WORDS, "duration": DUR, "brand": "os",
             "description": FX["description"], "composition_version": "storyboard",
             "gate_b_rules": craft.GATE_B_RULES, "used_fallback": False}
g0 = craft._recompute_video_maker_gate(stored_cc, "short", description=FX["description"],
                                       description_required=True, brand="os")
ok(g0["checks"].get("B") == "FAIL" and g0["checks"].get("C") == "FAIL",
   f"stored board recomputes B/C FAIL ({g0['checks']})")
ok(any("vs_card_not_allowed" in r for r in g0["reasons"]), "stored board: vs_card_not_allowed")

# ---------------------------------------------------------------------------
print("provided teaser: deterministic VO-sentence board, no LLM")
_NoLLM.calls = 0
html = _compose(True)
ok(bool(html) and worker._looks_valid(html), "compose returns a valid board")
ok(_NoLLM.calls == 0, "no LLM call for a provided teaser")
beats = craft.beats_from_html(html)
types = [b["type"] for b in beats]
ok(types[0] == "hook" and types[-1] == "cta", f"hook first, cta last ({types[0]}, {types[-1]})")
mids = beats[1:-1]
ok(all(t == "quote" for t in types[1:-1]), f"every mid card is a quote ({set(types[1:-1])})")
ok(not any(t in ("list", "compare", "term_define", "statement") for t in types),
   "no list / compare / term_define / statement card")
sents = storyboard._script_sentences(SCRIPT)
body = [s for s in sents[1:]
        if not (craft.is_endcard_vo_sentence(s) or craft.contains_subscribe_cta(s))]
ok([b["text"] for b in mids] == body,
   f"one verbatim whole-sentence card per body sentence ({len(mids)} == {len(body)})")
ok(mids[0]["text"] == "It's an AI attendant for small businesses.",
   "sentence 2 is a whole-sentence quote, verbatim")

# ---------------------------------------------------------------------------
print("timing on measured word times (Gate B windows)")
aw = storyboard.annotate_sentences(WORDS, SCRIPT)
for i, b in enumerate(beats):
    if i == 0 or b["type"] == "cta":
        continue
    st, en = float(b["start"]), float(b["start"]) + float(b["dur"])
    w0, wn = storyboard._spoken_span_of(b, aw)
    ok(w0 - craft.CARD_ENTER_LEAD_MAX_S - 1e-6 <= st <= w0 + 0.005,
       f"card {i} enters {st:.2f}s at/just before first word {w0:.2f}s")
    ok(en >= wn + craft.CARD_PAD_FLOOR_S - 0.005,
       f"card {i} holds to {en:.2f}s >= last word {wn:.2f}s + pad")
    ok(en <= wn + craft.STALE_CARD_MAX_S + 1e-6,
       f"card {i} exits {en:.2f}s <= last word + {craft.STALE_CARD_MAX_S}s (not stale)")
for i in range(1, len(beats)):
    pe = float(beats[i - 1]["start"]) + float(beats[i - 1]["dur"])
    ok(pe <= float(beats[i]["start"]) + 1e-6, f"card {i - 1} ends before card {i} starts")

# ---------------------------------------------------------------------------
print("full Video Maker gate on the new board")
cc = worker._creation_config(FX["subject"], _params(), html, SCRIPT, DUR, "portrait", None,
                             False, words=WORDS, brand="os")
gate = cc["craft_gate"]
ok(gate["result"] == "PASS", f"craft_gate PASS ({gate['checks']}; {gate.get('reasons')})")
ok(gate["checks"] == {"A": "PASS", "B": "PASS", "C": "PASS"}, "A/B/C all PASS")
ok(not (cc.get("card_timing") or {}).get("hits"), "no card_timing hits")
ok(not (cc.get("card_text") or {}).get("hits"), "no card_text hits")
ok(not (cc.get("card_vo") or {}).get("hits"), "no card_vo hits (every sentence has a card)")

# ---------------------------------------------------------------------------
print("tight pauses: clamp never overlaps the next card")
tight = []
for w in WORDS:   # squeeze every inter-sentence pause to ~0.05s
    tight.append(dict(w))
shift = 0.0
out = []
prev_end = None
for w in tight:
    st = float(w["start"]) - shift
    if prev_end is not None and st - prev_end > 0.05:
        shift += st - prev_end - 0.05
        st = prev_end + 0.05
    out.append({"text": w["text"], "start": round(st, 4), "dur": w["dur"]})
    prev_end = st + float(w["dur"])
tdur = round(prev_end + 0.6, 3)
th = _compose(True, words=out, duration=tdur)
ok(bool(th), "tight-pause board composes")
tb = craft.beats_from_html(th)
ok(all(float(tb[i - 1]["start"]) + float(tb[i - 1]["dur"]) <= float(tb[i]["start"]) + 1e-6
       for i in range(2, len(tb))), "no mid/CTA overlap with 0.05s pauses")
# (the hook keeps last word + pad by design in _cap_os_hook — pre-existing,
# not touched here; with a 0.05s pause it can overlap card 1 by ~0.15s)
taw = storyboard.annotate_sentences(out, SCRIPT)
ok(all(float(b["start"]) <= storyboard._spoken_span_of(b, taw)[0] + 0.005
       for b in tb[1:-1]), "every quote still enters at/before its first word")

# ---------------------------------------------------------------------------
print("scope: a generated (non-provided) teaser keeps the LLM board path")
_NoLLM.calls = 0
try:
    _compose(False)
except RuntimeError:
    pass
ok(_NoLLM.calls >= 1, "non-provided teaser still asks the LLM (unchanged)")

# ---------------------------------------------------------------------------
print("plumbing: worker + preflight forward provided=")
seen = {}
_orig = storyboard.compose


def _spy(**kw):
    seen.update(kw)
    return ""


storyboard.compose = _spy
try:
    worker._generate_composition(FX["subject"], SCRIPT, WORDS, "portrait", 1080, 1920, DUR,
                                 topic_id=45, brand="os", topic_name="Shipping",
                                 title=FX["title"], provided=True)
    ok(seen.get("provided") is True, "worker._generate_composition forwards provided=True")
    seen.clear()
    from app.services import gate_b_preflight
    gate_b_preflight._compose_board(SCRIPT, FX["subject"], _params(), WORDS, DUR, provided=True)
    ok(seen.get("provided") is True, "gate_b_preflight._compose_board forwards provided=True")
finally:
    storyboard.compose = _orig

print(f"\n{_checks} checks passed")
