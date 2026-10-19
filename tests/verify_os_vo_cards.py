"""OS VO-sentence cards + Gate B (blank_gap / stale_card / allowlist / quotes).

Run: PYTHONPATH=. .venv/bin/python tests/verify_os_vo_cards.py

OS-brand shorts: mid cards are one whole VO sentence (or #96 strong-clause
units when a sentence exceeds the mid cap). No compare / term_define /
list / isolated-term. Kill switch craft.OS_VO_SENTENCE_CARDS = False
restores the LLM path (post-filter still converts banned cards). RR
unchanged. Literal quotes enter at the first spoken word, hold the whole
span (mid-cap exception), exit last word + QUOTE_HOLD_TAIL_S.
"""
import inspect
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.services import craft, review_guard, topic_flags
from app.services.engines import storyboard, worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


SCRIPT = (
    "One folder broke Copilot CLI. Add a directory with the add-dir flag, and every "
    "Claude subagent failed on the spot. A maximum of 4 blocks with cache_control may "
    "be provided. Found 5. One block over the limit. The parent agent got an empty "
    "answer. Update Copilot CLI, and Anthropic subagents keep working with extra "
    "directories. Exact version in the description. Subagent came back empty? Read "
    "its error, not the parent's. Subscribe — next agent trap."
)
SUBJECT = "One folder broke Copilot CLI. · Agent traps 12"
ALLOW = [{"kind": "quote", "text": "A maximum of 4 blocks with cache_control may be provided.",
          "source": "https://example.test"},
         {"kind": "log", "text": "Found 5.", "source": "https://example.test"}]


def words_of(script, step=0.36, start=0.1):
    out, t = [], float(start)
    for w in script.split():
        out.append({"text": w.strip(".,!?:;"), "start": round(t, 3), "dur": 0.28})
        t += step
    return out, round(t + 0.6, 2)


print("kill switch + flags")
ok(craft.OS_VO_SENTENCE_CARDS is True, "OS_VO_SENTENCE_CARDS default on")
ok(craft.QUOTE_HOLD_TAIL_S == 0.3, "QUOTE_HOLD_TAIL_S = 0.3")
ok(craft.STALE_CARD_MAX_S == craft.BLANK_GAP_MAX_S == 0.5, "stale = blank budget 0.5s")
ok(Settings().topic_flags.get("allowlist_required") == [47]
   and topic_flags.DEFAULTS[topic_flags.ALLOWLIST_REQUIRED] == (47,),
   "allowlist_required default [47]")
ok(topic_flags.has(topic_flags.ALLOWLIST_REQUIRED, 47)
   and not topic_flags.has(topic_flags.ALLOWLIST_REQUIRED, 7),
   "topic 47 required, topic 7 not")
src = inspect.getsource(storyboard._compose_board)
ok("OS_VO_SENTENCE_CARDS" in src and "os_vo" in src,
   "compose branches on the kill switch")
ok("allowlist_required_reason" in inspect.getsource(
    __import__("app.routers.videos", fromlist=["approve"]).approve),
   "approve 409-holds allowlist_required")


print("deterministic OS board (no LLM)")
W, DUR = words_of(SCRIPT)
calls = []


def boom(*a, **k):
    calls.append(1)
    raise AssertionError("OS VO-sentence path must not call the LLM")


html = storyboard.compose(
    subject=SUBJECT, script=SCRIPT, words=W, duration=DUR, resolution="portrait",
    width=1080, height=1920, topic_id=7, content_format="short", language="English",
    llm=boom, brand="os", topic_name="Agent traps",
    allowed_types=["hook", "statement", "stat", "compare", "list", "term_define",
                   "quote", "code", "command", "cta"])
ok(html and not calls, "OS compose returns HTML without calling the LLM")
beats = craft.beats_from_html(html)
types = [b.get("type") for b in beats]
ok(types[0] == "hook" and types[-1] == "cta", "hook first, CTA last")
ok(not ({"compare", "term_define", "list"} & set(types)),
   f"no banned OS templates ({types})")
ok(not any(craft.is_term_card(b) for b in beats), "no isolated-term card")
mids = [b for b in beats[1:] if (b.get("type") or "") not in craft.CTA_TYPES]
ok(mids and all((b.get("type") or "") in ("quote", "statement") or b.get("_lit")
                or (b.get("type") in craft.CLI_BEAT_TYPES)
                for b in mids), f"mids are text/literal ({[b.get('type') for b in mids]})")
for b in mids:
    if (b.get("type") or "") == "quote":
        ok(craft.text_in_script(b.get("text"), SCRIPT),
           f"quote is VO-literal: {b.get('text')!r}")
        ok(not craft.is_endcard_vo(b.get("text") or ""),
           "endcard VO is not a mid card")
ok(beats[0].get("text", "").startswith("One folder broke Copilot CLI"),
   f"hook is the title claim ({beats[0].get('text')!r})")
# first VO sentence is the hook — not also a mid
ok(not any(craft.text_in_script("One folder broke Copilot CLI.", b.get("text") or "")
           and (b.get("type") or "") == "quote" and "Add a directory" not in (b.get("text") or "")
           for b in mids if (b.get("text") or "").startswith("One folder")),
   "hook sentence is not also a mid card")


print("kill switch off → LLM path")
calls.clear()
craft.OS_VO_SENTENCE_CARDS = False
try:
    storyboard.compose(
        subject=SUBJECT, script=SCRIPT, words=W, duration=DUR, resolution="portrait",
        width=1080, height=1920, topic_id=7, content_format="short", language="English",
        llm=lambda *a, **k: (calls.append(1) or json.dumps({"beats": [
            {"type": "hook", "cue": "One folder", "text": "One folder broke Copilot CLI."},
            {"type": "compare", "cue": "Add a directory",
             "left": {"title": "A", "items": ["x"]}, "right": {"title": "B", "items": ["y"]}},
            {"type": "cta", "cue": "Subscribe", "text": "Subscribe"}]})),
        brand="os")
finally:
    craft.OS_VO_SENTENCE_CARDS = True
ok(calls, "kill switch False calls the LLM on OS")


print("RR unchanged (still calls LLM)")
rr_calls = []
rr_script = "Chat sem fim não é engenharia. Eu fecho o fio. Subscribe — next IA trap."
rr_w, rr_d = words_of(rr_script)
storyboard.compose(
    subject="Chat sem fim não é engenharia. · IA 1", script=rr_script, words=rr_w,
    duration=rr_d, resolution="portrait", width=1080, height=1920, topic_id=3,
    content_format="short", language="Portuguese",
    llm=lambda *a, **k: (rr_calls.append(1) or json.dumps({"beats": [
        {"type": "hook", "cue": "Chat sem fim", "text": "Chat sem fim não é engenharia."},
        {"type": "quote", "cue": "Eu fecho", "text": "Eu fecho o fio."},
        {"type": "cta", "cue": "Subscribe", "text": "Se inscreve · IA"}]})),
    brand="rr")
ok(rr_calls, "RR compose still uses the LLM")


print("Gate B: OS template / literal / stale / quote-before-speech / allowlist")
banned = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "One folder broke Copilot CLI.",
     "object": "terminal"},
    {"type": "compare", "start": 2.12, "dur": 2.0, "cue": "Add a directory",
     "left": {"title": "Claude", "items": ["failed"]},
     "right": {"title": "Spot", "items": ["on the spot"]}},
    {"type": "term_define", "start": 4.24, "dur": 2.0, "term": "add-dir",
     "definition": "flag that adds a directory", "cue": "add-dir"},
    {"type": "quote", "start": 6.36, "dur": 1.2, "text": "cache_control", "isolated": True,
     "plain": True},
    {"type": "cta", "start": 7.68, "dur": 3.0, "text": "Subscribe · Agent traps"},
]
os_hits = craft.os_card_hits(banned, SCRIPT)
ok(any(h["check"] == "vs_card_not_allowed" for h in os_hits), "compare → vs_card_not_allowed")
ok(any(h["check"] == "single_term_card" for h in os_hits), "term_define / isolated → single_term_card")
g = craft.video_maker_gate(banned, brand="os", script=SCRIPT)
ok(g["checks"]["B"] == "FAIL" and any("OS cards" in r for r in g["reasons"]),
   "live OS hits are a Gate B FAIL")
ok(any(k["check"] == "os_cards" for k in g.get("kinds") or []),
   "kinds include os_cards")

invented = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "One folder broke Copilot CLI.",
     "object": "terminal"},
    {"type": "quote", "start": 2.12, "dur": 2.0, "text": "Invented slogan on screen.", "plain": True},
    {"type": "cta", "start": 4.24, "dur": 3.0, "text": "Subscribe · Agent traps"},
]
ok(any(h["check"] == "card_text_not_literal" for h in craft.os_card_hits(invented, SCRIPT)),
   "non-VO copy → card_text_not_literal")

# quote before speech
qw, _ = words_of("The fix landed today. Subscribe — next agent trap.", step=0.4, start=2.0)
qboard = [
    {"type": "hook", "start": 0.0, "dur": 1.8, "text": "The fix landed today.", "object": "terminal"},
    {"type": "quote", "start": 0.4, "dur": 2.0, "text": "The fix landed today.",
     "literal_quote": True},
    {"type": "cta", "start": 5.0, "dur": 2.5, "text": "Subscribe · Agent traps"},
]
qh = craft.quote_before_speech_hits(qboard, qw)
ok(qh and qh[0]["check"] == "quote_before_speech", f"quote before first word → hit ({qh})")

# stale hook (#1465 shape)
sw, _ = words_of("The crew failed the handoff. Next sentence is already spoken.",
                 step=0.3, start=0.1)
# first sentence ~5 words * 0.3 ≈ ends ~1.6s; hook held 7.48s
stale = [
    {"type": "hook", "start": 0.0, "dur": 7.48, "text": "The crew failed the handoff.",
     "object": "terminal"},
    {"type": "quote", "start": 7.6, "dur": 2.0, "text": "Next sentence is already spoken.",
     "plain": True},
    {"type": "cta", "start": 9.72, "dur": 2.5, "text": "Subscribe · CrewAI"},
]
sh = craft.stale_card_hits(stale, sw, "The crew failed the handoff. Next sentence is already spoken.")
ok(sh and sh[0]["check"] == "stale_card" and sh[0]["i"] == 0 and "leftover" in sh[0]["detail"],
   f"frozen hook → stale_card ({sh})")
gt = craft.video_maker_gate(stale, words=sw, script="The crew failed the handoff. Next sentence is already spoken.",
                            brand="os")
ok(gt["checks"]["B"] == "FAIL" and any("stale_card" in r for r in gt["reasons"]),
   "stale_card is a Gate B FAIL")

# allowlist required
al = craft.allowlist_required_marker(47, None)
ok(al and al["hits"] and al["hits"][0]["check"] == "allowlist_required",
   "topic 47 with no allowlist → marker hit")
ok(craft.allowlist_required_marker(7, None) is None, "topic 7 → no marker")
ok(craft.allowlist_required_marker(47, ALLOW)["hits"] == [], "topic 47 with allowlist → clean")
gal = craft.video_maker_gate(
    [{"type": "hook", "start": 0, "dur": 2, "text": "x", "object": "terminal"},
     {"type": "cta", "start": 2.12, "dur": 3, "text": "Subscribe"}],
    allowlist_check=al)
ok(gal["checks"]["B"] == "FAIL" and any("Allowlist required" in r for r in gal["reasons"]),
   "allowlist_required is a Gate B FAIL")

v47 = SimpleNamespace(topic_id=47, overrides_json=None, creation_config="{}")
ok((review_guard.allowlist_required_reason(v47) or "").startswith("held:"),
   "409 reason starts with held:")
v47ok = SimpleNamespace(topic_id=47,
                        overrides_json=json.dumps({"on_screen_allow": ALLOW}),
                        creation_config="{}")
ok(review_guard.allowlist_required_reason(v47ok) is None, "allowlist present → not held")
ok(review_guard.allowlist_required_reason(
    SimpleNamespace(topic_id=7, overrides_json=None, creation_config="{}")) is None,
   "topic 7 → not held")


print("literal quote timing (first word → last word + 0.3; mid-cap exception)")
qtext = "A maximum of 4 blocks with cache_control may be provided."
# 4.2s spoken span > 2.80 mid cap
qt, t0 = [], 3.0
for w in qtext.split():
    qt.append({"text": w.strip(".,"), "start": round(t0, 3), "dur": 0.35})
    t0 += 0.42
qt += [{"text": "Subscribe", "start": round(t0 + 0.4, 3), "dur": 0.3}]
qb = [
    {"type": "hook", "start": 0.0, "dur": 2.8, "text": "One folder broke Copilot CLI.",
     "object": "terminal"},
    {"type": "quote", "start": 1.2, "dur": 1.0, "text": qtext, "literal_quote": True},
    {"type": "cta", "start": 8.0, "dur": 2.5, "text": "Subscribe · Agent traps"},
]
storyboard._time_literal_quotes(qb, qt, ALLOW)
ok(abs(qb[1]["start"] - 3.0) < 1e-6, f"quote enters at first word ({qb[1]['start']})")
span_end = qt[len(qtext.split()) - 1]["start"] + qt[len(qtext.split()) - 1]["dur"]
expect = round(span_end + craft.QUOTE_HOLD_TAIL_S - 3.0, 3)
ok(abs(qb[1]["dur"] - expect) < 0.02, f"hold = spoken span + 0.3 ({qb[1]['dur']} vs {expect})")
ok(qb[1]["dur"] > craft.MID_BEAT_MAX_S, "literal quote may exceed the mid cap")
ok(qb[1].get("literal_quote") is True, "literal_quote flag stays on")
gg = craft.video_maker_gate(qb, beat_timing=craft.BEAT_TIMING_CURRENT, sources=ALLOW)
ok(gg["checks"]["B"] != "FAIL" or not any("mid card" in r and "beat[1]" in r for r in gg["reasons"]),
   f"Gate B hold check exempts the literal quote ({gg.get('reasons')})")

print("OS cover order: strong clause → weak clause → cap_extended_spoken")
long_and = ("Add a directory with the add-dir flag, and every Claude subagent "
            "failed on the spot.")
ok(storyboard._os_strong_parts(long_and) == [long_and],
   "no #96 strong seam on comma/and")
weak = storyboard._os_weak_parts(long_and)
ok(len(weak) == 2 and weak[0].startswith("Add a directory")
   and weak[1].lstrip().startswith("and every"),
   f"weak split on comma/and ({weak})")
ok(all(craft.is_cover_unit(p, SCRIPT) for p in weak),
   "each weak piece is a cover unit")
ok(craft.card_piece_reason("sends, and deploys.", SCRIPT) is not None,
   "RR-style comma fragment is still not a whole unit")
units = storyboard._os_sentence_units(long_and, force=True)
ok(len(units) >= 2, f"force-split long sentence ({units})")

# last-resort extend: no seam, spoken span > cap, hole before the next card
plain = "Exact version in the description."
pw, t0 = [], 2.0
for w in plain.split():
    pw.append({"text": w.strip("."), "start": round(t0, 3), "dur": 0.7})
    t0 += 0.75
# spoken ~ 4.5s, next card at 8.0
ext = [
    {"type": "hook", "start": 0.0, "dur": 1.8, "text": "One folder broke Copilot CLI.",
     "object": "terminal"},
    {"type": "quote", "start": 2.0, "dur": 2.48, "text": plain, "plain": True},
    {"type": "cta", "start": 8.0, "dur": 2.5, "text": "Subscribe · Agent traps"},
]
storyboard._os_cover_spoken_holes(ext, SCRIPT, pw)
ok(ext[1].get("cap_extended_spoken") is True, "no-seam long hold → cap_extended_spoken")
ok(abs((ext[1]["start"] + ext[1]["dur"]) - (pw[-1]["start"] + pw[-1]["dur"] + 0.3)) < 0.08,
   f"extended hold ends last word + 0.3 ({ext[1]['start'] + ext[1]['dur']:.2f})")
gx = craft.video_maker_gate(ext, beat_timing=craft.BEAT_TIMING_CURRENT, words=pw,
                            script=SCRIPT, brand="os")
ok(not any("mid card" in r and "beat[1]" in r for r in (gx.get("reasons") or [])),
   f"Gate B does not FAIL cap_extended_spoken for the cap ({gx.get('reasons')})")
ok(not any(h.get("check") == "sentence_too_long"
           for h in craft.sentence_too_long_hits(ext, pw)),
   "existing ~4.45s cap_extended_spoken is under the 4.5s hard limit")

print("sentence_too_long: 4.5s passes, 4.51s fails (plain + quote)")
ok(craft.SENTENCE_SPOKEN_MAX_S == 4.5, "SENTENCE_SPOKEN_MAX_S = 4.5")


def _spoken_words(text, t0, spoken):
    toks = [w.strip(".,!?:;") for w in text.split()]
    step = spoken / max(len(toks), 1)
    out = []
    for i, w in enumerate(toks):
        st = t0 + i * step
        du = (t0 + spoken - st) if i == len(toks) - 1 else step * 0.85
        out.append({"text": w, "start": round(st, 4), "dur": round(du, 4)})
    return out


def _span_board(text, words, *, literal=False, cap_ext=False):
    span = craft.spoken_span(text, words)
    st, en = span
    b = {"type": "quote", "text": text, "cue": text, "start": st,
         "dur": round(en + craft.QUOTE_HOLD_TAIL_S - st, 3)}
    if literal:
        b["literal_quote"] = True
    if cap_ext:
        b["plain"] = True
        b["cap_extended_spoken"] = True
    return [
        {"type": "hook", "text": "One folder broke Copilot CLI.", "start": 0.0,
         "dur": 0.8, "object": "terminal"},
        b,
        {"type": "cta", "text": "Subscribe · Agent traps",
         "start": round(st + float(b["dur"]) + 0.12, 3), "dur": 2.0},
    ]


SENT_HOLD = "Exact version in the description."
for kind, kw in (("plain cap_extended_spoken", {"cap_ext": True}),
                 ("literal quote", {"literal": True})):
    w45 = _spoken_words(SENT_HOLD, 1.0, 4.5)
    b45 = _span_board(SENT_HOLD, w45, **kw)
    span45 = craft.spoken_span(SENT_HOLD, w45)
    ok(abs((span45[1] - span45[0]) - 4.5) < 1e-3, f"{kind} 4.5s span measured")
    h45 = craft.sentence_too_long_hits(b45, w45)
    g45 = craft.video_maker_gate(b45, beat_timing=craft.BEAT_TIMING_CURRENT,
                                 words=w45, script=SCRIPT, brand="os")
    ok(not h45 and not any("sentence_too_long" in r for r in (g45.get("reasons") or [])),
       f"{kind} 4.50s spoken span PASSES sentence_too_long")
    w451 = _spoken_words(SENT_HOLD, 1.0, 4.51)
    b451 = _span_board(SENT_HOLD, w451, **kw)
    h451 = craft.sentence_too_long_hits(b451, w451)
    g451 = craft.video_maker_gate(b451, beat_timing=craft.BEAT_TIMING_CURRENT,
                                  words=w451, script=SCRIPT, brand="os")
    ok(h451 and h451[0]["check"] == "sentence_too_long"
       and "Exact version in the description." in h451[0]["detail"]
       and "4.51" in h451[0]["detail"],
       f"{kind} 4.51s names the sentence and duration ({h451})")
    ok(g451["checks"]["B"] == "FAIL"
       and any("sentence_too_long" in r for r in (g451.get("reasons") or []))
       and any(k.get("check") == "sentence_too_long" for k in (g451.get("kinds") or [])),
       f"{kind} 4.51s Gate B FAIL sentence_too_long")

# Weak-clause piece that echoes the previous sentence is not its own card
# (#1453: "Registration works" after "…and registration works.").
reg_prev = "Update the MCP Python SDK and registration works."
reg_sent = ("Registration works, and optional keys are left out instead of "
            "sent as null.")
ok(storyboard._os_unit_echoes("Registration works", [reg_prev]),
   "weak piece 'Registration works' echoes the previous sentence")
ok(storyboard._os_sentence_units(reg_sent, force=True, neighbors=[reg_prev])
   == [reg_sent],
   "echoing weak split is refused — keep the whole sentence")
reg_script = (
    "One type hint broke MCP tools. Update the MCP Python SDK and registration "
    "works. Registration works, and optional keys are left out instead of sent "
    "as null. Subscribe — next agent trap."
)
reg_beats = storyboard._os_vo_sentence_beats(
    subject="One type hint broke MCP tools. · Agent traps 14", script=reg_script)
reg_texts = [b.get("text") or "" for b in reg_beats]
ok(not any(t.strip().rstrip(".") == "Registration works" for t in reg_texts),
   f"generator does not emit the echoing piece ({reg_texts})")
ok(any("optional keys are left out" in t and t.startswith("Registration works")
       for t in reg_texts),
   "whole sentence stays one card when the weak split would echo")

print("slide_spam is Gate C (existing): statement/list echoing narration, no rich type")
ok(craft.STATEMENT_MAX_SHORTS == 1, "STATEMENT_MAX_SHORTS = 1")
ok("code" in craft.OBJECT_BEAT_TYPES and "compare" in craft.OBJECT_BEAT_TYPES,
   "rich types = code/command/diagram/compare/stat")
# OS board with allowlist log must stay code, not statement
html_os = storyboard.compose(
    subject=SUBJECT, script=SCRIPT, words=W, duration=DUR, resolution="portrait",
    width=1080, height=1920, topic_id=47, content_format="short", language="English",
    llm=boom, brand="os", topic_name="Agent traps", on_screen_allow=ALLOW)
os_b = craft.beats_from_html(html_os)
ok(any((b.get("type") or "") == "code"
       and "Found 5" in " ".join(b.get("lines") or [b.get("text") or ""])
       for b in os_b),
   f"allowlist log stays code ({[b.get('type') for b in os_b]})")
ok(not any((b.get("type") or "") == "statement" for b in os_b),
   "OS VO board has no statement (no slide_spam)")

print("isolated term is banned on OS even when allowlisted")
term_allow = [{"kind": "quote", "text": "cache_control", "source": "https://example.test"}]
ok_term = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "One folder broke Copilot CLI.",
     "object": "terminal"},
    {"type": "quote", "start": 2.12, "dur": 2.0, "text": "cache_control", "isolated": True,
     "plain": True},
    {"type": "cta", "start": 4.24, "dur": 3.0, "text": "Subscribe · Agent traps"},
]
ok(any(h["check"] == "single_term_card"
       for h in craft.os_card_hits(ok_term, SCRIPT, term_allow)),
   "allowlisted isolated term still → single_term_card")
echo_term = [
    ok_term[0],
    {"type": "quote", "start": 2.12, "dur": 2.0,
     "text": "A maximum of 4 blocks with cache_control may be provided.",
     "literal_quote": True},
    {"type": "quote", "start": 4.24, "dur": 2.0, "text": "cache_control", "isolated": True,
     "plain": True},
    ok_term[2],
]
ok(any(h["check"] == "echo_card"
       for h in craft.os_card_hits(echo_term, SCRIPT, term_allow)),
   "isolated term after a card that already showed it → echo_card")


print("worker persists card_timing + allowlist_check")
src_w = inspect.getsource(worker._creation_config)
ok('"card_timing": ctime' in src_w and '"allowlist_check": alchk' in src_w,
   "creation_config stores the new markers")
with patch.object(craft, "beats_from_html", return_value=banned):
    cc = worker._creation_config(
        SUBJECT, {"content_format": "short", "topic_id": 47, "brand": "os"},
        "<html>", SCRIPT, 12.0, "portrait", None, False, words=W, brand="os")
ok(cc.get("allowlist_check", {}).get("version") == craft.ALLOWLIST_REQ_V1
   and cc["allowlist_check"]["hits"],
   "topic 47 render without allowlist stamps allowlist_check hits")
ok(cc.get("card_timing", {}).get("version") == craft.CARD_TIMING_V1,
   "card_timing marker is stored when words exist")
ok(cc["card_text"].get("os") is True, "card_text marker carries os=True")


print()
print(f"ALL {_checks} CHECKS PASSED")
