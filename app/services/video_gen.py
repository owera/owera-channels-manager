"""Generate video subject ideas from a topic theme (via grok -p)."""

import logging
import re

from app.config import settings
from app.services.llm import complete

logger = logging.getLogger(__name__)

# The channel's spoken language lives implicitly in its render-profile voice id
# (e.g. "pt-BR-AntonioNeural"). Idea/script prompts must state it explicitly:
# an LLM otherwise infers language from the topic/title text and can drift into
# English on a Portuguese channel (three EN videos shipped to ch2 on 2026-07-07).
_VOICE_LANGUAGES = {
    "pt": "Brazilian Portuguese",
    "en": "English",
    "es": "Spanish",
}

# BCP-47 codes for YouTube metadata (defaultLanguage/defaultAudioLanguage) and the
# MPT /social-metadata language parameter. Keyed by the same voice-id prefix.
LANGUAGE_CODES = {
    "pt": "pt-BR",
    "en": "en-US",
    "es": "es-ES",
}

# Grok -p sometimes prefixes an idea with catalog-planning CoT and glues the
# real title on with no space (09-23 drafts 1321 / 1325: "Vou conferir o
# catálogo….O agente… · Claude Code 10"; 09-24 draft 1336: "O catálogo que
# você colou já passa de IA 172. Vou conferir o próximo número….Texto…").
# Drop those leading sentences. A line that is only planning is discarded —
# unlike script preamble, there is no word-count retry that needs the
# original text kept.
_IDEA_COT_START = re.compile(
    r"^(?:"
    r"vou conferir o cat[aá]logo|"
    r"vou conferir o pr[oó]ximo n[uú]mero|"
    r"o cat[aá]logo que voc[eê] colou j[aá] passa|"
    r"conferindo a contagem|"
    r"the hook has to|"
    r"the first line has to|"
    r"i'll check how|"
    r"checking the workspace|"
    r"the script has to|"
    r"the tests pin|"
    r"the title maps"
    r")\b",
    re.IGNORECASE,
)


# List markers only ("1. ", "2) ", "- ", "* "). A repeated class of digits,
# dots, and spaces also ate legitimate openers (2026-09-27 review 1347 / 1353 /
# 1355: "16GB rodou…" → "GB rodou…", "32B em Q4…" → "B em Q4…") because model
# sizes and memory figures start with a digit. Require a marker punctuation
# (or a bullet) plus whitespace so "32B", "16GB", and "1.5x" stay intact.
_LIST_MARKER = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


def _strip_list_marker(line: str) -> str:
    """Drop one leading list marker. Leave a title that starts with a number."""
    return _LIST_MARKER.sub("", line, count=1).strip().strip('"')


def _strip_idea_cot(title: str) -> str | None:
    """Return the title with leading idea-planning sentences removed.

    None means the line was only planning and must not become a video subject.
    """
    raw = (title or "").strip()
    if not raw:
        return None
    parts = [
        p.strip()
        for p in re.split(r"(?<=[.!?…])(?=\s|[A-ZÁÉÍÓÚÃÕÂÊÔ])", raw)
        if p.strip()
    ]
    if not parts:
        return None
    i = 0
    while i < len(parts) and _IDEA_COT_START.match(parts[i]):
        i += 1
    if i == 0:
        return raw
    if i >= len(parts):
        return None
    kept = " ".join(parts[i:]).strip()
    # #1406 (2026-10-02): "Conferindo a contagem… o 14B fez 62 tok/s. Sem,
    # 28. · Local 69" kept the lowercase tail of a sentence whose head was in
    # the planning text. A remainder that starts mid-sentence is no title.
    if kept and is_title_fragment(kept):
        return None
    return kept or None


def is_title_fragment(title: str | None) -> bool:
    """True for a title that starts mid-sentence (first letter lowercase,
    e.g. "o 14B fez 62 tok/s…") or whose head ends on a dangling , : ; —."""
    from app.services.subject_guard import subject_guard_reason

    head = (title or "").split("·", 1)[0].strip()
    if not head:
        return True
    # same start-of-subject shape rules as the pre-produce guard (lowercase
    # first word unless an allowlisted/code-ish literal, bare unit, leading
    # punctuation), applied when the idea is born instead of at produce time
    if subject_guard_reason(head) and not re.search(r"(R\$|US\$|\$|€|£)\s*\d", head):
        return True
    return head[-1:] in (",", ":", ";", "—", "–", "-")


def language_from_voice(voice_name: str | None) -> str | None:
    """Map a voice id like 'pt-BR-AntonioNeural[-Male]' to a language name for prompts."""
    if not voice_name:
        return None
    return _VOICE_LANGUAGES.get(voice_name.split("-", 1)[0].lower())


def code_from_voice(voice_name: str | None) -> str | None:
    """Map a voice id to a BCP-47 code ('pt-BR-Antonio…' -> 'pt-BR')."""
    if not voice_name:
        return None
    return LANGUAGE_CODES.get(voice_name.split("-", 1)[0].lower())


def channel_language(session, channel_id: int | None) -> str | None:
    """Language of a channel's default render-profile voice (None if unknown)."""
    import json

    from app.models import Channel, RenderProfile

    ch = session.get(Channel, channel_id) if channel_id else None
    if not ch or not ch.default_render_profile_id:
        return None
    profile = session.get(RenderProfile, ch.default_render_profile_id)
    if not profile:
        return None
    try:
        voice = json.loads(profile.params_json or "{}").get("voice_name")
    except ValueError:
        return None
    return language_from_voice(voice)


def channel_voice(session, channel_id: int | None) -> str | None:
    """A channel's default render-profile voice name (None if unknown)."""
    import json

    from app.models import Channel, RenderProfile

    ch = session.get(Channel, channel_id) if channel_id else None
    if not ch or not ch.default_render_profile_id:
        return None
    profile = session.get(RenderProfile, ch.default_render_profile_id)
    if not profile:
        return None
    try:
        return json.loads(profile.params_json or "{}").get("voice_name") or None
    except ValueError:
        return None


# Series numbering (P0 2026-10-03): the LLM picks "· <Series> NN" itself and
# re-used numbers (#1400–#1402 "Shipping 3/4/5" while 1–8 were already used).
# New ideas continue from the highest number used in that series on the
# channel, across every status (rejected/failed numbers stay burnt).
_SERIES_SUFFIX_RE = re.compile(r"·\s*([^·\d][^·]*?)\s+(\d{1,4})\s*$")


def series_numbers_used(subjects) -> dict[str, int]:
    """{series (casefolded): max number} over subjects/titles."""
    used: dict[str, int] = {}
    for s in subjects or []:
        m = _SERIES_SUFFIX_RE.search(str(s or "").strip())
        if m:
            key = m.group(1).strip().casefold()
            used[key] = max(used.get(key, 0), int(m.group(2)))
    return used


def renumber_series(ideas: list[str], used_subjects) -> list[str]:
    """Rewrite each idea's "· <Series> NN" to max-used + 1, in order."""
    used = series_numbers_used(used_subjects)
    out = []
    for idea in ideas or []:
        m = _SERIES_SUFFIX_RE.search(idea or "")
        if not m:
            out.append(idea)
            continue
        key = m.group(1).strip().casefold()
        nxt = used.get(key, 0) + 1
        used[key] = nxt
        out.append(f"{idea[:m.start(2)]}{nxt:0{len(m.group(2))}d}{idea[m.end(2):]}")
    return out


def channel_series_subjects(session, channel_id: int | None) -> list[str]:
    """Every subject and title on the channel (all statuses)."""
    from sqlmodel import select

    from app.models import Video

    if not channel_id:
        return []
    rows = session.exec(select(Video.subject, Video.title)
                        .where(Video.channel_id == channel_id)).all()
    return [x for r in rows for x in r if x]


# RR draft-time hook pace (P0 2026-10-03, #47): the title head IS the spoken
# claim (worker._lock_patterned_opener), so an RR short idea is born passing
# only if the head is ≤8 words and spoken by HOOK_GEN_SPOKEN_BY_S with the
# real voice (edge-tts, pt-BR rate -8% / pitch -2Hz, normalized). Ideas that
# miss are regenerated once with the measurements as feedback, then dropped.
HOOK_GEN_SPOKEN_BY_S = 2.8


def measure_claim(head: str, voice: str) -> float | None:
    """Real edge-tts spoken end (s) of ``head`` for ``voice`` (None: no TTS)."""
    import tempfile
    from pathlib import Path

    from app.services import craft
    from app.services.engines import worker

    with tempfile.TemporaryDirectory() as d:
        words = worker._tts(head, voice, Path(d) / "claim.mp3")
    return craft.claim_spoken_end(head, words)


def rr_hook_reason(title: str, voice: str | None, measure=None) -> str | None:
    """Why an RR short title would fail hook pace, or None when it passes."""
    from app.services import craft

    head = (title or "").split("·", 1)[0].strip()
    n = craft.claim_word_count(head)
    if n > craft.HOOK_CLAIM_MAX_WORDS:
        return f"{n} words (max {craft.HOOK_CLAIM_MAX_WORDS})"
    if not voice:
        return None
    try:
        end = (measure or measure_claim)(head, voice)
    except Exception as e:  # TTS outage: word count only, never block the refill
        logger.info("hook pace TTS check skipped for %r: %s", head, e)
        return None
    if end is not None and end > HOOK_GEN_SPOKEN_BY_S + 1e-6:
        return f"spoken by {end:.2f}s (max {HOOK_GEN_SPOKEN_BY_S:.1f}s)"
    return None


def idea_hook_kwargs(session, channel_id: int | None, content_format: str | None) -> dict:
    """{"hook_voice": voice} when the channel's new ideas need the RR
    draft-time hook check (RR shorts only), else {}."""
    from app.models import Channel
    from app.services import craft
    from app.services.engines import theme

    if (content_format or "short") == "long" or not channel_id:
        return {}
    ch = session.get(Channel, channel_id)
    brand = theme.infer_brand(channel_id, getattr(ch, "slug", None), getattr(ch, "name", None))
    if brand not in craft.HOOK_PACE_BRANDS:
        return {}
    return {"hook_voice": channel_voice(session, channel_id)}


def channel_language_code(session, channel_id: int | None) -> str | None:
    """BCP-47 code of a channel's default render-profile voice (None if unknown)."""
    import json

    from app.models import Channel, RenderProfile

    ch = session.get(Channel, channel_id) if channel_id else None
    if not ch or not ch.default_render_profile_id:
        return None
    profile = session.get(RenderProfile, ch.default_render_profile_id)
    if not profile:
        return None
    try:
        voice = json.loads(profile.params_json or "{}").get("voice_name")
    except ValueError:
        return None
    return code_from_voice(voice)


# 2026-09-22 Rodrigo killed the billing-AMOUNT hook formula (a billing verb +
# a money value as the hook: charged/billed/cobrou/"Prod added" + amount, or an
# idea that opens on a money value). Billing WORDS alone (billing, coupon,
# invoice, charge, plan, card) stay allowed. 2026-09-28 (CoS item 6) titles may
# not carry any currency value at all — the publish craft gate rejects them —
# so ideas with one are dropped here instead of becoming unproducible drafts.
# Keep this text free of literal money examples: a negative example still primes.
NO_BILLING_AMOUNT_HOOK = (
    "HOOK RULE: never use a charged/billed amount as the hook, and never put a money "
    "value (a currency symbol followed by a number) anywhere in the title. Lead with a "
    "useful spoken claim plus a concrete noun (the tool, file, command, model, flag, "
    "field, or setting) — the same rule as the Shipping and Agent traps series. "
    "Mentioning billing, a coupon, an invoice or a plan is fine without an amount."
)

# OS "Agent memory" series (item 9, 2026-09-28): five straight billing/price
# scenarios (EUR/USD, SAVE20 coupon, annual vs monthly, warranty, card) read as
# clones. Keep the "Chat did X. Prod did Y." template, spread the domains, cap
# billing/price at 1 in 5. Applies to NEW ideas only — never rewrites drafts.
AGENT_MEMORY_SERIES_RE = re.compile(r"agent\s+memory", re.IGNORECASE)
AGENT_MEMORY_DOMAINS = (
    "calendar/scheduling", "shipping address", "permission/access",
    "language/locale", "user preference", "order status", "timezone",
    "notifications", "file edits", "retries/rate limits", "environment (staging vs prod)",
    "identity/account", "search filters", "data retention/deletion",
)
AGENT_MEMORY_SPREAD = (
    "SCENARIO SPREAD (Agent memory): keep the title template exactly "
    "'Chat did X. Prod did Y.' (two short sentences, then the · Agent memory nn "
    "suffix). Spread the scenarios across different domains — "
    + ", ".join(AGENT_MEMORY_DOMAINS) + " — one domain per idea, no two in a row "
    "from the same domain. At most 1 in 5 ideas may be about billing/price "
    "(currency, coupons, plans, invoices, refunds, cards, warranties); the rest "
    "must be non-billing."
)
BILLING_WINDOW = 5          # sliding window for the 1-in-5 billing cap
_BILLING_THEME_RE = re.compile(
    r"\b(?:bill(?:ed|ing|s)?|charg(?:e|ed|es|ing)|pric(?:e|ed|es|ing)|invoic(?:e|ed|es)|"
    r"coupon|discount|refund(?:ed|s)?|warranty|subscription|checkout|payment|paid|pay|"
    r"card|annual|monthly|usd|eur|brl|tax|fee|cobr(?:ou|a|ar|an[çc]a)|pre[çc]o|fatura|"
    r"cupom|reembolso|assinatura|cart[aã]o|plano|mensal|anual)\b|[$€£]",
    re.IGNORECASE,
)


def is_billing_themed(text: str | None) -> bool:
    """Cheap keyword classifier: is this idea about billing/price?"""
    return bool(_BILLING_THEME_RE.search(text or ""))


def _is_agent_memory(topic_name: str | None, theme_prompt: str | None) -> bool:
    return bool(AGENT_MEMORY_SERIES_RE.search(f"{topic_name or ''}\n{theme_prompt or ''}"))


def _cap_billing(ideas: list[str], existing: list[str], window: int = BILLING_WINDOW) -> list[str]:
    """Keep at most 1 billing-themed idea in any `window` consecutive subjects,
    counting the tail of `existing` (the topic's recent subjects) before the new
    batch. Over-cap billing ideas are dropped (no extra LLM call); the autofill
    loop's next tick asks again for the shortfall."""
    tail = [bool(is_billing_themed(x)) for x in existing[-(window - 1):]] if window > 1 else []
    out: list[str] = []
    for idea in ideas:
        billing = is_billing_themed(idea)
        if billing and any(tail[-(window - 1):]):
            continue
        out.append(idea)
        tail.append(billing)
    return out


def enforce_hook_pace(ideas: list[str], n: int, *, hook_voice: str | None,
                      regen=None, measure=None) -> list[str]:
    """RR shorts: keep only ideas whose head passes rr_hook_reason (≤8 words,
    spoken ≤2.8s with ``hook_voice``). Misses trigger ONE ``regen(feedback)``
    call (→ list of titles) whose passing ideas top the batch back up."""
    good, bad = [], []
    for t in ideas or []:
        r = rr_hook_reason(t, hook_voice, measure)
        (bad if r else good).append((t, r))
    if bad and regen is not None and len(good) < n:
        fb = "; ".join(f'"{t.split("·", 1)[0].strip()}" = {r}' for t, r in bad[:6])
        try:
            retry = regen(
                "\nHOOK PACE (hard): the title before '·' is the spoken opening claim — "
                "max 8 words and spoken in under 2.8 seconds (one short sentence, "
                f"no second sentence). These failed: {fb}. Write shorter ones.") or []
        except Exception as e:
            logger.info("hook pace regeneration failed: %s", e)
            retry = []
        have = {t.lower() for t, _ in good + bad}
        for t in retry:
            if len(good) >= n:
                break
            if t.lower() in have:
                continue
            if not rr_hook_reason(t, hook_voice, measure):
                good.append((t, None))
                have.add(t.lower())
    if bad:
        logger.info("hook pace: dropped %d idea(s): %s", len(bad),
                    "; ".join(f"{t!r} {r}" for t, r in bad))
    return [t for t, _ in good][:max(0, n)]


def generate_ideas(topic_name: str, theme_prompt: str | None, existing: list[str],
                   n: int = 8, content_format: str = "short",
                   language: str | None = None) -> list[str]:

    avoid = "\n".join(f"- {s}" for s in existing[-60:])
    guidance = f"\nExtra guidance for this theme: {theme_prompt}" if theme_prompt else ""
    lang_rule = (f"\nHARD RULE: write every title in {language} — the channel publishes "
                 f"exclusively in {language}, whatever language the theme name is in." if language else "")
    from app.services.craft import (CRAFT_RULES_SHORT, contains_banned,
                                    contains_subscribe_cta, currency_in_text)
    craft_rule = f"\n{CRAFT_RULES_SHORT}\n{NO_BILLING_AMOUNT_HOOK}"
    agent_memory = _is_agent_memory(topic_name, theme_prompt)
    if agent_memory:
        craft_rule += f"\n{AGENT_MEMORY_SPREAD}"
    if content_format == "long":
        prompt = (
            f"Generate {n} distinct, compelling ideas for in-depth long-form YouTube videos, "
            f"all about the theme: \"{topic_name}\".{guidance}{lang_rule}{craft_rule}\n"
            "Each must be a clear, specific, search-friendly video title (6-15 words) covering a "
            "substantial topic worth several minutes. "
            "RULE: lead with a question, tension, or situation the viewer already feels — NOT the "
            "solution. The viewer clicks because they recognise their own problem, not because they "
            "want a feature explained. "
            "Top patterns: rhetorical question with real stakes (Is X Worth It or Just Pain?), "
            "root-cause reveal (Why Your X Fails in Production — and the Fix), "
            "direct comparison with honest verdict (X vs Y: Which Actually Wins in Production?), "
            "number-driven discovery (5 Mistakes That Break Your X), "
            "pattern interrupt (Stop Using X — Here's Why Y Wins Instead), "
            "brutal-truth reveal (The Truth Nobody Tells You About X — Until It Breaks in Prod). "
            "Use specific numbers, time durations, and concrete stakes when they fit naturally "
            "— they signal credibility and hold attention through a longer video. "
            "AVOID as title openers: 'Mastering', 'Deep Dive', 'Complete Guide', 'Optimize', "
            "'Introduction to' — these bury the emotional hook and suppress clicks. "
            "Do NOT repeat or closely paraphrase any of these existing titles:\n"
            f"{avoid or '(none yet)'}\n\n"
            "Return ONLY the titles, one per line, no numbering, no bullets, no commentary."
        )
    else:
        prompt = (
            f"Generate {n} distinct, engaging short-video ideas for a YouTube Shorts channel, "
            f"all about the theme: \"{topic_name}\".{guidance}{lang_rule}{craft_rule}\n"
            "Each must be a concise, hooky video title under 12 words, covering a specific angle "
            "of the theme. "
            "RULE: lead with the viewer's situation or mistake — NOT the solution. The hook works "
            "when a viewer thinks 'that's exactly what's happening to me' within 2 seconds. "
            "Top patterns: root-cause curiosity (Why Your X Keeps [bad outcome]), "
            "behavior interrupt (Stop Doing X — Do Y Instead), relatable setup "
            "(Your AI Forgets Everything Because You're Missing This), "
            "demystification (X Is Not Magic — It's Just Y), speed hook (X in 60 Seconds), "
            "visceral consequence (X Ate My [concrete loss] in [timeframe] — Here's Why), "
            "brutal-truth reveal (The X Nobody Tells You About Y). "
            "Use specific numbers and concrete stakes when they fit naturally (time durations, "
            "sizes, counts, measurable outcomes) — they signal credibility and magnify the hook. "
            "AVOID as openers: 'Mastering', 'Deep Dive', 'Optimize', 'Cut X%' — they attract "
            "no one who isn't already convinced. Avoid vague or generic titles. "
            "Do NOT repeat or closely paraphrase any of these existing titles:\n"
            f"{avoid or '(none yet)'}\n\n"
            "Return ONLY the titles, one per line, no numbering, no bullets, no commentary."
        )
    text = complete(prompt, timeout=int(settings.grok_timeout_seconds_light)) or ""

    seen = {s.lower() for s in existing}
    out: list[str] = []
    for line in text.splitlines():
        title = _strip_list_marker(line)
        title = _strip_idea_cot(title) or ""
        if not title or title.lower() in seen:
            continue
        if is_title_fragment(title):
            continue  # #1406: a sentence tail is not a title
        if contains_banned(title) or contains_subscribe_cta(title):
            continue
        if currency_in_text(title):
            continue  # publish gate rejects currency in titles (item 6)
        seen.add(title.lower())
        out.append(title)
    if agent_memory:
        out = _cap_billing(out, list(existing))
    return out[:max(0, n)]  # the model often returns more lines than asked
