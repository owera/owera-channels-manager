"""Generate video subject ideas from a topic theme (via litellm)."""

import re

from app.config import settings


def generate_ideas(topic_name: str, theme_prompt: str | None, existing: list[str],
                   n: int = 8) -> list[str]:
    import litellm

    avoid = "\n".join(f"- {s}" for s in existing[-60:])
    guidance = f"\nExtra guidance for this theme: {theme_prompt}" if theme_prompt else ""
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
