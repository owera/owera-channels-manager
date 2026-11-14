"""S2 — the single Terms + Privacy checkbox (LegalOps FIX4 via CMO, sha 6a389cd4d636, 14/11).

One box: the user ACCEPTS the Terms of Service and HAS READ the Privacy Notice
(a notice, not something accepted). Texts are LegalOps', verbatim;
the SPA renders the same strings (``frontend/src/termsConsent.ts``) with links on
"Terms of Service" / "Termos de Serviço" and "Privacy Notice" / "Aviso de
Privacidade", and sends the sha256 of the exact sentence shown + its locale.

On accept the wizard keeps the existing ``terms_accepted_at`` +
``privacy_accepted_at`` and stores ``terms_accepted_version`` (= that sha) and
``terms_accepted_locale``. The AI-data (Google Limited Use) checkbox is separate.

PT gate: the PT sentence links to https://owera.com/terms/pt/, which is not live
yet. Until ``TERMS_PT_LIVE`` is True (here AND in the SPA), the PT locale shows
the EN sentence with the EN links and the server accepts only the EN sha.
"""
from __future__ import annotations

import hashlib

TERMS_PT_LIVE = False

TEXT_EN = "I accept the Owera Terms of Service and have read the Privacy Notice."
TEXT_PT = "Aceito os Termos de Serviço da Owera e li o Aviso de Privacidade."

SHA_EN = hashlib.sha256(TEXT_EN.encode()).hexdigest()
SHA_PT = hashlib.sha256(TEXT_PT.encode()).hexdigest()

TERMS_URL_EN = "https://owera.com/terms/"
TERMS_URL_PT = "https://owera.com/terms/pt/"
PRIVACY_URL_EN = "https://owera.com/privacy/"
PRIVACY_URL_PT = "https://owera.com/privacy/pt/"


class TermsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def known_shas() -> dict[str, str]:
    """sha → locale the server accepts right now (PT only once /terms/pt/ is live)."""
    out = {SHA_EN: "en"}
    if TERMS_PT_LIVE:
        out[SHA_PT] = "pt"
    return out


def _norm_locale(locale: str | None) -> str:
    return "pt" if (locale or "").strip().lower().startswith("pt") else "en"


def displayed_locale(ui_locale: str | None) -> str:
    """Which sentence a browser locale sees: PT only when the PT terms are live."""
    return "pt" if (_norm_locale(ui_locale) == "pt" and TERMS_PT_LIVE) else "en"


def validate(sha: str | None, locale: str | None) -> str:
    """Locale ("en"/"pt") of the sentence the user ticked; else ``TermsError``.

    ``locale`` is the language of the sentence shown (the SPA sends "en" while
    the PT gate is closed, even for a pt-BR browser)."""
    known = known_shas()
    if not sha or sha not in known:
        raise TermsError("terms_text_changed",
                         "The Terms / Privacy text changed. Reload the page and accept it again.")
    if known[sha] != _norm_locale(locale):
        raise TermsError("terms_locale_mismatch", "The Terms / Privacy text and its language don't match.")
    return known[sha]
