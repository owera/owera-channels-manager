"""S5 ops CLI: python -m app.provision_test_workspace --email <email>.

Creates an is_test workspace with owner_email (internal name+test@owera.com
only), no Stripe call, no welcome or payment email, never paid_at; idempotent;
SPA Google login resolves it. A later real payment (checkout.session.completed
or invoice.paid) clears is_test, sets paid_at once and logs an audit line.

Run: PYTHONPATH=. uv run python tests/verify_provision_test_workspace.py
Uses a throwaway SQLite file (MANAGER_DB_PATH under a temp dir).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix="verify-s5-"))
os.environ["MANAGER_DB_PATH"] = str(_TMP / "s5.db")
os.environ["MANAGER_SPA_GOOGLE_LOGIN"] = "1"
os.environ["MANAGER_RESEND_API_KEY"] = "re_test_no_send"

# Pre-S5/S1 workspace table (as on claw0 today): init_db must ALTER it in place.
import sqlite3
_c = sqlite3.connect(os.environ["MANAGER_DB_PATH"])
_c.execute("CREATE TABLE workspace (id INTEGER PRIMARY KEY, slug VARCHAR NOT NULL UNIQUE, "
           "name VARCHAR NOT NULL, daily_youtube_quota_cap INTEGER NOT NULL DEFAULT 9000, "
           "subscription_status VARCHAR NOT NULL DEFAULT 'trialing', "
           "onboarding_complete BOOLEAN NOT NULL DEFAULT 0, owner_email VARCHAR, "
           "stripe_customer_id VARCHAR, stripe_subscription_id VARCHAR, "
           "welcome_email_sent BOOLEAN DEFAULT 0, created_at DATETIME NOT NULL, "
           "updated_at DATETIME NOT NULL)")
_c.execute("INSERT INTO workspace (id, slug, name, created_at, updated_at) VALUES "
           "(1, 'owera', 'Owera', '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
_c.commit()
_c.close()

import httpx
from sqlmodel import Session, select

from app import provision_test_workspace as ptw
from app.config import settings
from app.db import engine
from app.models import StripeCheckoutSession, StripeEvent, Workspace
from app.services import spa_google_login, stripe_email, stripe_provision

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(settings.db_path == str(_TMP / "s5.db"), "test DB is the temp file (never the real one)")


def run(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ptw.main(list(argv))
    return rc, json.loads(buf.getvalue())


def _boom(*a, **k):
    raise AssertionError("network/send must not happen")


# Any outbound HTTP, welcome email or Stripe provisioning path blows up.
with patch.object(httpx, "post", _boom), patch.object(httpx, "get", _boom), \
        patch.object(httpx.Client, "send", _boom), \
        patch.object(stripe_email, "send_welcome_email", _boom), \
        patch.object(stripe_provision, "deliver_welcome", _boom), \
        patch.object(stripe_provision, "handle_checkout_completed", _boom), \
        patch.object(stripe_provision, "process_event", _boom):
    print("create")
    rc, out = run("--email", "  E2E.Tester+Test@Owera.COM ")
    ok(rc == 0 and out["ok"] and out["created"] is True, "first run creates (exit 0)")
    ok(out["owner_email"] == "e2e.tester+test@owera.com", "owner_email normalized")
    ok(out["is_test"] is True, "is_test = true")
    ok(out["paid_at"] is None, "paid_at never written")
    ok(out["stripe_customer_id"] is None and out["stripe_subscription_id"] is None,
       "no Stripe ids")
    ok(out["welcome_email_sent"] is False, "no welcome/payment email")
    ok(out["workspace_id"] != 1, "never workspace 1")
    WS = out["workspace_id"]
    import sqlite3 as _sq
    _cc = _sq.connect(os.environ["MANAGER_DB_PATH"])
    cols = {r[1] for r in _cc.execute("PRAGMA table_info(workspace)")}
    row1 = _cc.execute("SELECT is_test, paid_at, first_draft_at FROM workspace WHERE id=1").fetchone()
    vcols = {r[1] for r in _cc.execute("PRAGMA table_info(video)")}
    _cc.close()
    ok({"is_test", "paid_at", "first_draft_at"} <= cols and "approved_by" in vcols,
       "migration: old workspace table ALTERed in place (+ video.approved_by)")
    ok(tuple(row1) == (0, None, None), "existing row: is_test=0, paid_at/first_draft_at NULL")

    print("idempotent")
    rc, out2 = run("--email", "e2e.tester+test@owera.com")
    ok(rc == 0 and out2["created"] is False and out2["workspace_id"] == WS,
       "re-run returns the existing workspace")
    with Session(engine) as s:
        n = len(s.exec(select(Workspace).where(
            Workspace.owner_email == "e2e.tester+test@owera.com")).all())
        ok(n == 1, "still one row for the email")
        ok(s.exec(select(StripeEvent)).all() == [] and
           s.exec(select(StripeCheckoutSession)).all() == [], "no Stripe rows written")

    print("refusals")
    rc, out = run("--email", "rodrigo@owera.com")
    ok(rc == 2 and not out["ok"], "owera SPA allowlist email refused (exit 2)")
    rc, out = run("--email", "not-an-email")
    ok(rc == 2 and not out["ok"], "invalid email refused")
    for bad in ("buyer@example.com", "someone+test@gmail.com", "plain@owera.com",
                "name+test@owera.com.evil.io", "name+test@sub.owera.com", "+test@owera.com",
                "name+prod@owera.com"):
        rc, out = run("--email", bad)
        ok(rc == 2 and not out["ok"] and "test emails only" in out["error"],
           f"non-internal email refused (exit 2): {bad}")
    rc, out = run("--email", "Ops.Bot+Test2@OWERA.com")
    ok(rc == 0 and out["created"] is True and out["owner_email"] == "ops.bot+test2@owera.com",
       "+test alias with suffix accepted (case-insensitive)")
    with Session(engine) as s:
        s.add(Workspace(slug="paid-buyer", name="Paid", owner_email="paid+test@owera.com",
                        stripe_subscription_id="sub_x", subscription_status="active"))
        ws1 = s.get(Workspace, 1)
        ws1.owner_email = "owner1+test@owera.com"
        s.add(ws1)
        s.commit()
    rc, out = run("--email", "paid+test@owera.com")
    ok(rc == 0 and out["created"] is False and out["is_test"] is False,
       "existing paid workspace returned unchanged, never flipped to test")
    rc, out = run("--email", "owner1+test@owera.com")
    ok(rc == 2 and "workspace 1" in out["error"], "owera workspace email refused")

    print("--name")
    rc, out = run("--email", "named+test@owera.com", "--name", "E2E Buyer")
    ok(rc == 0 and out["name"] == "E2E Buyer" and out["slug"].startswith("named"), "custom name, slug from email")

print("SPA Google login resolves the test workspace (no restart, no allowlist edit)")
with Session(engine) as s:
    slug = s.get(Workspace, WS).slug
ok(spa_google_login.workspace_for_email("E2E.Tester+TEST@owera.com") == slug,
   "workspace_for_email → test workspace slug")

print("a later real checkout for the same email clears is_test and sets paid_at")
import logging as _logging


class _Cap(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, r):
        self.lines.append(r.getMessage())


cap = _Cap()
_logging.getLogger("manager.stripe_provision").addHandler(cap)
_logging.getLogger("manager.stripe_provision").setLevel(_logging.INFO)
settings.stripe_cm_payment_links = "plink_cm"
ev = {"id": "evt_s5", "type": "checkout.session.completed", "created": 1_790_000_000,
      "data": {"object": {"id": "cs_s5", "mode": "subscription", "payment_link": "plink_cm",
                          "customer": "cus_s5", "subscription": "sub_s5",
                          "customer_details": {"email": "e2e.tester+test@owera.com"}}}}
with patch.object(stripe_email, "send_welcome_email", return_value=True):
    with Session(engine) as s:
        ok(stripe_provision.process_event(s, ev) == 200, "checkout processed")
with Session(engine) as s:
    w = s.get(Workspace, WS)
    ok(w.is_test is False, "checkout: is_test cleared (paying customer never stays test)")
    ok(w.paid_at is not None and int(w.paid_at.replace(tzinfo=__import__("datetime").timezone.utc)
                                      .timestamp()) == 1_790_000_000, "checkout: paid_at from event.created")
audit = [l for l in cap.lines if l.startswith("workspace_test_to_paid")]
ok(audit == [f"workspace_test_to_paid workspace_id={WS} event=checkout.session.completed event_id=evt_s5"],
   "checkout: one structured audit line")
ok(not any("@" in l for l in audit), "audit line has no email (no PII)")

print("invoice.paid on an is_test workspace clears is_test, paid_at only if missing")
with Session(engine) as s:
    s.add(Workspace(slug="inv-test", name="Inv", owner_email="inv+test@owera.com", is_test=True,
                    stripe_customer_id="cus_inv", stripe_subscription_id="sub_inv"))
    s.add(Workspace(slug="inv-test2", name="Inv2", owner_email="inv2+test@owera.com", is_test=True,
                    stripe_customer_id="cus_inv2",
                    paid_at=__import__("datetime").datetime(2026, 1, 2)))
    s.commit()
    INV = s.exec(select(Workspace).where(Workspace.slug == "inv-test")).one().id
    INV2 = s.exec(select(Workspace).where(Workspace.slug == "inv-test2")).one().id
cap.lines.clear()
inv = {"id": "evt_inv1", "type": "invoice.paid", "created": 1_791_000_000,
       "data": {"object": {"id": "in_1", "subscription": "sub_inv", "customer": "cus_inv",
                           "amount_paid": 4900, "customer_email": "inv+test@owera.com"}}}
with Session(engine) as s:
    ok(stripe_provision.process_event(s, inv) == 200, "invoice.paid processed")
with Session(engine) as s:
    w = s.get(Workspace, INV)
    ok(w.is_test is False and w.paid_at is not None, "invoice.paid: is_test cleared, paid_at set")
    PAID_INV = w.paid_at
ok([l for l in cap.lines if l.startswith("workspace_test_to_paid")] ==
   [f"workspace_test_to_paid workspace_id={INV} event=invoice.paid event_id=evt_inv1"],
   "invoice.paid: one audit line")

print("idempotent replay")
cap.lines.clear()
with Session(engine) as s:
    ok(stripe_provision.process_event(s, inv) == 200, "same event id replay → 200")
inv_b = {**inv, "id": "evt_inv2", "created": 1_799_000_000}
with Session(engine) as s:
    ok(stripe_provision.process_event(s, inv_b) == 200, "later invoice.paid → 200")
with patch.object(stripe_email, "send_welcome_email", return_value=True):
    with Session(engine) as s:
        ok(stripe_provision.process_event(s, {**ev, "id": "evt_s5b", "created": 1_799_000_000}) == 200,
           "checkout replay (new event id, same session) → 200")
with Session(engine) as s:
    w = s.get(Workspace, INV)
    ok(w.is_test is False and w.paid_at == PAID_INV, "invoice replays: paid_at never overwritten")
    w = s.get(Workspace, WS)
    ok(w.is_test is False and int(w.paid_at.replace(tzinfo=__import__("datetime").timezone.utc)
                                  .timestamp()) == 1_790_000_000, "checkout replay: paid_at unchanged")
ok(not [l for l in cap.lines if l.startswith("workspace_test_to_paid")], "replays: no new audit line")

print("invoice.paid edge cases")
inv2 = {"id": "evt_inv3", "type": "invoice.paid", "created": 1_792_000_000,
        "data": {"object": {"customer": "cus_inv2", "amount_paid": 4900}}}
with Session(engine) as s:
    stripe_provision.process_event(s, inv2)
with Session(engine) as s:
    w = s.get(Workspace, INV2)
    ok(w.is_test is False and w.paid_at == __import__("datetime").datetime(2026, 1, 2),
       "invoice.paid: existing paid_at kept, is_test cleared")
with Session(engine) as s:
    s.add(Workspace(slug="inv-zero", name="Zero", owner_email="zero+test@owera.com", is_test=True,
                    stripe_customer_id="cus_zero"))
    s.commit()
    ZW = s.exec(select(Workspace).where(Workspace.slug == "inv-zero")).one().id
with Session(engine) as s:
    stripe_provision.process_event(s, {"id": "evt_zero", "type": "invoice.paid",
                                       "data": {"object": {"customer": "cus_zero", "amount_paid": 0}}})
with Session(engine) as s:
    w = s.get(Workspace, ZW)
    ok(w.is_test is True and w.paid_at is None, "zero-amount invoice is not a payment: unchanged")
with Session(engine) as s:
    ws1 = s.get(Workspace, 1)
    ws1.stripe_customer_id = "cus_owera"
    ws1.is_test = True  # pathological; ws 1 is never touched
    s.add(ws1)
    s.commit()
with Session(engine) as s:
    stripe_provision.process_event(s, {"id": "evt_ws1", "type": "invoice.paid",
                                       "data": {"object": {"customer": "cus_owera", "amount_paid": 100}}})
with Session(engine) as s:
    w = s.get(Workspace, 1)
    ok(w.is_test is True and w.paid_at is None, "invoice.paid never touches ws 1")
    w.is_test = False
    s.add(w)
    s.commit()

print("MANAGER_TEST_WORKSPACE_EMAILS (PO CM option b: dedicated Gmail test users)")
ok(settings.test_workspace_emails == "", "default empty")
ok(spa_google_login.test_workspace_emails() == frozenset(), "empty → no extra test emails")
with patch.object(settings, "test_workspace_emails",
                  " E2E.Buyer@Gmail.com , other.tester@gmail.com,, rodrigo@owera.com "):
    ok(spa_google_login.test_workspace_emails() ==
       {"e2e.buyer@gmail.com", "other.tester@gmail.com", "rodrigo@owera.com"},
       "parsed: comma-separated, trimmed, case-insensitive")
    with patch.object(httpx, "post", _boom), patch.object(stripe_email, "send_welcome_email", _boom):
        rc, out = run("--email", "e2e.buyer@GMAIL.com")
        ok(rc == 0 and out["created"] is True and out["is_test"] is True
           and out["owner_email"] == "e2e.buyer@gmail.com", "allowlisted Gmail accepted by the CLI")
        GW, GSLUG = out["workspace_id"], out["slug"]
        rc, out = run("--email", "stranger@gmail.com")
        ok(rc == 2 and not out["ok"] and "MANAGER_TEST_WORKSPACE_EMAILS" in out["error"],
           "non-allowlisted email refused (exit 2)")
        rc, out = run("--email", "rodrigo@owera.com")
        ok(rc == 2 and "allowlist" in out["error"], "owera allowlist email refused even if listed")
        rc, out = run("--email", "owner1+test@owera.com")
        ok(rc == 2 and "workspace 1" in out["error"], "ws 1 refusal unchanged")

    print("SPA Google login on is_test workspaces")
    ok(spa_google_login.workspace_for_email("E2E.Buyer@gmail.com") == GSLUG,
       "allowlisted account signs in to its own test workspace")
    ok(spa_google_login.workspace_for_email("other.tester@gmail.com") is None,
       "another allowlisted account does not get that workspace (owns none)")
    ok(spa_google_login.workspace_for_email("stranger@gmail.com") is None,
       "non-allowlisted account: no workspace")
    with Session(engine) as s:
        tw = s.get(Workspace, GW)
        ok(not spa_google_login.may_sign_in_to_test_workspace("stranger@gmail.com", tw),
           "non-allowlisted, non-owner account refused on the is_test workspace")
        ok(not spa_google_login.may_sign_in_to_test_workspace("rodrigo@owera.com", tw),
           "owera email refused on the is_test workspace")
        # A legacy/hand-made is_test row owned by the owera email: still never mapped.
        s.add(Workspace(slug="leak-test", name="Leak", owner_email="rodrigo@owera.com", is_test=True))
        s.add(Workspace(slug="dup-a", name="Dup A", owner_email="dup@gmail.com", is_test=True))
        s.add(Workspace(slug="dup-b", name="Dup B", owner_email="dup@gmail.com"))
        s.add(Workspace(slug="legacy-test", name="Legacy", owner_email="legacy@example.com", is_test=True))
        s.commit()
    ok(spa_google_login.workspace_for_email("rodrigo@owera.com") == "owera",
       "owera email maps to owera, never to a test workspace")
    ok(spa_google_login.workspace_for_email("dup@gmail.com") is None,
       "one account owning two workspaces resolves to none (never two)")
    ok(spa_google_login.workspace_for_email("legacy@example.com") == "legacy-test",
       "provisioned owner email of an is_test workspace may sign in")
with patch.object(settings, "test_workspace_emails", ""):
    ok(spa_google_login.workspace_for_email("e2e.buyer@gmail.com") == GSLUG,
       "owner email still resolves when later removed from the env list")
    ok(spa_google_login.workspace_for_email("paid+test@owera.com") is not None,
       "non-test (paid) workspace login unchanged")

print("not exposed over HTTP")
import app.main as main
paths = {getattr(r, "path", "") for r in main.app.routes}
ok(not any("provision" in p for p in paths), "no HTTP route for provisioning")

print(f"\nALL {_checks} CHECKS PASSED")
