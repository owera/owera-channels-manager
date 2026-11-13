"""Stripe webhook event handling — provision workspaces, sync subscription status."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
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


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).replace(tzinfo=None) if v else None
    except (TypeError, ValueError, OSError):
        return None


def _invoice_subscription_id(invoice: dict[str, Any]) -> str | None:
    """Invoice → subscription id across API versions (top-level `subscription`
    before 2025-03-31.basil; `parent.subscription_details.subscription` after)."""
    sub = invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if not sub:
        details = ((invoice.get("parent") or {}).get("subscription_details") or {})
        sub = details.get("subscription")
    return (sub or "") or None


def _subscription_period_end(sub: dict[str, Any]) -> datetime | None:
    """Subscription → current period end across API versions (top-level before
    basil; per item `items.data[].current_period_end` after)."""
    if sub.get("current_period_end"):
        return _ts(sub.get("current_period_end"))
    items = ((sub.get("items") or {}).get("data") or [])
    ends = [i.get("current_period_end") for i in items if i.get("current_period_end")]
    return _ts(max(ends)) if ends else None


def _invoice_period_end(invoice: dict[str, Any]) -> datetime | None:
    lines = ((invoice.get("lines") or {}).get("data") or [])
    ends = [((ln.get("period") or {}).get("end")) for ln in lines]
    ends = [e for e in ends if e]
    return _ts(max(ends)) if ends else None


def _state_workspace(session: Session, *, subscription_id: str | None,
                     customer_id: str | None) -> Workspace | None:
    ws = _find_workspace(session, subscription_id=subscription_id,
                         customer_id=customer_id, owner_email=None)
    if not ws or ws.id == OWERA_WORKSPACE_ID:
        return None
    return ws


def _in_order(ws: Workspace, event_created: int | None, ev_type: str) -> bool:
    """True if this event is not older than the last applied state change.
    Ties apply (same second; Stripe gives no finer order)."""
    if event_created is None or ws.stripe_state_event_created is None:
        return True
    if event_created < ws.stripe_state_event_created:
        logger.info("stripe %s for workspace id=%s ignored: older (created=%s) than applied state (%s)",
                    ev_type, ws.id, event_created, ws.stripe_state_event_created)
        return False
    return True


def _apply(session: Session, ws: Workspace, event_created: int | None, *, status: str | None = None,
           raw: str | None = None, period_end: datetime | None = None,
           cancel_at_period_end: bool | None = None, paid_at: datetime | None = None,
           customer_id: str | None = None, subscription_id: str | None = None) -> None:
    if status is not None:
        ws.subscription_status = status
    if raw is not None:
        ws.stripe_raw_status = raw
    if period_end is not None:
        ws.subscription_current_period_end = period_end
    if cancel_at_period_end is not None:
        ws.subscription_cancel_at_period_end = bool(cancel_at_period_end)
    if paid_at is not None:
        ws.last_invoice_paid_at = paid_at
    if customer_id:
        ws.stripe_customer_id = customer_id
    if subscription_id:
        ws.stripe_subscription_id = subscription_id
    if event_created is not None:
        ws.stripe_state_event_created = max(event_created, ws.stripe_state_event_created or 0)
    ws.updated_at = utcnow()
    session.add(ws)


def _sync_subscription_fields(session: Session, sub: dict[str, Any],
                              event_created: int | None = None,
                              ev_type: str = "customer.subscription.updated") -> None:
    subscription_id = (sub.get("id") or "") or None
    customer_id = (sub.get("customer") or "") or None
    ws = _state_workspace(session, subscription_id=subscription_id, customer_id=customer_id)
    if not ws or not _in_order(ws, event_created, ev_type):
        return
    _apply(session, ws, event_created,
           status=_map_subscription_status(sub.get("status")),
           raw=(sub.get("status") or None),
           period_end=_subscription_period_end(sub),
           cancel_at_period_end=bool(sub.get("cancel_at_period_end")),
           customer_id=customer_id, subscription_id=subscription_id)


def handle_subscription_deleted(session: Session, sub: dict[str, Any],
                                event_created: int | None = None) -> None:
    subscription_id = (sub.get("id") or "") or None
    customer_id = (sub.get("customer") or "") or None
    ws = _state_workspace(session, subscription_id=subscription_id, customer_id=customer_id)
    if not ws or not _in_order(ws, event_created, "customer.subscription.deleted"):
        return
    # Data is retained; only the plan state changes.
    _apply(session, ws, event_created, status="canceled", raw=sub.get("status") or "canceled",
           cancel_at_period_end=False)


def handle_payment_failed(session: Session, invoice: dict[str, Any],
                          event_created: int | None = None) -> None:
    subscription_id = _invoice_subscription_id(invoice)
    customer_id = (invoice.get("customer") or "") or None
    ws = _state_workspace(session, subscription_id=subscription_id, customer_id=customer_id)
    if not ws or not _in_order(ws, event_created, "invoice.payment_failed"):
        return
    if ws.subscription_status == "canceled":
        return          # a dunning retry after cancel never revives or demotes it
    _apply(session, ws, event_created, status="past_due", raw="past_due")


def handle_invoice_paid(session: Session, invoice: dict[str, Any],
                        event_created: int | None = None) -> None:
    """invoice.paid: record paid-through, and recover past_due/unpaid → active."""
    subscription_id = _invoice_subscription_id(invoice)
    customer_id = (invoice.get("customer") or "") or None
    if not subscription_id:
        return          # one-time invoice (not a CM subscription)
    ws = _state_workspace(session, subscription_id=subscription_id, customer_id=customer_id)
    if not ws or not _in_order(ws, event_created, "invoice.paid"):
        return
    paid_at = _ts((invoice.get("status_transitions") or {}).get("paid_at")) or utcnow()
    status = "active" if ws.subscription_status in ("past_due", "unpaid", "inactive", "trialing") else None
    _apply(session, ws, event_created, status=status, raw="active" if status else None,
           period_end=_invoice_period_end(invoice), paid_at=paid_at)


def dispatch_event(session: Session, event: dict[str, Any]) -> int | None:
    """Apply side effects for a verified Stripe event (idempotency handled by caller).
    Returns a workspace id whose welcome email should be delivered after commit."""
    ev_type = str(event.get("type") or "")
    data = event.get("data") or {}
    obj = data.get("object") or {}

    created = event.get("created")
    try:
        created = int(created) if created is not None else None
    except (TypeError, ValueError):
        created = None

    if ev_type == "checkout.session.completed":
        return handle_checkout_completed(session, obj, str(event.get("id") or ""))
    elif ev_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        _sync_subscription_fields(session, obj, created, ev_type)
    elif ev_type == "customer.subscription.deleted":
        handle_subscription_deleted(session, obj, created)
    elif ev_type == "invoice.payment_failed":
        handle_payment_failed(session, obj, created)
    elif ev_type == "invoice.paid":
        handle_invoice_paid(session, obj, created)
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
