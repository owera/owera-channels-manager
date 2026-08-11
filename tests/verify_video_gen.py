"""Dependency-free regression checks for app/services/video_gen.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_video_gen.py

``video_gen`` is the title/idea choke point the autofill loop, topics API, and
trends API all share: voice → language plumbing feeds every PT/EN/ES prompt,
and ``generate_ideas`` is the only writer of board subjects. A silent break
here ships English titles on a Portuguese channel (the 2026-07-07 incident)
or floods the board past its horizon (the 2026-07-26 overshoot, when the
model returned more lines than asked). Language helpers have a few pins in
``verify_growth.py``; ``generate_ideas`` itself had zero direct coverage
(autofill only stubs it).

Covers, dependency-free (no network, no live LLM):
  - module contracts: voice-prefix tables for pt/en/es, name ↔ BCP-47 pairs
  - ``language_from_voice`` / ``code_from_voice``: None/empty, known voices,
    case-insensitive prefix, unknown prefix → None, multi-suffix voices
  - ``channel_language`` / ``channel_language_code``: None channel id, missing
    channel, unbound profile, missing profile row, corrupt JSON, empty/
    missing voice_name, happy path via default render profile
  - ``generate_ideas``: short vs long prompt branches, language HARD RULE,
    theme_prompt guidance, existing-title avoid list (last-60 window),
    bullet/number/quote stripping, case-insensitive dedupe (existing +
    within response), ``n`` cap (incl. n=0), empty/None LLM content,
    litellm model + drop_params pins

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.models import Channel, RenderProfile
from app.services import video_gen

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------
print("module contracts: voice language + BCP-47 tables")

ok(set(video_gen._VOICE_LANGUAGES) == {"pt", "en", "es"},
   "voice-language table covers exactly pt/en/es")
ok(set(video_gen.LANGUAGE_CODES) == {"pt", "en", "es"},
   "BCP-47 table covers exactly the same pt/en/es keys")
ok(video_gen._VOICE_LANGUAGES["pt"] == "Brazilian Portuguese",
   "pt voice prefix → Brazilian Portuguese (prompt language name)")
ok(video_gen._VOICE_LANGUAGES["en"] == "English", "en → English")
ok(video_gen._VOICE_LANGUAGES["es"] == "Spanish", "es → Spanish")
ok(video_gen.LANGUAGE_CODES == {
    "pt": "pt-BR", "en": "en-US", "es": "es-ES",
}, "BCP-47 codes pinned (metadata / YouTube defaultLanguage)")
# Cross-table consistency: every name in _VOICE_LANGUAGES has a matching code
# via the same key — a rename of one without the other would desync prompts
# from YouTube language tags.
for k in video_gen._VOICE_LANGUAGES:
    ok(k in video_gen.LANGUAGE_CODES,
       f"LANGUAGE_CODES has key {k!r} matching _VOICE_LANGUAGES")


# ---------------------------------------------------------------------------
# language_from_voice / code_from_voice
# ---------------------------------------------------------------------------
print("language_from_voice / code_from_voice")

ok(video_gen.language_from_voice(None) is None, "language: None voice → None")
ok(video_gen.language_from_voice("") is None, "language: empty voice → None")
ok(video_gen.code_from_voice(None) is None, "code: None voice → None")
ok(video_gen.code_from_voice("") is None, "code: empty voice → None")

ok(video_gen.language_from_voice("pt-BR-AntonioNeural-Male")
   == "Brazilian Portuguese",
   "language: pt-BR-AntonioNeural-Male → Brazilian Portuguese")
ok(video_gen.code_from_voice("pt-BR-AntonioNeural-Male") == "pt-BR",
   "code: pt-BR-AntonioNeural-Male → pt-BR")
ok(video_gen.language_from_voice("en-US-AndrewNeural") == "English",
   "language: en-US-AndrewNeural → English")
ok(video_gen.code_from_voice("en-US-AndrewNeural") == "en-US",
   "code: en-US-AndrewNeural → en-US")
ok(video_gen.language_from_voice("es-ES-ElviraNeural") == "Spanish",
   "language: es-ES-ElviraNeural → Spanish")
ok(video_gen.code_from_voice("es-ES-ElviraNeural") == "es-ES",
   "code: es-ES-ElviraNeural → es-ES")

# Prefix is case-insensitive (split then .lower()) — Azure voice ids are mixed.
ok(video_gen.language_from_voice("PT-BR-AntonioNeural") == "Brazilian Portuguese",
   "language: uppercase PT prefix still maps")
ok(video_gen.code_from_voice("EN-US-AndrewNeural") == "en-US",
   "code: uppercase EN prefix still maps")

# Unknown prefix must not invent a language (would inject garbage HARD RULE).
ok(video_gen.language_from_voice("fr-FR-DeniseNeural") is None,
   "language: unknown fr prefix → None")
ok(video_gen.code_from_voice("fr-FR-DeniseNeural") is None,
   "code: unknown fr prefix → None")
ok(video_gen.language_from_voice("AntonioNeural") is None,
   "language: no-hyphen id uses whole string as prefix → unknown → None")
ok(video_gen.code_from_voice("not-a-voice") is None,
   "code: nonsense prefix → None")

# Only the first segment before '-' is the key — region suffixes must not
# pollute the lookup (pt-PT would still be 'pt' → Brazilian Portuguese today).
ok(video_gen.language_from_voice("pt-PT-RaquelNeural") == "Brazilian Portuguese",
   "language: first segment only (pt-PT-* still keys on 'pt')")
ok(video_gen.code_from_voice("pt-PT-RaquelNeural") == "pt-BR",
   "code: first segment only (pt-PT-* → pt-BR via LANGUAGE_CODES['pt'])")


# ---------------------------------------------------------------------------
# channel_language / channel_language_code (DB-backed)
# ---------------------------------------------------------------------------
print("channel_language / channel_language_code")


def fresh_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


ok(video_gen.channel_language(fresh_session(), None) is None,
   "channel_language: channel_id=None → None")
ok(video_gen.channel_language_code(fresh_session(), None) is None,
   "channel_language_code: channel_id=None → None")

with fresh_session() as s:
    ok(video_gen.channel_language(s, 99999) is None,
       "channel_language: missing channel row → None")
    ok(video_gen.channel_language_code(s, 99999) is None,
       "channel_language_code: missing channel row → None")

# Channel with no default render profile
with fresh_session() as s:
    ch = Channel(slug="no-prof", name="NoProf", default_render_profile_id=None)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) is None,
       "channel_language: unbound default_render_profile_id → None")
    ok(video_gen.channel_language_code(s, ch.id) is None,
       "channel_language_code: unbound default_render_profile_id → None")

# Channel points at a missing profile row (FK not enforced by SQLite here)
with fresh_session() as s:
    ch = Channel(slug="ghost-prof", name="Ghost", default_render_profile_id=4242)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) is None,
       "channel_language: missing profile row → None")
    ok(video_gen.channel_language_code(s, ch.id) is None,
       "channel_language_code: missing profile row → None")

# Corrupt params_json → ValueError from json.loads → None (never raises)
with fresh_session() as s:
    p = RenderProfile(name="bad-json", params_json="{not-json")
    s.add(p)
    s.commit()
    s.refresh(p)
    ch = Channel(slug="bad-json", name="Bad", default_render_profile_id=p.id)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) is None,
       "channel_language: corrupt params_json → None (swallows ValueError)")
    ok(video_gen.channel_language_code(s, ch.id) is None,
       "channel_language_code: corrupt params_json → None")

# Empty / missing voice_name
with fresh_session() as s:
    p = RenderProfile(name="no-voice", params_json=json.dumps({"foo": 1}))
    s.add(p)
    s.commit()
    s.refresh(p)
    ch = Channel(slug="no-voice", name="NoVoice", default_render_profile_id=p.id)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) is None,
       "channel_language: params without voice_name → None")
    ok(video_gen.channel_language_code(s, ch.id) is None,
       "channel_language_code: params without voice_name → None")

# params_json="" (falsy) is coerced via `or "{}"` before json.loads.
# (params_json=None is stored as the column default "{}" by the model.)
with fresh_session() as s:
    p = RenderProfile(name="empty-str", params_json="")
    s.add(p)
    s.commit()
    s.refresh(p)
    # SQLModel may have stored "" as-is; pin whatever round-tripped.
    ok(p.params_json == "" or p.params_json == "{}",
       f"empty-string params_json round-trip is '' or defaulted (got {p.params_json!r})")
    ch = Channel(slug="empty-str", name="EmptyStr", default_render_profile_id=p.id)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) is None,
       "channel_language: empty-string params_json → None (via or \"{}\")")
    ok(video_gen.channel_language_code(s, ch.id) is None,
       "channel_language_code: empty-string params_json → None")

# Happy path: pt-BR voice on the default profile
with fresh_session() as s:
    p = RenderProfile(
        name="pt",
        params_json=json.dumps({"voice_name": "pt-BR-AntonioNeural"}),
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    ch = Channel(slug="c2", name="C2", default_render_profile_id=p.id)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) == "Brazilian Portuguese",
       "channel_language: happy path via default render profile")
    ok(video_gen.channel_language_code(s, ch.id) == "pt-BR",
       "channel_language_code: happy path via default render profile")

# en voice + es voice (proves we don't hardcode a single language)
with fresh_session() as s:
    p = RenderProfile(
        name="en",
        params_json=json.dumps({"voice_name": "en-US-AndrewNeural"}),
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    ch = Channel(slug="en-ch", name="EN", default_render_profile_id=p.id)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) == "English",
       "channel_language: en voice → English")
    ok(video_gen.channel_language_code(s, ch.id) == "en-US",
       "channel_language_code: en voice → en-US")

with fresh_session() as s:
    p = RenderProfile(
        name="es",
        params_json=json.dumps({"voice_name": "es-ES-ElviraNeural"}),
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    ch = Channel(slug="es-ch", name="ES", default_render_profile_id=p.id)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) == "Spanish",
       "channel_language: es voice → Spanish")
    ok(video_gen.channel_language_code(s, ch.id) == "es-ES",
       "channel_language_code: es voice → es-ES")


# ---------------------------------------------------------------------------
# generate_ideas — prompt shape + response parsing
# ---------------------------------------------------------------------------
print("generate_ideas: short/long prompts, language rule, parsing, n-cap")

_llm_calls: list[dict] = []


def _completion_factory(text: str):
    def _completion(*, model, messages, drop_params=True, **kw):
        _llm_calls.append({
            "model": model,
            "messages": messages,
            "drop_params": drop_params,
            **kw,
        })
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])
    return _completion


def _run_ideas(**kwargs):
    """Call generate_ideas with a stub litellm; kwargs go to generate_ideas."""
    text = kwargs.pop("_text", "A solid hook title about caches\n"
                               "Why Your X Keeps Failing in Prod")
    # Defaults that most tests want.
    kwargs.setdefault("topic_name", "AI Agents")
    kwargs.setdefault("theme_prompt", None)
    kwargs.setdefault("existing", [])
    kwargs.setdefault("n", 8)
    kwargs.setdefault("content_format", "short")
    kwargs.setdefault("language", None)
    with patch.dict("sys.modules", {
        "litellm": SimpleNamespace(completion=_completion_factory(text)),
    }):
        return video_gen.generate_ideas(**kwargs)


# --- short form happy path ---
_llm_calls.clear()
out = _run_ideas(
    _text="Why Your Agent Forgets Everything\nStop Chaining Models Blindly",
    topic_name="AI Agents",
    n=8,
    content_format="short",
)
ok(out == ["Why Your Agent Forgets Everything",
           "Stop Chaining Models Blindly"],
   "short form: two clean lines become two titles")
ok(len(_llm_calls) == 1, "exactly one litellm.completion call")
ok(_llm_calls[0]["model"] == settings.litellm_model,
   f"uses settings.litellm_model ({settings.litellm_model!r})")
ok(_llm_calls[0]["drop_params"] is True, "drop_params=True (provider-compat)")
ok(len(_llm_calls[0]["messages"]) == 1
   and _llm_calls[0]["messages"][0]["role"] == "user",
   "single user message (no system role)")
prompt = _llm_calls[0]["messages"][0]["content"]
ok("short-video ideas" in prompt or "YouTube Shorts" in prompt,
   "short form prompt names Shorts / short-video")
ok("AI Agents" in prompt, "topic_name is embedded in the prompt")
ok("in-depth long-form" not in prompt,
   "short form does not use the long-form brief")
ok("6-15 words" not in prompt,
   "short form does not carry the long 6-15-words rule")
# Discriminating pin: short form has the "under 12 words" rule; long has 6-15.
ok("under 12 words" in prompt,
   "short form pins the under-12-words title rule")
ok("HARD RULE" not in prompt,
   "language=None → no HARD RULE clause (legacy behavior)")
ok("Extra guidance" not in prompt,
   "theme_prompt=None → no Extra guidance line")


# --- long form prompt branch ---
_llm_calls.clear()
_run_ideas(
    _text="Is X Worth It or Just Pain?\nWhy Your X Fails in Production",
    topic_name="GPU Rigs",
    content_format="long",
    n=5,
)
prompt_long = _llm_calls[0]["messages"][0]["content"]
ok("in-depth long-form" in prompt_long or "long-form YouTube" in prompt_long,
   "long form prompt names long-form / in-depth")
ok("6-15 words" in prompt_long,
   "long form pins the 6-15 words title rule")
ok("under 12 words" not in prompt_long,
   "long form does not carry the short under-12 rule")
ok("GPU Rigs" in prompt_long, "long form embeds topic_name")
ok("Generate 5 distinct" in prompt_long,
   "long form embeds n in 'Generate {n} distinct…'")


# --- language HARD RULE (the 07-07 incident fix) ---
_llm_calls.clear()
_run_ideas(
    _text="Por que seu agente esquece tudo",
    language="Brazilian Portuguese",
    topic_name="Agentes de IA",  # PT topic — still must state HARD RULE
    content_format="short",
)
prompt_pt = _llm_calls[0]["messages"][0]["content"]
ok("HARD RULE" in prompt_pt and "Brazilian Portuguese" in prompt_pt,
   "language set → HARD RULE names the channel language")
ok("exclusively in Brazilian Portuguese" in prompt_pt,
   "HARD RULE says the channel publishes exclusively in that language")


# --- theme_prompt guidance ---
_llm_calls.clear()
_run_ideas(
    _text="A title",
    theme_prompt="focus on production failures, not tutorials",
    topic_name="Observability",
)
prompt_g = _llm_calls[0]["messages"][0]["content"]
ok("Extra guidance for this theme: focus on production failures, not tutorials"
   in prompt_g,
   "theme_prompt is injected as 'Extra guidance for this theme: …'")


# --- existing titles: avoid list uses last 60, and appears in the prompt ---
_llm_calls.clear()
existing = [f"Old Title {i}" for i in range(70)]  # 70 > 60 window
_run_ideas(_text="Brand New Hook Title", existing=existing, n=3)
prompt_av = _llm_calls[0]["messages"][0]["content"]
ok("Old Title 69" in prompt_av and "Old Title 10" in prompt_av,
   "recent existing titles appear in the avoid list")
ok("Old Title 0" not in prompt_av and "Old Title 9" not in prompt_av,
   "avoid list is the last 60 only (oldest 10 of 70 are dropped)")
ok("- Old Title 69" in prompt_av,
   "avoid entries are bullet-prefixed '- …'")


# --- empty existing → '(none yet)' ---
_llm_calls.clear()
_run_ideas(_text="Only Title", existing=[], n=1)
prompt_none = _llm_calls[0]["messages"][0]["content"]
ok("(none yet)" in prompt_none,
   "empty existing → avoid list shows '(none yet)'")


# --- response parsing: bullets, numbers, quotes, blanks, whitespace ---
_llm_calls.clear()
messy = "\n".join([
    "",
    "  1. First Numbered Title  ",
    "2) Second Numbered Title",
    "- Bullet Title Here",
    "* Star Bullet Title",
    '"Quoted Title Words"',
    "   ",
    "Plain Clean Title",
    "10. Another Numbered One",
])
out_messy = _run_ideas(_text=messy, n=10, existing=[])
ok("First Numbered Title" in out_messy,
   "leading '1. ' numbering stripped")
ok("Second Numbered Title" in out_messy,
   "leading '2) ' numbering stripped")
ok("Bullet Title Here" in out_messy, "leading '- ' bullet stripped")
ok("Star Bullet Title" in out_messy, "leading '* ' bullet stripped")
ok("Quoted Title Words" in out_messy,
   "surrounding double quotes stripped")
ok("Plain Clean Title" in out_messy, "plain line kept as-is")
ok("Another Numbered One" in out_messy,
   "leading '10. ' multi-digit numbering stripped")
ok("" not in out_messy and all(t.strip() for t in out_messy),
   "blank / whitespace-only lines are dropped")


# --- case-insensitive dedupe against existing ---
_llm_calls.clear()
out_dedup = _run_ideas(
    _text="Why Caches Lie\nWHY CACHES LIE\nA Fresh Angle On Caches",
    existing=["why caches lie"],  # already on the board
    n=8,
)
ok(out_dedup == ["A Fresh Angle On Caches"],
   "case-insensitive dedupe drops existing + within-response duplicates")
ok("Why Caches Lie" not in out_dedup and "WHY CACHES LIE" not in out_dedup,
   "neither casing of an existing title survives")


# --- within-response case-insensitive dedupe (no existing) ---
_llm_calls.clear()
out_self = _run_ideas(
    _text="Same Title Twice\nsame title twice\nDifferent Third Title",
    existing=[],
    n=8,
)
ok(out_self == ["Same Title Twice", "Different Third Title"],
   "within-response case-insensitive dedupe keeps the first casing only")


# --- n cap (the 07-26 overshoot: model returns more than asked) ---
_llm_calls.clear()
many_lines = "\n".join(f"Title Number {i}" for i in range(20))
out_cap = _run_ideas(_text=many_lines, n=5, existing=[])
ok(len(out_cap) == 5, f"n=5 caps output at 5 even when model returns 20 (got {len(out_cap)})")
ok(out_cap == [f"Title Number {i}" for i in range(5)],
   "n cap keeps the first n titles in order")

# n is also embedded in the prompt so the model is asked for the right count
ok("Generate 5 distinct" in _llm_calls[0]["messages"][0]["content"],
   "n reaches the prompt as 'Generate {n} distinct…'")


# --- n=0 → [] (and still calls the LLM — current contract; pin it) ---
_llm_calls.clear()
out_zero = _run_ideas(_text="Should Not Appear\nNor This", n=0, existing=[])
ok(out_zero == [], "n=0 → empty list (max(0, n) slice)")
ok(len(_llm_calls) == 1,
   "n=0 still invokes litellm (slice is post-parse; not a short-circuit)")


# --- n<0 → [] via max(0, n); bare out[:n] would reverse-slice ---
_llm_calls.clear()
out_neg = _run_ideas(
    _text="Neg One\nNeg Two\nNeg Three",
    n=-1,
    existing=[],
)
ok(out_neg == [],
   "n=-1 → [] (max(0, n) guard; out[:n] would reverse-slice to all-but-last)")


# --- empty / None LLM content ---
_llm_calls.clear()
out_empty = _run_ideas(_text="", n=8)
ok(out_empty == [], "empty LLM content → []")


def _none_content(*, model, messages, drop_params=True, **kw):
    _llm_calls.append({"model": model})
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=None))])


_llm_calls.clear()
with patch.dict("sys.modules", {
    "litellm": SimpleNamespace(completion=_none_content),
}):
    out_none = video_gen.generate_ideas("T", None, [], n=4)
ok(out_none == [], "None LLM content (or \"\") → [] via `or \"\"`")


# --- content that is only existing titles → [] ---
_llm_calls.clear()
out_all_known = _run_ideas(
    _text="Already There\nALREADY THERE",
    existing=["Already There"],
    n=5,
)
ok(out_all_known == [],
   "when every returned line collides with existing → []")


# --- short form embeds n too ---
_llm_calls.clear()
_run_ideas(_text="X", n=3, content_format="short")
ok("Generate 3 distinct" in _llm_calls[0]["messages"][0]["content"],
   "short form also embeds n in 'Generate {n} distinct…'")


# --- forbidden openers stay in the short-form brief (growth control) ---
_llm_calls.clear()
_run_ideas(_text="X", content_format="short")
sp = _llm_calls[0]["messages"][0]["content"]
for banned in ("Mastering", "Deep Dive", "Optimize"):
    ok(banned in sp,
       f"short-form brief still names forbidden opener {banned!r}")


_llm_calls.clear()
_run_ideas(_text="X", content_format="long")
lp = _llm_calls[0]["messages"][0]["content"]
for banned in ("Mastering", "Deep Dive", "Complete Guide", "Introduction to"):
    ok(banned in lp,
       f"long-form brief still names forbidden opener {banned!r}")


print()
print(f"ALL {_checks} CHECKS PASSED")
