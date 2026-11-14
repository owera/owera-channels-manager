"""Shared test fixture (not a suite entry): a stored Terms consent record with the
legal document shas pinned, as the wizard / Settings writes it (CTO 14/11).

Workspaces that a test marks as "terms accepted" by setting the columns directly
need this record too, or the S8 external release refuses with
``terms_reaccept_required`` (verify_terms_consent pins that rule)."""
from __future__ import annotations

from app.models import TermsConsentEvent
from app.services import terms_consent as tc

PINNED_DOC_SHA = "0" * 64


def pin_terms_record(session, ws_id: int, locale: str = "en", doc_sha: str = PINNED_DOC_SHA) -> None:
    pt = locale == "pt"
    session.add(TermsConsentEvent(
        workspace_id=ws_id, user_id="fixture", locale=locale,
        sentence_sha=tc.SHA_PT if pt else tc.SHA_EN,
        terms_url=tc.TERMS_URL_PT if pt else tc.TERMS_URL_EN, terms_doc_sha=doc_sha,
        privacy_url=tc.PRIVACY_URL_PT if pt else tc.PRIVACY_URL_EN, privacy_doc_sha=doc_sha))
