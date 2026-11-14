"""S5 ops CLI: python -m app.provision_test_workspace --email <email>.

Creates an is_test workspace with owner_email, no Stripe call, no welcome or
payment email, never paid_at; idempotent; SPA Google login resolves it.

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
    rc, out = run("--email", "  E2E.Tester@Example.COM ")
    ok(rc == 0 and out["ok"] and out["created"] is True, "first run creates (exit 0)")
    ok(out["owner_email"] == "e2e.tester@example.com", "owner_email normalized")
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
    rc, out2 = run("--email", "e2e.tester@example.com")
    ok(rc == 0 and out2["created"] is False and out2["workspace_id"] == WS,
       "re-run returns the existing workspace")
    with Session(engine) as s:
        n = len(s.exec(select(Workspace).where(
            Workspace.owner_email == "e2e.tester@example.com")).all())
        ok(n == 1, "still one row for the email")
        ok(s.exec(select(StripeEvent)).all() == [] and
           s.exec(select(StripeCheckoutSession)).all() == [], "no Stripe rows written")

    print("refusals")
    rc, out = run("--email", "rodrigo@owera.com")
    ok(rc == 2 and not out["ok"], "owera SPA allowlist email refused (exit 2)")
    rc, out = run("--email", "not-an-email")
    ok(rc == 2 and not out["ok"], "invalid email refused")
    with Session(engine) as s:
        s.add(Workspace(slug="paid-buyer", name="Paid", owner_email="paid@example.com",
                        stripe_subscription_id="sub_x", subscription_status="active"))
        ws1 = s.get(Workspace, 1)
        ws1.owner_email = "owner1@example.com"
        s.add(ws1)
        s.commit()
    rc, out = run("--email", "paid@example.com")
    ok(rc == 0 and out["created"] is False and out["is_test"] is False,
       "existing paid workspace returned unchanged, never flipped to test")
    rc, out = run("--email", "owner1@example.com")
    ok(rc == 2 and "workspace 1" in out["error"], "owera workspace email refused")

    print("--name")
    rc, out = run("--email", "named@example.com", "--name", "E2E Buyer")
    ok(rc == 0 and out["name"] == "E2E Buyer" and out["slug"] == "named", "custom name, slug from email")

print("SPA Google login resolves the test workspace (no restart, no allowlist edit)")
with Session(engine) as s:
    slug = s.get(Workspace, WS).slug
ok(spa_google_login.workspace_for_email("E2E.Tester@example.com") == slug,
   "workspace_for_email → test workspace slug")

print("a later real checkout for the same email keeps is_test and sets paid_at")
settings.stripe_cm_payment_links = "plink_cm"
ev = {"id": "evt_s5", "type": "checkout.session.completed", "created": 1_790_000_000,
      "data": {"object": {"id": "cs_s5", "mode": "subscription", "payment_link": "plink_cm",
                          "customer": "cus_s5", "subscription": "sub_s5",
                          "customer_details": {"email": "e2e.tester@example.com"}}}}
with patch.object(stripe_email, "send_welcome_email", return_value=True):
    with Session(engine) as s:
        ok(stripe_provision.process_event(s, ev) == 200, "checkout processed")
with Session(engine) as s:
    w = s.get(Workspace, WS)
    ok(w.is_test is True and w.paid_at is not None, "is_test kept (flagged), paid_at from event")

print("not exposed over HTTP")
import app.main as main
paths = {getattr(r, "path", "") for r in main.app.routes}
ok(not any("provision" in p for p in paths), "no HTTP route for provisioning")

print(f"\nALL {_checks} CHECKS PASSED")
