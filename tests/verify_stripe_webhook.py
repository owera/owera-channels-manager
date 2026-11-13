"""Stripe provisioning webhook — signature, provision, idempotency, auth exempt.

Run: PYTHONPATH=. uv run python tests/verify_stripe_webhook.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault("MANAGER_APP_PASSWORD", "")
os.environ.setdefault("MANAGER_SPA_GOOGLE_LOGIN", "0")
os.environ["MANAGER_STRIPE_WEBHOOK_SECRET"] = ""
os.environ["MANAGER_RESEND_API_KEY"] = "re_test_no_send"

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main_mod
from app.config import settings
from app.db import get_session
from app.models import StripeEvent, Workspace

_checks = 0
TEST_SECRET = "whsec_test_signing_secret_for_hmac_key"
_SENT: list[dict] = []


def stripe_sign_payload(payload: bytes, secret: str, ts: int | None = None) -> tuple[int, str]:
    """Stripe-compatible v1 signature (independent of app.services.stripe_verify)."""
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.{payload.decode('utf-8')}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return ts, sig


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def sign_header(payload: bytes, secret: str = TEST_SECRET, ts: int | None = None, *, extra_v1: str | None = None) -> str:
    ts_i, sig = stripe_sign_payload(payload, secret, ts=ts)
    if extra_v1:
        return f"t={ts_i},v1={extra_v1},v1={sig}"
    return f"t={ts_i},v1={sig}"


def checkout_event(
    event_id: str,
    *,
    email: str,
    customer: str = "cus_test",
    subscription: str = "sub_test",
    cs_id: str | None = None,
    mode: str = "subscription",
    payment_link: str | None = "plink_1UL1ccADrWLjH4u9HD9GC4n4",
) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": cs_id or f"cs_{event_id}",
                "mode": mode,
                "payment_link": payment_link,
                "customer": customer,
                "subscription": subscription,
                "customer_details": {"email": email},
            }
        },
    }


def post_event(client: TestClient, event: dict, secret: str = TEST_SECRET, sig: str | None = None):
    body = json.dumps(event).encode("utf-8")
    headers = {}
    if sig is not None:
        headers["Stripe-Signature"] = sig
    elif secret:
        headers["Stripe-Signature"] = sign_header(body, secret)
    return client.post("/api/stripe/webhook", content=body, headers=headers)


def _capture_resend(*args, **kwargs):
    payload = kwargs.get("json") or {}
    _SENT.append(payload)
    class _Resp:
        def raise_for_status(self):
            return None
    return _Resp()


def make_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


engine = make_engine()
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.commit()


def _override_session():
    with Session(engine) as session:
        yield session


main_mod.app.dependency_overrides[get_session] = _override_session
client = TestClient(main_mod.app)

# --- secret unset → 503 ---
settings.stripe_webhook_secret = ""
r = post_event(client, checkout_event("evt_unconfigured", email="buyer@example.com"), secret="")
ok(r.status_code == 503, "secret unset returns 503")

settings.stripe_webhook_secret = TEST_SECRET

from app.services import stripe_verify

# --- unit: correct key material (full whsec_ string, not base64 body) ---
_probe = {"id": "evt_unit", "type": "ping"}
_probe_bytes = json.dumps(_probe).encode("utf-8")
_ts, _sig = stripe_sign_payload(_probe_bytes, TEST_SECRET)
_unit_hdr = f"t={_ts},v1={_sig}"
parsed = stripe_verify.construct_event(_probe_bytes, _unit_hdr, TEST_SECRET)
ok(parsed["id"] == "evt_unit", "construct_event accepts Stripe-style HMAC key")
try:
    import stripe as stripe_sdk

    stripe_sdk.WebhookSignature.verify_header(
        _probe_bytes.decode("utf-8"),
        _unit_hdr,
        TEST_SECRET,
        tolerance=300,
    )
    ok(True, "official stripe package agrees with our signature")
except ImportError:
    print("  skip: stripe package not installed (uv sync --group dev)")
except Exception as e:
    ok(False, f"official stripe package verify_header failed: {e}")

# --- timestamp tolerance ---
_stale_ts = int(time.time()) - 301
_stale_hdr = sign_header(_probe_bytes, TEST_SECRET, ts=_stale_ts)
try:
    stripe_verify.construct_event(_probe_bytes, _stale_hdr, TEST_SECRET)
    ok(False, "stale timestamp should raise")
except stripe_verify.SignatureVerificationError:
    ok(True, "timestamp outside 300s rejected")

# --- multiple v1 signatures: first wrong, second correct ---
_bad_sig = "0" * 64
_good_hdr = sign_header(_probe_bytes, TEST_SECRET, extra_v1=_bad_sig)
parsed2 = stripe_verify.construct_event(_probe_bytes, _good_hdr, TEST_SECRET)
ok(parsed2["id"] == "evt_unit", "accepts when any v1 matches")

# --- bad / missing signature → 400 ---
body = json.dumps(checkout_event("evt_bad", email="x@y.com")).encode()
r = client.post("/api/stripe/webhook", content=body)
ok(r.status_code == 400, "missing signature 400")

r = client.post(
    "/api/stripe/webhook",
    content=body,
    headers={"Stripe-Signature": "t=1,v1=deadbeef"},
)
ok(r.status_code == 400, "bad signature 400")

# current timestamp + wrong-length / non-hex v1 must 400 (not 500 from compare_digest)
_now = int(time.time())
r = client.post(
    "/api/stripe/webhook",
    content=body,
    headers={"Stripe-Signature": f"t={_now},v1=deadbeef"},
)
ok(r.status_code == 400, "wrong-length v1 at current ts returns 400")
r = client.post(
    "/api/stripe/webhook",
    content=body,
    headers={"Stripe-Signature": f"t={_now},v1=not-valid-hex-and-also-wrong-len"},
)
ok(r.status_code == 400, "non-hex wrong-length v1 at current ts returns 400")

# --- valid signature → 200 + provision ---
settings.resend_api_key = "re_test_key"
_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    r = post_event(client, checkout_event("evt_checkout_1", email="alice@buyer.com"))
ok(r.status_code == 200, "valid signature 200")

with Session(engine) as s:
    ws = s.exec(select(Workspace).where(Workspace.owner_email == "alice@buyer.com")).first()
    ok(ws is not None and ws.id != 1, "workspace created, not owera")
    ok(ws.subscription_status == "active", "subscription_status active")
    ok(ws.stripe_customer_id == "cus_test", "stripe_customer_id stored")
    ok(ws.stripe_subscription_id == "sub_test", "stripe_subscription_id stored")
    ok(ws.welcome_email_sent is True, "welcome_email_sent set")
    ok(len(_SENT) == 1 and _SENT[0]["to"] == ["alice@buyer.com"], "welcome email sent once")
    ok(_SENT[0].get("reply_to") == "contact@owera.com", "reply-to contact@owera.com")

import app.db as db_mod
from app.services import spa_google_login

with patch.object(db_mod, "engine", engine):
    mapped = spa_google_login.workspace_for_email("alice@buyer.com")
ok(mapped == ws.slug, "SPA login maps buyer email to new workspace")

# --- replay same event id → no duplicate ---
_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    r2 = post_event(client, checkout_event("evt_checkout_1", email="alice@buyer.com"))
ok(r2.status_code == 200, "replay same event id returns 200")
ok(len(_SENT) == 0, "replay sends no second welcome email")

with Session(engine) as s:
    count = len(s.exec(select(Workspace).where(Workspace.owner_email == "alice@buyer.com")).all())
    ok(count == 1, "replay does not duplicate workspace")

# --- second event same subscription → no duplicate workspace ---
with patch("httpx.post", side_effect=_capture_resend):
    r3 = post_event(
        client,
        checkout_event("evt_checkout_2", email="alice@buyer.com", subscription="sub_test"),
    )
ok(r3.status_code == 200, "second checkout same subscription 200")
with Session(engine) as s:
    count = len(s.exec(select(Workspace).where(Workspace.stripe_subscription_id == "sub_test")).all())
    ok(count == 1, "same subscription does not create second workspace")

# --- deleted + payment_failed keep data, mark inactive ---
with Session(engine) as s:
    prov = Workspace(
        slug="bob-ws",
        name="Bob",
        owner_email="bob@buyer.com",
        subscription_status="active",
        stripe_customer_id="cus_bob",
        stripe_subscription_id="sub_bob",
    )
    s.add(prov)
    s.commit()
    s.refresh(prov)
    bob_id = prov.id

del_event = {
    "id": "evt_sub_del",
    "type": "customer.subscription.deleted",
    "data": {"object": {"id": "sub_bob", "customer": "cus_bob", "status": "canceled"}},
}
r4 = post_event(client, del_event)
ok(r4.status_code == 200, "subscription.deleted 200")
with Session(engine) as s:
    bob = s.get(Workspace, bob_id)
    ok(bob is not None and bob.subscription_status == "canceled", "deleted → canceled, row kept")

fail_event = {
    "id": "evt_inv_fail",
    "type": "invoice.payment_failed",
    "data": {"object": {"subscription": "sub_bob", "customer": "cus_bob"}},
}
r5 = post_event(client, fail_event)
ok(r5.status_code == 200, "invoice.payment_failed 200")
with Session(engine) as s:
    bob = s.get(Workspace, bob_id)
    # P6: a dunning failure after the subscription was deleted keeps it canceled.
    ok(bob.subscription_status == "canceled", "payment_failed after cancel keeps canceled")

# --- unknown type → 200 ---
unk = {"id": "evt_unknown", "type": "customer.created", "data": {"object": {}}}
ok(post_event(client, unk).status_code == 200, "unknown event type 200")

# --- no Basic auth required ---
settings.app_password = "agent-secret"
client2 = TestClient(main_mod.app)
ok(
    post_event(client2, checkout_event("evt_noauth", email="carol@buyer.com")).status_code == 200,
    "webhook needs no Basic auth",
)

# --- rodrigo never emailed ---
_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    post_event(client, checkout_event("evt_rodrigo", email="rodrigo@owera.com", subscription="sub_rod"))
ok(len(_SENT) == 0, "no welcome email to rodrigo@owera.com")

with Session(engine) as s:
    rod = s.exec(select(Workspace).where(Workspace.stripe_subscription_id == "sub_rod")).first()
    ok(rod is None or rod.id != 1, "rodrigo checkout did not attach to owera workspace id 1")

# --- StripeEvent table exists ---
with Session(engine) as s:
    ok(s.get(StripeEvent, "evt_checkout_1") is not None, "StripeEvent row recorded")

print(f"\nALL {_checks} CHECKS PASSED")
