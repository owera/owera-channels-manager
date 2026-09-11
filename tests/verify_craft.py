"""Craft gates: spoken-title publish lock, Decolar claim alignment, CTA ban.

    PYTHONPATH=. .venv/bin/python tests/verify_craft.py
"""
import json
import sys
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, publish_loop, thumbnail, youtube
from app.services.engines import storyboard, theme

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
print("spoken title regex")

ok(craft.spoken_title_ok("Copilot billed the cancelled run · Copilot Credits 14"),
   "EN Copilot Credits nn matches")
ok(craft.spoken_title_ok("A API chegou como produto · IA 3"),
   "PT IA nn matches")
ok(craft.spoken_title_ok("Ollama na RTX · Local 7"), "Local nn matches")
ok(craft.spoken_title_ok("Memory died between chats · Agent memory 2"),
   "Agent memory nn matches")
ok(craft.spoken_title_ok("Crew blew the budget · CrewAI 9"), "CrewAI nn matches")
ok(craft.spoken_title_ok("Claude ate the context · Claude Code 11"),
   "Claude Code nn matches")
ok(craft.spoken_title_ok("x · copilot credits 1"),
   "series match is case-insensitive")
ok(not craft.spoken_title_ok("Copilot billed the cancelled run"),
   "missing · series nn is rejected")
ok(not craft.spoken_title_ok("Hook · Other Series 1"),
   "unknown series label is rejected")
ok(not craft.spoken_title_ok("Hook · Copilot Credits"),
   "missing integer nn is rejected")
ok(not craft.spoken_title_ok(""), "empty title is rejected")
ok(not craft.spoken_title_ok(None), "None title is rejected")

ok(craft.title_gate_reason("nope", "short") == craft.TITLE_GATE_REASON,
   "shorts without the pattern get the parked-via-reject reason")
ok(craft.title_gate_reason("nope", "long") is None,
   "longs are exempt from the series suffix")
ok(craft.title_gate_reason("x · IA 1", "short") is None,
   "matching short is allowed")
ok("pré-pattern" in craft.TITLE_GATE_REASON and "reject" in craft.TITLE_GATE_REASON,
   "error text tells ops to park via reject, not mass-retitle")


# ---------------------------------------------------------------------------
print("Decolar: first spoken sentence + claim alignment")

ok(craft.first_spoken_sentence("Your RAG reads junk. Then we fix it.")
   == "Your RAG reads junk.",
   "first sentence is split on period")
ok(craft.spoken_hook_source("Your RAG reads junk · Copilot Credits 1",
                            "Ignored other script.")
   == "Your RAG reads junk",
   "title before · is the spoken hook source")
ok(craft.compress_claim("Your RAG reads junk because it embeds garbage tokens", 8)
   == "Your RAG reads junk because it embeds garbage",
   "compress_claim keeps the first 8 words, original casing (not Title Case)")
ok(craft.compress_claim("Your RAG reads junk.", 8) == "Your RAG reads junk",
   "compress_claim strips the trailing sentence period (on-screen claim, not a slogan)")
ok(craft.claim_aligned("Your RAG reads junk", "Your RAG reads junk because X"),
   "compression of the same claim aligns")
ok(craft.claim_aligned("Reranking in 5 lines", "Reranking in 5 lines · Copilot Credits 1"),
   "title echo aligns")
ok(not craft.claim_aligned("The Cache Is Lying", "Reranking in 5 lines"),
   "curiosity-gap slogan does NOT align (the old thumbnail brief)")


# ---------------------------------------------------------------------------
print("banned CTA scan/strip")

ok(craft.contains_banned("Follow for more RAG fixes tomorrow"),
   "follow for more is banned")
ok(craft.contains_banned("Siga-amanhã o servidor MCP"),
   "Siga-amanhã is banned")
ok(craft.contains_banned("Siga → Amanhã: overlap"),
   "Siga → is banned")
ok(craft.contains_banned("Join the waitlist for Owera Cloud"),
   "waitlist + Cloud is banned")
ok(craft.contains_banned("Follow us on Instagram and LinkedIn"),
   "Instagram/LinkedIn is banned")
ok(craft.contains_banned("SMY drop tomorrow"), "SMY is banned")
ok(not craft.contains_banned("Retrieve wide, rerank hard, generate thin."),
   "a builder punch is clean")
ok("Rerank hard" in craft.strip_banned(
    "Follow for more. Rerank hard. Siga amanhã."),
   "strip_banned drops CTA sentences and keeps the lesson")


# ---------------------------------------------------------------------------
print("brand_of")

ok(craft.brand_of("ch1") == "os", "ch1 → os (Owera B&W)")
ok(craft.brand_of("owera-software", "Owera Software") == "os", "owera slug → os")
ok(craft.brand_of("ch2") == "rr", "ch2 → rr")
ok(craft.brand_of("rodrigo-recio") == "rr", "recio slug → rr")
ok(craft.brand_of("other") is None, "unknown slug stays unbranded (legacy palette)")
ok(craft.brand_of(None, None, 1) == "os", "channel_id=1 → os")
ok(craft.brand_of(None, None, 2) == "rr", "channel_id=2 → rr")
ok(craft.brand_of("mystery", None, 1) == "os", "id=1 fills in when slug has no token")
ok(craft.brand_of("rodrigo-recio", None, 1) == "rr", "recio slug wins over id=1")


# ---------------------------------------------------------------------------
print("theme brand palettes kill shared neon")

os_th = theme.resolve(1, "hello", brand="os")
ok(os_th["bg_base"] == "#000000" and os_th["fg"] == "#ffffff",
   "OS canvas is black/white")
ok(os_th["accent"].lower() in {p[0] for p in theme._OS_PALETTE},
   "OS accent stays in the B&W family")
ok("#5b8cff" not in os_th["accent"], "OS does not use the neon blue")
ok(os_th["logo"] == theme.OS_LOGO_FILE, "OS carries the O crop filename")
rr_th = theme.resolve(1, "hello", brand="rr")
ok(rr_th["bg_base"] == "#000000", "RR canvas is black, not #0b0b16 neon")
ok(rr_th["accent"] == "#c41e5a", "RR accent is burgundy #C41E5A")
ok(rr_th["logo"] == "", "RR carries no Owera logo")
ok(rr_th["accent"] != os_th["accent"], "OS and RR are distinct systems")
ok(rr_th["glow"] != os_th["glow"], "OS cold glow ≠ RR burgundy glow")
legacy = theme.resolve(1, "hello")
ok(legacy["accent"] == "#00c9a7" and legacy["fg_dim"] == "#c9d2ff",
   "unbranded resolve keeps the legacy palette (unit tests / leftover templates)")


# ---------------------------------------------------------------------------
print("mute-scroll stills: OS vs RR HTML diverge in glow + logo")

_HOOK = [{"type": "hook", "cue": "", "text": "Cache billed the cancelled run",
          "emoji": "", "start": 0.0, "dur": 3.0}]
os_html = storyboard.build_index_html(
    _HOOK, theme.resolve(1, "hello", brand="os"), "portrait", 1080, 1920, 3.0)
rr_html = storyboard.build_index_html(
    _HOOK, theme.resolve(1, "hello", brand="rr"), "portrait", 1080, 1920, 3.0)
ok('data-brand="os"' in os_html, "OS composition is tagged data-brand=os")
ok('data-brand="rr"' in rr_html, "RR composition is tagged data-brand=rr")
ok('id="brand-mark"' in os_html and theme.OS_LOGO_FILE in os_html,
   "OS frame0 carries the O crop")
ok("height:64px" in os_html and "opacity:.9" in os_html,
   "OS mark is ~64px / 90% opacity on 1080×1920")
ok("left:48px" in os_html and "bottom:48px" in os_html,
   "OS mark sits ≥48px from edges")
ok('id="brand-mark"' not in rr_html, "RR frame0 has no brand-mark node")
for tok in theme.RR_FORBIDDEN_MARKS:
    ok(tok not in rr_html.lower(), f"RR HTML has no {tok!r}")
os_l, rr_l = os_html.lower(), rr_html.lower()
for bad in theme.OS_FORBIDDEN_HEX:
    ok(bad not in os_l, f"OS HTML has no forbidden {bad}")
ok("#1a1a1a" in os_l, "OS still has the cold glow")
ok("#4a1528" in rr_l and "#2a0a14" in rr_l, "RR still has the burgundy upper-corner glow")
ok("#c41e5a" in rr_l, "RR still uses #C41E5A as object stroke")
ok("linear-gradient(90deg" not in os_l and "linear-gradient(90deg" not in rr_l,
   "no rainbow neon-bar top gradient on branded stills")
ok(os_html != rr_html, "OS and RR storyboard HTML are not identical")

os_thumb = thumbnail._thumbnail_html("Cache billed the cancelled run", brand="os")
rr_thumb = thumbnail._thumbnail_html("Cache billed the cancelled run", brand="rr")
ok(theme.OS_LOGO_FILE in os_thumb and 'id="brand-mark"' in os_thumb,
   "OS thumb carries the O crop")
ok('id="brand-mark"' not in rr_thumb, "RR thumb has no brand-mark")
ok('id="accent"' not in os_thumb and 'id="accent"' not in rr_thumb,
   "branded thumbs drop the neon-bar identity")
ok("#4a1528" in rr_thumb.lower() and "#c41e5a" in rr_thumb.lower(),
   "RR thumb glow + stroke are burgundy")
ok("#1a1a1a" in os_thumb.lower(), "OS thumb cold glow is present")
for tok in theme.RR_FORBIDDEN_MARKS:
    ok(tok not in rr_thumb.lower(), f"RR thumb has no {tok!r}")
ok(os_thumb != rr_thumb, "OS and RR thumbs are not identical")


# ---------------------------------------------------------------------------
print("storyboard lock: frame0 = first spoken sentence, no Follow force")

WORDS = [{"text": w, "start": i * 0.5, "dur": 0.4}
         for i, w in enumerate("Your RAG reads junk then we fix the embed".split())]
script = "Your RAG reads junk. Then we fix the embed path."


def _board():
    import json
    return json.dumps({"beats": [
        {"type": "hook", "cue": "Your RAG", "text": "Curiosity gap slogan", "emoji": "💸"},
        {"type": "stat", "cue": "reads junk", "value": "42", "unit": "ms", "label": "per call"},
        {"type": "list", "cue": "then we", "items": ["one", "two"]},
        {"type": "cta", "cue": "fix the", "text": "Follow", "sub": "Follow for more"},
    ]})


html = storyboard.compose(
    subject="Your RAG reads junk · Copilot Credits 1", script=script,
    words=WORDS, duration=12.0, resolution="portrait", width=1080, height=1920,
    topic_id=1, content_format="short", language="English",
    llm=lambda *a, **k: _board(),
)
ok(html is not None, "compose returns HTML")
hook_html = html.split('class="beat hook"', 1)[1].split('class="beat ', 1)[0]
ok(all(f'<span class="word">{w}</span>' in hook_html
       for w in "Your RAG reads junk".split()),
   "frame0 is the first spoken sentence (Decolar)")
ok("Curiosity" not in hook_html and "slogan" not in hook_html,
   "curiosity-gap hook text is overwritten, not shown")
ok("Follow" not in html and "Siga" not in html,
   "compose does NOT force Follow/Siga (banned)")
ok("💸" not in html.split("beat hook")[1].split("beat ")[0] if "beat hook" in html else True,
   "hook emoji is stripped (no second punch)")


# ---------------------------------------------------------------------------
print("thumbnail hook: aligned compression, not a gap")

from unittest.mock import patch


def _gap_llm(prompt, system=None, max_tokens=None):
    return "The Cache Is Lying"


with patch.object(thumbnail, "_llm", side_effect=_gap_llm):
    out = thumbnail._hook_text("ignored", "Reranking in 5 lines · Copilot Credits 1")
ok(out == "Reranking in 5 lines",
   "curiosity-gap LLM output is rejected; fallback is the spoken title (not Title Case)")

sys_src = thumbnail._hook_text.__doc__ + ""
import inspect
src = inspect.getsource(thumbnail._hook_text)
ok("DECOLAR LOCK" in src and "Repeating the title is REQUIRED" in src,
   "thumbnail system prompt requires repeating the title")
ok("curiosity GAP" not in src and "Do NOT reuse the title" not in src,
   "old curiosity-gap / do-not-reuse-title brief is gone")
ok("Compress THIS claim" in src,
   "user prompt asks to compress THIS claim, not open a gap")


# ---------------------------------------------------------------------------
print("publish_loop refuses a pré-pattern title (no upload)")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
s = Session(engine)
ch = Channel(slug="ch1", name="Owera Software", oauth_status=OAuthStatus.CONNECTED)
s.add(ch); s.commit(); s.refresh(ch)
t = Topic(channel_id=ch.id, name="Credits", theme_prompt="x", content_format="short")
s.add(t); s.commit(); s.refresh(t)
v = Video(channel_id=ch.id, topic_id=t.id, subject="billed",
          status=VideoStatus.APPROVED, video_path="/tmp/x.mp4",
          title="Copilot billed the cancelled run")  # no · series nn
s.add(v); s.commit(); s.refresh(v)

_uploads = []
_orig_get, _orig_up = youtube.get_service, youtube.upload_video
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: (_uploads.append("up") or "vid")
publish_loop._publish_one(s, ch, v)
ok(_uploads == [], "bad title never opens upload_video")
ok(v.status == VideoStatus.REVIEW, "blocked publish returns the row to review")
ok(v.error and "spoken series pattern" in v.error,
   "blocked publish records the gate reason")
ok(v.yt_video_id is None, "no YouTube id on a blocked publish")

v2 = Video(channel_id=ch.id, topic_id=t.id, subject="ok",
           status=VideoStatus.APPROVED, video_path="/tmp/x.mp4",
           title="Copilot billed the cancelled run · Copilot Credits 14")
s.add(v2); s.commit(); s.refresh(v2)
# Don't actually run the rest of publish (playlist/thumbnail) — just the gate.
blocked = craft.title_gate_reason(v2.title, "short")
ok(blocked is None, "matching title is not blocked")
youtube.get_service, youtube.upload_video = _orig_get, _orig_up


# ---------------------------------------------------------------------------
print("Video Maker craft gate A/B/C (Shorts)")


def _pass_beats(*_a, **_k):
    return [
        {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Your RAG reads junk",
         "object": "receipt", "cue": "Your RAG"},
        {"type": "code", "start": 2.0, "dur": 2.4, "lines": ["rerank(q)"],
         "cue": "rerank the pile"},
        {"type": "stat", "start": 4.5, "dur": 2.4, "value": "40", "unit": "%",
         "cue": "forty percent"},
        {"type": "cta", "start": 7.0, "dur": 3.5, "text": "Rerank first",
         "cue": "rerank first"},
    ]


g = craft.video_maker_gate(_pass_beats())
ok(g["result"] == "PASS" and g["checks"] == {"A": "PASS", "B": "PASS", "C": "PASS"},
   "golden short: object hook + code in 0–3s, mid ≤3s, cta ≤4s, 0 statements")
ok(g["reasons"] == [], "PASS carries no fail reasons")
ok(craft.video_maker_gate_reason({"beats": _pass_beats()}, "short") is None,
   "video_maker_gate_reason is None on PASS")
ok(craft.review_gate_reason("x · IA 1", "short", {"beats": _pass_beats()}) is None,
   "review_gate_reason allows a patterned title + PASS board")

# A — FAIL typography-only (emoji is not an object)
typo = [
    {"type": "hook", "start": 0.0, "dur": 3.0, "text": "Your RAG reads junk",
     "emoji": "💸", "object": "🔥", "cue": "Your RAG"},
    {"type": "statement", "start": 3.0, "dur": 2.5, "text": "Then we fix it",
     "cue": "Then we"},
    {"type": "stat", "start": 5.5, "dur": 2.5, "value": "40", "cue": "forty"},
    {"type": "cta", "start": 8.0, "dur": 3.5, "text": "Fix the embed", "cue": "fix"},
]
ga = craft.video_maker_gate(typo)
ok(ga["checks"]["A"] == "FAIL" and ga["result"] == "FAIL",
   "A FAIL: hook+statement until t=3, emoji object stripped")
ok("[A]" in ga["reasons"][0] and "emoji" in ga["reasons"][0].lower(),
   "A fail reason names the 0–3s object rule and that emoji does not count")

# A — PASS via rich beat in the window, no hook.object
a_rich = [
    {"type": "hook", "start": 0.0, "dur": 1.5, "text": "Your RAG reads junk", "cue": "Your"},
    {"type": "command", "start": 1.5, "dur": 2.5, "command": "curl /bill", "cue": "curl"},
    {"type": "cta", "start": 4.0, "dur": 3.0, "text": "Read the bill", "cue": "read"},
]
ok(craft.video_maker_gate(a_rich)["checks"]["A"] == "PASS",
   "A PASS: command beat starts at t=1.5 (in the 0–3s window)")

# A — PASS via hook.object even when the first rich beat is after t=3
a_obj = [
    {"type": "hook", "start": 0.0, "dur": 3.2, "text": "Your RAG reads junk",
     "object": "terminal", "cue": "Your"},
    {"type": "code", "start": 3.2, "dur": 2.5, "lines": ["x=1"], "cue": "code"},
    {"type": "cta", "start": 5.8, "dur": 3.0, "text": "Show the term", "cue": "show"},
]
ok(craft.video_maker_gate(a_obj)["checks"]["A"] == "PASS",
   "A PASS: hook.object=terminal counts even if the rich beat starts after t=3")
ok(craft.video_maker_gate(a_obj)["checks"]["B"] == "FAIL",
   "B still FAILs that 3.2s hook (next_cue − cue > 3.0) — letters are independent")

# B — mid >3s
b_mid = _pass_beats()
b_mid[1] = {"type": "code", "start": 2.0, "dur": 4.5, "lines": ["x"], "cue": "slow"}
b_mid[2] = {"type": "stat", "start": 6.6, "dur": 2.0, "value": "1", "cue": "one"}
b_mid[3] = {"type": "cta", "start": 8.6, "dur": 3.0, "text": "Go", "cue": "go"}
gb = craft.video_maker_gate(b_mid)
ok(gb["checks"]["B"] == "FAIL" and "beat[1]" in gb["reasons"][0],
   "B FAIL: mid code held 4.60s (6.6 − 2.0)")
ok("3.0" in gb["reasons"][0], "B fail reason cites the 3.0s mid cap")

# B — CTA >4s
b_cta = _pass_beats()
b_cta[-1] = {"type": "cta", "start": 7.0, "dur": 5.5, "text": "Go", "cue": "go"}
# last beat span falls back to dur
gbc = craft.video_maker_gate(b_cta)
ok(gbc["checks"]["B"] == "FAIL" and "cta" in gbc["reasons"][0],
   "B FAIL: cta held 5.50s (limit 4.0s)")
ok("4.0" in gbc["reasons"][0] and "Follow" in gbc["reasons"][0],
   "B fail reason cites the 4.0s endcard cap (not Follow-tomorrow)")

b_cta_ok = _pass_beats()
b_cta_ok[-1] = {"type": "cta", "start": 7.0, "dur": 4.0, "text": "Go", "cue": "go"}
ok(craft.video_maker_gate(b_cta_ok)["checks"]["B"] == "PASS",
   "B PASS: cta exactly 4.0s is allowed")

# C — ≥2 statements
c_stmt = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Hook", "object": "bill", "cue": "h"},
    {"type": "statement", "start": 2.0, "dur": 2.0, "text": "One", "cue": "one more"},
    {"type": "statement", "start": 4.0, "dur": 2.0, "text": "Two", "cue": "two more"},
    {"type": "stat", "start": 6.0, "dur": 2.0, "value": "1", "cue": "stat"},
    {"type": "cta", "start": 8.0, "dur": 3.0, "text": "Go", "cue": "go"},
]
gc = craft.video_maker_gate(c_stmt)
ok(gc["checks"]["C"] == "FAIL" and "2 statement" in gc["reasons"][0],
   "C FAIL: 2 statements (tightened from the old tolerance of 2)")

# C — list >3 items
c_list = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Hook", "object": "bill", "cue": "h"},
    {"type": "list", "start": 2.0, "dur": 2.5,
     "items": [{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}],
     "cue": "steps"},
    {"type": "stat", "start": 4.5, "dur": 2.0, "value": "1", "cue": "stat"},
    {"type": "cta", "start": 6.5, "dur": 3.0, "text": "Go", "cue": "go"},
]
gl = craft.video_maker_gate(c_list)
ok(gl["checks"]["C"] == "FAIL" and "4 items" in gl["reasons"][0],
   "C FAIL: list with 4 items (max 3)")

# C — two lists
c_two = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Hook", "object": "bill", "cue": "h"},
    {"type": "list", "start": 2.0, "dur": 2.0, "items": [{"text": "a"}], "cue": "a"},
    {"type": "list", "start": 4.0, "dur": 2.0, "items": [{"text": "b"}], "cue": "b"},
    {"type": "stat", "start": 6.0, "dur": 2.0, "value": "1", "cue": "stat"},
    {"type": "cta", "start": 8.0, "dur": 3.0, "text": "Go", "cue": "go"},
]
ok("2 list" in craft.video_maker_gate(c_two)["reasons"][0],
   "C FAIL: two list beats (max 1)")

# C — kept list within constraints + 1 statement + rich type
c_ok = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Hook", "object": "bill", "cue": "h"},
    {"type": "statement", "start": 2.0, "dur": 2.0, "text": "One idea", "cue": "idea"},
    {"type": "list", "start": 4.0, "dur": 2.4,
     "items": [{"text": "a"}, {"text": "b"}, {"text": "c"}], "cue": "three"},
    {"type": "code", "start": 6.5, "dur": 2.4, "lines": ["x=1"], "cue": "code"},
    {"type": "cta", "start": 9.0, "dur": 3.0, "text": "Go", "cue": "go"},
]
ok(craft.video_maker_gate(c_ok)["result"] == "PASS",
   "C PASS: 1 statement + 1 list (3 items, ≤3s) + a code beat")

# C — list/statement echo with no rich type
c_echo = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Your RAG reads junk",
     "object": "receipt", "cue": "Your RAG"},
    {"type": "statement", "start": 2.0, "dur": 2.5, "text": "reads junk",
     "cue": "reads junk then"},
    {"type": "cta", "start": 4.5, "dur": 3.0, "text": "Go", "cue": "go"},
]
ge = craft.video_maker_gate(c_echo)
ok(ge["checks"]["C"] == "FAIL" and "re-displays narration" in ge["reasons"][0],
   "C FAIL: statement only re-displays narration without a rich type")

# Longs exempt; fallback FAIL; legacy fail-open
ok(craft.video_maker_gate(typo, content_format="long")["result"] == "PASS",
   "longs are exempt from A+B+C")
ok(craft.video_maker_gate_reason({"beats": typo}, "long") is None,
   "video_maker_gate_reason is None for longs")
fb = craft.video_maker_gate([], used_fallback=True)
ok(fb["result"] == "FAIL" and fb["checks"]["A"] == "FAIL" and fb["checks"]["C"] == "FAIL",
   "kinetic-text fallback with no beats FAILs A and C")
ok(craft.video_maker_gate_reason({}, "short") is None,
   "missing beat snapshot fail-opens (pré-gate inventory)")
ok(craft.video_maker_gate_reason({"craft_gate": {"result": "FAIL",
                                                "reasons": ["[A] no object"]}},
                                "short") == "Video Maker craft gate FAIL: [A] no object",
   "stored FAIL verdict is honored when beats were not snapshotted")

reason = craft.video_maker_gate_reason({"beats": typo}, "short")
ok(reason.startswith("Video Maker craft gate FAIL:") and "[A]" in reason,
   "gate reason is operator-readable and letter-tagged")
ok(craft.review_gate_reason("nope", "short", {"beats": _pass_beats()})
   == craft.TITLE_GATE_REASON,
   "review_gate_reason: title pattern wins over a PASS board")
ok("craft gate FAIL" in (craft.review_gate_reason("x · IA 1", "short", {"beats": typo}) or ""),
   "review_gate_reason: patterned title still blocked by A+B+C")

# hook_object / snapshot / html parse
ok(craft.hook_object({"object": "  receipt "}) == "receipt", "hook_object trims")
ok(craft.hook_object({"object": "💸"}) == "", "hook_object rejects emoji")
ok(craft.hook_object({"prop": "terminal"}) == "terminal", "hook_object reads prop alias")
html = (
    '<div class="beat hook" data-start="0" data-duration="2.0"></div>'
    '<script type="application/json" id="storyboard-beats">'
    '[{"type":"stat","start":1.0,"dur":2.0}]</script>'
)
ok(craft.beats_from_html(html) == [{"type": "stat", "start": 1.0, "dur": 2.0}],
   "beats_from_html prefers the embedded JSON snapshot")
ok(craft.beats_from_html('<div class="beat hook" id="b0" data-start="0" '
                         'data-duration="2.1" data-track-index="0"></div>')
   == [{"type": "hook", "start": 0.0, "dur": 2.1}],
   "beats_from_html scrapes data-start/duration when the snapshot is missing")


# ---------------------------------------------------------------------------
print("publish_loop refuses a craft-gate FAIL (no upload)")

v3 = Video(channel_id=ch.id, topic_id=t.id, subject="craft",
           status=VideoStatus.APPROVED, video_path="/tmp/x.mp4",
           title="Copilot billed the cancelled run · Copilot Credits 14",
           creation_config=json.dumps({"beats": typo, "used_fallback": False}))
s.add(v3); s.commit(); s.refresh(v3)
_uploads = []
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: (_uploads.append("up") or "vid")
publish_loop._publish_one(s, ch, v3)
ok(_uploads == [], "craft-gate FAIL never opens upload_video")
ok(v3.status == VideoStatus.REVIEW, "blocked craft publish returns the row to review")
ok(v3.error and "craft gate FAIL" in v3.error and "[A]" in v3.error,
   "blocked craft publish records the letter-tagged reason")
youtube.get_service, youtube.upload_video = _orig_get, _orig_up


print(f"\nALL {_checks} CHECKS PASSED")
