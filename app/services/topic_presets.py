"""S2 onboarding wizard: niche + topic presets (EN + PT-BR).

There is no topic catalog in CM: Generics v1 (docs/GENERICS_V1.md) makes the
niche free text (``Channel.niche``) and a topic is ``name`` + optional
``theme_prompt`` + ``content_format``. These presets are only a starting point
the wizard offers; the user can edit every field or type free text instead.

Copy: CMO CM-WIZARD-COPY.md §1 (sha df45bd814374), verbatim. Same ids in both
locales so a saved preset_id resolves in either. Nothing here is Owera-specific
(no Owera series, no OS/RR topics). Shorts only: the first draft should be the
cheapest, fastest render.
"""

from __future__ import annotations

COPY_STATUS = "CMO copy 2026-11-14"

WIZARD_TOPIC_COUNT = 3
MAX_TOPIC_NAME_CHARS = 80
MAX_THEME_PROMPT_CHARS = 400
MAX_NICHE_CHARS = 200


def _t(name: str, theme_prompt: str) -> dict:
    return {"name": name, "theme_prompt": theme_prompt, "content_format": "short"}


# Same shape as today's NICHE_PRESETS (id, label, niche, topics[3]).
# Keep ids identical across locales so a saved preset_id still resolves.
NICHE_PRESETS_EN: list[dict] = [
    {
        "id": 'cooking',
        "label": 'Cooking',
        "niche": 'Simple home cooking for everyday meals',
        "topics": [
            _t('Weeknight dinners', 'One everyday dinner, shown step by step with a short ingredient list'),
            _t('Kitchen basics', 'One basic kitchen skill beginners can use in many recipes'),
            _t('One ingredient, three ways', 'Three simple ways to use one common ingredient at home'),
        ],
    },
    {
        "id": 'fitness',
        "label": 'Fitness',
        "niche": 'Practical fitness tips for training at home or at the gym',
        "topics": [
            _t('Form check', 'How to do one common movement with safe, clear form'),
            _t('Home workouts', 'A short workout using little or no equipment'),
            _t('Recovery basics', 'One simple recovery habit after a workout'),
        ],
    },
    {
        "id": 'personal_finance',
        "label": 'Personal finance',
        "niche": 'Clear personal finance basics for everyday money decisions',
        "topics": [
            _t('Your first budget', 'How to make a simple monthly budget and follow it'),
            _t('Everyday debt', 'How one common type of debt works, in plain language'),
            _t('Saving habits', 'One small habit that helps you set money aside'),
        ],
    },
    {
        "id": 'tech_tips',
        "label": 'Tech tips',
        "niche": 'Everyday tech tips for phones, apps and home devices',
        "topics": [
            _t('Phone shortcuts', 'One useful shortcut or setting most people miss on their phone'),
            _t('Privacy basics', 'One simple way to keep an everyday account or device more private'),
            _t('Fix everyday tech', 'How to solve one common phone or computer issue, step by step'),
        ],
    },
    {
        "id": 'local_business',
        "label": 'Local business',
        "niche": 'A local business sharing useful know-how with its customers',
        "topics": [
            _t('Behind the scenes', 'How a typical job or order is done, from start to finish'),
            _t('Customer questions', 'The question customers ask most, answered clearly'),
            _t('Seasonal tips', 'What customers should know or do this time of year'),
        ],
    },
]

NICHE_PRESETS_PT: list[dict] = [
    {
        "id": 'cooking',
        "label": 'Culinária',
        "niche": 'Receitas simples para o dia a dia em casa',
        "topics": [
            _t('Jantar do dia a dia', 'Um jantar do cotidiano, mostrado passo a passo com poucos ingredientes'),
            _t('Básicos da cozinha', 'Uma técnica básica de cozinha que serve para várias receitas'),
            _t('Um ingrediente, três jeitos', 'Três jeitos simples de usar um ingrediente comum em casa'),
        ],
    },
    {
        "id": 'fitness',
        "label": 'Fitness',
        "niche": 'Dicas práticas de treino em casa ou na academia',
        "topics": [
            _t('Postura certa', 'Como fazer um movimento comum com postura clara e segura'),
            _t('Treino em casa', 'Um treino curto com pouco ou nenhum equipamento'),
            _t('Recuperação', 'Um hábito simples de recuperação depois do treino'),
        ],
    },
    {
        "id": 'personal_finance',
        "label": 'Finanças pessoais',
        "niche": 'Noções claras de finanças pessoais para o dia a dia',
        "topics": [
            _t('Seu primeiro orçamento', 'Como montar um orçamento mensal simples e acompanhá-lo'),
            _t('Dívidas no dia a dia', 'Como funciona um tipo de dívida comum, em linguagem simples'),
            _t('Hábitos de poupança', 'Um hábito pequeno que ajuda você a guardar um pouco de dinheiro'),
        ],
    },
    {
        "id": 'tech_tips',
        "label": 'Dicas de tech',
        "niche": 'Dicas de tecnologia para celular, aplicativos e aparelhos de casa',
        "topics": [
            _t('Atalhos no celular', 'Um atalho ou ajuste útil que muita gente não conhece no celular'),
            _t('Privacidade básica', 'Um jeito simples de deixar uma conta ou um aparelho mais privado'),
            _t('Consertos do dia a dia', 'Como resolver um problema comum de celular ou computador, passo a passo'),
        ],
    },
    {
        "id": 'local_business',
        "label": 'Negócio local',
        "niche": 'Um negócio local compartilhando o que sabe com seus clientes',
        "topics": [
            _t('Bastidores', 'Como um serviço ou pedido típico é feito, do começo ao fim'),
            _t('Dúvidas dos clientes', 'A pergunta que os clientes mais fazem, respondida com clareza'),
            _t('Dicas da estação', 'O que os clientes devem saber ou fazer nesta época do ano'),
        ],
    },
]

NICHE_PRESETS = NICHE_PRESETS_EN  # drop-in default (current API is EN-only)

_BY_ID_EN = {p["id"]: p for p in NICHE_PRESETS_EN}
_BY_ID_PT = {p["id"]: p for p in NICHE_PRESETS_PT}


def _locale(locale: str | None) -> str:
    return "pt" if str(locale or "").lower().startswith("pt") else "en"


def get(preset_id: str | None, locale: str | None = None) -> dict | None:
    table = _BY_ID_PT if _locale(locale) == "pt" else _BY_ID_EN
    return table.get((preset_id or "").strip())


def public_presets(locale: str | None = None) -> dict:
    """Payload for the wizard (GET /api/onboarding/wizard)."""
    loc = _locale(locale)
    niches = NICHE_PRESETS_PT if loc == "pt" else NICHE_PRESETS_EN
    return {"copy_status": COPY_STATUS, "topic_count": WIZARD_TOPIC_COUNT,
            "locale": loc, "niches": niches}
