"""Google Limited Use consent (Google API Services User Data Policy).

Before CM sends a non-internal workspace's Google-sourced data (video titles,
descriptions, tags, channel stats) to the LLM, the user ticks a checkbox whose
text LegalOps approved (14/11). The server stores who, when, which locale and
the sha256 of the EXACT text shown, and only accepts a sha of a text it knows.
If the text changes, the stored sha is no longer known: the consent is stale,
the LLM is refused (``ai_data_ack_stale``) and the SPA asks again.

Withdrawing (``POST /api/onboarding/ai-data/withdraw``, Settings) or
disconnecting YouTube clears it, which cuts the LLM at the next call. Data
deletion after a disconnect stays on the existing #214 path
(``notify.mark_operator_disconnect`` / ``youtube_purge``: 7 / 30 days); this
module never deletes anything.

Naming: always "Google Limited Use consent" in code and logs.
"""

from __future__ import annotations

import hashlib
import logging

from sqlmodel import Session

from app.models import GoogleLimitedUseConsentEvent, Workspace, utcnow

logger = logging.getLogger("manager.google_limited_use")

LOG_EVENT = "google_limited_use_consent"

# LegalOps-approved checkbox texts (verbatim). The SPA shows one of them next to
# a privacy-policy link; frontend/src/googleLimitedUse.ts must carry the same
# strings and shas (tests/verify_google_limited_use.py pins both).
TEXT_EN = ("I agree that Channels Manager sends my videos' titles, descriptions, tags and "
           "channel stats to an AI provider in the US, only to create my own drafts.")
TEXT_PT = ("Concordo que o Channels Manager envie os títulos, descrições e tags dos meus vídeos "
           "e as estatísticas do canal a um provedor de IA nos EUA, só para criar os meus "
           "próprios rascunhos.")
TEXTS: dict[str, str] = {"en": TEXT_EN, "pt": TEXT_PT}

# Google-sourced fields the text names, in the L3 allowlist's vocabulary
# (llm_guard.YT_FIELDS must equal this set).
TEXT_FIELDS: frozenset[str] = frozenset({"video_title", "video_description", "video_tags",
                                         "channel_stats"})


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


KNOWN_SHAS: dict[str, str] = {text_sha(t): loc for loc, t in TEXTS.items()}


def is_valid(ws) -> bool:
    """A current consent: ticked, not withdrawn, and for a text we still show."""
    return (getattr(ws, "ai_data_ack_at", None) is not None
            and (getattr(ws, "ai_data_ack_version", None) or "") in KNOWN_SHAS)


def block_reason(ws) -> str | None:
    """Release-check reason, or None when the consent is current."""
    if getattr(ws, "ai_data_ack_at", None) is None:
        return "ai_data_not_acknowledged"
    if (getattr(ws, "ai_data_ack_version", None) or "") not in KNOWN_SHAS:
        return "ai_data_ack_stale"
    return None


class ConsentError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate(locale: str | None, sha: str | None) -> str:
    """The locale the sha belongs to; raises ``ConsentError`` otherwise."""
    loc = KNOWN_SHAS.get((sha or "").strip().lower())
    if loc is None:
        raise ConsentError("ai_data_text_changed",
                           "the consent text changed; reload and read it again")
    if (locale or "").strip().lower()[:2] != loc:
        raise ConsentError("ai_data_locale_mismatch", "the consent text and locale don't match")
    return loc


def record_ack(session: Session, ws: Workspace, *, user: str | None, locale: str | None,
               sha: str | None) -> Workspace:
    loc = validate(locale, sha)
    now = utcnow()
    ws.ai_data_ack_at = now
    ws.ai_data_ack_version = sha.strip().lower()
    ws.ai_data_ack_locale = loc
    ws.ai_data_ack_user = (user or "")[:200] or None
    ws.ai_data_ack_withdrawn_at = None
    session.add(ws)
    session.add(GoogleLimitedUseConsentEvent(workspace_id=ws.id, action="ack", user=ws.ai_data_ack_user,
                                             locale=loc, text_sha=ws.ai_data_ack_version))
    session.commit()
    session.refresh(ws)
    logger.info("%s action=ack workspace_id=%s locale=%s text_sha=%s", LOG_EVENT, ws.id, loc,
                ws.ai_data_ack_version[:12])
    from app.services import llm_guard
    llm_guard.clear_block(ws.id)
    session.refresh(ws)
    return ws


def withdraw(session: Session, ws: Workspace, *, user: str | None, reason: str) -> bool:
    """Clear the consent (cuts the LLM at the next call). False if none was set."""
    if ws.ai_data_ack_at is None:
        return False
    ws.ai_data_ack_at = None
    ws.ai_data_ack_withdrawn_at = utcnow()
    session.add(ws)
    session.add(GoogleLimitedUseConsentEvent(workspace_id=ws.id, action="withdraw", reason=reason,
                                             user=(user or "")[:200] or None,
                                             locale=ws.ai_data_ack_locale,
                                             text_sha=ws.ai_data_ack_version))
    session.commit()
    session.refresh(ws)
    logger.info("%s action=withdraw workspace_id=%s reason=%s", LOG_EVENT, ws.id, reason)
    return True


def status(ws: Workspace) -> dict:
    """SPA view. ``required`` = the ack page must be shown before the first LLM use:
    release flag ON, workspace not internal (allowlist), consent not current."""
    from app.services import activation, llm_guard
    exempt = ws.id in llm_guard.internal_workspace_ids()
    valid = is_valid(ws)
    return {
        "required": bool(llm_guard.external_release_enabled() and not exempt and not valid),
        "exempt": exempt,
        "valid": valid,
        "stale": ws.ai_data_ack_at is not None and not valid,
        "acked_at": activation.iso_utc(ws.ai_data_ack_at),
        "locale": ws.ai_data_ack_locale,
        "text_sha": ws.ai_data_ack_version,
        "withdrawn_at": activation.iso_utc(ws.ai_data_ack_withdrawn_at),
    }


def request_user(request) -> str | None:
    """Who ticked it: the SPA Google session e-mail, else the Basic Auth actor."""
    try:
        from app.services import spa_google_login
        payload = request.session.get(spa_google_login.SESSION_KEY) if "session" in request.scope else None
        if isinstance(payload, dict) and payload.get("email"):
            return spa_google_login.normalized_email(str(payload["email"]))
    except Exception:  # noqa: BLE001
        pass
    from app.workspace import request_identity
    ident = request_identity(request)
    return ident[1] if ident else None
