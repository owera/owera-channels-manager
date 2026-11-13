"""Stripe checkout idempotency — event.id AND checkout session.id, race-safe.

Two live endpoints (or Stripe retries) can deliver checkout.session.completed
for the same Checkout Session under different event ids, at the same time.
Exactly one workspace and at most one welcome email must result. OSI one-time
Payment Link checkouts on the shared account must never provision.

Run: PYTHONPATH=. uv run python tests/verify_stripe_idempotency.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

os.environ.setdefault("MANAGER_APP_PASSWORD", "")
os.environ.setdefault("MANAGER_SPA_GOOGLE_LOGIN", "0")

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import StripeCheckoutSession, StripeEvent, Workspace
from app.services import stripe_provision

CM_LINK = "plink_1UL1ccADrWLjH4u9HD9GC4n4"
OSI_LINK = "plink_1UXkXiADrWLjH4u9j4dUmYIH"
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def ev(event_id, cs_id, *, email="buyer@example.com", sub="sub_1", cus="cus_1",
       link=CM_LINK, mode="subscription"):
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {"id": cs_id, "mode": mode, "payment_link": link,
                                "customer": cus, "subscription": sub,
                                "customer_details": {"email": email}}}}


def file_engine():
    d = tempfile.mkdtemp()
    eng = create_engine(f"sqlite:///{d}/t.db", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(Workspace(id=1, slug="owera", name="Owera"))
        s.commit()
    return eng


SENT: list[str] = []
_lock = threading.Lock()


def fake_send(email):
    time.sleep(0.05)              # widen the race window
    with _lock:
        SENT.append(email)
    return True


def count_ws(eng, **kw):
    with Session(eng) as s:
        q = select(Workspace).where(Workspace.id != 1)
        for k, v in kw.items():
            q = q.where(getattr(Workspace, k) == v)
        return len(s.exec(q).all())


# --- 1. same session, two event ids, sequential (two endpoints / retry) ---
eng = file_engine()
SENT.clear()
with patch("app.services.stripe_email.send_welcome_email", side_effect=fake_send):
    with Session(eng) as s:
        ok(stripe_provision.process_event(s, ev("evt_a", "cs_1")) == 200, "first event 200")
    with Session(eng) as s:
        ok(stripe_provision.process_event(s, ev("evt_b", "cs_1")) == 200, "second event id, same session 200")
ok(count_ws(eng) == 1, "one workspace for one checkout session")
ok(len(SENT) == 1, "one welcome email for one checkout session")
with Session(eng) as s:
    claim = s.get(StripeCheckoutSession, "cs_1")
    ok(claim is not None and claim.event_id == "evt_a" and claim.workspace_id, "session claim row links event + workspace")
    ok(s.get(StripeEvent, "evt_b") is not None, "second event id still recorded")

# --- 2. OSI one-time checkout on the shared account never provisions ---
SENT.clear()
with patch("app.services.stripe_email.send_welcome_email", side_effect=fake_send):
    with Session(eng) as s:
        r = stripe_provision.process_event(
            s, ev("evt_osi", "cs_osi", email="osi@buyer.com", sub=None, cus="cus_osi",
                  link=OSI_LINK, mode="payment"))
ok(r == 200, "OSI checkout 200")
ok(count_ws(eng, owner_email="osi@buyer.com") == 0, "OSI checkout creates no CM workspace")
ok(len(SENT) == 0, "OSI checkout sends no CM welcome")

# --- 3. no payment_link: only subscription mode provisions ---
with patch("app.services.stripe_email.send_welcome_email", side_effect=fake_send):
    with Session(eng) as s:
        stripe_provision.process_event(s, ev("evt_pay", "cs_pay", email="p@b.com", sub=None, link=None, mode="payment"))
    with Session(eng) as s:
        stripe_provision.process_event(s, ev("evt_subm", "cs_subm", email="m@b.com", sub="sub_m", cus="cus_m", link=None))
ok(count_ws(eng, owner_email="p@b.com") == 0, "payment-mode session without link ignored")
ok(count_ws(eng, owner_email="m@b.com") == 1, "subscription-mode session without link provisions")


def race(eng, events):
    barrier = threading.Barrier(len(events))
    codes = []

    def run(e):
        barrier.wait()
        with Session(eng) as s:
            c = stripe_provision.process_event(s, e)
        with _lock:
            codes.append((e["id"], c))

    ts = [threading.Thread(target=run, args=(e,)) for e in events]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return codes


# --- 4. concurrent: different event ids, same session ---
for n in (2, 6):
    eng = file_engine()
    SENT.clear()
    events = [ev(f"evt_r{n}_{i}", "cs_race", email="race@buyer.com") for i in range(n)]
    with patch("app.services.stripe_email.send_welcome_email", side_effect=fake_send):
        codes = race(eng, events)
        ok(all(c in (200, 500) for _, c in codes), f"[{n}x] concurrent deliveries return 200/500 only")
        ok(count_ws(eng) == 1, f"[{n}x] concurrent different event ids → exactly one workspace")
        ok(len(SENT) <= 1, f"[{n}x] concurrent → at most one welcome email")
        # Stripe retries anything that got 500; retries are no-ops.
        for eid, c in codes:
            if c != 200:
                with Session(eng) as s:
                    ok(stripe_provision.process_event(s, next(e for e in events if e["id"] == eid)) == 200,
                       f"[{n}x] retry of {eid} succeeds")
    ok(count_ws(eng) == 1, f"[{n}x] after retries still one workspace")
    ok(len(SENT) == 1, f"[{n}x] after retries exactly one welcome email")

# --- 5. concurrent: same event id delivered twice ---
eng = file_engine()
SENT.clear()
with patch("app.services.stripe_email.send_welcome_email", side_effect=fake_send):
    codes = race(eng, [ev("evt_same", "cs_same", email="same@buyer.com")] * 3)
ok(count_ws(eng) == 1, "concurrent same event id → one workspace")
ok(len(SENT) == 1 or any(c == 500 for _, c in codes), "concurrent same event id → one email (or retried)")
ok(len(SENT) <= 1, "concurrent same event id → never two emails")

# --- 6. failed send releases the welcome claim; next event retries once ---
eng = file_engine()
SENT.clear()
with patch("app.services.stripe_email.send_welcome_email", return_value=False):
    with Session(eng) as s:
        stripe_provision.process_event(s, ev("evt_f1", "cs_f1", email="f@b.com", sub="sub_f", cus="cus_f"))
with Session(eng) as s:
    w = s.exec(select(Workspace).where(Workspace.owner_email == "f@b.com")).one()
    ok(w.welcome_email_sent is False, "failed send leaves welcome_email_sent False")
    wid = w.id
with patch("app.services.stripe_email.send_welcome_email", side_effect=fake_send):
    with Session(eng) as s:
        ok(stripe_provision.deliver_welcome(s, wid) is True, "deliver_welcome retries after failure")
    with Session(eng) as s:
        ok(stripe_provision.deliver_welcome(s, wid) is False, "deliver_welcome is once-only")
ok(SENT == ["f@b.com"], "exactly one email after retry")

# --- 7. unique index on workspace.stripe_subscription_id; tolerant migration ---
import app.db as db_mod
from sqlalchemy.exc import IntegrityError

eng = file_engine()
with patch.object(db_mod, "engine", eng):
    db_mod._ensure_unique_stripe_subscription()
    db_mod._ensure_unique_stripe_subscription()      # idempotent
with Session(eng) as s:
    s.add(Workspace(slug="u1", name="u1", stripe_subscription_id="sub_u"))
    s.add(Workspace(slug="n1", name="n1"))
    s.add(Workspace(slug="n2", name="n2"))
    s.commit()
try:
    with Session(eng) as s:
        s.add(Workspace(slug="u2", name="u2", stripe_subscription_id="sub_u"))
        s.commit()
    ok(False, "duplicate subscription id should be rejected")
except IntegrityError:
    ok(True, "unique index rejects a second workspace for one subscription (NULLs allowed)")

eng = file_engine()
with Session(eng) as s:
    s.add(Workspace(slug="d1", name="d1", stripe_subscription_id="sub_dup"))
    s.add(Workspace(slug="d2", name="d2", stripe_subscription_id="sub_dup"))
    s.commit()
with patch.object(db_mod, "engine", eng):
    db_mod._ensure_unique_stripe_subscription()
ok(True, "existing duplicates: index skipped with a warning, startup does not fail")

# --- 8. IntegrityError path (row committed by a racing writer after our pre-check) ---
eng = file_engine()
with Session(eng) as s:
    s.add(StripeCheckoutSession(id="cs_x", event_id="evt0"))
    s.add(StripeEvent(id="evt_dup", event_type="x"))
    s.commit()
with Session(eng) as s:
    with patch.object(Session, "get", return_value=None):      # simulate a lost race
        ok(stripe_provision.claim_checkout_session(s, "cs_x", "evt1") is False,
           "claim hits PK IntegrityError → treated as already provisioned")
        ok(stripe_provision.record_event_idempotent(s, "evt_dup", "x") is False,
           "event PK IntegrityError → treated as replay")
        ok(stripe_provision.record_event_idempotent(s, "evt_new", "x") is True,
           "savepoint rollback keeps the outer transaction usable")
    s.commit()
with Session(eng) as s:
    ok(s.get(StripeEvent, "evt_new") is not None, "outer transaction commits after a savepoint rollback")

print(f"\nALL {_checks} CHECKS PASSED")
