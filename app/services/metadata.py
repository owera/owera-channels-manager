"""Generate YouTube metadata (title/description/tags) for a rendered topic.

Reuses MPT's /social-metadata endpoint (platform youtube_shorts), falling back to
litellm. Mapping mirrors channel/produce.py exactly."""

import json
import re

from app.config import settings
from app.services.mpt_client import mpt

EXTRA_TAGS = ["AI", "AI engineering", "machine learning"]


def _from_meta(subject: str, meta: dict) -> dict:
    title = (meta.get("title") or subject)[:100]
    caption = meta.get("caption", "") or ""
    hashtags = meta.get("hashtags", []) or []
    description = (caption + "\n\n" + " ".join(hashtags)).strip()
    tags = [h.lstrip("#") for h in hashtags] + EXTRA_TAGS
    return {"title": title, "description": description, "tags": tags}


def _litellm_fallback(subject: str, script: str, content_format: str = "short") -> dict:
    """Direct LLM call if the MPT endpoint is unavailable."""
    try:
        import litellm

        if content_format == "long":
            prompt = (
                "You are a YouTube copywriter for in-depth long-form videos. For the video "
                "below return a single minified JSON object with keys title (<=100 chars, "
                "clear and search-friendly), caption (<=800 chars, a substantive 2-3 sentence "
                "summary then a call to action, no hashtags inside), hashtags (array of 5 "
                "strings each starting with #). No commentary.\n\n"
                f"Subject: {subject}\n\nScript: {script[:4000]}"
            )
        else:
            prompt = (
                "You are a YouTube Shorts copywriter. For the video below return a single "
                "minified JSON object with keys title (<=100 chars, hooky), caption (<=400 chars, "
                "ends with a call to action, no hashtags inside), hashtags (array of 3 strings "
                "each starting with #). No commentary.\n\n"
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


def generate(subject: str, script: str, content_format: str = "short") -> dict:
    platform = "youtube" if content_format == "long" else "youtube_shorts"
    meta = mpt.social_metadata(subject, script or "", platform=platform, language="en-US")
    if meta:
        return _from_meta(subject, meta)
    return _litellm_fallback(subject, script or "", content_format)
