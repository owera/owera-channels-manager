"""Generate video subject ideas from a topic theme (via litellm)."""

import re

from app.config import settings

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


def generate_ideas(topic_name: str, theme_prompt: str | None, existing: list[str],
                   n: int = 8, content_format: str = "short",
                   language: str | None = None) -> list[str]:
    import litellm

    avoid = "\n".join(f"- {s}" for s in existing[-60:])
    guidance = f"\nExtra guidance for this theme: {theme_prompt}" if theme_prompt else ""
    lang_rule = (f"\nHARD RULE: write every title in {language} — the channel publishes "
                 f"exclusively in {language}, whatever language the theme name is in." if language else "")
    if content_format == "long":
        prompt = (
            f"Generate {n} distinct, compelling ideas for in-depth long-form YouTube videos, "
            f"all about the theme: \"{topic_name}\".{guidance}{lang_rule}\n"
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
            f"all about the theme: \"{topic_name}\".{guidance}{lang_rule}\n"
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
            "Use specific numbers and concrete stakes when they fit naturally (dollar amounts, "
            "time durations, measurable outcomes) — they signal credibility and magnify the hook. "
            "AVOID as openers: 'Mastering', 'Deep Dive', 'Optimize', 'Cut X%' — they attract "
            "no one who isn't already convinced. Avoid vague or generic titles. "
            "Do NOT repeat or closely paraphrase any of these existing titles:\n"
            f"{avoid or '(none yet)'}\n\n"
            "Return ONLY the titles, one per line, no numbering, no bullets, no commentary."
        )
    resp = litellm.completion(
        model=settings.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        drop_params=True,
    )
    text = resp.choices[0].message.content or ""

    seen = {s.lower() for s in existing}
    out: list[str] = []
    for line in text.splitlines():
        title = re.sub(r"^\s*[-*\d.)\s]+", "", line).strip().strip('"')
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        out.append(title)
    return out
