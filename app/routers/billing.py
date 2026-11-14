"""Workspace billing: status + Stripe Customer Portal session ("Manage billing").

Authenticated like every /api route (Basic Auth / SPA session) and scoped to the
caller's workspace. Uses the Stripe customer id saved at provisioning
(Workspace.stripe_customer_id). Creates a portal *session* only; the portal
configuration in Stripe is never read or changed here.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.models import Workspace
from app.workspace import current_workspace

logger = logging.getLogger("manager.billing")

router = APIRouter(prefix="/api/billing", tags=["billing"])

STRIPE_PORTAL_URL = "https://api.stripe.com/v1/billing_portal/sessions"
OWERA_WORKSPACE_ID = 1


def _customer_id(ws: Workspace) -> str | None:
    if ws.id == OWERA_WORKSPACE_ID:
        return None
    cid = (ws.stripe_customer_id or "").strip()
    return cid if cid.startswith("cus_") else None


@router.get("")
def billing_status(ws: Workspace = Depends(current_workspace)):
    """What the Settings page needs to show (or hide) the Manage billing button."""
    return {
        "subscription_status": ws.subscription_status,
        "has_billing": _customer_id(ws) is not None,
        "portal_enabled": bool((settings.stripe_secret_key or "").strip()),
    }


@router.post("/portal")
def create_portal_session(ws: Workspace = Depends(current_workspace)):
    """Create a Stripe Customer Portal session for this workspace's customer and
    return its one-time URL. The SPA redirects there; Stripe sends the buyer back
    to MANAGER_STRIPE_PORTAL_RETURN_URL."""
    key = (settings.stripe_secret_key or "").strip()
    if not key:
        raise HTTPException(503, "billing portal not configured")
    customer = _customer_id(ws)
    if not customer:
        raise HTTPException(404, "no Stripe billing for this workspace")
    try:
        r = httpx.post(
            STRIPE_PORTAL_URL,
            data={"customer": customer, "return_url": settings.stripe_portal_return_url},
            headers={"Authorization": f"Bearer {key}"},
            timeout=15.0,
        )
    except Exception as e:
        logger.warning("portal session for workspace id=%s failed: %s", ws.id, type(e).__name__)
        raise HTTPException(502, "could not reach Stripe")
    if r.status_code != 200:
        code = ""
        try:
            code = (r.json().get("error") or {}).get("code") or ""
        except Exception:
            pass
        logger.warning("portal session for workspace id=%s: Stripe HTTP %s %s",
                       ws.id, r.status_code, code)
        raise HTTPException(502, "Stripe could not open the billing portal")
    url = (r.json() or {}).get("url") or ""
    if not url.startswith("https://billing.stripe.com/"):
        logger.warning("portal session for workspace id=%s: unexpected url host", ws.id)
        raise HTTPException(502, "Stripe returned an unexpected portal URL")
    logger.info("portal session created for workspace id=%s", ws.id)
    return {"url": url}
