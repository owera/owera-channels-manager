"""S2 — the single Terms + Privacy checkbox (LegalOps 14/11: "Terms of Use" wording).

One box: the user ACCEPTS the Terms of Use and HAS READ the Privacy Notice
(a notice, not something accepted). Texts are LegalOps', verbatim;
the SPA renders the same strings (``frontend/src/termsConsent.ts``) with links on
"Terms of Use" / "Termos de Uso" and "Privacy Notice" / "Aviso de
Privacidade", and sends the sha256 of the exact sentence shown + its locale.

On accept the wizard keeps the existing ``terms_accepted_at`` +
``privacy_accepted_at`` and stores ``terms_accepted_version`` (= that sha) and
``terms_accepted_locale``. The AI-data (Google Limited Use) checkbox is separate.

CTO / LegalOps gates (14/11):
- Configurable version: the server accepts the configured shas
  ``MANAGER_TERMS_VERSION_SHA_EN`` / ``MANAGER_TERMS_VERSION_SHA_PT`` (defaults =
  sha256 of the "Terms of Use" sentences below) plus the FIX4 "Terms of Service"
  sentence shas (``LEGACY_SHAS``) so earlier acceptances stay valid. The SPA sends
  the sha of the sentence it shows; an unknown one fails closed (409).
- PT gate: the PT sentence links to https://owera.com/terms/pt/, which is not live
  yet. Until ``MANAGER_TERMS_PT_LIVE`` is true (the SPA reads ``terms.pt_live`` from
  ``GET /api/onboarding/wizard``) a PT-locale user sees NO Terms box and NO AI-data
  box in the wizard, only the "coming soon" notice (CTO / LegalOps 14/11) with a way
  to switch the assistant to English; the server accepts only EN shas, and a
  stored PT acceptance is NOT consent (``is_valid`` / S8 ``terms_pt_not_live``).
- Consent record (``record`` → ``TermsConsentEvent``): sentence sha, Terms URL +
  document sha, Privacy URL + document sha (``MANAGER_TERMS_DOC_SHA_EN/_PT``,
  ``MANAGER_PRIVACY_DOC_SHA_EN/_PT``; never fetched live, archived per sha by
  ``scripts/archive_legal_docs.py``), locale, time, user id, workspace id.
"""
from __future__ import annotations

import hashlib

from app.config import settings

TEXT_EN = "I accept the Owera Terms of Use and have read the Privacy Notice."
TEXT_PT = "Aceito os Termos de Uso da Owera e li o Aviso de Privacidade."
# FIX4 sentences (CMO sha 6a389cd4d636). Acceptances stored under them stay valid.
LEGACY_TEXT_EN = "I accept the Owera Terms of Service and have read the Privacy Notice."
LEGACY_TEXT_PT = "Aceito os Termos de Serviço da Owera e li o Aviso de Privacidade."

# sha256 of the sentences in this file (= the code defaults of the config shas).
SHA_EN = hashlib.sha256(TEXT_EN.encode()).hexdigest()
SHA_PT = hashlib.sha256(TEXT_PT.encode()).hexdigest()
LEGACY_SHAS: dict[str, str] = {hashlib.sha256(LEGACY_TEXT_EN.encode()).hexdigest(): "en",
                               hashlib.sha256(LEGACY_TEXT_PT.encode()).hexdigest(): "pt"}


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
    if not cfg["en"]:
        return {}                       # misconfigured: accept nothing (fail closed)
    out = {sha: loc for sha, loc in LEGACY_SHAS.items() if loc == "en" or pt_live()}
    out[cfg["en"]] = "en"
    if pt_live() and cfg["pt"]:
        out[cfg["pt"]] = "pt"
    return out


def current_shas() -> dict[str, str]:
    """locale → the sha of the sentence the SPA shows now (no legacy)."""
    cfg = configured_shas()
    out = {"en": cfg["en"]} if cfg["en"] else {}
    if pt_live() and cfg["pt"]:
        out["pt"] = cfg["pt"]
    return out


def docs_for(locale: str) -> dict[str, str | None]:
    """URLs shown with the sentence + the configured document shas for that locale."""
    pt = locale == "pt"
    return {
        "terms_url": TERMS_URL_PT if pt else TERMS_URL_EN,
        "terms_doc_sha": (settings.terms_doc_sha_pt if pt else settings.terms_doc_sha_en).strip().lower() or None,
        "privacy_url": PRIVACY_URL_PT if pt else PRIVACY_URL_EN,
        "privacy_doc_sha": (settings.privacy_doc_sha_pt if pt else settings.privacy_doc_sha_en).strip().lower() or None,
    }


def record(session, ws, *, sentence_sha: str, locale: str, user_id: str | None):
    """Append the consent record (caller commits)."""
    from app.models import TermsConsentEvent
    ev = TermsConsentEvent(workspace_id=ws.id, user_id=(user_id or "")[:200] or None, locale=locale,
                           sentence_sha=(sentence_sha or "").strip().lower(), **docs_for(locale))
    session.add(ev)
    return ev


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


def doc_block_reason(ws) -> str | None:
    """CTO 14/11 fail closed: ``terms_doc_unpinned`` when the Terms or Privacy document
    sha for the accepted locale is not configured (MANAGER_TERMS_DOC_SHA_<L> /
    MANAGER_PRIVACY_DOC_SHA_<L>). Used by the S8 external release only; internal
    workspaces never reach it."""
    loc = "pt" if (getattr(ws, "terms_accepted_locale", None) or "").lower().startswith("pt") else "en"
    d = docs_for(loc)
    if not d["terms_doc_sha"] or not d["privacy_doc_sha"]:
        return "terms_doc_unpinned"
    return None


def is_valid(ws) -> bool:
    return block_reason(ws) is None


def public_flags() -> dict:
    """What the SPA needs: the PT gate and the shas the server accepts now."""
    return {"pt_live": pt_live(), "accepted_shas": current_shas()}
