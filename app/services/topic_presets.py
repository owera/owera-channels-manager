"""S2 onboarding wizard: niche + topic presets.

There is no topic catalog in CM: Generics v1 (docs/GENERICS_V1.md) makes the
niche free text (``Channel.niche``) and a topic is ``name`` + optional
``theme_prompt`` + ``content_format``. These presets are only a starting point
the wizard offers; the user can edit every field or type free text instead.

COPY PENDING CMO REVIEW: every label, niche line and topic below is a neutral
English placeholder written by Channels Dev, not approved copy. Nothing here is
Owera-specific (no Owera series, no OS/RR topics) so it is safe for any
instance. Replace the text, not the structure, when the CMO copy lands.
"""

from __future__ import annotations

COPY_STATUS = "copy pending CMO review"

# How many topics the wizard asks for (NEXT-CUT S2: "niche + 3 topics").
WIZARD_TOPIC_COUNT = 3
MAX_TOPIC_NAME_CHARS = 80
MAX_THEME_PROMPT_CHARS = 400
MAX_NICHE_CHARS = 200


def _t(name: str, theme_prompt: str) -> dict:
    return {"name": name, "theme_prompt": theme_prompt, "content_format": "short"}


# id -> preset. Shorts only: the first draft should be the cheapest, fastest render.
NICHE_PRESETS: list[dict] = [
    {
        "id": "how_to",
        "label": "How-to and tips",
        "niche": "Practical how-to tips for beginners in my field",
        "topics": [
            _t("Beginner mistakes", "Common mistakes beginners make and how to avoid each one"),
            _t("Quick how-to", "One small task explained step by step in under a minute"),
            _t("Myths vs facts", "A popular belief in the field, and what is actually true"),
        ],
    },
    {
        "id": "explainers",
        "label": "Explainers",
        "niche": "Short, clear explainers of ideas people find confusing",
        "topics": [
            _t("Explained simply", "One concept explained with an everyday example"),
            _t("Why it works", "The reason behind something people use every day"),
            _t("Before and after", "What changes when you understand this one idea"),
        ],
    },
    {
        "id": "product_reviews",
        "label": "Product reviews",
        "niche": "Honest, quick reviews and comparisons of everyday products",
        "topics": [
            _t("Is it worth it?", "One product, its best and worst points, and who it suits"),
            _t("Head to head", "Two similar products compared on what matters most"),
            _t("Hidden features", "Useful features most owners never find"),
        ],
    },
    {
        "id": "local_business",
        "label": "Local business",
        "niche": "A local business sharing know-how with its customers",
        "topics": [
            _t("Behind the scenes", "How a typical job or order is done, start to finish"),
            _t("Customer questions", "The question customers ask most, answered clearly"),
            _t("Seasonal tips", "What customers should know or do this time of year"),
        ],
    },
    {
        "id": "learning",
        "label": "Learning and study",
        "niche": "Study and learning techniques for students and self-learners",
        "topics": [
            _t("Study techniques", "One study method, how to do it and when it helps"),
            _t("Remember more", "A simple trick to remember what you learn"),
            _t("Focus and habits", "A small habit that makes learning easier"),
        ],
    },
]

_BY_ID = {p["id"]: p for p in NICHE_PRESETS}


def get(preset_id: str | None) -> dict | None:
    return _BY_ID.get((preset_id or "").strip())


def public_presets() -> dict:
    """Payload for the wizard (GET /api/onboarding/wizard)."""
    return {"copy_status": COPY_STATUS, "topic_count": WIZARD_TOPIC_COUNT,
            "niches": NICHE_PRESETS}
