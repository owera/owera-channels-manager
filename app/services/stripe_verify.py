"""Stripe webhook signature verification (no live API calls, no stripe package)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class WebhookNotConfigured(Exception):
    pass


class SignatureVerificationError(Exception):
    pass


def _secret_bytes(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        return base64.b64decode(secret[6:])
    return secret.encode("utf-8")


def construct_event(
    payload: bytes,
    sig_header: str | None,
    secret: str,
    *,
    tolerance: int = 300,
) -> dict:
    """Verify Stripe-Signature and parse the JSON event body."""
    if not (secret or "").strip():
        raise WebhookNotConfigured()
    if not sig_header:
        raise SignatureVerificationError("missing signature header")

    timestamp: str | None = None
    signatures: list[str] = []
    for part in sig_header.split(","):
        key, _, val = part.partition("=")
        key = key.strip()
        val = val.strip()
        if key == "t":
            timestamp = val
        elif key == "v1":
            signatures.append(val)

    if not timestamp or not signatures:
        raise SignatureVerificationError("malformed signature header")

    try:
        ts = int(timestamp)
    except ValueError as e:
        raise SignatureVerificationError("invalid timestamp") from e

    if abs(time.time() - ts) > tolerance:
        raise SignatureVerificationError("timestamp outside tolerance")

    try:
        body = payload.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SignatureVerificationError("payload not utf-8") from e

    signed_payload = f"{timestamp}.{body}".encode("utf-8")
    expected = hmac.new(_secret_bytes(secret), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise SignatureVerificationError("signature mismatch")

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise SignatureVerificationError("invalid json") from e
