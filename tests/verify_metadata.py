"""Dependency-free regression checks for app/services/metadata.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_metadata.py

``metadata.generate`` + ``_from_meta`` turn MPT (or grok -p) social-metadata into
the title/description/tags that land on every published video; a silent break
ships EN titles on a PT channel, drops tags, or leaves a dead MPT blocking
publish. ``finalize_description`` is already partially covered by
verify_growth / verify_chapters — this suite owns the pure mapping helpers and
the generate choke point, plus finalize edges those suites leave open.

Covers, dependency-free (no network, no DB, no live YouTube):
  - ``_from_meta``: title fallback/truncation, caption+hashtag description,
    hashtag ``#`` strip, EXTRA_TAGS always appended, empty/missing fields
  - ``generate``: MPT happy path (platform + language code mapping pinned),
    MPT-None → grok-cli fallback (short vs long prompts, language rule),
    both-dead → last-resort heuristic so review is never blocked; script=None
    normalised; subject never mutated by callers' layers
  - ``finalize_description`` residual edges: es/unknown lang → EN CTA,
    case-insensitive BCP-47 prefix, None/whitespace base, channel-only and
    playlist-only blocks, chapters already present not re-appended while CTA
    still lands, YouTube 5000-char clamp after append

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

from app.services import metadata

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ----------------------------------------------------------------- _from_meta
print("_from_meta: MPT/LLM payload → title/description/tags")

m = metadata._from_meta("Subject fallback", {
    "title": "A real title",
    "caption": "First sentence about the topic.",
    "hashtags": ["#ai", "#ml", "#agents"],
})
ok(m["title"] == "A real title", "title taken from meta when present")
ok(m["description"] == "First sentence about the topic.\n\n#ai #ml #agents",
   "description is caption + blank line + space-joined hashtags")
ok(m["tags"] == ["ai", "ml", "agents"] + metadata.EXTRA_TAGS,
   "hashtags stripped of # and EXTRA_TAGS always appended")

# title fallback + hard 100-char clamp (YouTube title limit)
long_title = "T" * 150
m2 = metadata._from_meta("Subject fallback", {"title": long_title})
ok(m2["title"] == "T" * 100, "title clamped to YouTube's 100-char limit")
ok(len(m2["title"]) == 100, "clamped title length is exactly 100")

m3 = metadata._from_meta("Subject fallback", {"title": "", "caption": "", "hashtags": []})
ok(m3["title"] == "Subject fallback", "empty title falls back to subject")
ok(m3["description"] == "", "empty caption+hashtags → empty description")
ok(m3["tags"] == list(metadata.EXTRA_TAGS),
   "no hashtags → tags is exactly EXTRA_TAGS (no # residue)")

m4 = metadata._from_meta("S", {})  # all keys missing
ok(m4["title"] == "S", "missing title key → subject")
ok(m4["description"] == "", "missing caption/hashtags → empty description")
ok(m4["tags"] == list(metadata.EXTRA_TAGS), "missing hashtags → EXTRA_TAGS only")

# hashtags that already lack # still work; None fields coerce safely
m5 = metadata._from_meta("S", {"title": None, "caption": None, "hashtags": None})
ok(m5["title"] == "S", "None title → subject")
ok(m5["description"] == "", "None caption/hashtags → empty description")
ok(m5["tags"] == list(metadata.EXTRA_TAGS), "None hashtags → EXTRA_TAGS only")

m6 = metadata._from_meta("S", {"hashtags": ["plain", "#mixed"]})
ok(m6["tags"][:2] == ["plain", "mixed"],
   "bare hashtags pass through; leading # stripped only when present")

# EXTRA_TAGS contract — the three evergreen tags must stay (publish + discovery)
ok(metadata.EXTRA_TAGS == ["AI", "AI engineering", "machine learning"],
   "EXTRA_TAGS pin (a rename here silently drops discovery tags on every publish)")


# -------------------------------------------------------------- generate (MPT)
print("generate: MPT happy path (platform + language mapping)")

_calls: list[dict] = []


def _mpt_ok(subject, script, platform="youtube_shorts", language="en-US"):
    _calls.append({"subject": subject, "script": script,
                   "platform": platform, "language": language})
    return {"title": "MPT Title", "caption": "MPT cap", "hashtags": ["#mpt"]}


with patch.object(metadata.mpt, "social_metadata", side_effect=_mpt_ok):
    out = metadata.generate("Subj", "the script body", content_format="short",
                            language="Brazilian Portuguese")
ok(out == metadata._from_meta("Subj", {
    "title": "MPT Title", "caption": "MPT cap", "hashtags": ["#mpt"],
}), "MPT hit → _from_meta of its payload (no litellm)")
ok(_calls[-1]["platform"] == "youtube_shorts",
   "short format → platform=youtube_shorts")
ok(_calls[-1]["language"] == "pt-BR",
   "Brazilian Portuguese → MPT language pt-BR")
ok(_calls[-1]["subject"] == "Subj" and _calls[-1]["script"] == "the script body",
   "subject/script reach MPT unchanged")

with patch.object(metadata.mpt, "social_metadata", side_effect=_mpt_ok):
    metadata.generate("S", "x", content_format="long", language="Spanish")
ok(_calls[-1]["platform"] == "youtube",
   "long format → platform=youtube (not youtube_shorts)")
ok(_calls[-1]["language"] == "es-ES", "Spanish → MPT language es-ES")

with patch.object(metadata.mpt, "social_metadata", side_effect=_mpt_ok):
    metadata.generate("S", "x", content_format="short", language="English")
ok(_calls[-1]["language"] == "en-US", "English → MPT language en-US")

with patch.object(metadata.mpt, "social_metadata", side_effect=_mpt_ok):
    metadata.generate("S", "x", content_format="short", language=None)
ok(_calls[-1]["language"] == "en-US",
   "language=None → en-US (legacy default, never None to MPT)")

with patch.object(metadata.mpt, "social_metadata", side_effect=_mpt_ok):
    metadata.generate("S", "x", content_format="short", language="Klingon")
ok(_calls[-1]["language"] == "en-US",
   "unknown language name → en-US (no KeyError, no raw name leak)")

# script=None must not reach MPT as None (social_metadata signature is str)
with patch.object(metadata.mpt, "social_metadata", side_effect=_mpt_ok):
    metadata.generate("S", None)  # type: ignore[arg-type]
ok(_calls[-1]["script"] == "", "script=None normalised to '' before MPT")

# Language code table itself — a drift here would ship every PT channel as en-US
ok(metadata._LANGUAGE_MPT_CODES == {
    "Brazilian Portuguese": "pt-BR",
    "English": "en-US",
    "Spanish": "es-ES",
}, "_LANGUAGE_MPT_CODES pin (voice-language names from video_gen.channel_language)")


# --------------------------------------------------- generate (grok-cli fallback)
print("generate: MPT dead → grok-cli fallback")

_llm_calls: list[dict] = []


def _fake_complete(prompt, system=None, max_tokens=None):
    _llm_calls.append({"prompt": prompt, "system": system,
                       "max_tokens": max_tokens})
    return json.dumps({
        "title": "LLM Title",
        "caption": "LLM caption about the topic.",
        "hashtags": ["#llm", "#ai", "#code"],
    })


with patch.object(metadata.mpt, "social_metadata", return_value=None), \
     patch.object(metadata, "complete", side_effect=_fake_complete):
    out = metadata.generate("Subj", "script text", content_format="short",
                            language="Brazilian Portuguese")
ok(out["title"] == "LLM Title", "LLM title used when MPT returns None")
ok(out["tags"][:3] == ["llm", "ai", "code"], "LLM hashtags stripped + kept")
ok(metadata.EXTRA_TAGS[0] in out["tags"], "EXTRA_TAGS still appended on LLM path")
ok(len(_llm_calls) == 1, "exactly one llm.complete call on the fallback path")
prompt = _llm_calls[0]["prompt"]
ok("HARD RULE" in prompt and "Brazilian Portuguese" in prompt,
   "language rule present in the LLM prompt when language is set")
ok("Shorts" in prompt or "short" in prompt.lower(),
   "short format uses the Shorts copywriter prompt")
ok("Subject: Subj" in prompt and "script text" in prompt,
   "subject + script reach the LLM prompt")

# long-form prompt branch + no-language (no HARD RULE)
_llm_calls.clear()
long_script = "long script word " * 400  # ~6800 chars — past the 4000 cap
ok(len(long_script) > 4000, "fixture script is long enough to hit the 4000 cap")
with patch.object(metadata.mpt, "social_metadata", return_value=None), \
     patch.object(metadata, "complete", side_effect=_fake_complete):
    metadata.generate("LongSub", long_script, content_format="long",
                      language=None)
prompt_long = _llm_calls[0]["prompt"]
ok("long-form" in prompt_long.lower() or "in-depth" in prompt_long.lower(),
   "long format uses the long-form copywriter prompt")
ok("HARD RULE" not in prompt_long,
   "language=None → no HARD RULE language clause (legacy en behavior)")
# The prompt embeds script[:4000]; a regression that passes the full script
# would bloat context. Pin that the embedded script is capped.
script_in_prompt = prompt_long.split("Script:", 1)[-1].strip()
ok(len(script_in_prompt) <= 4000 + 5,  # tiny slack for trailing whitespace
   "long-form prompt caps script at 4000 chars")
# And the full script is NOT present (proves the slice actually fired)
ok(long_script not in prompt_long,
   "full uncapped script is absent from the prompt")

# fenced JSON response is stripped before parse
def _fenced_complete(prompt, system=None, max_tokens=None):
    return "```json\n" + json.dumps({
        "title": "Fenced", "caption": "c", "hashtags": ["#x"],
    }) + "\n```"


with patch.object(metadata.mpt, "social_metadata", return_value=None), \
     patch.object(metadata, "complete", side_effect=_fenced_complete):
    out_f = metadata.generate("S", "x")
ok(out_f["title"] == "Fenced",
   "markdown-fenced JSON from the LLM is stripped and parsed")


# ----------------------------------------------- generate (last-resort heuristic)
print("generate: MPT + LLM both dead → last-resort heuristic")


def _boom_complete(*a, **k):
    raise RuntimeError("llm down")


with patch.object(metadata.mpt, "social_metadata", return_value=None), \
     patch.object(metadata, "complete", side_effect=_boom_complete):
    out_h = metadata.generate("Heuristic Subject That Is Quite Long " + "Z" * 120,
                              "script", content_format="short")
ok(out_h["title"] == ("Heuristic Subject That Is Quite Long " + "Z" * 120)[:100],
   "heuristic title is subject[:100] (publish never blocked)")
ok(out_h["description"] == "Heuristic Subject That Is Quite Long " + "Z" * 120,
   "heuristic description is the full subject (not clamped — finalize does that)")
ok(out_h["tags"] == list(metadata.EXTRA_TAGS),
   "heuristic tags are EXTRA_TAGS only")

# LLM returns unparseable content → same heuristic
def _garbage_complete(*a, **k):
    return "not json at all"


with patch.object(metadata.mpt, "social_metadata", return_value=None), \
     patch.object(metadata, "complete", side_effect=_garbage_complete):
    out_g = metadata.generate("S", "x")
ok(out_g == {"title": "S", "description": "S", "tags": list(metadata.EXTRA_TAGS)},
   "unparseable LLM body → same last-resort heuristic")

# empty content from LLM
def _empty_complete(*a, **k):
    return None


with patch.object(metadata.mpt, "social_metadata", return_value=None), \
     patch.object(metadata, "complete", side_effect=_empty_complete):
    out_e = metadata.generate("S", "x")
ok(out_e["title"] == "S", "None LLM content → heuristic (no AttributeError)")


# MPT returns empty dict / falsy → also falls through (if meta is falsy)
with patch.object(metadata.mpt, "social_metadata", return_value={}), \
     patch.object(metadata, "complete", side_effect=_fake_complete):
    # {} is falsy in Python → fallback. Pin that so a `if meta is not None`
    # rewrite (which would _from_meta an empty dict → title=subject, no tags
    # from LLM) fails this suite.
    _llm_calls.clear()
    out_empty = metadata.generate("S", "x")
ok(out_empty["title"] == "LLM Title",
   "MPT empty-dict is falsy → LLM fallback (not _from_meta of {})")


# --------------------------------- finalize_description residual edges
print("finalize_description: residual edges (growth/chapters leave these open)")

# Spanish / unknown language → EN CTA (es is not in _CTA_LINES)
d_es = metadata.finalize_description("Base.", "es-ES", "UCes", "PLes")
ok("Subscribe" in d_es and "Inscreva-se" not in d_es,
   "es-ES has no dedicated CTA → falls through to EN")
d_xx = metadata.finalize_description("Base.", "xx-YY", "UCx", None)
ok("Subscribe" in d_xx, "unknown language code → EN CTA")

# Case-insensitive BCP-47 prefix
d_PT = metadata.finalize_description("Base.", "PT-br", "UCpt", None)
ok("Inscreva-se" in d_PT, "language_code prefix is case-insensitive (PT-br → pt)")

# None / whitespace base
ok(metadata.finalize_description(None, "en-US", None, None) == "",  # type: ignore[arg-type]
   "None description → empty string (no TypeError)")
ok(metadata.finalize_description("   \n  ", "en-US", None, None) == "",
   "whitespace-only description strips to empty")

# Channel-only / playlist-only
d_ch = metadata.finalize_description("Base.", "en-US", "UConly", None)
ok("channel/UConly" in d_ch and "playlist" not in d_ch,
   "channel id alone → subscribe link, no playlist line")
d_pl = metadata.finalize_description("Base.", "en-US", None, "PLonly")
ok("playlist?list=PLonly" in d_pl and "sub_confirmation" not in d_pl,
   "playlist id alone → playlist line, no subscribe link")

# Chapters already present: header check skips re-append, CTA still lands
base_with_ch = "Intro.\n\n⏱ Chapters:\n0:00 Hook\n0:15 Body"
d_ch_present = metadata.finalize_description(
    base_with_ch, "en-US", "UCx", None,
    chapter_lines=["0:00 Hook", "0:15 Body", "0:40 End"])
ok(d_ch_present.count("⏱ Chapters:") == 1,
   "chapters header already in base → not re-appended")
ok("sub_confirmation=1" in d_ch_present,
   "CTA still appended when chapters were already present")
ok(d_ch_present.count("0:00 Hook") == 1,
   "existing chapter body not duplicated")

# PT chapters header
d_pt_ch = metadata.finalize_description(
    "Base.", "pt-BR", "UCx", None,
    chapter_lines=["0:00 Início", "0:20 Meio", "0:40 Fim"])
ok("⏱ Capítulos:" in d_pt_ch and "Chapters:" not in d_pt_ch,
   "pt-BR uses Capítulos header (not EN Chapters)")
# chapters segment comes BEFORE the CTA block
cap_i = d_pt_ch.index("⏱ Capítulos:")
cta_i = d_pt_ch.index("Inscreva-se")
ok(cap_i < cta_i, "chapters block is ordered before the subscribe CTA")

# 5000-char clamp applies AFTER append (not just on the base)
# A base that fits, plus a CTA that would push past 5000, must still clamp.
base_near = "y" * 4950
d_clamp = metadata.finalize_description(base_near, "en-US", "UCclamp", "PLclamp")
ok(len(d_clamp) <= 5000, "finalize clamps to 5000 even when the CTA pushes over")
ok(len(d_clamp) < len(base_near) + 200, "clamp actually truncated something")

# Idempotency marker is specifically sub_confirmation=1 (not a looser 'youtube.com')
almost = "Base.\n\nhttps://www.youtube.com/channel/UCx  (no confirm flag)"
d_almost = metadata.finalize_description(almost, "en-US", "UCx", None)
ok("sub_confirmation=1" in d_almost,
   "a bare channel URL without sub_confirmation=1 is NOT treated as already-finalized")
ok(d_almost.count("youtube.com/channel") == 2,
   "subscribe link still appended when only a bare URL was present")

# CTA lines table pin — es deliberately absent (falls to en)
ok(set(metadata._CTA_LINES.keys()) == {"pt", "en"},
   "_CTA_LINES keys are exactly {pt, en} (es falls through)")
ok(metadata._SUB_CONFIRM_MARKER == "sub_confirmation=1",
   "idempotency marker pin")


# -------------------------------------------------------------- module wiring
print("module wiring: public surface")
ok(callable(metadata.generate) and callable(metadata.finalize_description),
   "generate + finalize_description are the public entry points")
ok(callable(metadata._from_meta) and callable(metadata._llm_fallback),
   "helpers are importable (suites + backfill reach them)")


print(f"\nALL {_checks} CHECKS PASSED")
