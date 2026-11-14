"""S5 — ops-only: create an E2E test workspace without a Stripe charge.

    PYTHONPATH=. uv run python -m app.provision_test_workspace --email <google account>

Writes one ``Workspace`` row with ``owner_email`` (so SPA Google login resolves
it through ``spa_google_login.workspace_for_email``, no restart, no allowlist
edit) and ``is_test=True``. No Stripe call, no welcome/payment email
(``welcome_email_sent`` is left False and nothing is sent), and ``paid_at`` is
never written (A0 must not show a fake payment).

Idempotent: if a workspace already has this owner_email, it is returned
unchanged (``"created": false``) — including a real paid workspace, which is
never flipped to test. Prints one JSON object. Exit 0 ok, 2 refused.

Not exposed over HTTP. Run on claw0 only with CTO's go (it writes prod DB).
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlmodel import Session, select

OWERA_WORKSPACE_ID = 1


def provision(session: Session, email: str, name: str | None = None) -> dict:
    from app.models import Workspace
    from app.services import spa_google_login
    from app.services.stripe_provision import _slug_from_email, _workspace_name_from_email

    key = spa_google_login.normalized_email(email)
    if not key or "@" not in key or key.startswith("@") or key.endswith("@"):
        return {"ok": False, "error": "invalid email"}
    if key in spa_google_login._SPA_EMAIL_WORKSPACE:
        return {"ok": False, "error": "email is on the owera SPA allowlist; refusing"}

    existing = session.exec(select(Workspace).where(Workspace.owner_email == key)).first()
    if existing is not None:
        if existing.id == OWERA_WORKSPACE_ID:
            return {"ok": False, "error": "email belongs to workspace 1 (owera); refusing"}
        return {"ok": True, "created": False, **_summary(existing)}

    ws = Workspace(
        slug=_slug_from_email(key, session),
        name=(name or "").strip()[:80] or f"{_workspace_name_from_email(key)} (test)",
        owner_email=key,
        is_test=True,
    )
    session.add(ws)
    session.flush()
    if ws.id == OWERA_WORKSPACE_ID:  # never attach a test buyer to owera
        session.rollback()
        return {"ok": False, "error": "new workspace got id=1; refusing"}
    session.commit()
    session.refresh(ws)
    return {"ok": True, "created": True, **_summary(ws)}


def _summary(ws) -> dict:
    from app.services.activation import iso_utc
    return {
        "workspace_id": ws.id, "slug": ws.slug, "name": ws.name,
        "owner_email": ws.owner_email, "is_test": bool(ws.is_test),
        "paid_at": iso_utc(ws.paid_at),
        "stripe_customer_id": ws.stripe_customer_id,
        "stripe_subscription_id": ws.stripe_subscription_id,
        "welcome_email_sent": bool(ws.welcome_email_sent),
        "created_at": iso_utc(ws.created_at),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.provision_test_workspace",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--email", required=True, help="Google account that will sign in")
    ap.add_argument("--name", default=None, help="display name (default: from email)")
    args = ap.parse_args(argv)

    from app.db import engine, init_db
    init_db()  # additive columns (is_test/paid_at/...) — same as app startup
    with Session(engine) as session:
        out = provision(session, args.email, args.name)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
