"""Craft gates: spoken-title publish lock, Decolar claim alignment, CTA ban.

    PYTHONPATH=. .venv/bin/python tests/verify_craft.py
"""
import json
import re
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
ok(craft.spoken_title_ok("Gate failed mid at 5.8s. Craft parked it. · Shipping 1"),
   "Shipping nn matches")
ok(craft.spoken_title_ok("Chat said done. Prod was still running. · Agent traps 1"),
   "Agent traps nn matches")
ok(craft.spoken_title_ok("x · shipping 2"), "Shipping match is case-insensitive")
ok(craft.spoken_title_ok("x · agent traps 3"), "Agent traps match is case-insensitive")
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
ok(craft.title_gate_reason(
        "Copilot billed $79 when the model timed out · Copilot Credits 80",
        "short") is None,
   "Credits title with spoken phrase + $N + ·nn is allowed")
ok(craft.title_gate_reason("x · IA 1", "short") == craft.TITLE_CLAIM_REASON,
   "Credits/IA ·nn alone without useful phrase/$N/noun is blocked")
ok(craft.title_gate_reason(
        "Follow tomorrow the chunking fix · Copilot Credits 1", "short")
   == craft.TITLE_BANNED_REASON,
   "Follow-tomorrow Credits title fails pre-approve ban")
ok(craft.title_gate_reason(
        "Memory died between chats · Agent memory 2", "short") is None,
   "non-Credits/IA patterned series still only needs ·nn")
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
print("Decolar: opening object echoes the spoken first phrase")

ok(craft.opening_object("Copilot billed the cancelled run")["label"] == "BILL",
   "billed title → BILL (object of the angle, not a generic COPILOT badge)")
ok(craft.opening_object("Copilot billed the cancelled run")["kind"] == "bill",
   "billed title is a bill UI (invoice / credit counter)")
ok(craft.opening_object("Copilot billed $58 when the model timed out")["amount"] == "$58",
   "spoken $58 lands on the credit counter")
ok(craft.opening_object("Copilot billed $79 before writing a single line")["amount"] == "$79",
   "spoken $79 stays a numeral on the bill widget")
ok(craft.dollar_numerals("Copilot billed $79 · Copilot Credits 80") == ["$79"],
   "dollar_numerals extracts $79")
ok(craft.preserves_dollar_numerals(
        "Copilot billed $79 before writing a single line",
        "Copilot billed $79"),
   "frame0/thumb compression may shorten but must keep $79")
ok(not craft.preserves_dollar_numerals(
        "Copilot billed $79 before writing a single line",
        "Copilot billed seventy-nine dollars"),
   "spelled-out seventy-nine dollars is rejected on frame0/thumb")
ok(craft.spelled_dollar_amount("seventy-nine dollars"),
   "spelled_dollar_amount catches word forms")
ok(craft.compress_claim("Copilot billed $79 before writing a single line today", 8)
   == "Copilot billed $79 before writing a single line",
   "compress_claim keeps $79 as a numeral token")

ok(craft.opening_object("Your RAG is slow and still wrong")["label"] == "RAG",
   "RAG title → RAG chrome")
ok(craft.opening_object("Sua RAG busca lixo e você culpa o modelo")["label"] == "RAG",
   "PT RAG opener → RAG (fold + lexicon)")
ok(craft.opening_object("Memory died between chats")["label"] == "MEMORY",
   "memory title → MEMORY")
ok(craft.opening_object("Toda ferramenta nova vira mais uma integração")["label"] == "MCP",
   "PT integração → MCP")
ok(craft.opening_object("mcp tools list in one server")["label"] == "MCP",
   "mcp command title → MCP")
ok(craft.opening_object("Paste this into the terminal")["kind"] == "terminal",
   "terminal title → terminal widget")
ok(craft.opening_object("VRAM 24GB batch died")["kind"] == "gpu",
   "GPU/VRAM/batch → gpu meter (not 💸)")
ok(craft.opening_object("Chrome ate the tab")["kind"] == "app"
   and craft.opening_object("Chrome ate the tab")["label"] == "CHROME",
   "Chrome → named app icon")
ok(craft.opening_object("Ollama na RTX ainda cabe")["kind"] == "terminal"
   and craft.opening_object("Ollama na RTX ainda cabe")["prompt"] == "$ ollama run",
   "Ollama → terminal / Ollama prompt (noun of the phrase)")
ok(craft.opening_object("The API is now a paid product")["kind"] == "receipt",
   "API/paid/product → receipt / API stub")
ok(craft.opening_object("Hello Hook")["label"] == "HELLO",
   "unkeyed copy mines the first distinctive noun (not OBJECT/RECEIPT)")
ok(craft.opening_object("")["label"] == "OBJECT",
   "empty copy falls back to OBJECT (never an emoji)")
ok("💸" not in craft.object_markup(craft.opening_object("VRAM 24GB"))
   and "🔥" not in craft.object_markup(craft.opening_object("Copilot billed $58")),
   "object markup never uses 💸/🔥")
ok('data-kind="bill"' in craft.object_markup(craft.opening_object("Copilot billed $58")),
   "bill markup is a widget, not typography-only")
ok(craft.object_echoes("BILL", "Copilot billed $27 when the model timed out"),
   "BILL echoes a billed spoken phrase")
ok(craft.object_echoes("RAG", "Sua RAG busca lixo e você culpa o modelo"),
   "RAG echoes the PT spoken opener")
ok(not craft.object_echoes("OARS", "Your RAG is slow and still wrong"),
   "generic oars do NOT echo a RAG spoken phrase")
ok(not craft.object_echoes("💸", "Copilot billed the cancelled run"),
   "emoji label does not echo a billed claim")
ok(craft.emoji_first("💸 Copilot billed"),
   "emoji-first hook is flagged")
ok(not craft.emoji_first("Copilot billed 💸 later"),
   "trailing emoji alone is not emoji-first")
ok(craft.emoji_soup("💸🔥"),
   "emoji soup (two+ emoji, no words) is flagged")
ok(not craft.emoji_soup("Copilot billed the cancelled run"),
   "plain spoken claim is not emoji soup")
ok("concrete object" in craft.CRAFT_RULES_SHORT.lower()
   or "receipt" in craft.CRAFT_RULES_SHORT.lower(),
   "idea/script craft addendum names the object-on-frame0 rule")


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
ok(not craft.contains_banned("Subscribe — next Copilot Credits trap."),
   "series endcard VO is not a banned Follow/waitlist/Cloud CTA")


# ---------------------------------------------------------------------------
print("series endcard template")

os_card = craft.series_endcard("Copilot billed $27. · Copilot Credits 75", brand="os")
ok(os_card["series"] == "Copilot Credits" and os_card["noun"] == "trap",
   "OS default series/noun = Copilot Credits / trap")
ok(os_card["vo"] == "Subscribe — next Copilot Credits trap.",
   "OS VO is the exact CMO line")
ok(os_card["chip"] == "Subscribe · Copilot Credits",
   "OS chip is Subscribe · Copilot Credits")
ok(os_card["micro"] == "same series",
   "short chip gets the same-series micro")
ok(len(os_card["vo"].rstrip(".").split()) <= 8,
   "OS VO is ≤8 words")
ok(craft.endcard_clean(os_card), "OS endcard passes the hard-ban gate")

rr_card = craft.series_endcard("Você lotou a VRAM. · IA 175", brand="rr")
ok(rr_card["vo"] == "Subscribe — next IA trap."
   and rr_card["chip"] == "Subscribe · IA",
   "RR default VO/chip = next IA trap / Subscribe · IA")
ok(craft.endcard_clean(rr_card), "RR endcard passes the hard-ban gate")

am = craft.series_endcard("Memory died between chats · Agent memory 2")
ok(am["vo"] == "Subscribe — next Agent memory trap."
   and am["chip"] == "Subscribe · Agent memory",
   "non-Credits/IA series only swaps {series}/{noun}")
ok(craft.endcard_clean(am), "Agent memory endcard invents no extra CTA")

bill = craft.series_endcard(
    "Copilot billed the cancelled run · Copilot Credits 14",
    "The receipt on the bill is the drop.",
)
ok(bill["noun"] == "receipt" and bill["vo"].endswith("receipt."),
   "noun is picked from title/script (receipt before bill/drop/trap)")

ok(craft.series_of(None, "os") == "Agent memory",
   "missing title + OS brand → Agent memory (Credits wedge killed)")
ok(craft.series_of(None, "rr") == "IA",
   "missing title + RR brand → IA")
ok(craft.series_of(None, None) == "Copilot Credits",
   "English public fallback is Copilot Credits")

pinned = craft.ensure_series_endcard_vo(
    "Your RAG reads junk. Then we fix the embed path.",
    "Your RAG reads junk · Copilot Credits 1",
    brand="os",
)
ok(pinned.endswith("Subscribe — next Copilot Credits trap."),
   "ensure appends the VO after the claim")
ok(pinned.startswith("Your RAG reads junk."),
   "ensure does not rewrite the Decolar opener")
already = craft.ensure_series_endcard_vo(
    "Lesson punch. Subscribe — next Local drop.",
    "Ollama on the 3060 · Local 7",
)
ok(already.endswith("Subscribe — next Local drop."),
   "already-shaped last sentence is canonicalized, not doubled")
ok(already.count("Subscribe —") == 1, "VO is pinned once")

ok(craft.ENDCARD_MAX_S == 3.78 and craft.ENDCARD_CARD_MAX_S == 3.9,
   "endcard ≤3.9s incl. fade → hold ceiling 3.78s (VM 2026-09-29; was 4.0s hold)")
for bad in ("Follow tomorrow", "amanhã", "waitlist", "owera.com",
            "Owera Cloud", "part 2 coming", "SMY", "💸", "neon"):
    ok(craft.endcard_scan_banned(bad), f"endcard bans {bad!r}")
ok(not craft.endcard_scan_banned(os_card["vo"]),
   "canonical VO is clean of endcard bans")
ok(not craft.endcard_clean({"vo": os_card["vo"], "chip": "Subscribe now",
                            "micro": ""}),
   "non-template Subscribe on the chip fails endcard_clean")
ok(not craft.endcard_clean({"vo": os_card["vo"], "chip": "· Copilot Credits",
                            "micro": ""}),
   "legacy · {series} chip fails — CMO lock requires Subscribe · {series}")
ok(not craft.endcard_clean({"vo": os_card["vo"], "chip": os_card["chip"],
                            "micro": "Follow tomorrow"}),
   "invented extra CTA on the micro fails")
ok("Subscribe" in os_card["vo"] and "Subscribe" in os_card["chip"]
   and craft.endcard_clean(os_card),
   "endcard VO + chip may contain Subscribe (YPP#5 exception — not Follow-tomorrow)")

miolo = (
    "Subscribe for more RAG fixes. Your RAG reads junk. "
    "Se inscreve amanhã. Then we fix the embed. "
    "Subscribe — next Copilot Credits trap."
)
ok(craft.contains_subscribe_cta("Subscribe for more RAG fixes."),
   "mid-body Subscribe CTA is detected")
ok(craft.mid_body_has_subscribe(miolo),
   "Subscribe before the endcard VO is a mid-body leak")
ok(not craft.mid_body_has_subscribe(
    "Your RAG reads junk. Subscribe — next Copilot Credits trap."),
   "Subscribe only as the trailing endcard VO is not a leak")
stripped_miolo = craft.strip_mid_subscribe(miolo)
ok("Subscribe for more" not in stripped_miolo
   and "Se inscreve" not in stripped_miolo,
   "strip_mid_subscribe drops mid-short Subscribe / Inscreva")
ok(stripped_miolo.endswith("Subscribe — next Copilot Credits trap."),
   "strip_mid_subscribe keeps the trailing endcard VO")
ok("Your RAG reads junk" in stripped_miolo and "Then we fix the embed" in stripped_miolo,
   "miolo lesson sentences survive the Subscribe strip")
pinned_leak = craft.ensure_series_endcard_vo(
    "Subscribe now. Your RAG reads junk. Inscreva-se já.",
    "Your RAG reads junk · Copilot Credits 1",
    brand="os",
)
ok(not craft.mid_body_has_subscribe(pinned_leak),
   "ensure strips mid Subscribe then pins the endcard VO")
ok(pinned_leak.startswith("Your RAG reads junk."),
   "Decolar opener survives a Subscribe-first LLM draft")
ok(pinned_leak.count("Subscribe") == 1,
   "exactly one Subscribe remains — the endcard VO")


# ---------------------------------------------------------------------------
print("global sanitize stays: mid-script / title / description")

mid = (
    "Your RAG reads junk. Follow tomorrow for the rest. "
    "Join the waitlist. Owera Cloud is live. See owera.com."
)
cleaned = craft.strip_banned(mid)
ok("Your RAG reads junk" in cleaned,
   "global sanitize keeps the mid-script lesson")
ok(not craft.contains_banned(cleaned),
   "global sanitize is not inverted — mid-script CTAs still match")
ok("Follow" not in cleaned and "tomorrow" not in cleaned.lower(),
   "Follow-tomorrow is stripped from mid-script")
ok("waitlist" not in cleaned.lower(), "waitlist is stripped from mid-script")
ok("Cloud" not in cleaned and "owera.com" not in cleaned,
   "Cloud / owera.com are stripped from mid-script")
ok(not craft.contains_banned("Subscribe — next Copilot Credits trap."),
   "endcard Subscribe VO is still allowed after the mid-script strip")

pinned_mid = craft.ensure_series_endcard_vo(
    cleaned, "Your RAG reads junk · Copilot Credits 1", brand="os",
)
ok(pinned_mid.endswith("Subscribe — next Copilot Credits trap."),
   "Subscribe is pinned only as the endcard VO")
ok(not craft.contains_banned(pinned_mid.rsplit("Subscribe —", 1)[0]),
   "body before the endcard VO stays clean of Follow/waitlist/Cloud")

for title in (
    "Follow tomorrow the chunking fix · Copilot Credits 1",
    "Join the waitlist · IA 1",
    "Owera Cloud is live · Local 1",
    "Read more at owera.com · CrewAI 1",
):
    ok(craft.contains_banned(title), f"title still banned: {title!r}")
ok(not craft.contains_banned("Copilot billed the cancelled run · Copilot Credits 14"),
   "clean patterned title is not banned")

from app.services import metadata
dirty_meta = metadata._sanitize_meta({
    "title": "Follow tomorrow the chunking fix · Copilot Credits 1",
    "description": "Join the waitlist. Owera Cloud is live. See owera.com. Subscribe now.",
    "tags": ["x"],
})
ok("Follow" not in (dirty_meta["title"] or ""),
   "generic title strip still drops Follow-tomorrow")
ok(not craft.contains_banned(dirty_meta["description"]),
   "generic description still cannot carry Follow-tomorrow/waitlist/Cloud")
ok("waitlist" not in (dirty_meta["description"] or "").lower()
   and "Cloud" not in (dirty_meta["description"] or "")
   and "owera.com" not in (dirty_meta["description"] or ""),
   "description body is stripped of waitlist / Cloud / owera.com")
ok("Subscribe" not in (dirty_meta["description"] or "")
   and "Subscribe" not in (dirty_meta["title"] or ""),
   "generic title/description still cannot carry a mid-body Subscribe CTA")


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
ok("linear-gradient(90deg,#" not in os_l and "linear-gradient(90deg,#" not in rr_l,
   "no rainbow neon-bar top gradient on branded stills")
ok("repeating-linear-gradient(90deg" in os_l,
   "receipt/bill tear is a serration, not the neon identity bar")
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
ok("obj-bill" in os_html and "obj-bill" in rr_html,
   "billed spoken noun renders a bill widget on both brands")
ok("--obj-accent:var(--stroke)" in os_html and "--obj-accent:var(--stroke)" in rr_html,
   "frame0 object widgets inherit brand --stroke (OS gray / RR burgundy)")
ok("color:#fff" in craft.OBJECT_CSS,
   "object widget CSS sets color:#fff (white on dark stills, not inherited black)")
ok("obj-bill" in os_thumb and "obj-bill" in rr_thumb,
   "billed thumb uses the bill widget, not a generic RECEIPT slab")
ok("--obj-accent:#c41e5a" in rr_thumb.lower(),
   "RR thumb object is themed burgundy")
ok("#c41e5a" not in os_thumb.lower(),
   "OS thumb object is not painted RR burgundy")


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
ok("cta-chip" in html and "Subscribe · Copilot Credits" in html,
   "compose locks the series chip (not a Follow box)")
ok("same series" in html, "chip micro is same series when it fits")
ok("💸" not in html.split("beat hook")[1].split("beat ")[0] if "beat hook" in html else True,
   "hook emoji is stripped (no second punch)")
ok('class="cta-chip"' in html,
   "shorts endcard paints class=cta-chip")
ok('class="cta-box"' not in html and 'class="cta-arrow"' not in html,
   "shorts endcard does not paint the neon CTA box")
cta_html = html.split('class="beat cta"', 1)[1]
ok("Subscribe · Copilot Credits" in cta_html,
   "endcard chip paints Subscribe · {series}")
m = re.search(r'class="beat cta"[^>]*data-duration="([0-9.]+)"', html)
ok(m and float(m.group(1)) <= craft.ENDCARD_MAX_S + 1e-6,
   "compose endcard hold is ≤3.78s (3.9s incl. fade)")
ok('data-object="RAG"' in hook_html and 'data-kind="object"' in hook_html,
   "frame0 object is the spoken noun (RAG), not a generic RECEIPT/emoji")
ok('class="hobj"' in hook_html and 'class="htext"' in hook_html,
   "frame0 keeps hook type under the object (object does not cover line 1)")
ok(hook_html.find("hobj") < hook_html.find("htext"),
   "object is above the type block")
ok("💸" not in hook_html and "🔥" not in hook_html,
   "frame0 does not use 💸/🔥 as the object")
ok(craft.object_hard_fail(hook_html, "Your RAG reads junk",
                          "Your RAG reads junk. Then we fix the embed path.") is None,
   "composed frame0 passes the Designer hard-FAIL gate")


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
ok(v.status == VideoStatus.REJECTED, "blocked publish auto-rejects (anti-nonsense)")
ok(v.craft_review == "fail", "blocked publish sets craft_review=fail")
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
ok(craft.review_gate_reason(
        "Copilot billed $79 when the model timed out · Copilot Credits 80",
        "short", {"beats": _pass_beats()}) is None,
   "review_gate_reason allows a Credits $N title + PASS board")

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
ok(craft.video_maker_gate(a_obj)["checks"]["B"] == "PASS",
   "B PASS: hook 3.2s is EXEMPT from Gate B (miolo-only; Gate A covers object 0–3s)")

# B — mid over the HARD 3.0s cap (closes ~5–5.8s command auto-approve hole)
b_mid = _pass_beats()
b_mid[1] = {"type": "command", "start": 2.0, "dur": 5.8, "command": "x", "cue": "slow"}
b_mid[2] = {"type": "stat", "start": 7.9, "dur": 2.0, "value": "1", "cue": "one"}
b_mid[3] = {"type": "cta", "start": 10.0, "dur": 3.0, "text": "Go", "cue": "go"}
gb = craft.video_maker_gate(b_mid)
ok(gb["checks"]["B"] == "FAIL" and "beat[1]" in gb["reasons"][0],
   "B FAIL: mid command held 5.80s (over the 3.0s HARD cap)")
ok("3.0" in gb["reasons"][0], "B fail reason cites the 3.0s mid cap")

# B — aligner-capped mid: hold 2.80 + 0.20 measured fade = 3.0s card must PASS.
# (VM 2026-09-29: the 3.0s limit INCLUDES the fade; RR check 2026-09-30: a
# 2.88s hold still measured 3.05–3.08s → current hold cap 2.80s.)
b_cap = _pass_beats()
b_cap[1] = {"type": "code", "start": 2.0, "dur": 2.80, "lines": ["x"], "cue": "code"}
b_cap[2] = {"type": "stat", "start": 4.92, "dur": 2.0, "value": "1", "cue": "one"}
b_cap[3] = {"type": "cta", "start": 7.04, "dur": 3.0, "text": "Go", "cue": "go"}
gcap = craft.video_maker_gate(b_cap)
ok(gcap["checks"]["B"] == "PASS",
   "B PASS: code hold 2.80 + 0.20 fade = 3.0s card (at the cap)")
ok(gcap["result"] == "PASS",
   "board sitting on the 3.0s-incl-fade mid cap + 3.0s cta is a full PASS")
b_288 = _pass_beats()
b_288[1] = {"type": "code", "start": 2.0, "dur": 2.88, "lines": ["x"], "cue": "code"}
b_288[2] = {"type": "stat", "start": 5.0, "dur": 2.0, "value": "1", "cue": "one"}
b_288[3] = {"type": "cta", "start": 7.12, "dur": 3.0, "text": "Go", "cue": "go"}
g288 = craft.video_maker_gate(b_288)
ok(g288["checks"]["B"] == "FAIL" and "3.08s incl. fade" in g288["reasons"][0],
   "B FAIL (new renders): 2.88s hold = 3.08s card (RR 2026-09-30 measured 3.05–3.08s)")
ok(craft.video_maker_gate(b_288, beat_timing=craft.BEAT_TIMING_INCL_FADE)["checks"]["B"] == "PASS",
   "boards stamped card_incl_fade (29 Sep renders) keep the 2.88s cap — no retroactive reject")
ok(craft.video_maker_gate_reason({"beats": b_288, "beat_timing": craft.BEAT_TIMING_INCL_FADE}) is None
   and craft.video_maker_gate_reason({"beats": b_288, "beat_timing": craft.BEAT_TIMING_HOLD_280}),
   "review/publish gate: marker card_incl_fade → 2.88 cap; card_hold_280 → 2.80 cap")
ok(craft.BEAT_TIMING_CURRENT == craft.BEAT_TIMING_HOLD_280 and craft.MID_BEAT_MAX_S == 2.80
   and abs(craft.MID_BEAT_MAX_S + craft.MID_FADE_S - 3.0) < 1e-9,
   "current timing marker = card_hold_280; hold 2.80 + fade 0.20 = 3.0")
b_old3 = _pass_beats()
b_old3[1] = {"type": "code", "start": 2.0, "dur": 3.0, "lines": ["x"], "cue": "code"}
b_old3[2] = {"type": "stat", "start": 5.12, "dur": 2.0, "value": "1", "cue": "one"}
b_old3[3] = {"type": "cta", "start": 7.2, "dur": 3.0, "text": "Go", "cue": "go"}
g_old3 = craft.video_maker_gate(b_old3)
ok(g_old3["checks"]["B"] == "FAIL" and "beat[1]" in g_old3["reasons"][0]
   and "3.20s incl. fade" in g_old3["reasons"][0],
   "B FAIL: the old 3.0s hold is a 3.20s card incl. the measured fade (VM 3.05–3.20s)")
b_end = _pass_beats()
b_end[3] = {"type": "cta", "start": 7.0, "dur": 4.0, "text": "Go", "cue": "go"}
g_end = craft.video_maker_gate(b_end)
ok(g_end["checks"]["B"] == "FAIL" and "cta" in g_end["reasons"][0]
   and "limit 3.9s" in g_end["reasons"][0],
   "B FAIL: 4.0s endcard hold = 4.12s card > 3.9s incl. fade (RR 4.05–4.08s)")
b_end[3]["dur"] = 3.78
ok(craft.video_maker_gate(b_end)["checks"]["B"] == "PASS",
   "B PASS: endcard hold 3.78 + 0.12 fade = 3.9s")
# Stored inventory: boards without the beat_timing marker were rendered under
# the old 3.0/4.0 HOLD caps (CoS: WARN, not FAIL) → the review/publish gate
# re-checks them with the legacy caps; new renders carry the marker → strict.
_legacy_board = _pass_beats()
_legacy_board[1] = {"type": "code", "start": 2.0, "dur": 3.0, "lines": ["x"], "cue": "code"}
_legacy_board[2] = {"type": "stat", "start": 5.12, "dur": 2.0, "value": "1", "cue": "one"}
_legacy_board[3] = {"type": "cta", "start": 7.24, "dur": 4.0, "text": "Go", "cue": "go"}
ok(craft.video_maker_gate_reason({"beats": _legacy_board}, "short") is None,
   "legacy stored board (3.0 hold / 4.0 endcard, no marker) is not auto-failed by timing")
_strict_cc = {"beats": _legacy_board, "beat_timing": craft.BEAT_TIMING_INCL_FADE}
_sr = craft.video_maker_gate_reason(_strict_cc, "short")
ok(_sr is not None and "incl. fade" in _sr,
   "new render (beat_timing marker) with a 3.0s hold FAILs the incl.-fade timing")
_legacy_long = [dict(b) for b in _legacy_board]
_legacy_long[1]["dur"] = 5.8
ok(craft.video_maker_gate_reason({"beats": _legacy_long}, "short") is not None,
   "legacy boards still FAIL the old HARD caps (5.8s mid)")
_legacy_rep = [dict(b) for b in _legacy_board]
_legacy_rep[2] = {"type": "code", "start": 5.12, "dur": 2.0, "lines": ["x"], "cue": "code2"}
_lr = craft.video_maker_gate_reason({"beats": _legacy_rep}, "short")
ok(_lr is not None and "Repeated card" in _lr,
   "legacy boards still FAIL the repeated-card check (CoS strict, #40)")
ok(craft.video_maker_gate(_legacy_board)["checks"]["B"] == "FAIL",
   "render-time gate (worker, no legacy flag) is strict")

# Legacy 7.5s quote shape must now FAIL Gate B (new queue HARD).
b_old = _pass_beats()
b_old[1] = {"type": "quote", "start": 37.44, "dur": 7.5,
            "text": "Converter não é deploy.", "cue": "converter"}
b_old[2] = {"type": "cta", "start": 45.06, "dur": 4.0, "text": "Go", "cue": "go"}
b_old = [b_old[0], b_old[1], b_old[2]]
ok(craft.video_maker_gate(b_old)["checks"]["B"] == "FAIL",
   "B FAIL: former 7.5s aligner-cap quote is over the 3.0s HARD mid cap")

# Same board with no stored dur still measures visual hold via cue-span − GAP.
b_nodur = [
    {"type": "hook", "start": 0.0, "dur": 2.0, "text": "Hook",
     "object": "bill", "cue": "h"},
    {"type": "quote", "start": 37.44, "text": "Converter não é deploy.",
     "cue": "converter"},
    {"type": "cta", "start": 45.06, "dur": 4.0, "text": "Go", "cue": "go"},
]
ok(craft.video_maker_gate(b_nodur)["checks"]["B"] == "FAIL",
   "B FAIL without dur: 45.06−37.44−0.12 = 7.50s visual hold > 3.0")

# B — CTA >4s
b_cta = _pass_beats()
b_cta[-1] = {"type": "cta", "start": 7.0, "dur": 5.5, "text": "Go", "cue": "go"}
# last beat span falls back to dur
gbc = craft.video_maker_gate(b_cta)
ok(gbc["checks"]["B"] == "FAIL" and "cta" in gbc["reasons"][0],
   "B FAIL: cta held 5.50s (limit 3.9s incl. fade)")
ok("3.9" in gbc["reasons"][0] and "Follow" in gbc["reasons"][0],
   "B fail reason cites the 3.9s-incl-fade endcard cap (not Follow-tomorrow)")

b_cta_ok = _pass_beats()
b_cta_ok[-1] = {"type": "cta", "start": 7.0, "dur": 3.78, "text": "Go", "cue": "go"}
ok(craft.video_maker_gate(b_cta_ok)["checks"]["B"] == "PASS",
   "B PASS: cta hold exactly 3.78s (3.9s incl. fade) is allowed")

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
ok(ge["checks"]["C"] == "FAIL" and any("re-displays narration" in r for r in ge["reasons"]),
   "C FAIL: statement only re-displays narration without a rich type")

# C — Subscribe CTA on mid cards (legal only on trailing cta/endcard)
c_sub = _pass_beats()
c_sub[1] = {"type": "statement", "start": 2.0, "dur": 2.4, "text": "Subscribe now",
            "cue": "stay"}
c_sub[2] = {"type": "stat", "start": 4.5, "dur": 2.4, "value": "40", "cue": "forty"}
gs = craft.video_maker_gate(c_sub)
ok(gs["checks"]["C"] == "FAIL" and "Subscribe CTA" in gs["reasons"][0],
   "C FAIL: mid statement 'Subscribe now' (endcard-only)")
ok("beat[1]" in gs["reasons"][0] and "endcard" in gs["reasons"][0],
   "Subscribe fail reason names the mid beat and that only the endcard may say it")

c_sub_lcase = _pass_beats()
c_sub_lcase[1] = {"type": "list", "start": 2.0, "dur": 2.4,
                  "items": [{"text": "please subscribe"}], "cue": "list"}
c_sub_lcase[2] = {"type": "stat", "start": 4.5, "dur": 2.4, "value": "1", "cue": "one"}
ok("subscribe" in craft.video_maker_gate(c_sub_lcase)["reasons"][0].lower(),
   "C FAIL: lowercase subscribe on a mid list item")

c_end = _pass_beats()
c_end[-1] = {"type": "cta", "start": 7.0, "dur": 3.5, "text": "Subscribe",
             "sub": "for the series", "cue": "go"}
ok(craft.video_maker_gate(c_end)["result"] == "PASS",
   "C PASS: Subscribe on the final cta/endcard is allowed (Rodrigo CoS)")

c_series = _pass_beats()
c_series.append({"type": "endcard", "start": 10.5, "dur": 3.0,
                 "text": "Subscribe", "sub": "next short tomorrow"})
# last pre-endcard cta span becomes 10.5-7.0=3.5 ≤4; endcard 3.0 ≤4
ok(craft.video_maker_gate(c_series)["result"] == "PASS",
   "C PASS: Subscribe on a trailing endcard after cta is not blocked")

c_noun = _pass_beats()
c_noun[1] = {"type": "statement", "start": 2.0, "dur": 2.4,
             "text": "subscribers churn", "cue": "churn"}
c_noun[2] = {"type": "stat", "start": 4.5, "dur": 2.4, "value": "1", "cue": "one"}
ok(craft.video_maker_gate(c_noun)["result"] == "PASS",
   "C PASS: the noun 'subscribers' is not a Subscribe CTA")

c_pt = _pass_beats()
c_pt[1] = {"type": "statement", "start": 2.0, "dur": 2.4,
           "text": "Inscreva-se agora", "cue": "agora"}
c_pt[2] = {"type": "stat", "start": 4.5, "dur": 2.4, "value": "1", "cue": "one"}
ok("Inscreva" in craft.video_maker_gate(c_pt)["reasons"][0]
   or "inscreva" in craft.video_maker_gate(c_pt)["reasons"][0].lower(),
   "C FAIL: PT Inscreva-se on a mid card is the same CTA")

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
ok("craft gate FAIL" in (craft.review_gate_reason(
        "Your RAG reads junk and still wrong · IA 12", "short",
        {"beats": typo}) or ""),
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
           script="Copilot billed the cancelled run. Subscribe — next Copilot Credits trap.",
           creation_config=json.dumps({"beats": typo, "used_fallback": False}))
s.add(v3); s.commit(); s.refresh(v3)
_uploads = []
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: (_uploads.append("up") or "vid")
publish_loop._publish_one(s, ch, v3)
ok(_uploads == [], "craft-gate FAIL never opens upload_video")
ok(v3.status == VideoStatus.REJECTED, "blocked craft publish auto-rejects")
ok(v3.craft_review == "fail", "blocked craft publish sets craft_review=fail")
ok(v3.error and "craft gate FAIL" in v3.error and "[A]" in v3.error,
   "blocked craft publish records the letter-tagged reason")
youtube.get_service, youtube.upload_video = _orig_get, _orig_up


print("Gate B repeated card (VM 2026-09-29 / RR publish check 2026-09-29)")


def _rb(beats):
    t = 0.0
    out = []
    for b in beats:
        b = dict(b)
        b["start"] = round(t, 3)
        b["dur"] = craft.CTA_BEAT_MAX_S if b["type"] == "cta" else craft.MID_BEAT_MAX_S
        t += b["dur"] + craft.BEAT_GAP_S
        out.append(b)
    return out


_H = {"type": "hook", "text": "Ollama parado prende 7GB de VRAM", "object": "GPU meter",
      "cue": "Ollama parado"}
_E = {"type": "cta", "text": "Subscribe · IA", "cue": "Subscribe"}
_S7 = {"type": "stat", "value": "7", "unit": "GB", "label": "presos na VRAM", "cue": "a"}
_CMP = {"type": "compare", "title": "Peso na GPU vs Tokens", "cue": "b",
        "left": {"title": "Peso", "items": ["7GB"]}, "right": {"title": "Tokens", "items": ["1GB"]}}
_TD = {"type": "term_define", "term": "Keep-alive", "definition": "5 minutos na VRAM", "cue": "c"}
_CMD = {"type": "command", "command": "pytest -q && git diff -U0", "cue": "d"}
_S0 = {"type": "stat", "value": "0", "unit": "", "label": "teto fatura sem limite", "cue": "e"}
_Q = {"type": "quote", "text": "Engenharia é escolher o byte", "cue": "f"}

# #1372: cards 1–3 replayed as cards 4–6.
g_replay = craft.video_maker_gate(_rb([_H, _S7, _CMP, _TD, _S7, _CMP, _TD, _Q, _E]))
ok(g_replay["checks"]["B"] == "FAIL" and g_replay["result"] == "FAIL",
   "cards 1–3 replayed as 4–6 (#1372) → Gate B FAIL")
ok(any("[B] Repeated card" in r and "replays beat[1]" in r and "replays beat[3]" in r
       for r in g_replay["reasons"]),
   "replay reason names each replayed card")
# #1373: double card back-to-back (pytest terminal ×2, 6.2s).
g_double = craft.video_maker_gate(_rb([_H, _CMD, _CMD, _S7, _Q, _E]))
ok(g_double["checks"]["B"] == "FAIL"
   and any("beat[1]→beat[2]" in r and "back-to-back" in r for r in g_double["reasons"]),
   "double card back-to-back (#1373) → Gate B FAIL")
# #1375 / #1358: triple card (stat ×3, 9.4s).
_trip_hits = craft.repeated_card_hits(_rb([_H, _S0, _S0, _S0, _CMD, _E]))
ok(len(_trip_hits) == 2 and "beat[1]→beat[2]" in _trip_hits[0] and "beat[2]→beat[3]" in _trip_hits[1],
   "triple card (#1375/#1358) → two back-to-back hits")
ok(craft.video_maker_gate(_rb([_H, _S0, _S0, _S0, _CMD, _E]))["checks"]["B"] == "FAIL",
   "triple card → Gate B FAIL")
# Near-equality: punctuation and case trimmed.
_q1 = {"type": "quote", "text": "Isso não é engenharia.", "cue": "x"}
_q2 = {"type": "statement", "text": "isso NÃO é engenharia!", "cue": "y"}
ok(craft.repeated_card_hits(_rb([_H, _q1, _q2, _E])) != [],
   "neighbours equal after trimming punctuation/case/accents → repeat")
ok(craft.screen_text_near("modelo no hd leva 4 minutos nao e engenharia",
                          "modelo no hd leva 4 minutos nao engenharia"),
   "near-equal: one dropped word ('é') still counts as the same card")
ok(not craft.screen_text_near("2 bytes por token em fp16", "1 byte por token em fp16"),
   "different numbers are different cards (not a repeat)")
# Clean boards and legacy snapshots stay PASS.
ok(craft.repeated_card_hits(_rb([_H, _S7, _CMP, _TD, _CMD, _Q, _E])) == [],
   "distinct cards → no repeat")
ok(craft.video_maker_gate(_rb([_H, _S7, _CMP, _TD, _CMD, _Q, _E]))["checks"]["B"] == "PASS",
   "distinct cards → Gate B PASS")
_legacy = [{"type": "stat", "start": 3.0, "dur": 3.0}, {"type": "stat", "start": 6.12, "dur": 3.0}]
ok(craft.repeated_card_hits([{"type": "hook", "start": 0, "dur": 2.88}] + _legacy) == [],
   "scraped legacy beats (no copy) never match (fail-open)")
ok(craft.repeated_card_hits(_rb([_H, _S7, _CMP, _S7, _Q, _E])) != [],
   "a single later replay of a mid card also FAILs (CoS rule: adjacent or later replay)")
ok(craft.repeated_card_hits(_rb([_H, _S7, _CMP, _TD, _Q, _E, dict(_E)])) == [],
   "two trailing cta beats with the same chip are not a repeated card")
_hook_again = dict(_Q, text="Ollama parado prende 7GB de VRAM")
ok(craft.repeated_card_hits(_rb([_H, _hook_again, _S7, _E])) != [],
   "a mid card that repeats the hook text right after frame0 is a repeat")
# Real #1370 stored board (creation_config.beats) → FAIL via review gate.
_v1370 = [
    {"type": "hook", "start": 0.0, "dur": 2.72, "text": "KV em FP16 estoura a 8GB, não é engenharia",
     "cue": "KV em FP16 estoura", "object": "VRAM"},
    {"type": "stat", "start": 2.84, "dur": 3.0, "value": "8", "unit": "GB",
     "label": "KV FP16 na placa", "cue": "Isso não é engenharia"},
    {"type": "stat", "start": 5.96, "dur": 3.0, "value": "8", "unit": "GB",
     "label": "KV FP16 na placa", "cue": "Isso não é engenharia"},
    {"type": "stat", "start": 9.08, "dur": 3.0, "value": "2", "unit": "bytes",
     "label": "por token em FP16", "cue": "dois bytes por"},
    {"type": "stat", "start": 12.2, "dur": 3.0, "value": "8", "unit": "GB",
     "label": "KV FP16 na placa", "cue": "Isso não é engenharia"},
    {"type": "cta", "start": 15.32, "dur": 4.0, "text": "Subscribe · IA", "cue": "Subscribe"},
]
_r1370 = craft.video_maker_gate_reason({"beats": _v1370}, "short")
ok(_r1370 is not None and "Repeated card" in _r1370,
   "#1370 stored board → review gate blocks with a Repeated card reason")
ok("Repeated card" not in (craft.video_maker_gate_reason({"beats": _v1370}, "long") or ""),
   "long-form stays exempt from the Video Maker gate")
# Snapshot keeps the copy the check compares (compare columns / term_define).
_snap = craft.snapshot_beats([_TD, _CMP])
ok(_snap[0].get("term") == "Keep-alive" and _snap[1].get("left", {}).get("title") == "Peso",
   "snapshot_beats keeps term/definition/left/right for the repeated-card check")

# --- Designer contrast split-card (2026-09-29 council P0) ---------------------
_sp = craft.contrast_split("Chat applied SAVE20. Prod charged full price. · Agent memory 33")
ok(_sp and _sp["top_label"] == "Chat" and _sp["top"] == "applied SAVE20."
   and _sp["bottom_label"] == "Prod" and _sp["bottom"] == "charged full price.",
   "split: OS Chat/Prod title → labelled top/bottom, digits + periods kept")
ok(craft.split_card_text(_sp) == _sp["head"] == "Chat applied SAVE20. Prod charged full price.",
   "split: card text rebuilds the whole head verbatim")
_sr = craft.contrast_split("Com ReBAR o 14B fez 48 tok/s. Sem, 11. · Local 57")
ok(_sr and _sr["top_label"] == "Com" and _sr["bottom_label"] == "Sem," and _sr["bottom"] == "11."
   and craft.split_card_text(_sr) == "Com ReBAR o 14B fez 48 tok/s. Sem, 11.",
   "split: RR Com/Sem (#1354 shape) keeps '14B', '48 tok/s', 'Sem, 11.'")
_si = craft.contrast_split("O prompt processa no CPU por 8 segundos, não é engenharia. · IA 211")
ok(_si and _si["top"] == "O prompt processa no CPU por 8 segundos," and _si["bottom"] == "não é engenharia."
   and _si["series"] == "IA",
   "split: IA one-sentence head splits at 'não é engenharia', comma stays on top")
_sg = craft.contrast_split("Chat said yes. Prod said no! · Agent memory 2")
ok(_sg and _sg["top_label"] == "Chat" and _sg["bottom"] == "said no!", "split: ! kept")
_sx = craft.contrast_split("Your agent forgot the key. The run failed. · Agent memory 7")
ok(_sx and _sx["top_label"] == "" and _sx["top"] == "Your agent forgot the key."
   and _sx["bottom"] == "The run failed.", "split: two sentences without Chat/Prod → unlabelled cards")
ok(craft.contrast_split("Chat applied SAVE20. Prod charged full price. · Shipping 4") is None,
   "split: Shipping series → None (only Agent memory / IA / Local)")
ok(craft.contrast_split("Chat applied SAVE20. Prod charged full price.") is None,
   "split: no ' · Series N' suffix → None (no brand-default series)")
ok(craft.contrast_split("Chat applied SAVE20. Prod charged full price. · Agent memory 33",
                        provided_thumb=True) is None,
   "split: provided thumbnail (#39) → None")
ok(craft.contrast_split("Ollama idle holds 7GB of VRAM · Local 12") is None,
   "split: one sentence without the verdict → None (normal hook card)")
ok(craft.contrast_split("A. B. C. · IA 3") is None, "split: three sentences → None")
ok(craft.contrast_split(None) is None and craft.contrast_split("") is None, "split: empty → None")
_fs = craft.split_font_px("applied SAVE20.", 1080, 1920)
ok(108 <= _fs <= 221, "split: type size is 2–3× the old hook at 1080w (%d px)" % _fs)
ok(craft.split_font_px("xx " * 40, 1080, 1920) >= 108, "split: long card of short words never drops below 10% width")
_mk = craft.split_card_markup(_sp, 1080, 1920)
ok(_mk.count('class="sc ') == 2 and _mk.count("✕") == 1 and 'sc-bot"><div class="sc-x"' in _mk,
   "split: shared markup has 2 cards, the ✕ stamp on the bottom card only")
ok("SAVE20." in _mk and "charged full price." in _mk, "split: markup carries the verbatim text")
_mke = craft.split_card_markup({"top": "<b>", "bottom": "a&b"}, 1080, 1920)
ok("&lt;b&gt;" in _mke and "a&amp;b" in _mke, "split: markup escapes HTML")

# --- RR hook pace (P1 d): claim ≤8 words, spoken by 3.0s, first cut by 2.5s ---
ok(craft.claim_word_count("Com ReBAR o 14B fez 48 tok/s. Sem, 11.") == 9
   and craft.claim_word_count("num_ctx em 32k come a VRAM, não é engenharia.") == 9
   and craft.claim_word_count("Jogar o node_modules no contexto não é engenharia.") == 8
   and craft.claim_word_count("a — b") == 2,
   "claim_word_count counts tokens with a letter/digit (dash is not a word)")
_w = [{"text": t, "start": round(0.35 * k, 3), "dur": 0.3}
      for k, t in enumerate("Jogar o node_modules no contexto não é engenharia Isso".split())]
ok(craft.claim_spoken_end("Jogar o node_modules no contexto não é engenharia.", _w) == 2.75,
   "claim_spoken_end = end of the last claim word (edge-tts boundaries, folded)")
_w2 = [{"text": t, "start": round(0.4 * k, 3), "dur": 0.3}
       for k, t in enumerate("Com ReBAR o quatorze B fez quarenta e oito tok s Sem onze".split())]
ok(craft.claim_spoken_end("Com ReBAR o 14B fez 48 tok/s. Sem, 11.", _w2) == round(0.4 * 8 + 0.3, 3),
   "claim_spoken_end: TTS tokens differ (numbers read out) → end of the n-th spoken word")
ok(craft.claim_spoken_end("x y", []) is None and craft.claim_spoken_end("", _w) is None,
   "claim_spoken_end: no word timings → None")
_hb = [{"type": "hook", "start": 0.0, "dur": 2.38, "text": "Jogar o node_modules no contexto não é engenharia."},
       {"type": "stat", "start": 2.5, "dur": 2.88, "value": "0", "unit": "passos", "label": "x"},
       {"type": "cta", "start": 5.5, "dur": 3.5, "text": "Subscribe · IA"}]
_m = craft.hook_pace_marker(_hb, _w, "rr")
ok(_m == {"version": craft.HOOK_PACE_V1, "claim_words": 8, "claim_spoken_end": 2.75},
   "hook_pace_marker (rr short): version + claim words + spoken end")
ok(craft.hook_pace_marker(_hb, _w, "os") is None and craft.hook_pace_marker(_hb, _w, None) is None
   and craft.hook_pace_marker(_hb, _w, "rr", "long") is None,
   "hook_pace_marker: OS / unknown brand / long-form → None (out of scope)")
ok(craft.hook_pace_hits(_hb, _m) == [], "8 words, spoken 2.75s, cut 2.5s → in spec")
ok(craft.hook_pace_hits(_hb, None) == [] and craft.hook_pace_hits(_hb, {"version": "x"}) == [],
   "no/unknown marker → never checked (approved inventory)")
_hb9 = [dict(_hb[0], text="Com ReBAR o 14B fez 48 tok/s. Sem, 11.")] + [dict(_hb[1], start=2.62)] + _hb[2:]
_h = craft.hook_pace_hits(_hb9, {"version": craft.HOOK_PACE_V1, "claim_spoken_end": 3.2})
ok(len(_h) == 3 and "claim 9 words" in _h[0] and "3.20s" in _h[1] and "2.62s" in _h[2],
   "9 words + spoken 3.20s + cut 2.62s → three hook-pace hits")
_gr = craft.video_maker_gate_reason({"beats": _hb9, "beat_timing": craft.BEAT_TIMING_INCL_FADE,
                                     "hook_pace": {"version": craft.HOOK_PACE_V1,
                                                   "claim_spoken_end": 3.2}}, "short")
ok(_gr and "RR hook pace: FAIL" in _gr, "marked RR board out of spec → review/publish gate blocks")
# Approved RR #1371 stored board (no marker): hook 4.64s, first cut 4.76s → still allowed.
_v1371 = [
    {"type": "hook", "start": 0.0, "dur": 4.64, "text": "Jogar o node_modules no contexto não é engenharia",
     "cue": "Jogar o node_modules", "object": "JOGAR"},
    {"type": "stat", "start": 4.76, "dur": 3.0, "value": "0", "unit": "passos",
     "label": "a pasta não compila", "cue": "Isso não é engenharia"},
    {"type": "command", "start": 7.88, "dur": 3.0, "cue": "O modelo não compila",
     "command": "npm ls --depth=0"},
    {"type": "stat", "start": 11.0, "dur": 3.0, "value": "100", "unit": "%",
     "label": "refeito pelo npm", "cue": "o npm reinstala"},
    {"type": "quote", "start": 14.12, "dur": 3.0,
     "text": "Quem escolhe o contexto manda na resposta.", "cue": "Quem escolhe o contexto"},
    {"type": "cta", "start": 17.24, "dur": 4.0, "text": "Subscribe · IA", "cue": "Subscribe — next"},
]
ok(craft.video_maker_gate_reason({"beats": _v1371}, "short") is None,
   "approved #1371 (no hook_pace marker) is not retroactively rejected at publish")
_r71m = craft.video_maker_gate_reason({"beats": _v1371, "hook_pace": {"version": craft.HOOK_PACE_V1}}, "short")
ok(_r71m and "first cut at 4.76s" in _r71m,
   "the same board WITH the marker would fail (the marker is what scopes the check)")

# --- Series chip from the real topic (P1 e: Shipping #1308/#1311 showed "Copilot Credits") ---
# Live data (read-only API, 2026-09-29): both on topic 45 "Shipping", channel 1 (OS);
# stored endcard chip "Subscribe · Copilot Credits" while the VO said "next Shipping trap".
_v1308_title = "You still can't tell if it works. · Shipping 4"
_v1308_subject = "You're looking at a builder teaser, not a live walkthrough. · Shipping 4"
_v1311_title = "You left the craft and the script edit vanished. · Shipping 3"
for _t in (_v1308_title, _v1308_subject, _v1311_title):
    for _b in ("os", None):
        ok(craft.series_of(_t, _b, "Shipping") == "Shipping",
           "series_of(%r…, brand=%s, topic=Shipping) → Shipping" % (_t[:24], _b))
# Pre-#33 failure mode: the series suffix isn't recognised (no suffix / unknown) and the
# brand is unknown → used to fall to "Copilot Credits". The topic now wins.
ok(craft.series_of("You still can't tell if it works.", None) == "Copilot Credits",
   "(baseline) no suffix + no brand + no topic → Copilot Credits fallback unchanged")
ok(craft.series_of("You still can't tell if it works.", None, "Shipping") == "Shipping",
   "#1308 failure mode: no recognised suffix + no brand → topic 'Shipping', not Copilot Credits")
ok(craft.series_of("You left the craft and the script edit vanished.", "os", "Shipping") == "Shipping",
   "#1311 failure mode: OS brand default (Agent memory) loses to the real topic")
ok(craft.series_of("Chat set limit 3. Prod looped 30 calls. · Agent memory 36", "os", "Shipping")
   == "Agent memory", "a real title suffix still wins over the topic")
ok(craft.series_from_topic("Agent memory and state in production") == "Agent memory"
   and craft.series_from_topic("Claude Code production workflows") == "Claude Code"
   and craft.series_from_topic("Agent traps") == "Agent traps"
   and craft.series_from_topic("ia") == "IA"
   and craft.series_from_topic("Copilot Credits: the invoice you missed") == "Copilot Credits",
   "series_from_topic: exact label or leading whole-word label (live topic names)")
ok(craft.series_from_topic("Engenheiro de IA no Brasil") is None
   and craft.series_from_topic("MCP Model Context Protocol") is None
   and craft.series_from_topic("Localhost tricks") is None
   and craft.series_from_topic("") is None and craft.series_from_topic(None) is None,
   "series_from_topic: no substring / partial-word matches → None (brand default)")
ok(craft.series_of("x", "rr", "Rodar IA local em 2026 (Ollama, VRAM, quantização)") == "IA",
   "unmapped topic name → brand default (RR → IA), unchanged")
_card = craft.series_endcard("You still can't tell if it works.", None, None, topic_name="Shipping")
ok(_card["chip"] == "Subscribe · Shipping" and "next Shipping" in _card["vo"],
   "series_endcard: chip and VO both follow the topic (they can no longer disagree)")
ok("next Shipping" in craft.ensure_series_endcard_vo("A. B.", "You still can't tell.", topic_name="Shipping"),
   "ensure_series_endcard_vo: topic_name reaches the pinned VO")

# --- RR batch 29/09 Gate B (P0 2026-09-30) ----------------------------------
print("RR P0 2026-09-30: frame0 word fit, TTS underscore, fragment cards, 2.80 hold")
# (4) frame0/thumb split card never breaks inside a word (#1381 "engenha/ria",
# #1386 "escanea/do"): the font shrinks so the longest word fits one line.
ok("overflow-wrap:anywhere" not in craft.SPLIT_CSS and "word-break:break" not in craft.SPLIT_CSS
   and "overflow-wrap:normal" in craft.SPLIT_CSS,
   "split CSS: no overflow-wrap:anywhere / break-all (a word is never split by the browser)")
for _t in ("não é engenharia.", "PDF escaneado", "Ollama sem CUDA", "NUM_PARALLEL multiplica a VRAM.",
           "Placa NVIDIA na máquina", "desproporcionalmente", "n_batch alto estoura a prefill."):
    _px = craft.split_font_px(_t, 1080, 1920)
    _ems = max(sum(craft.SPLIT_CAP_EM if (c.isupper() or c.isdigit()) else craft.SPLIT_CHAR_EM
                   for c in w) for w in re.split(r"[\s_/]+", _t) if w)
    ok(_px * _ems <= 1080 * craft.SPLIT_TEXT_W_FRAC + 1e-6,
       "split font %dpx: longest word of %r fits one line of the card" % (_px, _t))
ok(craft.split_font_px("não é engenharia.", 1080, 1920) < craft.split_font_px("não é bom.", 1080, 1920),
   "split font shrinks for a long word ('engenharia.') instead of breaking it")
_mk = craft.split_card_markup({"top": "NUM_PARALLEL multiplica", "bottom": "a VRAM.", "stamp": True},
                              1080, 1920)
ok("NUM_<wbr>PARALLEL" in _mk and "engenha<wbr>" not in _mk,
   "split markup: an identifier may wrap only after '_' (<wbr>); plain words never get a break point")
ok("escanea" not in craft._split_text_html("PDF escaneado").replace("escaneado", ""),
   "split markup keeps 'escaneado' whole")

# (5) TTS reads '_' as 'underline': spoken text is normalized, display is not.
ok(craft.tts_spoken_text("n_batch alto estoura a prefill.") == "n batch alto estoura a prefill.",
   "tts_spoken_text: n_batch → 'n batch'")
ok(craft.tts_spoken_text("num_gpu 0 é só CPU.") == "num gpu 0 é só CPU.", "tts_spoken_text: num_gpu → 'num gpu'")
ok(craft.tts_spoken_text("NUM_PARALLEL multiplica a VRAM.") == "num parallel multiplica a VRAM.",
   "tts_spoken_text: NUM_PARALLEL → 'num parallel' (VRAM/CPU untouched)")
ok(craft.tts_spoken_text("O contexto mora no n_ctx. Corta o n_ubatch.") ==
   "O contexto mora no n ctx. Corta o n ubatch.", "tts_spoken_text: every identifier in the script")
ok(craft.tts_spoken_text("Sem underscore aqui. Subscribe — next IA trap.") ==
   "Sem underscore aqui. Subscribe — next IA trap.", "tts_spoken_text: text without identifiers unchanged")
_tw = [{"text": "n", "start": 0.1, "dur": 0.2}, {"text": "batch", "start": 0.35, "dur": 0.4},
       {"text": "alto", "start": 0.8, "dur": 0.3}, {"text": "estoura", "start": 1.15, "dur": 0.45},
       {"text": "a", "start": 1.65, "dur": 0.1}, {"text": "prefill", "start": 1.8, "dur": 0.5}]
_rw = craft.remerge_tts_words(_tw, "n_batch alto estoura a prefill.")
ok([w["text"] for w in _rw] == ["n_batch", "alto", "estoura", "a", "prefill"]
   and _rw[0]["start"] == 0.1 and abs(_rw[0]["dur"] - 0.65) < 1e-6,
   "remerge_tts_words: 'n'+'batch' → one 'n_batch' word spanning both")
ok(craft.claim_spoken_end("n_batch alto estoura a prefill.", _rw) == 2.3
   and craft.claim_spoken_end("n_batch alto estoura a prefill.", _tw) == 2.3,
   "claim_spoken_end: displayed claim matches normalized TTS words (merged or not)")
ok(craft.remerge_tts_words(_tw[2:], "alto estoura") == _tw[2:], "remerge_tts_words: no identifier → unchanged")
ok([w["text"] for w in craft.remerge_tts_words([{"text": "n batch", "start": 0.1, "dur": 0.6}], "n_batch")]
   == ["n_batch"], "remerge_tts_words: one boundary for 'n batch' → displayed 'n_batch'")
ok([w["text"] for w in craft.remerge_tts_words(
    [{"text": t, "start": k * 0.3, "dur": 0.25} for k, t in enumerate(
        "O contexto mora no n ctx Corta o n batch e o n ubatch junto".split())],
    "O contexto mora no n_ctx. Corta o n_batch e o n_ubatch junto.")]
   == "O contexto mora no n_ctx Corta o n_batch e o n_ubatch junto".split(),
   "remerge_tts_words: every identifier in a script, in order (live edge-tts shape)")

# (7) no card is a suffix/fragment duplicate of its neighbour (#1382 'obedece').
_fr = [{"type": "hook", "start": 0.0, "dur": 2.38, "text": "n_batch alto estoura a prefill."},
       {"type": "quote", "start": 2.5, "dur": 2.8, "text": "A prefill manda. O n_batch obedece."},
       {"type": "quote", "start": 5.42, "dur": 1.4, "text": "obedece"},
       {"type": "cta", "start": 6.94, "dur": 3.0, "text": "Subscribe · IA"}]
ok(any("fragment of its neighbour" in h for h in craft.repeated_card_hits(_fr)),
   "repeated card: #1382 card 'obedece' after 'A prefill manda. O n_batch obedece.' is a fragment")
ok(craft.video_maker_gate(_fr)["checks"]["B"] == "FAIL", "fragment neighbour → Gate B FAIL")
ok(craft.screen_text_fragment("a prefill manda o n batch obedece", "obedece")
   and craft.screen_text_fragment("no padrao local", "o cache segue em 16 bits no padrao local")
   and not craft.screen_text_fragment("o cache segue em 16 bits", "no padrao local")
   and not craft.screen_text_fragment("obedece", "obedece"),
   "screen_text_fragment: prefix/suffix/middle runs only, equal/unrelated texts are not fragments")
_ok_board = [dict(_fr[0]), dict(_fr[1]), {"type": "quote", "start": 5.42, "dur": 1.4,
                                           "text": "Testa a prefill com o prompt longo."}, dict(_fr[3])]
ok(craft.repeated_card_hits(_ok_board) == [], "distinct sentence cards are not fragments")

# (6) new renders: mid hold ≤2.80 so hold + fade ≤3.0s; marker set by the worker.
ok(craft.BEAT_TIMING_CURRENT == craft.BEAT_TIMING_HOLD_280 and craft.MID_BEAT_MAX_S == 2.80
   and craft.MID_BEAT_MAX_S + craft.MID_FADE_S <= 3.0 + 1e-9,
   "new renders: 2.80s hold + 0.20s fade ≤ 3.0s (Gate B strict, card_incl_fade measured 3.05–3.08)")
_b305 = [{"type": "hook", "start": 0.0, "dur": 2.38, "text": "Um dois três."},
         {"type": "stat", "start": 2.5, "dur": 2.85, "value": "1", "unit": "x", "label": "a"},
         {"type": "cta", "start": 5.47, "dur": 3.0, "text": "Subscribe · IA"}]
ok(craft.video_maker_gate(_b305, beat_timing=craft.BEAT_TIMING_CURRENT)["checks"]["B"] == "FAIL",
   "2.85s mid hold (≈3.05 incl. fade) → Gate B FAIL on a card_hold_280 render")

# --- Card ↔ speech sync (CoS 2026-09-30): never after its words, lead ≤1.1s ---
print("card sync: a card never appears after its own speech; lead ≤ 1.1s (CoS 2026-09-30)")
_sw = [{"text": t, "start": round(0.4 * k, 3), "dur": 0.35} for k, t in enumerate(
    "Contexto come a VRAM O peso do modelo é só parte O cache cresce com cada token "
    "Mede a VRAM antes Subscribe next IA trap".split())]
# word starts: O peso 1.6 · O cache 4.4 · Mede 6.8 · Subscribe 8.4


def _sb(q1, q2, q3, cta, cta_dur=3.0):
    return [{"type": "hook", "start": 0.0, "dur": 1.38, "text": "Contexto come a VRAM", "cue": "Contexto",
             "object": "GPU"},
            {"type": "quote", "start": q1, "dur": 2.0, "text": "O peso do modelo é só parte", "cue": "O peso"},
            {"type": "stat", "start": q2, "dur": 2.0, "value": "4", "unit": "x", "label": "cache",
             "cue": "O cache cresce"},
            {"type": "quote", "start": q3, "dur": 1.0, "text": "Mede a VRAM antes", "cue": "Mede a VRAM"},
            {"type": "cta", "start": cta, "dur": cta_dur, "text": "Subscribe · IA", "cue": "Subscribe"}]


ok(craft.card_speech_starts(_sb(1.5, 4.0, 6.6, 8.2), _sw) == [0.0, 1.6, 4.4, 6.8, 8.4],
   "card_speech_starts: each card's own words (quote text / cue), in narration order")
_sync_ok = craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 8.2), _sw)
ok(_sync_ok["version"] == craft.SYNC_V1 and [n["status"] for n in _sync_ok["notes"]] == ["ok"] * 4
   and [n["lead"] for n in _sync_ok["notes"]] == [0.1, 0.4, 0.2, 0.2]
   and all(set(n) >= {"i", "type", "start", "speech_start", "lead", "status"} for n in _sync_ok["notes"]),
   "card_sync marker: one note per card (start, speech_start, lead, status)")
ok(craft.video_maker_gate(_sb(1.5, 4.0, 6.6, 8.2), card_sync=_sync_ok)["checks"]["B"] == "PASS",
   "in sync (leads 0.1–0.4s) → Gate B PASS")
_late = craft.card_sync_marker(_sb(1.5, 4.6, 6.6, 8.2), _sw)
_gl = craft.video_maker_gate(_sb(1.5, 4.6, 6.6, 8.2), card_sync=_late)
ok(_gl["checks"]["B"] == "FAIL" and any("AFTER its speech" in r and "card 2 (stat)" in r for r in _gl["reasons"]),
   "card 0.2s AFTER its own speech → Gate B FAIL (hard rule)")
_early = craft.card_sync_marker(_sb(1.5, 3.1, 6.6, 8.2), _sw)
_ge = craft.video_maker_gate(_sb(1.5, 3.1, 6.6, 8.2), card_sync=_early)
ok(_ge["checks"]["B"] == "FAIL" and any("leads its speech" in r and "1.30s" in r for r in _ge["reasons"]),
   "card leading its speech by 1.3s (> 1.1s ceiling) → Gate B FAIL")
_lead11 = craft.card_sync_marker(_sb(1.5, 3.3, 6.6, 8.2), _sw)
ok(craft.video_maker_gate(_sb(1.5, 3.3, 6.6, 8.2), card_sync=_lead11)["checks"]["B"] == "PASS",
   "lead of 1.1s (the ceiling) → PASS")
_capd = craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 8.6, cta_dur=craft.CTA_BEAT_MAX_S), _sw)
ok(_capd["notes"][-1]["status"] == "late_capped"
   and craft.video_maker_gate(_sb(1.5, 4.0, 6.6, 8.6, cta_dur=craft.CTA_BEAT_MAX_S), card_sync=_capd)["checks"]["B"] == "PASS",
   "endcard after the Subscribe VO only because it sits at its 3.78s cap → noted late_capped, not failed")
_capl = craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 8.6, cta_dur=2.0), _sw)
ok(_capl["notes"][-1]["status"] == "late", "endcard late while under its cap → late (fails)")
_um = _sb(1.5, 4.0, 6.6, 8.2)
_um[2]["cue"] = "palavras que ninguém falou"
_umk = craft.card_sync_marker(_um, _sw)
ok(_umk["notes"][1]["status"] == "unmatched" and craft.card_sync_hits(_umk) == [],
   "card whose words are not in the narration → noted 'unmatched' for the VM, not failed")
_cont = _sb(1.5, 4.0, 6.6, 8.2)
_cont.insert(3, {"type": "quote", "start": 5.9, "dur": 0.58, "text": "O cache cresce com cada token", "cue": "x"})
ok(craft.card_sync_notes(_cont, _sw)[2]["status"] == "late",
   "a card continuing a sentence that started under the previous card is 'late', never unmatched")
ok(craft.video_maker_gate(_sb(1.5, 4.6, 6.6, 8.2))["checks"]["B"] == "PASS",
   "unmarked board (older renders): no sync check — nothing rejected retroactively")
ok(craft.video_maker_gate_reason({"beats": _sb(1.5, 4.6, 6.6, 8.2), "beat_timing": craft.BEAT_TIMING_CURRENT,
                                  "card_sync": _late}) is not None
   and craft.video_maker_gate_reason({"beats": _sb(1.5, 4.0, 6.6, 8.2), "beat_timing": craft.BEAT_TIMING_CURRENT,
                                      "card_sync": _sync_ok}) is None,
   "review/publish gate reads creation_config.card_sync")
ok(craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 8.2), _sw, "long") is None
   and craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 8.2), [], "short") is None,
   "card_sync: long-form and no word timings → no marker")

# --- Brand pronunciation lexicon (OS 2026-09-30, #1385: "Owera's" heard as "Ora's") ---
print("TTS lexicon: Owera respelled for English voices, merged back to the display word")
ok(craft.tts_spoken_text("Channels Manager is Owera's builder.") == "Channels Manager is Oh-weh-ruh's builder.",
   "tts_spoken_text: Owera's → Oh-weh-ruh's (possessive kept)")
ok(craft.tts_spoken_text("OWERA ships. Owera’s plan.") == "Oh-weh-ruh ships. Oh-weh-ruh’s plan.",
   "tts_spoken_text: any case, curly possessive")
ok(craft.tts_spoken_text("Go to owera.com/channels or @owera.") == "Go to owera.com/channels or @owera.",
   "tts_spoken_text: URL and handle untouched")
ok(craft.tts_spoken_text("Owera's n_batch.", "pt-BR-AntonioNeural") == "Owera's n batch.",
   "tts_spoken_text: PT voice keeps Owera (identifier rule still applies)")
ok(craft.tts_spoken_text("Oweras and Owerabot stay.") == "Oweras and Owerabot stay.",
   "tts_spoken_text: only the whole word Owera")
_lw1 = [{"text": "is", "start": 1.0, "dur": 0.2}, {"text": "Oh-weh-ruh's", "start": 1.3, "dur": 0.57},
        {"text": "builder", "start": 1.9, "dur": 0.4}]
_lm1 = craft.remerge_tts_words(_lw1, "Channels Manager is Owera's builder")
ok([w["text"] for w in _lm1] == ["is", "Owera's", "builder"] and _lm1[1]["start"] == 1.3,
   "remerge: one-token respelling → display word Owera's")
_lw2 = [{"text": "is", "start": 1.0, "dur": 0.2}, {"text": "Oh", "start": 1.3, "dur": 0.1},
        {"text": "weh", "start": 1.4, "dur": 0.1}, {"text": "ruh", "start": 1.5, "dur": 0.3},
        {"text": "builds", "start": 1.9, "dur": 0.4}]
_lm2 = craft.remerge_tts_words(_lw2, "This is Owera builds")
ok([w["text"] for w in _lm2] == ["is", "Owera", "builds"] and _lm2[1]["start"] == 1.3
   and abs(_lm2[1]["dur"] - 0.5) < 1e-6,
   "remerge: multi-token respelling → one word with both ends' time")
ok(craft.remerge_tts_words(_lw1, "Channels Manager is Owera's builder", "pt-BR-AntonioNeural") == _lw1,
   "remerge: PT voice → words unchanged")
ok(craft.claim_spoken_end("Channels Manager is Owera's builder", [{"text": "Channels", "start": 0.0, "dur": 0.4},
   {"text": "Manager", "start": 0.4, "dur": 0.4}] + _lm1) == 2.3,
   "claim_spoken_end matches the displayed claim through the remerged brand word")

# --- Product teaser: no invented CLI (OS Shipping, #1385 Gate B 2026-09-30) ---
print("product teaser: fabricated CLI card → Gate B FAIL (marker-gated)")
ok(craft.is_product_teaser("Your thumbnail, not a template, in Channels Manager. · Shipping 8")
   and craft.is_product_teaser("x", topic_name="Shipping")
   and not craft.is_product_teaser("n_batch estoura a VRAM · IA 3", topic_name="IA")
   and not craft.is_product_teaser("Agent memory leaks", topic_name="Agent memory and state in production"),
   "is_product_teaser: Shipping title suffix or topic; RR/OS explainers are not")
_s85 = ("Your thumbnail, not a template, in Channels Manager. Channels Manager is Owera's builder for "
        "YouTube channels, and it's still in development. Now your design wins. Subscribe — next Shipping drop.")
_b85 = [{"type": "hook", "start": 0.0, "dur": 3.2, "text": "Your thumbnail, not a template", "object": "thumbnail"},
        {"type": "command", "start": 3.4, "dur": 2.8, "cue": "Channels Manager is Owera's", "prompt": "$",
         "command": "channels thumb cover.png", "output": ["image/png accepted", "youtube thumbnail set"]},
        {"type": "quote", "start": 6.4, "dur": 2.0, "text": "Now your design wins", "cue": "Now your design wins"},
        {"type": "cta", "start": 8.6, "dur": 3.0, "text": "Subscribe · Shipping", "cue": "Subscribe"}]
_cli = craft.cli_check_marker(_b85, _s85, title="x · Shipping 8")
ok(_cli["version"] == craft.CLI_V1 and len(_cli["hits"]) == 1 and _cli["hits"][0]["i"] == 1
   and "channels thumb cover.png" in _cli["hits"][0]["text"],
   "cli_check: '$ channels thumb cover.png' is not in the script → hit")
_g85 = craft.video_maker_gate(_b85, card_sync=None, cli_check=_cli)
ok(_g85["checks"]["B"] == "FAIL" and any(r.startswith("[B] Fabricated CLI: FAIL") for r in _g85["reasons"]),
   "Gate B FAIL: fabricated CLI on a product teaser")
_s85b = _s85.replace("Now your design wins.", "Run channels thumb cover png. Now your design wins.")
_b85b = [dict(b) for b in _b85]
_b85b[1] = dict(_b85b[1], output=[])
ok(craft.cli_check_marker(_b85b, _s85b, topic_name="Shipping")["hits"] == [],
   "cli_check: a command the narration literally says → no hit")
ok(craft.cli_check_marker(_b85, _s85, title="n_batch · IA 3", topic_name="IA") is None
   and craft.cli_check_marker(_b85, _s85, topic_name="Shipping", content_format="long") is None,
   "cli_check: RR/OS explainers and longs carry no marker (illustrative stills unchanged)")
ok(craft.video_maker_gate(_b85)["checks"]["B"] == "PASS",
   "unmarked board (older renders, e.g. #1379/#1385): no CLI check — nothing rejected retroactively")
ok(craft.video_maker_gate_reason({"beats": _b85, "beat_timing": craft.BEAT_TIMING_CURRENT, "cli_check": _cli})
   and "Fabricated CLI" in craft.video_maker_gate_reason({"beats": _b85, "beat_timing": craft.BEAT_TIMING_CURRENT,
                                                          "cli_check": _cli}),
   "review/publish gate reads creation_config.cli_check")

# --- Endcard straddle: the endcard never cuts into a content sentence (#1385) ---
print("card sync: endcard straddling the last content sentence → Gate B FAIL")
_ssc = "Contexto come a VRAM. O peso do modelo é só parte. O cache cresce com cada token. Mede a VRAM antes. Subscribe — next IA trap."
ok(craft.endcard_vo_content_end(_sw, _ssc) == 6.8 + 1.2 + 0.35,
   "endcard_vo_content_end: end of 'antes', the last word before the Subscribe line")
ok(craft.endcard_vo_content_end(_sw, "Contexto come a VRAM.") is None,
   "endcard_vo_content_end: no Subscribe line → None")
_st_bad = craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 7.9), _sw, script=_ssc)
ok(_st_bad["notes"][-1]["status"] == "straddle" and _st_bad["notes"][-1]["content_end"] == 8.35,
   "endcard at 7.90 while 'antes' is spoken until 8.35 → straddle")
_gst = craft.video_maker_gate(_sb(1.5, 4.0, 6.6, 7.9), card_sync=_st_bad)
ok(_gst["checks"]["B"] == "FAIL" and any("cuts in before the last content sentence ends" in r for r in _gst["reasons"]),
   "Gate B FAIL: endcard cuts into the last content sentence")
_st_ok = craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 8.35), _sw, script=_ssc)
ok(_st_ok["notes"][-1]["status"] == "ok"
   and craft.video_maker_gate(_sb(1.5, 4.0, 6.6, 8.35), card_sync=_st_ok)["checks"]["B"] == "PASS",
   "endcard after the content sentence ends, before the Subscribe word → ok")
ok("content_end" not in craft.card_sync_marker(_sb(1.5, 4.0, 6.6, 7.9), _sw)["notes"][-1],
   "no script → no straddle note (Part 1 marker shape unchanged)")

print(f"\nALL {_checks} CHECKS PASSED")
