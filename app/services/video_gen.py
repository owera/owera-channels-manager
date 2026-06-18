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
            "substantial topic worth several minutes. "
            "RULE: lead with a question, tension, or situation the viewer already feels — NOT the "
            "solution. The viewer clicks because they recognise their own problem, not because they "
            "want a feature explained. "
            "Top patterns: rhetorical question with real stakes (Is X Worth It or Just Pain?), "
            "root-cause reveal (Why Your X Fails in Production — and the Fix), "
            "direct comparison with honest verdict (X vs Y: Which Actually Wins in Production?), "
            "number-driven discovery (5 Mistakes That Break Your X), "
            "pattern interrupt (Stop Using X — Here's Why Y Wins Instead). "
            "AVOID as title openers: 'Mastering', 'Deep Dive', 'Complete Guide', 'Optimize', "
            "'Introduction to' — these bury the emotional hook and suppress clicks. "
            "Do NOT repeat or closely paraphrase any of these existing titles:\n"
            f"{avoid or '(none yet)'}\n\n"
            "Return ONLY the titles, one per line, no numbering, no bullets, no commentary."
        )
    else:
        prompt = (
            f"Generate {n} distinct, engaging short-video ideas for a YouTube Shorts channel, "
            f"all about the theme: \"{topic_name}\".{guidance}\n"
            "Each must be a concise, hooky video title under 12 words, covering a specific angle "
            "of the theme. "
            "RULE: lead with the viewer's situation or mistake — NOT the solution. The hook works "
            "when a viewer thinks 'that's exactly what's happening to me' within 2 seconds. "
            "Top patterns: root-cause curiosity (Why Your X Keeps [bad outcome]), "
            "behavior interrupt (Stop Doing X — Do Y Instead), relatable setup "
            "(Your AI Forgets Everything Because You're Missing This), "
            "demystification (X Is Not Magic — It's Just Y), speed hook (X in 60 Seconds). "
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
