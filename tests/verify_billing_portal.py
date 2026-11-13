"""P5: Manage billing → Stripe Customer Portal session (httpx mocked, no live calls).

Run: PYTHONPATH=. uv run python tests/verify_billing_portal.py
"""
from __future__ import annotations

import io
import logging
import sys
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.models import Workspace
from app.routers import billing
from app.workspace import current_workspace

_checks = 0
KEY = "rk_test_FAKE_restricted_key_value"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera", stripe_customer_id="cus_owera"))
    s.add(Workspace(id=2, slug="buyer", name="Buyer", subscription_status="active", stripe_customer_id="cus_B123"))
    s.add(Workspace(id=3, slug="trial", name="Trial"))
    s.commit()

CURRENT = {"id": 2}
app = FastAPI()
app.include_router(billing.router)


def _ws():
    with Session(engine) as s:
        return s.get(Workspace, CURRENT["id"])


app.dependency_overrides[current_workspace] = _ws
client = TestClient(app)

CALLS = []


def stripe_ok(url, **kw):
    CALLS.append((url, kw))
    return httpx.Response(200, json={"id": "bps_1", "url": "https://billing.stripe.com/p/session/test_abc"},
                          request=httpx.Request("POST", url))


log = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(log))
logging.getLogger().setLevel(logging.INFO)

# not configured
settings.stripe_secret_key = ""
ok(client.post("/api/billing/portal").status_code == 503, "no key → 503")
ok(client.get("/api/billing").json() == {"subscription_status": "active", "has_billing": True,
                                          "portal_enabled": False}, "status reports portal disabled")

settings.stripe_secret_key = KEY
settings.stripe_portal_return_url = "https://channels.owera.com/settings"
with patch("httpx.post", side_effect=stripe_ok):
    r = client.post("/api/billing/portal")
ok(r.status_code == 200 and r.json()["url"].startswith("https://billing.stripe.com/"), "buyer → portal url")
url, kw = CALLS[0]
ok(url == "https://api.stripe.com/v1/billing_portal/sessions", "calls POST /v1/billing_portal/sessions only")
ok(kw["data"] == {"customer": "cus_B123", "return_url": "https://channels.owera.com/settings"},
   "uses the saved customer id and the app return_url (no configuration param)")
ok(kw["headers"]["Authorization"] == f"Bearer {KEY}", "authenticates with the configured key")

# owera workspace / no customer → 404, no Stripe call
CALLS.clear()
with patch("httpx.post", side_effect=stripe_ok):
    CURRENT["id"] = 1
    ok(client.post("/api/billing/portal").status_code == 404, "owera workspace (id 1) → 404")
    ok(client.get("/api/billing").json()["has_billing"] is False, "owera: has_billing false")
    CURRENT["id"] = 3
    ok(client.post("/api/billing/portal").status_code == 404, "workspace without customer → 404")
ok(CALLS == [], "no Stripe call without a customer")
CURRENT["id"] = 2

# Stripe errors → 502, logged without key
for resp in (httpx.Response(400, json={"error": {"code": "resource_missing", "message": "No such customer"}}),
             httpx.Response(500, text="oops")):
    with patch("httpx.post", return_value=resp):
        ok(client.post("/api/billing/portal").status_code == 502, f"Stripe HTTP {resp.status_code} → 502")
with patch("httpx.post", side_effect=httpx.ConnectTimeout("t")):
    ok(client.post("/api/billing/portal").status_code == 502, "network error → 502")
with patch("httpx.post", return_value=httpx.Response(200, json={"url": "https://evil.example/x"})):
    ok(client.post("/api/billing/portal").status_code == 502, "unexpected portal host refused")
ok(KEY not in log.getvalue(), "key never logged")
ok("resource_missing" in log.getvalue(), "Stripe error code logged")

# route is behind auth in the real app (not in the public exemptions)
import app.main as main_mod
settings.app_password = "pw"
c2 = TestClient(main_mod.app)
ok(c2.post("/api/billing/portal").status_code == 401, "real app: /api/billing/portal requires auth")
settings.app_password = ""
print(f"\nALL {_checks} CHECKS PASSED")
