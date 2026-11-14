"""S2 (PO CM 14/11): week-1 render/publish budget for buyer workspaces.

A *buyer* workspace = NOT in the internal LLM allowlist
(``MANAGER_LLM_INTERNAL_WORKSPACE_IDS``) AND created through the S2 wizard
(``wizard_started_at``) or Stripe (``paid_at`` / ``stripe_customer_id``).
Internal workspaces (ws1/ws2) and their channels (OS / RR) never change.

- A buyer's new channel defaults to ``BUYER_DAILY_RENDER`` / ``BUYER_DAILY_PUBLISH``
  (2/day) unless the caller set the budgets explicitly.
- Day-1 burst: during the first ``BURST_HOURS`` after the channel was created,
  the render loop lets it render up to ``DAY1_BURST_RENDERS`` (3) per day, so the
  first draft plus two more land on day one. Publish budget is not burst.
"""

from __future__ import annotations

from datetime import timedelta, timezone

from app.models import utcnow

BUYER_DAILY_RENDER = 2
BUYER_DAILY_PUBLISH = 2
DAY1_BURST_RENDERS = 3
BURST_HOURS = 24


def is_buyer(ws) -> bool:
    if ws is None or getattr(ws, "id", None) is None:
        return False
    from app.services import llm_guard
    if int(ws.id) in llm_guard.internal_workspace_ids():
        return False
    return bool(getattr(ws, "wizard_started_at", None) or getattr(ws, "paid_at", None)
                or getattr(ws, "stripe_customer_id", None))


def buyer_budgets() -> dict:
    return {"daily_render_budget": BUYER_DAILY_RENDER, "daily_publish_budget": BUYER_DAILY_PUBLISH}


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def in_burst(session, channel, now=None) -> bool:
    from app.models import Workspace
    created = _aware(getattr(channel, "created_at", None))
    # No null rescue: a None budget TypeErrors here exactly like the loop's own
    # comparison did (tests/verify_channels.py pins that).
    if created is None or channel.daily_render_budget <= 0:
        return False
    now = _aware(now) or utcnow()
    if now - created >= timedelta(hours=BURST_HOURS):
        return False
    return is_buyer(session.get(Workspace, channel.workspace_id))


def effective_render_budget(session, channel, now=None) -> int:
    """The render loop's daily cap for ``channel`` (burst-aware). Non-buyer
    channels and buyer channels after day 1: ``daily_render_budget`` unchanged.
    A 0 budget (operator stall) is never lifted."""
    b = channel.daily_render_budget
    if in_burst(session, channel, now):   # TypeError on None (no `or 0` rescue)
        return max(b, DAY1_BURST_RENDERS)
    return b
