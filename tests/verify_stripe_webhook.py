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
) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test",
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
    ok(bob.subscription_status == "past_due", "payment_failed → past_due")

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

# --- unpaid checkout must not provision; delayed success must ---
# Stripe sends checkout.session.completed with payment_status=unpaid for a
# delayed method, then checkout.session.async_payment_succeeded once the
# invoice is collected. Fulfillment is the paid (or no_payment_required)
# signal, not the completed event by itself.
def _session_event(event_id, event_type, email, *, payment_status, customer, subscription):
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": {
            "id": "cs_" + event_id,
            "customer": customer,
            "subscription": subscription,
            "payment_status": payment_status,
            "customer_details": {"email": email},
        }},
    }

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    unpaid = _session_event(
        "evt_gina_unpaid", "checkout.session.completed", "gina@buyer.com",
        payment_status="unpaid", customer="cus_gina", subscription="sub_gina_pending",
    )
    ok(post_event(client, unpaid).status_code == 200, "unpaid checkout is 200")
ok(len(_SENT) == 0, "unpaid checkout sends no welcome email")
with Session(engine) as s:
    gina = s.exec(select(Workspace).where(Workspace.owner_email == "gina@buyer.com")).first()
    ok(gina is None, "unpaid checkout does not create a workspace")
    ok(s.get(StripeEvent, "evt_gina_unpaid") is not None,
       "unpaid checkout is recorded so Stripe will not retry it")

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    delayed = _session_event(
        "evt_gina_paid_later", "checkout.session.async_payment_succeeded", "gina@buyer.com",
        payment_status="paid", customer="cus_gina", subscription="sub_gina",
    )
    ok(post_event(client, delayed).status_code == 200, "async_payment_succeeded after unpaid is 200")
ok(len(_SENT) == 1 and _SENT[0]["to"] == ["gina@buyer.com"],
   "async_payment_succeeded sends one welcome email")
with Session(engine) as s:
    gina = s.exec(select(Workspace).where(Workspace.owner_email == "gina@buyer.com")).one()
    ok(gina.id != 1, "delayed checkout workspace is not owera")
    ok(gina.subscription_status == "active", "async_payment_succeeded sets active")
    ok(gina.stripe_subscription_id == "sub_gina", "async_payment_succeeded stores the subscription id")
    ok(gina.welcome_email_sent is True, "async_payment_succeeded marks the welcome email sent")

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    frank_paid = _session_event(
        "evt_frank_paid", "checkout.session.completed", "frank@buyer.com",
        payment_status="paid", customer="cus_frank", subscription="sub_frank",
    )
    ok(post_event(client, frank_paid).status_code == 200, "paid checkout is 200")
with Session(engine) as s:
    frank = s.exec(select(Workspace).where(Workspace.owner_email == "frank@buyer.com")).one()
    ok(frank.subscription_status == "active", "paid checkout sets active")
    ok(frank.stripe_subscription_id == "sub_frank", "paid checkout stores the subscription id")
    frank_id = frank.id
ok(len(_SENT) == 1, "paid checkout sends one welcome email")

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    frank_unpaid = _session_event(
        "evt_frank_unpaid", "checkout.session.completed", "frank@buyer.com",
        payment_status="unpaid", customer="cus_frank", subscription="sub_frank_pending",
    )
    ok(post_event(client, frank_unpaid).status_code == 200, "unpaid resubscribe checkout is 200")
ok(len(_SENT) == 0, "unpaid resubscribe sends no welcome email")
with Session(engine) as s:
    frank = s.get(Workspace, frank_id)
    ok(frank.subscription_status == "active", "unpaid resubscribe leaves the workspace active")
    ok(frank.stripe_subscription_id == "sub_frank",
       "unpaid resubscribe does not replace the subscription id")
    ok(len(s.exec(select(Workspace).where(Workspace.owner_email == "frank@buyer.com")).all()) == 1,
       "unpaid resubscribe does not create a second workspace")

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    frank_later = _session_event(
        "evt_frank_later", "checkout.session.async_payment_succeeded", "frank@buyer.com",
        payment_status="paid", customer="cus_frank", subscription="sub_frank_new",
    )
    ok(post_event(client, frank_later).status_code == 200, "async resubscribe success is 200")
ok(len(_SENT) == 0, "async resubscribe does not send a second welcome email")
with Session(engine) as s:
    frank = s.get(Workspace, frank_id)
    ok(frank.subscription_status == "active", "async resubscribe leaves the workspace active")
    ok(frank.stripe_subscription_id == "sub_frank_new",
       "async resubscribe stores the new subscription id")
    ok(len(s.exec(select(Workspace).where(Workspace.owner_email == "frank@buyer.com")).all()) == 1,
       "async resubscribe does not create a second workspace")

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    failed_delay = _session_event(
        "evt_ira_failed", "checkout.session.async_payment_failed", "ira@buyer.com",
        payment_status="unpaid", customer="cus_ira", subscription="sub_ira",
    )
    ok(post_event(client, failed_delay).status_code == 200, "async_payment_failed is 200")
ok(len(_SENT) == 0, "async_payment_failed sends no welcome email")
with Session(engine) as s:
    ira = s.exec(select(Workspace).where(Workspace.owner_email == "ira@buyer.com")).first()
    ok(ira is None, "async_payment_failed does not create a workspace")

_SENT.clear()
with patch("httpx.post", side_effect=_capture_resend):
    free = _session_event(
        "evt_henry_free", "checkout.session.completed", "henry@buyer.com",
        payment_status="no_payment_required", customer="cus_henry", subscription="sub_henry",
    )
    ok(post_event(client, free).status_code == 200, "no_payment_required checkout is 200")
with Session(engine) as s:
    henry = s.exec(select(Workspace).where(Workspace.owner_email == "henry@buyer.com")).one()
    ok(henry.subscription_status == "active", "no_payment_required checkout sets active")
    ok(henry.stripe_subscription_id == "sub_henry", "no_payment_required checkout stores the subscription id")
ok(len(_SENT) == 1, "no_payment_required checkout sends one welcome email")

print(f"\nALL {_checks} CHECKS PASSED")
