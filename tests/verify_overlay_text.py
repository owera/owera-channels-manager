"""Regression checks: overlay (frame0 hook) + thumbnail text keep punctuation and digits.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_overlay_text.py

Bug (2026-09-28/29 review):
- #1363 "Chat routed to Maya. Prod paged Lee." → frame0 "Chat routed to Maya"
  (sentence 2 + period gone), thumb "Chat routed to Maya prod paged Lee" (run-on).
- #1354 "Com ReBAR o 14B fez 48 tok/s. Sem, 11." → frame0 "… 48 tok/s",
  thumb "Com ReBAR 14B fez 48 tok/s sem 11".
- #1347/#1350/#1353/#1355/#1356 lost the leading digit upstream in
  generate_ideas (fixed in 445aae4); pinned here so overlay/thumb never drop one.

Root cause: storyboard._lock_opening_hook used first_spoken_sentence +
compress_claim (rstrip . ! ? … , ; :), and thumbnail._hook_text asked the LLM
for "no trailing punctuation" and accepted any claim-aligned run-on; its
fallback was compress_claim(…, 8).

Pins the new craft.overlay_* helpers, both render paths, the sanitizers that
must stay (Subscribe/endcard, $N, emoji, currency titles), and the #37
provided-script hook check. Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

from app.services import craft, thumbnail
from app.services.engines import storyboard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


MAYA = "Chat routed to Maya. Prod paged Lee."
MAYA_TITLE = MAYA + " · Agent memory 32"
MAYA_SCRIPT = MAYA + " She caught the chat off a remembered name. Subscribe — next Agent memory trap."
TOKS = "48 tok/s. Sem, 11."
REBAR = "Com ReBAR o 14B fez 48 tok/s. Sem, 11."
REBAR_TITLE = REBAR + " · Local 57"
REBAR_SCRIPT = REBAR + " Com ReBAR, o mesmo 14B fez 48 tok/s. Sem ReBAR, 11. Subscribe — next Local trap."
LEAD = "16GB rodou o 14B. O contexto 32k, não."
LEAD_TITLE = LEAD + " · Local 50"
LEAD_SCRIPT = LEAD + " Quantizado, o 14B é um bloco fixo na RAM. Subscribe — next Local trap."
LEAD2 = "32B em Q4 rodou na 24GB. O Q8, não."


def sentences(text):
    return [p for p in craft._OVERLAY_SENT_SPLIT_RE.split(text) if p.strip()]


# ---------------------------------------------------------------------------
print("craft.overlay_claim: punctuation + digits verbatim")
ok(craft.overlay_claim(MAYA) == MAYA, "Maya/Lee keeps both periods")
ok(len(sentences(craft.overlay_claim(MAYA))) == 2, "Maya/Lee stays two sentences")
ok(craft.overlay_claim(TOKS) == TOKS, "'48 tok/s. Sem, 11.' keeps digits, period and comma")
ok(craft.overlay_claim(REBAR) == REBAR, "9-word ReBAR claim kept whole (≤12-word ceiling)")
ok(craft.overlay_claim(LEAD) == LEAD, "leading digit kept (16GB …)")
ok(craft.overlay_claim(LEAD2) == LEAD2, "leading digit kept (32B …)")
ok(craft.overlay_claim("Why did the bill hit 3x?") == "Why did the bill hit 3x?",
   "question mark kept")
ok(craft.overlay_claim("It shipped anyway!") == "It shipped anyway!", "exclamation kept")
ok(craft.overlay_claim("  Chat   routed\nto Maya.  ") == "Chat routed to Maya.",
   "whitespace collapsed, period kept")
ok(craft.overlay_claim("Prod paged Lee, ") == "Prod paged Lee",
   "only a dangling comma is trimmed")
long_two = ("One two three four five six seven. Eight nine ten eleven twelve "
            "thirteen fourteen.")
ok(craft.overlay_claim(long_two) == "One two three four five six seven.",
   ">12 words: whole sentences that fit are kept (period included)")
long_one = " ".join(f"w{i}" for i in range(1, 16)) + "."
ok(craft.overlay_claim(long_one) == " ".join(f"w{i}" for i in range(1, 13)),
   ">12-word single sentence: word-clipped at 12")
ok(craft.overlay_claim("") == "" and craft.overlay_claim(None) == "", "empty → ''")
ok(craft.compress_claim(MAYA, 4) == "Chat routed to Maya",
   "compress_claim (endcard chip path) unchanged")

print("craft.overlay_hook_source: whole title head, series number dropped")
ok(craft.overlay_hook_source(MAYA_TITLE) == MAYA, "title head keeps both sentences")
ok(craft.overlay_hook_source(None, None, LEAD_TITLE) == LEAD,
   "no title → subject head (not the raw subject with · Local 50)")
ok("50" not in craft.overlay_hook_source(LEAD_TITLE)
   and "16" in craft.overlay_hook_source(LEAD_TITLE),
   "series episode number is not overlay copy; claim digits are")
ok(craft.overlay_hook_source(None, MAYA_SCRIPT, MAYA_TITLE) == MAYA,
   "narration opens on the head → both sentences (not just sentence 1)")
ok(craft.overlay_hook_source(None, REBAR_SCRIPT, REBAR_TITLE) == REBAR,
   "ReBAR narration opens on the head → full claim with 'Sem, 11.'")
ok(craft.overlay_hook_source(None, "Your agent forgets. Here is why.", "Agent memory idea")
   == "Your agent forgets.",
   "narration not opening on the head → first spoken sentence (unchanged rule)")
ok(craft.overlay_hook_source(None, "", MAYA_TITLE) == MAYA, "empty script → subject head")
ok(craft.script_opens_with("chat ROUTED to maya.  prod paged lee. more", MAYA),
   "script_opens_with folds case/whitespace")
ok(not craft.script_opens_with("Chat routed to Maya. Other.", MAYA),
   "script_opens_with needs the whole head")

print("craft.overlay_preserves_claim: LLM-thumb guard")
ok(craft.overlay_preserves_claim(MAYA, MAYA), "exact copy preserves")
ok(craft.overlay_preserves_claim(MAYA, MAYA.upper()), "case is free")
ok(not craft.overlay_preserves_claim(MAYA, "Chat routed to Maya prod paged Lee"),
   "run-on (periods lost) rejected")
ok(not craft.overlay_preserves_claim(MAYA, "Chat routed to Maya. Prod paged Lee"),
   "final period lost rejected")
ok(not craft.overlay_preserves_claim(REBAR, "Com ReBAR 14B fez 48 tok/s sem 11"),
   "'… tok/s sem 11' (period + comma lost) rejected")
ok(not craft.overlay_preserves_claim(LEAD, "GB rodou o 14B. O contexto 32k, não."),
   "leading digit dropped rejected")
ok(not craft.overlay_preserves_claim(REBAR, "Com ReBAR o 14B fez 48 tok/s. Sem,."),
   "trailing number dropped rejected")
ok(craft.overlay_numbers("32B em Q4 rodou na 24GB. 1.5x") == ["32", "4", "24", "1.5"],
   "overlay_numbers finds leading/inner/decimal numbers")
ok(craft.overlay_preserves_claim("Reranking in 5 lines", "Reranking in 5 lines"),
   "no-punctuation claim unaffected")

# ---------------------------------------------------------------------------
print("frame0: storyboard._lock_opening_hook + render_hook")


def lock(script, subject):
    beats = [{"type": "hook", "text": "old slogan", "cue": "x", "emoji": "🔥"},
             {"type": "stat", "value": "1", "cue": "y"}]
    storyboard._lock_opening_hook(beats, script, subject)
    return beats


def hook_html(beat):
    beat = {**beat, "start": 0.0, "dur": 3.0}
    html, _tw = storyboard.render_hook(beat, {"i": 0, "start": 0.0, "dur": 3.0,
                                              "is_last": False, "portrait": True})
    return html


b = lock(MAYA_SCRIPT, MAYA_TITLE)
ok(b[0]["text"] == MAYA, "frame0 Maya/Lee = both sentences, both periods")
h = hook_html(b[0])
ok('<span class="word">Maya.</span>' in h and '<span class="word">Lee.</span>' in h,
   "rendered frame0 words keep 'Maya.' and 'Lee.'")
b = lock(REBAR_SCRIPT, REBAR_TITLE)
ok(b[0]["text"] == REBAR, "frame0 ReBAR keeps '48 tok/s. Sem, 11.'")
h = hook_html(b[0])
ok(all(f'<span class="word">{w}</span>' in h for w in ("48", "tok/s.", "Sem,", "11.")),
   "rendered frame0 words keep 48 / tok/s. / Sem, / 11.")
b = lock(LEAD_SCRIPT, LEAD_TITLE)
ok(b[0]["text"] == LEAD and b[0]["text"].startswith("16GB"),
   "frame0 keeps the leading digit (16GB …)")
ok('<span class="word">16GB</span>' in hook_html(b[0]), "rendered frame0 keeps '16GB'")
b = lock("32B em Q4 rodou na 24GB. O Q8, não. Depois.", LEAD2 + " · Local 56")
ok(b[0]["text"] == LEAD2, "frame0 keeps '32B …' and 'O Q8, não.'")
ok(b[0]["emoji"] == "" and b[0]["object"], "frame0 still strips emoji and stamps an object")
# Kept sanitizing: endcard/Subscribe never becomes frame0.
b = lock("Subscribe — next IA trap.", "PDF escaneado não é engenharia. · IA 205")
ok(b[0]["text"] == "PDF escaneado não é engenharia.",
   "endcard VO as first line → subject head (no Subscribe on frame0, no series nn)")
ok(not craft.contains_subscribe_cta(b[0]["text"]), "frame0 never carries Subscribe")
# $N numerals stay numerals on frame0.
b = lock("Copilot billed $79 for a cancelled run. Here is why.",
         "Copilot billed $79 for a cancelled run.")
ok("$79" in b[0]["text"] and craft.preserves_dollar_numerals(
    "Copilot billed $79 for a cancelled run.", b[0]["text"]),
   "$79 stays $79 on frame0")

# ---------------------------------------------------------------------------
print("thumb: thumbnail._hook_text + _thumbnail_html")


def thumb(title, llm_out, subject="subject"):
    with patch.object(thumbnail, "_llm", return_value=llm_out):
        return thumbnail._hook_text(subject, title)


ok(thumb(MAYA_TITLE, "Chat routed to Maya prod paged Lee") == MAYA,
   "run-on LLM thumb rejected → Maya/Lee with both periods")
ok(thumb(MAYA_TITLE, MAYA) == MAYA, "faithful LLM copy (punctuation kept) accepted")
ok(thumb(REBAR_TITLE, "Com ReBAR 14B fez 48 tok/s sem 11") == REBAR,
   "'… tok/s sem 11' LLM thumb rejected → full claim with 'Sem, 11.'")
ok(thumb(LEAD_TITLE, "GB rodou o 14B. O contexto 32k, não.") == LEAD,
   "LLM thumb that drops the leading digit rejected → '16GB …'")
ok(thumb(LEAD_TITLE, "16GB rodou o 14B. O contexto 32k, não.") == LEAD,
   "leading-digit thumb survives either way")
with patch.object(thumbnail, "_llm", side_effect=RuntimeError("down")):
    ok(thumbnail._hook_text("s", MAYA_TITLE) == MAYA, "LLM down → Maya/Lee verbatim")
    ok(thumbnail._hook_text("s", TOKS) == TOKS, "LLM down → '48 tok/s. Sem, 11.' verbatim")
    ok(thumbnail._hook_text(LEAD_TITLE, None) == LEAD,
       "no title → subject head, leading digit kept, no '· Local 50'")
_sys = {}
with patch.object(thumbnail, "_llm",
                  side_effect=lambda p, system=None, max_tokens=None: _sys.update(s=system) or MAYA):
    thumbnail._hook_text("s", MAYA_TITLE)
ok("no trailing punctuation" not in _sys["s"] and "punctuation exactly" in _sys["s"]
   and "EVERY number" in _sys["s"],
   "thumb LLM brief asks to keep punctuation and every number")
# Kept sanitizing on the thumb.
ok(thumb("Copilot billed the cancelled run", "💸 billed already")
   == "Copilot billed the cancelled run", "emoji-first thumb still rejected")
ok(thumb("Copilot billed $79 for a run", "Copilot billed seventy-nine dollars")
   == "Copilot billed $79 for a run", "spelled-out $N still rejected")
ok(thumb("Reranking in 5 lines · Copilot Credits 1", "The Cache Is Lying")
   == "Reranking in 5 lines", "curiosity-gap slogan still rejected; series nn not shown")
html = thumbnail._thumbnail_html(MAYA, brand="os", content_format="short")
ok(">Chat routed to Maya. Prod paged Lee.</div>" in html,
   "thumb card embeds the hook with both periods")
html = thumbnail._thumbnail_html(LEAD, brand="rr", content_format="short")
ok(">16GB rodou o 14B. O contexto 32k, não.</div>" in html,
   "thumb card embeds the leading digit, comma and periods")

# ---------------------------------------------------------------------------
print("currency title rules (#36) unchanged")
ok(craft.nonsense_title_reason("Copilot cobrou $47 no plano · Copilot Credits 3")
   == craft.CURRENCY_TITLE_REASON, "$N in a title still a publish-gate reject")
ok(craft.nonsense_title_reason("Sai R$50 por mês · IA 7") == craft.CURRENCY_TITLE_REASON,
   "R$N in a title still a publish-gate reject")
ok(craft.nonsense_title_reason(REBAR_TITLE) is None,
   "digits without a currency sign are not a currency title")

# ---------------------------------------------------------------------------
print("#37 provided-script hook check: same pass/fail as before")
A1_SUBJECT = "PDF escaneado colado no chat não é engenharia. · IA 205"
A1_SCRIPT = ("PDF escaneado colado no chat não é engenharia. Não é. Página escaneada é foto. "
             "Subscribe — next IA trap.")
MISALIGNED = "Hoje vamos falar de produtividade. Não é. Subscribe — next IA trap."
ok(craft.provided_script_hook_reason(A1_SCRIPT, title=A1_SUBJECT) is None, "A1 aligned → pass")
ok(craft.provided_script_hook_reason(A1_SCRIPT, subject=A1_SUBJECT) is None,
   "no title → subject head → pass")
r = craft.provided_script_hook_reason(MISALIGNED, title=A1_SUBJECT)
ok(r and r.startswith(craft.PROVIDED_SCRIPT_HOOK_REASON), "misaligned → fail with reason")
ok(craft.provided_script_hook_reason("It costs seventy-nine dollars. More.",
                                     title="Copilot Credits burned $79 · Copilot Credits 12")
   is not None, "$N spelled out on the spoken line → fail")
ok(craft.provided_script_hook_reason(MAYA_SCRIPT, title=MAYA_TITLE) is None,
   "Maya/Lee provided script aligned → pass")
ok(craft.provided_script_hook_reason(LEAD_SCRIPT, title=LEAD_TITLE) is None,
   "leading-digit provided script aligned → pass")
ok(craft.provided_script_hook_reason("", title=MAYA_TITLE) is not None, "empty script → fail")
cc = json.dumps({"script_source": "provided"})
ok(craft.publish_craft_block_reason(title=A1_SUBJECT, script=A1_SCRIPT, creation_config=cc,
                                    content_format="short") is None,
   "publish gate: aligned provided script clears")
r = craft.publish_craft_block_reason(title=A1_SUBJECT, script=MISALIGNED, creation_config=cc,
                                     content_format="short")
ok(r and r.startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
   "publish gate: misaligned provided script still blocked with the reason")
ok(craft.claim_aligned("Chat routed to Maya", MAYA) and craft.claim_aligned(MAYA, MAYA),
   "claim_aligned unchanged for the old 1-sentence and new 2-sentence overlay")

print(f"\nALL {_checks} CHECKS PASSED")
