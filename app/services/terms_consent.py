"""S2 — the single Terms + Privacy checkbox (LegalOps FIX4 via CMO, sha 6a389cd4d636, 14/11).

One box: the user ACCEPTS the Terms of Service and HAS READ the Privacy Notice
(a notice, not something accepted). Texts are LegalOps', verbatim;
the SPA renders the same strings (``frontend/src/termsConsent.ts``) with links on
"Terms of Service" / "Termos de Serviço" and "Privacy Notice" / "Aviso de
Privacidade", and sends the sha256 of the exact sentence shown + its locale.

On accept the wizard keeps the existing ``terms_accepted_at`` +
``privacy_accepted_at`` and stores ``terms_accepted_version`` (= that sha) and
``terms_accepted_locale``. The AI-data (Google Limited Use) checkbox is separate.

CTO / LegalOps gates (14/11):
- Configurable version: the server accepts and stores ONLY the configured shas
  ``MANAGER_TERMS_VERSION_SHA_EN`` / ``MANAGER_TERMS_VERSION_SHA_PT`` (defaults =
  sha256 of the FIX4 sentences below). The SPA sends the sha of the sentence it
  shows, so a wording change without a matching config change fails closed (409).
- PT gate: the PT sentence links to https://owera.com/terms/pt/, which is not live
  yet. Until ``MANAGER_TERMS_PT_LIVE`` is true the SPA (it reads the flag from
  ``GET /api/onboarding/wizard`` → ``terms.pt_live``) shows PT-locale users the
  EN sentence with the EN links, the server accepts only the EN sha, and a stored
  PT acceptance is NOT consent (``is_valid`` / S8 ``terms_pt_not_live``): the
  wizard asks again in EN.
"""
from __future__ import annotations

import hashlib

from app.config import settings

TEXT_EN = "I accept the Owera Terms of Service and have read the Privacy Notice."
TEXT_PT = "Aceito os Termos de Serviço da Owera e li o Aviso de Privacidade."

# sha256 of the sentences in this file (= the code defaults of the config shas).
SHA_EN = hashlib.sha256(TEXT_EN.encode()).hexdigest()
SHA_PT = hashlib.sha256(TEXT_PT.encode()).hexdigest()


def pt_live() -> bool:
    return bool(settings.terms_pt_live)


def configured_shas() -> dict[str, str]:
    """locale → the configured accepted sha (MANAGER_TERMS_VERSION_SHA_EN / _PT)."""
    return {"en": (settings.terms_version_sha_en or "").strip().lower(),
            "pt": (settings.terms_version_sha_pt or "").strip().lower()}

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
    cfg = configured_shas()
    out = {cfg["en"]: "en"} if cfg["en"] else {}
    if pt_live() and cfg["pt"]:
        out[cfg["pt"]] = "pt"
    return out


def _norm_locale(locale: str | None) -> str:
    return "pt" if (locale or "").strip().lower().startswith("pt") else "en"


def displayed_locale(ui_locale: str | None) -> str:
    """Which sentence a browser locale sees: PT only when the PT terms are live."""
    return "pt" if (_norm_locale(ui_locale) == "pt" and pt_live()) else "en"


def validate(sha: str | None, locale: str | None) -> str:
    """Locale ("en"/"pt") of the sentence the user ticked; else ``TermsError``.

    ``locale`` is the language of the sentence shown (the SPA sends "en" while
    the PT gate is closed, even for a pt-BR browser)."""
    known = known_shas()
    sha = (sha or "").strip().lower()
    if not sha or sha not in known:
        raise TermsError("terms_text_changed",
                         "The Terms / Privacy text changed. Reload the page and accept it again.")
    if known[sha] != _norm_locale(locale):
        raise TermsError("terms_locale_mismatch", "The Terms / Privacy text and its language don't match.")
    return known[sha]


def block_reason(ws) -> str | None:
    """``None`` when the stored acceptance counts as consent.

    A PT-locale acceptance does not count while the PT gate is closed (the PT
    terms page isn't live), whatever text the row claims."""
    if getattr(ws, "terms_accepted_at", None) is None or getattr(ws, "privacy_accepted_at", None) is None:
        return "terms_privacy_not_accepted"
    if (getattr(ws, "terms_accepted_locale", None) or "").lower().startswith("pt") and not pt_live():
        return "terms_pt_not_live"
    return None


def is_valid(ws) -> bool:
    return block_reason(ws) is None


def public_flags() -> dict:
    """What the SPA needs: the PT gate and the shas the server accepts now."""
    return {"pt_live": pt_live(), "accepted_shas": {loc: sha for sha, loc in known_shas().items()}}
