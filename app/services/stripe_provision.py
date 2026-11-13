"""Stripe webhook event handling — provision workspaces, sync subscription status."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.models import StripeCheckoutSession, StripeEvent, Workspace, utcnow
from app.services import spa_google_login, stripe_email

logger = logging.getLogger("manager.stripe_provision")

OWERA_WORKSPACE_ID = 1

_ACTIVE = frozenset({"active", "trialing"})
_INACTIVE = frozenset({"past_due", "unpaid", "canceled", "incomplete_expired"})


def _map_subscription_status(stripe_status: str | None) -> str:
    s = (stripe_status or "").strip().lower()
    if s in _ACTIVE:
        return "active"
    if s == "past_due":
        return "past_due"
    if s == "unpaid":
        return "unpaid"
    if s in ("canceled", "incomplete_expired"):
        return "canceled"
    if s:
        return s
    return "inactive"


def _slug_from_email(email: str, session: Session) -> str:
    local = email.split("@", 1)[0]
    base = re.sub(r"[^a-z0-9]+", "-", local.lower()).strip("-")[:40] or "workspace"
    slug = base
    n = 2
    while session.exec(select(Workspace).where(Workspace.slug == slug)).first():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _workspace_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ")
    return (local.title()[:80] or "Channels workspace").strip()


def _find_workspace(
    session: Session,
    *,
    subscription_id: str | None,
    customer_id: str | None,
    owner_email: str | None,
) -> Workspace | None:
    if subscription_id:
        ws = session.exec(
            select(Workspace).where(Workspace.stripe_subscription_id == subscription_id)
        ).first()
        if ws:
            return ws
    if customer_id:
        ws = session.exec(
            select(Workspace).where(Workspace.stripe_customer_id == customer_id)
        ).first()
        if ws and ws.id != OWERA_WORKSPACE_ID:
            return ws
    if owner_email:
        key = spa_google_login.normalized_email(owner_email)
        ws = session.exec(select(Workspace).where(Workspace.owner_email == key)).first()
        if ws and ws.id != OWERA_WORKSPACE_ID:
            return ws
    return None


def record_event_idempotent(session: Session, event_id: str, event_type: str) -> bool:
    """Insert StripeEvent row. Returns True if this is the first time we see event_id.

    Safe under concurrent delivery of the same event id: the primary key rejects
    the second insert (IntegrityError) and that delivery is treated as a replay."""
    existing = session.get(StripeEvent, event_id)
    if existing:
        return False
    try:
        with session.begin_nested():
            session.add(StripeEvent(id=event_id, event_type=event_type, created_at=utcnow()))
    except IntegrityError:
        return False
    return True


def _cm_payment_links() -> frozenset[str]:
    raw = settings.stripe_cm_payment_links or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def is_cm_checkout(obj: dict[str, Any]) -> bool:
    """True when a Checkout Session belongs to Channels Manager.

    The live Stripe account also sells OSI through one-time Payment Links, and
    every endpoint receives every checkout.session.completed. Only the CM links
    (MANAGER_STRIPE_CM_PAYMENT_LINKS) provision; a session without a
    payment_link provisions only when it is a subscription checkout."""
    link = (obj.get("payment_link") or "").strip()
    if link:
        return link in _cm_payment_links()
    return (obj.get("mode") or "") == "subscription"


def claim_checkout_session(session: Session, checkout_session_id: str, event_id: str) -> bool:
    """Reserve a Checkout Session id. False if another event already provisioned it."""
    if session.get(StripeCheckoutSession, checkout_session_id) is not None:
        return False
    try:
        with session.begin_nested():
            session.add(StripeCheckoutSession(id=checkout_session_id, event_id=event_id))
    except IntegrityError:
        return False
    return True


def _checkout_email(obj: dict[str, Any]) -> str | None:
    details = obj.get("customer_details") or {}
    email = details.get("email") or obj.get("customer_email")
    return spa_google_login.normalized_email(str(email or "")) or None


def handle_checkout_completed(
    session: Session, data_object: dict[str, Any], event_id: str = ""
) -> int | None:
    """Provision for a CM checkout. Returns the workspace id that may need the
    welcome email (sent after commit by ``deliver_welcome``), or None."""
    if not is_cm_checkout(data_object):
        logger.info(
            "checkout.session.completed ignored: not a CM checkout (payment_link=%s mode=%s)",
            data_object.get("payment_link") or "-", data_object.get("mode") or "-",
        )
        return None
    cs_id = (data_object.get("id") or "").strip()
    if cs_id and not claim_checkout_session(session, cs_id, event_id):
        logger.info("checkout session %s already provisioned; skipping", cs_id)
        return None

    email = _checkout_email(data_object)
    customer_id = (data_object.get("customer") or "") or None
    subscription_id = (data_object.get("subscription") or "") or None

    ws = _find_workspace(
        session,
        subscription_id=subscription_id,
        customer_id=customer_id,
        owner_email=email,
    )
    if ws is None:
        if not email:
            logger.warning("checkout.session.completed without buyer email; skipping provision")
            return None
        slug = _slug_from_email(email, session)
        ws = Workspace(
            slug=slug,
            name=_workspace_name_from_email(email),
            owner_email=email,
            subscription_status="active",
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
        )
        session.add(ws)
        session.flush()
        session.refresh(ws)
        if ws.id == OWERA_WORKSPACE_ID:
            logger.error("provision refused: new workspace incorrectly got id=1")
            raise RuntimeError("refusing to attach Stripe buyer to owera workspace")
    else:
        if email and not ws.owner_email:
            ws.owner_email = email
        if customer_id:
            ws.stripe_customer_id = customer_id
        if subscription_id:
            ws.stripe_subscription_id = subscription_id
        ws.subscription_status = "active"
        ws.updated_at = utcnow()
        session.add(ws)

    if cs_id:
        claim = session.get(StripeCheckoutSession, cs_id)
        if claim is not None:
            claim.workspace_id = ws.id
            session.add(claim)
    session.flush()
    if email and not ws.welcome_email_sent:
        return ws.id
    return None


def deliver_welcome(session: Session, workspace_id: int) -> bool:
    """Send the welcome email at most once per workspace, after the provisioning
    commit. The flag is claimed with a conditional UPDATE (atomic in SQLite), so
    two concurrent deliveries cannot both send. A failed send releases the claim
    so a later event can retry. Returns True if this call sent the email."""
    res = session.execute(
        text("UPDATE workspace SET welcome_email_sent = 1, updated_at = :now "
             "WHERE id = :id AND welcome_email_sent = 0 AND owner_email IS NOT NULL "
             "AND id != :owera"),
        {"id": workspace_id, "now": utcnow(), "owera": OWERA_WORKSPACE_ID},
    )
    session.commit()
    if res.rowcount != 1:
        return False
    ws = session.get(Workspace, workspace_id)
    session.refresh(ws)
    if ws and ws.owner_email and stripe_email.send_welcome_email(ws.owner_email):
        return True
    session.execute(
        text("UPDATE workspace SET welcome_email_sent = 0 WHERE id = :id"),
        {"id": workspace_id},
    )
    session.commit()
    return False


def _sync_subscription_fields(session: Session, sub: dict[str, Any]) -> None:
    subscription_id = (sub.get("id") or "") or None
    customer_id = (sub.get("customer") or "") or None
    status = _map_subscription_status(sub.get("status"))

    ws = _find_workspace(
        session,
        subscription_id=subscription_id,
        customer_id=customer_id,
        owner_email=None,
    )
    if not ws or ws.id == OWERA_WORKSPACE_ID:
        return
    if customer_id:
        ws.stripe_customer_id = customer_id
    if subscription_id:
        ws.stripe_subscription_id = subscription_id
    ws.subscription_status = status
    ws.updated_at = utcnow()
    session.add(ws)


def handle_subscription_deleted(session: Session, sub: dict[str, Any]) -> None:
    subscription_id = (sub.get("id") or "") or None
    customer_id = (sub.get("customer") or "") or None
    ws = _find_workspace(
        session,
        subscription_id=subscription_id,
        customer_id=customer_id,
        owner_email=None,
    )
    if not ws or ws.id == OWERA_WORKSPACE_ID:
        return
    ws.subscription_status = "canceled"
    ws.updated_at = utcnow()
    session.add(ws)


def handle_payment_failed(session: Session, invoice: dict[str, Any]) -> None:
    subscription_id = (invoice.get("subscription") or "") or None
    customer_id = (invoice.get("customer") or "") or None
    ws = _find_workspace(
        session,
        subscription_id=subscription_id,
        customer_id=customer_id,
        owner_email=None,
    )
    if not ws or ws.id == OWERA_WORKSPACE_ID:
        return
    ws.subscription_status = "past_due"
    ws.updated_at = utcnow()
    session.add(ws)


def dispatch_event(session: Session, event: dict[str, Any]) -> int | None:
    """Apply side effects for a verified Stripe event (idempotency handled by caller).
    Returns a workspace id whose welcome email should be delivered after commit."""
    ev_type = str(event.get("type") or "")
    data = event.get("data") or {}
    obj = data.get("object") or {}

    if ev_type == "checkout.session.completed":
        return handle_checkout_completed(session, obj, str(event.get("id") or ""))
    elif ev_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        _sync_subscription_fields(session, obj)
    elif ev_type == "customer.subscription.deleted":
        handle_subscription_deleted(session, obj)
    elif ev_type == "invoice.payment_failed":
        handle_payment_failed(session, obj)
    return None


def process_event(session: Session, event: dict[str, Any]) -> int:
    """Record + apply one verified event in a single transaction. Returns an HTTP
    status: 200 handled/replay/ignored, 500 handler error (Stripe retries).

    Concurrency (SQLite): a second delivery that races the first either waits for
    the write lock and then sees the committed StripeEvent/StripeCheckoutSession
    row (no-op), or gets "database is locked" and returns 500, so Stripe retries
    later and the retry is a no-op. Either way nothing is provisioned twice."""
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    try:
        first_time = record_event_idempotent(session, event_id, event_type)
        if not first_time:
            session.commit()
            return 200
        welcome_ws = dispatch_event(session, event)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("stripe event id=%s type=%s handler failed", event_id, event_type)
        return 500
    if welcome_ws is not None:
        try:
            deliver_welcome(session, welcome_ws)
        except Exception:
            session.rollback()
            logger.exception("welcome delivery failed for workspace id=%s", welcome_ws)
    return 200
