"""Dependency-free regression checks for the analytics pass's publish-quota
reservation (``analytics_loop._publish_reserve``) — the guard that stops the
noon analytics pass from eating the publish drip's headroom.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_analytics_reserve.py

Why this matters: a full 5-publish day costs 8750 of the 9000-unit cap, so an
analytics pass that runs before a channel's publish window opens (ch2: 14:00
UTC, passes at ~00:xx/12:xx) and spends past ~250 units silently costs the
channel its 5th upload of the day. Observed live on 07-28 and 07-31 (ch2
published 4/5 both days, analytics 788/740 units). The reserve holds back
exactly the remaining publishes' cost; it must vanish once the day's publishes
are done (else the post-window pass would starve), for paused channels (no
publish is coming), and for the slice of an over-cap budget the publish gate
itself would refuse (budget 6 x 1750 > 9000 would otherwise pin the reserve
above the whole cap and block analytics forever).

Covers, dependency-free (in-memory SQLite, no network/creds):
  - full reserve before any publish; shrinking per publish; zero after budget.
  - only current-quota-day successes count (old-day and error rows ignored).
  - paused channel -> 0; zero budget -> 0; budget > cap-fits clamped.
  - the derived analytics cap (cap - reserve) reproduces the observed live
    arithmetic: 250 units of analytics coexist with a 5-publish day at 9000.
Exits non-zero on the first failed assertion.
"""
import sys
from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# Importing app.models registers every table on SQLModel.metadata so
# create_all() below builds the full schema.
from app.models import Channel, JobRun
from app.config import settings
from app.services import quota
from app.services.analytics_loop import _publish_reserve, _QUOTA_PER_PUBLISH

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh_session() -> Session:
    """A private in-memory DB per test, so cases can't leak into each other."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(session, *, budget=5, paused=False) -> Channel:
    ch = Channel(name="c", slug="c", daily_publish_budget=budget, paused=paused)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def add_publish(session, channel_id, *, status="success", offset_hours=0):
    session.add(JobRun(channel_id=channel_id, kind="publish", status=status,
                       quota_cost=1600,
                       created_at=quota._quota_day_start() + timedelta(hours=offset_hours)))
    session.commit()


CAP = settings.youtube_daily_quota_cap

print("per-publish cost constant")
ok(_QUOTA_PER_PUBLISH == 1750,
   f"upload+thumbnail+playlist+comment = 1750 (got {_QUOTA_PER_PUBLISH})")

print("reserve before / during / after the day's publishes")
s = fresh_session()
ch = make_channel(s, budget=5)
ok(_publish_reserve(s, ch) == 5 * 1750, "0 published -> full 5-publish reserve (8750)")
add_publish(s, ch.id)
add_publish(s, ch.id)
ok(_publish_reserve(s, ch) == 3 * 1750, "2 published -> 3-publish reserve")
for _ in range(3):
    add_publish(s, ch.id)
ok(_publish_reserve(s, ch) == 0, "budget exhausted -> reserve 0 (post-window pass runs free)")
add_publish(s, ch.id)
ok(_publish_reserve(s, ch) == 0, "over-budget publish still -> 0, never negative")

print("only this quota day's successful publishes count")
s = fresh_session()
ch = make_channel(s, budget=5)
add_publish(s, ch.id, offset_hours=-5)          # previous quota day
add_publish(s, ch.id, status="error")            # failed attempt
ok(_publish_reserve(s, ch) == 5 * 1750,
   "old-day and error publish rows don't shrink the reserve")

print("paused / unset / over-cap budgets")
s = fresh_session()
ch = make_channel(s, budget=5, paused=True)
ok(_publish_reserve(s, ch) == 0, "paused channel reserves nothing")
s = fresh_session()
ch = make_channel(s, budget=0)
ok(_publish_reserve(s, ch) == 0, "zero budget reserves nothing")
s = fresh_session()
ch = make_channel(s, budget=6)                   # model default; 6*1750 > cap
fits = CAP // 1750
ok(_publish_reserve(s, ch) == fits * 1750,
   f"budget 6 clamps to the {fits} publishes that fit under the cap")
for _ in range(fits):
    add_publish(s, ch.id)
ok(_publish_reserve(s, ch) == 0,
   "clamped budget: reserve is 0 after the fittable publishes, analytics never starves")

print("derived analytics cap reproduces the live coexistence arithmetic")
s = fresh_session()
ch = make_channel(s, budget=5)
analytics_cap = CAP - _publish_reserve(s, ch)
ok(analytics_cap == 250,
   "pre-window pass may spend up to 250 units (the exact headroom a 5-publish day leaves)")
ok(analytics_cap + 5 * 1750 <= CAP, "analytics cap + full publish day fits under the cap")

print(f"\nall {_checks} checks passed")
