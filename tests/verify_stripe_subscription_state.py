"""P6: subscription state from invoice.paid / invoice.payment_failed /
customer.subscription.updated|deleted — signed fixtures through the real route,
idempotent, ordered by event.created. No live Stripe calls, no secrets logged.

Run: PYTHONPATH=. uv run python tests/verify_stripe_subscription_state.py
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import sys
import time
from datetime import datetime

os.environ.setdefault("MANAGER_APP_PASSWORD", "")
os.environ.setdefault("MANAGER_SPA_GOOGLE_LOGIN", "0")

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main_mod
from app.config import settings
from app.db import get_session
from app.models import StripeEvent, Workspace

SECRET = "whsec_test_p6_fixture_secret"
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def post(event: dict, secret: str = SECRET):
    body = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return client.post("/api/stripe/webhook", content=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"})


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera", stripe_customer_id="cus_owera", stripe_subscription_id="sub_owera"))
    s.add(Workspace(id=2, slug="buyer", name="Buyer", subscription_status="active",
                    stripe_customer_id="cus_B", stripe_subscription_id="sub_B"))
    s.commit()


def _sess():
    with Session(engine) as session:
        yield session


main_mod.app.dependency_overrides[get_session] = _sess
client = TestClient(main_mod.app)
settings.stripe_webhook_secret = SECRET

log = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(log))
logging.getLogger().setLevel(logging.INFO)

T0 = 1_800_000_000
PERIOD_END = T0 + 30 * 86400


def ws(i=2) -> Workspace:
    with Session(engine) as s:
        return s.get(Workspace, i)


def ev(eid, etype, obj, created):
    return {"id": eid, "type": etype, "created": created, "data": {"object": obj}}


def inv_failed(eid, created, *, new_api=False):
    obj = {"id": "in_1", "customer": "cus_B"}
    if new_api:
        obj["parent"] = {"subscription_details": {"subscription": "sub_B"}}
    else:
        obj["subscription"] = "sub_B"
    return ev(eid, "invoice.payment_failed", obj, created)


def inv_paid(eid, created, *, new_api=True, period_end=PERIOD_END):
    obj = {"id": "in_2", "customer": "cus_B", "status_transitions": {"paid_at": created},
           "lines": {"data": [{"period": {"start": created, "end": period_end}}]}}
    if new_api:
        obj["parent"] = {"subscription_details": {"subscription": "sub_B"}}
    else:
        obj["subscription"] = "sub_B"
    return ev(eid, "invoice.paid", obj, created)


def sub_updated(eid, created, status, *, cancel=False, new_api=True):
    obj = {"id": "sub_B", "customer": "cus_B", "status": status, "cancel_at_period_end": cancel}
    if new_api:
        obj["items"] = {"data": [{"current_period_end": PERIOD_END}]}
    else:
        obj["current_period_end"] = PERIOD_END
    return ev(eid, "customer.subscription.updated", obj, created)


# 1. payment failed → past_due (old API shape)
ok(post(inv_failed("evt_f1", T0 + 10)).status_code == 200, "invoice.payment_failed 200")
ok(ws().subscription_status == "past_due", "payment_failed → past_due")
ok(ws().stripe_state_event_created == T0 + 10, "applied event.created recorded")

# 2. invoice.paid (new API shape: parent.subscription_details) recovers → active
ok(post(inv_paid("evt_p1", T0 + 20)).status_code == 200, "invoice.paid 200")
w = ws()
ok(w.subscription_status == "active", "invoice.paid recovers past_due → active")
ok(w.last_invoice_paid_at == datetime.utcfromtimestamp(T0 + 20), "last_invoice_paid_at stored")
ok(w.subscription_current_period_end == datetime.utcfromtimestamp(PERIOD_END), "paid-through (period end) stored")

# 3. a late, older payment_failed is ignored (ordering by created)
ok(post(inv_failed("evt_f0_late", T0 + 5, new_api=True)).status_code == 200, "late event 200")
ok(ws().subscription_status == "active", "older payment_failed arriving late does not demote")
ok("ignored: older" in log.getvalue(), "out-of-order event logged")

# 4. replay of an applied event is a no-op (event.id idempotency)
with Session(engine) as s:
    w = s.get(Workspace, 2); w.subscription_status = "active"; s.add(w); s.commit()
ok(post(inv_failed("evt_f1", T0 + 10)).status_code == 200 and ws().subscription_status == "active",
   "replayed event id does nothing")

# 5. subscription.updated: cancel at period end, then past_due/unpaid mapping (items[] period end)
ok(post(sub_updated("evt_u1", T0 + 30, "active", cancel=True)).status_code == 200, "subscription.updated 200")
w = ws()
ok(w.subscription_cancel_at_period_end is True and w.stripe_raw_status == "active", "cancel_at_period_end stored")
ok(w.subscription_current_period_end == datetime.utcfromtimestamp(PERIOD_END), "period end from items[] (new API)")
post(sub_updated("evt_u2", T0 + 40, "unpaid", new_api=False))
ok(ws().subscription_status == "unpaid", "unpaid mapped (old API shape)")
post(sub_updated("evt_u3", T0 + 50, "active"))
ok(ws().subscription_status == "active" and ws().subscription_cancel_at_period_end is False, "reactivated, cancel flag cleared")

# 6. deleted → canceled (data kept); later-created paid/failed do not revive; older updated ignored
ok(post(ev("evt_d1", "customer.subscription.deleted",
           {"id": "sub_B", "customer": "cus_B", "status": "canceled"}, T0 + 60)).status_code == 200, "deleted 200")
ok(ws().subscription_status == "canceled" and ws() is not None, "deleted → canceled, workspace kept")
post(sub_updated("evt_u_old", T0 + 55, "active"))
ok(ws().subscription_status == "canceled", "older subscription.updated after delete ignored")
post(inv_failed("evt_f_after", T0 + 70))
ok(ws().subscription_status == "canceled", "payment_failed after delete keeps canceled")

# 7. same-second tie applies (Stripe gives no finer order)
with Session(engine) as s:
    w = s.get(Workspace, 2); w.subscription_status = "active"; w.stripe_state_event_created = T0 + 100
    s.add(w); s.commit()
post(inv_failed("evt_tie", T0 + 100))
ok(ws().subscription_status == "past_due", "same-second event applies")

# 8. one-time invoice (no subscription) and unknown customer: ignored
post(ev("evt_onetime", "invoice.paid", {"id": "in_x", "customer": "cus_B"}, T0 + 200))
ok(ws().subscription_status == "past_due", "one-time invoice.paid (no subscription) ignored")
r = post(ev("evt_unknown", "invoice.paid", {"id": "in_y", "customer": "cus_nobody",
                                             "subscription": "sub_nobody"}, T0 + 200))
ok(r.status_code == 200, "unknown customer → 200, nothing to update")

# 9. owera workspace never touched
post(ev("evt_ow", "customer.subscription.deleted", {"id": "sub_owera", "customer": "cus_owera"}, T0 + 300))
ok(ws(1).subscription_status == "trialing", "owera workspace (id 1) never changed by Stripe events")

# 10. bad signature rejected; secret never logged
ok(post(inv_paid("evt_bad", T0 + 400), secret="whsec_wrong").status_code == 400, "bad signature 400")
ok(SECRET not in log.getvalue(), "signing secret never logged")
with Session(engine) as s:
    ok(s.get(StripeEvent, "evt_p1") is not None, "events recorded for idempotency")

print(f"\nALL {_checks} CHECKS PASSED")
