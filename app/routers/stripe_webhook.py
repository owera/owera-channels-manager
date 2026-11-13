"""Stripe billing webhooks — public, signature-verified."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session

from app.config import settings
from app.db import get_session
from app.services import stripe_provision, stripe_verify

logger = logging.getLogger("manager.stripe_webhook")

router = APIRouter(prefix="/api/stripe", tags=["stripe"])


@router.post("/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)):
    secret = (settings.stripe_webhook_secret or "").strip()
    if not secret:
        return Response(status_code=503, content="Stripe webhook not configured")

    raw = await request.body()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe_verify.construct_event(raw, sig_header, secret)
    except stripe_verify.WebhookNotConfigured:
        return Response(status_code=503, content="Stripe webhook not configured")
    except stripe_verify.SignatureVerificationError:
        return Response(status_code=400, content="Invalid signature")

    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        return Response(status_code=400, content="Missing event id")

    logger.info("stripe event id=%s type=%s", event_id, event_type)

    status = stripe_provision.process_event(session, event)
    if status != 200:
        return Response(status_code=status, content="handler error")
    return Response(status_code=200, content="ok")
