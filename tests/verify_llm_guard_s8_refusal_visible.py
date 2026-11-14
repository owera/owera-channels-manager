"""S8 — a guard refusal is never silent (PO CM 14/11, condition i).

Run: PYTHONPATH=. uv run python tests/verify_llm_guard_s8_refusal_visible.py

In-memory SQLite, no network, no real LLM. Covers:
  - flag ON, non-internal workspace: every refusal persists
    Workspace.llm_blocked_reason/at; GET /api/onboarding/status (onboarding.evaluate)
    exposes llm_blocked {reason, step, message "Drafts paused: …", at}
  - each reason maps to a setup step (terms_privacy / payment / ai_data / support)
  - the same reason repeated doesn't move llm_blocked_at; a new reason replaces it
  - the next allowed call clears it
  - integration: paid + terms + privacy + api backend + ack → allowed with the
    flag ON; same without terms → refused, persisted, cleared after acceptance
  - flag OFF: #217 unchanged, nothing persisted, no banner (even a stale row)
  - internal workspaces never get a banner
"""
from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.db as appdb

from app.config import settings
from app.models import Workspace
from app.services import llm_guard, onboarding

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
# S2: Google Limited Use consent = sha of the exact LegalOps text (EN).
from app.services.google_limited_use import TEXT_EN as _GLU_EN, text_sha as _glu_sha  # noqa: E402
_GLU_SHA = _glu_sha(_GLU_EN)
eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="buyer", name="Buyer", subscription_status="active",
                    privacy_accepted_at=NOW, ai_data_ack_at=NOW,
                    ai_data_ack_version=_GLU_SHA))  # S2 AI-data step                      # paid, terms missing
    s.add(Workspace(id=3, slug="trial", name="Trial", terms_accepted_at=NOW,
                    privacy_accepted_at=NOW))                      # not paid
    s.add(Workspace(id=4, slug="ok", name="OK", subscription_status="active",
                    terms_accepted_at=NOW, privacy_accepted_at=NOW, ai_data_ack_at=NOW,
                    ai_data_ack_version=_GLU_SHA))  # S2 AI-data step
    s.commit()

appdb_patch = patch.object(appdb, "engine", eng)
appdb_patch.start()

ON = dict(llm_external_release=True, llm_backend="api", xai_data_retention_ack="30d_no_training",
          llm_internal_workspace_ids="1")
OFF = dict(llm_external_release=False, llm_internal_workspace_ids="1")
F = ("topic",)


@contextlib.contextmanager
def cfg(**kw):
    with contextlib.ExitStack() as st:
        for k, v in kw.items():
            st.enter_context(patch.object(settings, k, v))
        yield


def refused(ws, ct="ideas", fields=F):
    try:
        llm_guard.require(ct, workspace_id=ws, fields=fields)
        return None
    except llm_guard.LLMBlocked as e:
        return e


def row(ws):
    with Session(eng) as s:
        return s.get(Workspace, ws)


def status(ws):
    with Session(eng) as s:
        return onboarding.evaluate(s, s.get(Workspace, ws))["llm_blocked"]


print("columns")
cols = {c.name for c in Workspace.__table__.columns}
ok({"llm_blocked_reason", "llm_blocked_at"} <= cols, "Workspace.llm_blocked_reason/at exist")
src = (ROOT / "app/db.py").read_text()
ok('("llm_blocked_reason", "VARCHAR")' in src and '("llm_blocked_at", "DATETIME")' in src,
   "additive nullable columns via _add_missing_columns")

print("flag OFF = #217: nothing persisted, no banner")
with cfg(**OFF):
    e = refused(2)
    ok(e is not None and e.reason is None, "ws2 refused with the exact #217 block (reason None)")
    ok(row(2).llm_blocked_reason is None and row(2).llm_blocked_at is None, "flag OFF: nothing persisted")
    ok(status(2) is None, "flag OFF: onboarding status llm_blocked = null")

print("integration: paid + terms + privacy + api + ack → allowed (flag ON)")
with cfg(**ON):
    ok(refused(4) is None, "ws4 (paid, terms, privacy, api, ack) allowed")
    ok(status(4) is None, "no banner when allowed")

print("same without terms → refused, persisted, visible")
with cfg(**ON):
    e = refused(2)
    ok(e is not None and e.reason == "terms_privacy_not_accepted", "ws2 refused: terms_privacy_not_accepted")
    r = row(2)
    ok(r.llm_blocked_reason == "terms_privacy_not_accepted" and r.llm_blocked_at is not None,
       "reason + at persisted on the workspace")
    st = status(2)
    ok(st is not None and st["step"] == "terms_privacy" and st["reason"] == "terms_privacy_not_accepted",
       "onboarding status: llm_blocked step terms_privacy")
    ok(st["message"].startswith("Drafts paused: finish setup step") and "Terms" in st["message"]
       and "privacy" in st["message"].lower(), "user text names the setup step (CMO copy)")
    ok(st["at"] and st["at"].endswith("+00:00"), "at is ISO UTC")
    AT1 = row(2).llm_blocked_at
    refused(2)
    ok(row(2).llm_blocked_at == AT1, "same reason again: llm_blocked_at unchanged (first seen)")

print("accept terms → next allowed call clears it")
with Session(eng) as s:
    w = s.get(Workspace, 2)
    w.terms_accepted_at = NOW
    s.add(w)
    s.commit()
with cfg(**ON):
    ok(row(2).llm_blocked_reason == "terms_privacy_not_accepted", "still shown until the next call")
    ok(refused(2) is None, "ws2 allowed after terms acceptance")
    ok(row(2).llm_blocked_reason is None and row(2).llm_blocked_at is None, "cleared on the allowed call")
    ok(status(2) is None, "banner gone")

print("each reason → a setup step")
with cfg(**ON):
    e = refused(3)
    ok(e.reason == "not_paid_or_test" and status(3)["step"] == "payment"
       and "Payment" in status(3)["message"], "not_paid_or_test → payment step")
    with cfg(llm_backend="cli"):
        e = refused(3)
    ok(e.reason == "backend_may_train" and row(3).llm_blocked_reason == "backend_may_train",
       "a new reason replaces the old one")
    ok(status(3)["step"] == "support" and "contact" in status(3)["message"].lower(),
       "backend_may_train → contact support")
    e = refused(3, ct="rubric_harness")
    ok(e.reason == "purpose_not_released" and status(3)["step"] == "support", "purpose_not_released → support")
    e = refused(3, fields=None)
    ok(e.reason == "fields_undeclared" and status(3)["step"] == "support", "fields_undeclared → support")
    e = refused(3, fields=("raw_youtube_payload",))
    ok(e is not None and row(3).llm_blocked_reason == "field_not_allowed", "field_not_allowed persisted (no field list)")
    with Session(eng) as s:
        w = s.get(Workspace, 3)
        w.llm_disabled = True
        s.add(w)
        s.commit()
    e = refused(3)
    ok(e.reason == "kill_switch" and status(3)["step"] == "support", "kill_switch → support")
    with patch.object(llm_guard, "_workspace_row", lambda ws: (None, "OperationalError")):
        e = refused(3)
    ok(e.reason == "workspace_unreadable", "workspace_unreadable refused (write best-effort)")
    ok(llm_guard.block_message("ai_data_not_acknowledged")["step"] == "ai_data",
       "ai_data step text reserved for S2")
    ok(llm_guard.block_message("something_new")["step"] == "support", "unknown reason → support")
    e = refused(99)
    ok(e is not None and e.reason == "workspace_unknown", "unknown workspace refused, nothing to write")

print("flag OFF hides a stale row; internal ws never gets a banner")
with cfg(**OFF):
    ok(row(3).llm_blocked_reason is not None and status(3) is None, "flag OFF: stale reason not shown")
with cfg(**ON):
    with Session(eng) as s:
        w = s.get(Workspace, 1)
        w.llm_disabled = True
        s.add(w)
        s.commit()
    e = refused(1)
    ok(e is not None and e.reason == "kill_switch", "internal ws1 kill switch still refuses")
    ok(row(1).llm_blocked_reason is None and status(1) is None, "internal ws: nothing persisted, no banner")

print("S1/A5 read surface")
ok(callable(llm_guard.blocked_status), "llm_guard.blocked_status(ws_row) for activation_report")
spa = (ROOT / "frontend/src/ui.tsx").read_text()
ok("LlmBlockedBanner" in spa and "llm_blocked" in (ROOT / "frontend/src/pages/Dashboard.tsx").read_text(),
   "SPA banner wired on the dashboard")

appdb_patch.stop()
print(f"\nALL {_checks} CHECKS PASSED")
