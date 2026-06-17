"""Generate video subject ideas from a topic theme (via litellm)."""

import re

from app.config import settings


def generate_ideas(topic_name: str, theme_prompt: str | None, existing: list[str],
                   n: int = 8, content_format: str = "short") -> list[str]:
    import litellm

    avoid = "\n".join(f"- {s}" for s in existing[-60:])
    guidance = f"\nExtra guidance for this theme: {theme_prompt}" if theme_prompt else ""
    if content_format == "long":
        prompt = (
            f"Generate {n} distinct, compelling ideas for in-depth long-form YouTube videos, "
            f"all about the theme: \"{topic_name}\".{guidance}\n"
            "Each must be a clear, specific, search-friendly video title (6-15 words) covering a "
            "substantial topic worth several minutes — a deep explanation, tutorial, comparison, "
            "case study, or guide. "
            "Prefer high-CTR hook patterns: direct comparisons (X vs Y: which actually wins?), "
            "rhetorical questions with real stakes (Is X worth it or just pain?), "
            "number-driven guides (5 mistakes that…), pattern interrupts (Stop using X — do Y), "
            "or urgency/speed hooks (in 60 seconds). Avoid vague or generic titles. "
            "Do NOT repeat or closely paraphrase any of these existing titles:\n"
            f"{avoid or '(none yet)'}\n\n"
            "Return ONLY the titles, one per line, no numbering, no bullets, no commentary."
        )
    else:
        prompt = (
            f"Generate {n} distinct, engaging short-video ideas for a YouTube Shorts channel, "
            f"all about the theme: \"{topic_name}\".{guidance}\n"
            "Each must be a concise, hooky video title under 12 words, covering a specific angle "
            "of the theme (a concept, technique, pitfall, comparison, or tip). "
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
