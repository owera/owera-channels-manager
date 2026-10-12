"""metrics/analytics jobs tolerate late wake-ups (misfire_grace_time=3600).

2026-10-10..12: with APScheduler's default 1s grace, every metrics/analytics run
that woke late was dropped ("was missed by"), so snapshots stopped on 10/10.

Run: PYTHONPATH=. .venv/bin/python tests/verify_scheduler_misfire.py
"""
import sys
from unittest.mock import patch
from app.services import scheduler

captured = {}
class FakeSched:
    def __init__(self, *a, **k): pass
    def add_job(self, func, trigger, **kw): captured[kw.get("id")] = kw
    def start(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: None

with patch.object(scheduler, "BackgroundScheduler", FakeSched):
    scheduler._scheduler = None
    scheduler.start()
fails = 0
for jid in ("metrics", "analytics"):
    g = captured.get(jid, {}).get("misfire_grace_time")
    good = g == 3600
    fails += not good
    print(("  ok:" if good else "FAIL:"), jid, "misfire_grace_time =", g)
scheduler._scheduler = None
if fails:
    sys.exit(1)
print("ALL 2 CHECKS PASSED")
