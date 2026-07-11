"""Generate YouTube metadata (title/description/tags) for a rendered topic.

Reuses MPT's /social-metadata endpoint (platform youtube_shorts), falling back to
litellm. Mapping mirrors channel/produce.py exactly.

Language: callers pass the channel's spoken language (from its render-profile voice,
`video_gen.channel_language`) so a PT-BR channel gets native PT-BR titles/descriptions/
tags instead of the historical hardcoded en-US — this is what lets YouTube match the
video to the right language audience.

`finalize_description` appends the subscribe-CTA + playlist/channel links block at
publish time (when the playlist is guaranteed to exist). Idempotent, so publish
retries never double-append.
"""

import json
import re

from app.config import settings
from app.services.mpt_client import mpt

EXTRA_TAGS = ["AI", "AI engineering", "machine learning"]

# MPT /social-metadata wants a BCP-47-ish code; prompts want the language name.
_LANGUAGE_MPT_CODES = {
    "Brazilian Portuguese": "pt-BR",
    "English": "en-US",
    "Spanish": "es-ES",
}

# Localized subscribe-CTA blocks appended to descriptions at publish time.
# Keyed by BCP-47 prefix; en is the fallback.
_CTA_LINES = {
    "pt": {
        "subscribe": "🔔 Inscreva-se — engenharia de IA na prática, todos os dias:",
        "playlist": "▶ Série completa:",
    },
    "en": {
        "subscribe": "🔔 Subscribe for daily hands-on AI engineering:",
        "playlist": "▶ Full series:",
    },
}

_SUB_CONFIRM_MARKER = "sub_confirmation=1"


def _from_meta(subject: str, meta: dict) -> dict:
    title = (meta.get("title") or subject)[:100]
    caption = meta.get("caption", "") or ""
    hashtags = meta.get("hashtags", []) or []
    description = (caption + "\n\n" + " ".join(hashtags)).strip()
    tags = [h.lstrip("#") for h in hashtags] + EXTRA_TAGS
    return {"title": title, "description": description, "tags": tags}


def finalize_description(description: str, language_code: str | None,
                         channel_yt_id: str | None, playlist_yt_id: str | None) -> str:
    """Append the subscribe-CTA + links block (localized). Called at publish time.
    Idempotent: if the block is already present (publish retry), returns unchanged."""
    base = (description or "").strip()
    if _SUB_CONFIRM_MARKER in base:
        return base
    lines = _CTA_LINES.get((language_code or "en").split("-")[0].lower(), _CTA_LINES["en"])
    block = []
    if channel_yt_id:
        block.append(f"{lines['subscribe']} "
                     f"https://www.youtube.com/channel/{channel_yt_id}?{_SUB_CONFIRM_MARKER}")
    if playlist_yt_id:
        block.append(f"{lines['playlist']} "
                     f"https://www.youtube.com/playlist?list={playlist_yt_id}")
    if not block:
        return base
    out = (base + "\n\n" + "\n".join(block)).strip()
    return out[:5000]


def _litellm_fallback(subject: str, script: str, content_format: str = "short",
                      language: str | None = None) -> dict:
    """Direct LLM call if the MPT endpoint is unavailable."""
    lang_rule = (f" HARD RULE: write the title, caption, and hashtags in {language}."
                 if language else "")
    try:
        import litellm

        if content_format == "long":
            prompt = (
                "You are a YouTube copywriter for in-depth long-form videos. For the video "
                "below return a single minified JSON object with keys title (<=100 chars, "
                "clear and search-friendly), caption (<=800 chars: the FIRST sentence must be "
                "keyword-rich — the exact phrase a developer would type into YouTube search — "
                "then a substantive 2-3 sentence summary, no hashtags inside), hashtags (array "
                f"of 5 strings each starting with #). No commentary.{lang_rule}\n\n"
                f"Subject: {subject}\n\nScript: {script[:4000]}"
            )
        else:
            prompt = (
                "You are a YouTube Shorts copywriter. For the video below return a single "
                "minified JSON object with keys title (<=100 chars, hooky), caption (<=400 chars: "
                "the FIRST sentence must be keyword-rich — what a developer would type into "
                "search — and it ends with a call to action, no hashtags inside), hashtags "
                f"(array of 3 strings each starting with #). No commentary.{lang_rule}\n\n"
                f"Subject: {subject}\n\nScript: {script[:2000]}"
            )
        resp = litellm.completion(
            model=settings.litellm_model,
            messages=[{"role": "user", "content": prompt}],
            drop_params=True,
        )
        text = resp.choices[0].message.content or ""
        text = re.sub(r"^```[a-zA-Z0-9]*\s*|\s*```$", "", text.strip())
        data = json.loads(text)
        return _from_meta(subject, data)
    except Exception:
        # Last-resort heuristic so the topic still reaches review.
        return {
            "title": subject[:100],
            "description": subject,
            "tags": EXTRA_TAGS,
        }


def generate(subject: str, script: str, content_format: str = "short",
             language: str | None = None) -> dict:
    """language is the channel's spoken-language name (e.g. 'Brazilian Portuguese')
    from video_gen.channel_language; None falls back to en-US (legacy behavior)."""
    platform = "youtube" if content_format == "long" else "youtube_shorts"
    mpt_language = _LANGUAGE_MPT_CODES.get(language or "", "en-US")
    meta = mpt.social_metadata(subject, script or "", platform=platform, language=mpt_language)
    if meta:
        return _from_meta(subject, meta)
    return _litellm_fallback(subject, script or "", content_format, language)
