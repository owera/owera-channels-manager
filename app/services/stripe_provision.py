"""Stripe webhook event handling — provision workspaces, sync subscription status."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlmodel import Session, select

from app.models import StripeEvent, Workspace, utcnow
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
    """Insert StripeEvent row. Returns True if this is the first time we see event_id."""
    existing = session.get(StripeEvent, event_id)
    if existing:
        return False
    session.add(StripeEvent(id=event_id, event_type=event_type, created_at=utcnow()))
    session.flush()
    return True


def _checkout_email(obj: dict[str, Any]) -> str | None:
    details = obj.get("customer_details") or {}
    email = details.get("email") or obj.get("customer_email")
    return spa_google_login.normalized_email(str(email or "")) or None


def _checkout_collected(data_object: dict[str, Any]) -> bool:
    """False only when Stripe says this checkout has not been paid yet.

    ``payment_status=unpaid`` means the session completed on a delayed
    method and the invoice is not collected. Provisioning then waits for
    ``checkout.session.async_payment_succeeded``. A missing status still
    provisions, so a payload that omits the field is not dropped.
    ``no_payment_required`` (trial or a 100% coupon) is collected.
    """
    status = str(data_object.get("payment_status") or "").strip().lower()
    return status != "unpaid"


def handle_checkout_completed(session: Session, data_object: dict[str, Any]) -> None:
    if not _checkout_collected(data_object):
        logger.info(
            "skipping checkout provision; payment_status=%s",
            data_object.get("payment_status"),
        )
        return
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
            return
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

    if email and not ws.welcome_email_sent:
        if stripe_email.send_welcome_email(email):
            ws.welcome_email_sent = True
            ws.updated_at = utcnow()
            session.add(ws)


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


def dispatch_event(session: Session, event: dict[str, Any]) -> None:
    """Apply side effects for a verified Stripe event (idempotency handled by caller)."""
    ev_type = str(event.get("type") or "")
    data = event.get("data") or {}
    obj = data.get("object") or {}

    if ev_type in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        handle_checkout_completed(session, obj)
    elif ev_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        _sync_subscription_fields(session, obj)
    elif ev_type == "customer.subscription.deleted":
        handle_subscription_deleted(session, obj)
    elif ev_type == "invoice.payment_failed":
        handle_payment_failed(session, obj)
