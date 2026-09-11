"""Generate YouTube metadata (title/description/tags) for a rendered topic.

Reuses MPT's /social-metadata endpoint (platform youtube_shorts), falling back to
the local Grok CLI (`grok -p`). Mapping mirrors channel/produce.py exactly.

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

from app.services.llm import GrokCLIError, complete
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
        "chapters": "⏱ Capítulos:",
    },
    "en": {
        "subscribe": "🔔 Subscribe for daily hands-on AI engineering:",
        "playlist": "▶ Full series:",
        "chapters": "⏱ Chapters:",
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
                         channel_yt_id: str | None, playlist_yt_id: str | None,
                         chapter_lines: list[str] | None = None) -> str:
    """Append the chapters list (long-form, when derivable) and the subscribe-CTA +
    links block (localized). Called at publish time. Idempotent: if a block is
    already present (publish retry), it is not appended again."""
    base = (description or "").strip()
    if _SUB_CONFIRM_MARKER in base:
        return base
    lines = _CTA_LINES.get((language_code or "en").split("-")[0].lower(), _CTA_LINES["en"])
    segments = []
    if chapter_lines and lines["chapters"] not in base:
        segments.append(lines["chapters"] + "\n" + "\n".join(chapter_lines))
    block = []
    if channel_yt_id:
        block.append(f"{lines['subscribe']} "
                     f"https://www.youtube.com/channel/{channel_yt_id}?{_SUB_CONFIRM_MARKER}")
    if playlist_yt_id:
        block.append(f"{lines['playlist']} "
                     f"https://www.youtube.com/playlist?list={playlist_yt_id}")
    if block:
        segments.append("\n".join(block))
    if not segments:
        return base
    out = (base + "\n\n" + "\n\n".join(segments)).strip()
    return out[:5000]


def _llm_fallback(subject: str, script: str, content_format: str = "short",
                  language: str | None = None) -> dict:
    """Direct LLM call if the MPT endpoint is unavailable."""
    lang_rule = (f" HARD RULE: write the title, caption, and hashtags in {language}."
                 if language else "")
    try:
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
                "minified JSON object with keys title (<=100 chars), caption (<=400 chars: "
                "the FIRST sentence must be keyword-rich — what a developer would type into "
                "search — no Follow/Siga/waitlist/Cloud/SMY/Instagram/LinkedIn CTA, no hashtags inside), hashtags "
                f"(array of 3 strings each starting with #). DECOLAR: the title MUST equal "
                "the first spoken sentence of the script (the title hook) — same claim, no "
                "second slogan, no curiosity gap. If the subject already contains "
                "'· <series> <nn>', the title MUST be that subject verbatim (keep the suffix). "
                f"No commentary.{lang_rule}\n\n"
                f"Subject: {subject}\n\nScript: {script[:2000]}"
            )
        text = complete(prompt) or ""
        text = re.sub(r"^```[a-zA-Z0-9]*\s*|\s*```$", "", text.strip())
        data = json.loads(text)
        return _from_meta(subject, data)
    except GrokCLIError:
        # OIDC expiry / grok -p failure: do not paper over with heuristic titles
        # and do not fall back to Anthropic/LiteLLM/XAI_API_KEY.
        raise
    except Exception:
        # Last-resort heuristic so unparseable model JSON still reaches review.
        return {
            "title": subject[:100],
            "description": subject,
            "tags": EXTRA_TAGS,
        }


def _lock_decolar_title(meta: dict, subject: str, script: str,
                        content_format: str) -> dict:
    """Decolar: the YouTube title IS the spoken title hook.

    Patterned subjects (`· <series> <nn>`) win verbatim so the publish gate
    cannot strand a good render in review (v1249). Otherwise a drifted LLM
    slogan is replaced by the first spoken sentence.
    """
    from app.services import craft
    if (content_format or "short") == "long":
        return meta
    subj = (subject or "").strip()
    if craft.spoken_title_ok(subj):
        meta["title"] = subj[:100]
        return meta
    hook = craft.first_spoken_sentence(script) or subj
    if not hook:
        return meta
    title = (meta.get("title") or "").strip()
    if _is_title_echo(title, hook):
        return meta
    meta["title"] = hook[:100]
    return meta


def _is_title_echo(title: str, hook: str) -> bool:
    """True only when title is the spoken hook or a compression of it.

    ``craft.claim_aligned`` is too loose here (two shared content words like
    RAG+slow would keep a second slogan). A title echo must be the hook, a
    substring of it, or a compression whose content words are all in the hook.
    """
    from app.services.engines import theme
    from app.services import craft
    h = theme.fold(title or "")
    s = theme.fold(hook or "")
    if not h or not s:
        return False
    if h == s or h in s or s.startswith(h) or s in h:
        return True
    hw = [w for w in h.split() if w not in craft.STOPWORDS]
    sw = {w for w in s.split() if w not in craft.STOPWORDS}
    return bool(hw) and all(w in sw for w in hw)


def generate(subject: str, script: str, content_format: str = "short",
             language: str | None = None) -> dict:
    """language is the channel's spoken-language name (e.g. 'Brazilian Portuguese')
    from video_gen.channel_language; None falls back to en-US (legacy behavior)."""
    platform = "youtube" if content_format == "long" else "youtube_shorts"
    mpt_language = _LANGUAGE_MPT_CODES.get(language or "", "en-US")
    meta = mpt.social_metadata(subject, script or "", platform=platform, language=mpt_language)
    if meta:
        out = _sanitize_meta(_from_meta(subject, meta))
    else:
        out = _sanitize_meta(_llm_fallback(subject, script or "", content_format, language))
    return _lock_decolar_title(out, subject, script or "", content_format)


def _sanitize_meta(meta: dict) -> dict:
    from app.services import craft

    def _copy(s):
        raw = s or ""
        out = craft.strip_banned(raw) or raw
        # Title / caption body are not the endcard. Strip Subscribe CTAs here;
        # finalize_description still appends the YT subscribe *link* at publish.
        if craft.contains_subscribe_cta(out) and not craft.is_endcard_vo(out):
            out = craft.strip_subscribe_cta(out)
        return out

    meta["title"] = _copy(meta.get("title"))
    meta["description"] = _copy(meta.get("description"))
    return meta
