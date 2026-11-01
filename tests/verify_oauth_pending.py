"""WS4 PR-B: oauth_pending + PKCE persistence checks."""
from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, OAuthPending, OAuthStatus, utcnow
from app.services import oauth_pending as op

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(id=1, slug="ch1", name="Ch1", oauth_status=OAuthStatus.DISCONNECTED))
    s.commit()

with Session(engine) as s:
    v, c = op.new_pkce_pair()
    ok(len(v) > 40 and len(c) > 20, "PKCE pair generated")
    op.create_pending(s, state="state-abc", channel_id=1, workspace_id=1,
                      code_verifier=v, redirect_uri="http://localhost/cb")
    row = s.exec(__import__("sqlmodel").select(OAuthPending)).first()
    ok(row and row.code_verifier == v, "pending row persisted")

with Session(engine) as s:
    p = op.consume_pending(s, state="state-abc", channel_id=1, workspace_id=1)
    ok(p is not None, "consume returns pending for matching state/channel/workspace")
    p2 = op.consume_pending(s, state="state-abc", channel_id=1, workspace_id=1)
    ok(p2 is None, "second consume is single-use")

with Session(engine) as s:
    op.create_pending(s, state="other-ws", channel_id=1, workspace_id=1,
                      code_verifier="v", redirect_uri="http://x")
    bad = op.consume_pending(s, state="other-ws", channel_id=1, workspace_id=2)
    ok(bad is None, "cross-workspace state rejected")
    ok(op.has_unconsumed_state(s, "other-ws"), "cross-workspace miss does not consume row")

with Session(engine) as s:
    op.create_pending(s, state="expired", channel_id=1, workspace_id=1,
                      code_verifier="v", redirect_uri="http://x")
    row = s.exec(__import__("sqlmodel").select(OAuthPending).where(
        OAuthPending.state_hash == op._state_hash("expired"))).one()
    row.expires_at = utcnow() - timedelta(minutes=1)
    s.add(row)
    s.commit()
    ex = op.consume_pending(s, state="expired", channel_id=1, workspace_id=1)
    ok(ex is None, "expired pending not returned")

print(f"\nALL {_checks} CHECKS PASSED")
