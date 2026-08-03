"""Dependency-free regression checks for services/scheduler.py.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_scheduler.py

The scheduler is the heartbeat: every loop (render, publish, metrics, analytics,
autofill, BGM replenish) only runs because start() registered it. A regression
here is invisible until a loop silently never ticks again — the exact failure
mode `_safe` exists to prevent ("never let a tick kill the scheduler thread").
It never had a direct test. Covers:
  - `_safe`: a raising tick neither propagates nor goes unlogged (one ERROR
    record with the tick name and traceback); a healthy tick passes through
  - `_music_replenish_tick`: replenishes strictly below bgm_pool_min, not at or
    above it, counting the pool through the real music_gen.pool_count (only
    `techno_*.wav` counts — mp3s/foreign wavs don't; a missing dir counts 0)
  - start(): all six jobs registered, each interval wired to ITS settings field
    (distinct sentinel values so a crossed wire fails), max_instances=1 +
    coalesce=True on every job, the staggered first runs (metrics/analytics/
    autofill/music within minutes of startup; render/publish wait a full
    interval), UTC timezone, and idempotence (a second start() is a no-op)
  - the registered job funcs are the _safe-wrapped ticks: ALL SIX are driven
    directly; each stub raises its own exception class, so per-loop routing,
    crash containment (however narrow a refactored except clause gets), and
    the crash log naming the right tick are proven for every loop
  - shutdown(): actually stops the scheduler thread AND clears the global so a
    later start() works; double-shutdown is a no-op
  - the UTC pin is forced honest: TZ is set to a non-UTC zone before start(),
    so a dropped timezone="UTC" fails even on a UTC-configured host/CI

Real BackgroundScheduler, started and shut down for real; the five loop ticks
are stubbed with recorders BEFORE start() so no real loop can ever run (and the
sentinel intervals put every first fire minutes-to-hours away regardless).
Exits non-zero on the first failed assertion.
"""
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from app.config import settings
from app.services import (
    analytics_loop,
    autofill_loop,
    metrics_loop,
    music_gen,
    publish_loop,
    render_loop,
    scheduler,
)

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def errors(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]


_log = logging.getLogger("manager.scheduler")
_recorder = _Recorder()
_orig_level = _log.level

# Stub every loop tick with a recorder BEFORE any start(): _safe captures the
# function at add_job time, so a registered job can only ever reach these.
_tick_calls = {"render": 0, "publish": 0, "metrics": 0, "analytics": 0, "autofill": 0}

# One exception class per loop: containment and routing get proven per loop,
# and a narrowed `except SomeError` in _safe can't hide behind a shared type.
_EXC = {"render": RuntimeError, "publish": ValueError, "metrics": KeyError,
        "analytics": IndexError, "autofill": TypeError,
        "music_replenish": ArithmeticError}


def _bump(name):
    def tick():
        _tick_calls[name] += 1
        raise _EXC[name](f"{name} stub boom")
    return tick


_ORIG = {
    "render": render_loop.tick, "publish": publish_loop.tick,
    "metrics": metrics_loop.tick, "analytics": analytics_loop.tick,
    "autofill": autofill_loop.tick, "replenish": music_gen.replenish,
}
_ORIG_SETTINGS = {k: getattr(settings, k) for k in (
    "bgm_dir", "bgm_pool_min", "bgm_pool_target", "render_tick_seconds",
    "publish_tick_seconds", "metrics_tick_hours", "analytics_tick_hours",
    "autofill_tick_minutes")}

_replenish_calls = []
_tmp = None  # assigned once the bgm tempdir exists; finally guards on it
_orig_tz = os.environ.get("TZ")


def _fake_replenish():
    _replenish_calls.append(1)
    return 3


def _raising_replenish():
    _replenish_calls.append(1)
    raise _EXC["music_replenish"]("replenish stub boom")


try:
    _log.addHandler(_recorder)
    _log.setLevel(logging.DEBUG)

    # --- _safe: the tick-crash containment wrapper ---------------------------
    print("_safe wrapper")

    ran = []
    scheduler._safe(lambda: ran.append(1), "demo")()
    ok(ran == [1], "_safe calls through to the wrapped tick")
    ok(_recorder.errors() == [], "a healthy tick logs no error")

    def _boom():
        raise RuntimeError("tick exploded")

    propagated = False
    try:
        scheduler._safe(_boom, "render")()
    except Exception:
        propagated = True
    ok(not propagated, "a raising tick never propagates out of the wrapper")
    errs = _recorder.errors()
    ok(len(errs) == 1, "the crash is logged exactly once at ERROR")
    ok("render tick failed" in errs[0].getMessage(),
       "the error names which tick failed")
    ok(errs[0].exc_info is not None and errs[0].exc_info[0] is RuntimeError,
       "the error carries the traceback (logger.exception, not .error)")

    # --- _music_replenish_tick: strictly-below-min gate ----------------------
    print("_music_replenish_tick threshold")

    music_gen.replenish = _fake_replenish
    tmp = _tmp = Path(tempfile.mkdtemp(prefix="verify-sched-bgm-"))
    settings.bgm_dir = str(tmp)
    settings.bgm_pool_min = 2
    settings.bgm_pool_target = 15  # a mutant comparing against target must fail

    (tmp / "techno_a.wav").write_bytes(b"x")
    (tmp / "techno_b.wav").write_bytes(b"x")
    scheduler._music_replenish_tick()
    ok(_replenish_calls == [], "pool at exactly min does NOT replenish (>= not >)")

    (tmp / "techno_c.wav").write_bytes(b"x")
    scheduler._music_replenish_tick()
    ok(_replenish_calls == [], "pool above min does not replenish")

    (tmp / "techno_b.wav").unlink()
    (tmp / "techno_c.wav").unlink()
    scheduler._music_replenish_tick()
    ok(len(_replenish_calls) == 1, "pool below min replenishes exactly once per tick")

    # the count must come from pool_count's semantics: only techno_*.wav is pool
    _replenish_calls.clear()
    (tmp / "techno_a.wav").unlink()
    (tmp / "techno_old.mp3").write_bytes(b"x")
    (tmp / "ambient.wav").write_bytes(b"x")
    scheduler._music_replenish_tick()
    ok(len(_replenish_calls) == 1,
       "mp3s and non-techno wavs don't count as pool (real pool_count semantics)")

    _replenish_calls.clear()
    settings.bgm_dir = str(tmp / "does-not-exist")
    scheduler._music_replenish_tick()
    ok(len(_replenish_calls) == 1, "a missing bgm dir counts as an empty pool")
    _replenish_calls.clear()

    # --- start(): registration, wiring, staggered first runs ------------------
    print("start() job wiring")

    render_loop.tick = _bump("render")
    publish_loop.tick = _bump("publish")
    metrics_loop.tick = _bump("metrics")
    analytics_loop.tick = _bump("analytics")
    autofill_loop.tick = _bump("autofill")

    # Make the UTC pin honest everywhere: on a UTC-configured host a dropped
    # timezone="UTC" would be invisible, so force a non-UTC zone first.
    os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, no DST
    time.tzset()

    # Distinct sentinel intervals: a job wired to the wrong settings field fails.
    settings.render_tick_seconds = 1111
    settings.publish_tick_seconds = 2222
    settings.metrics_tick_hours = 33
    settings.analytics_tick_hours = 44
    settings.autofill_tick_minutes = 55

    ok(scheduler._scheduler is None, "no scheduler exists before start()")
    from datetime import datetime, timezone
    t0 = datetime.now(timezone.utc)
    scheduler.start()
    t1 = datetime.now(timezone.utc)
    sch = scheduler._scheduler
    ok(sch is not None and sch.running, "start() brings up a running scheduler")
    ok(str(sch.timezone) == "UTC", "the scheduler runs in UTC, not the host tz")

    jobs = {j.id: j for j in sch.get_jobs()}
    ok(set(jobs) == {"render", "publish", "metrics", "analytics", "autofill",
                     "music_replenish"},
       "exactly the six loops are registered, by id")

    ok(jobs["render"].trigger.interval == timedelta(seconds=1111),
       "render interval comes from render_tick_seconds")
    ok(jobs["publish"].trigger.interval == timedelta(seconds=2222),
       "publish interval comes from publish_tick_seconds")
    ok(jobs["metrics"].trigger.interval == timedelta(hours=33),
       "metrics interval comes from metrics_tick_hours")
    ok(jobs["analytics"].trigger.interval == timedelta(hours=44),
       "analytics interval comes from analytics_tick_hours")
    ok(jobs["autofill"].trigger.interval == timedelta(minutes=55),
       "autofill interval comes from autofill_tick_minutes")
    ok(jobs["music_replenish"].trigger.interval == timedelta(hours=24),
       "music replenish runs daily")

    for jid, job in jobs.items():
        ok(job.max_instances == 1, f"{jid}: max_instances=1 (ticks never overlap)")
        ok(job.coalesce is True, f"{jid}: coalesce=True (missed runs collapse to one)")

    # Staggered first runs: the snapshot/refill loops start within minutes of
    # boot (trend data accumulates right away); render/publish wait one interval.
    for jid, offset in (("metrics", 30), ("analytics", 60), ("autofill", 45),
                        ("music_replenish", 120)):
        lo = t0 + timedelta(seconds=offset - 1)
        hi = t1 + timedelta(seconds=offset + 1)
        ok(lo <= jobs[jid].next_run_time <= hi,
           f"{jid}: first run ~{offset}s after startup, not a full interval away")
    for jid in ("render", "publish"):
        ok(jobs[jid].next_run_time >= t0 + timedelta(seconds=600),
           f"{jid}: no explicit first run — waits its interval (no thundering start)")

    ok(all(v == 0 for v in _tick_calls.values()),
       "no tick fired during the test window (sentinel intervals + stagger)")

    # --- every registered func is ITS loop's tick, _safe-wrapped -------------
    print("all six job funcs reach the right tick, crash-contained")

    for jid in ("render", "publish", "metrics", "analytics", "autofill"):
        _recorder.records.clear()
        before = dict(_tick_calls)
        contained = True
        try:
            jobs[jid].func()  # every stub raises — _safe must contain it
        except Exception:
            contained = False
        ok(contained, f"{jid}: registered job is _safe-wrapped (crash contained)")
        bumped = {k for k, v in _tick_calls.items() if v != before[k]}
        ok(bumped == {jid}, f"{jid}: job drives {jid}'s tick and no other loop")
        errs2 = _recorder.errors()
        ok(len(errs2) == 1 and f"{jid} tick failed" in errs2[0].getMessage(),
           f"{jid}: the contained crash is logged naming the {jid} tick")
        ok(errs2[0].exc_info is not None and errs2[0].exc_info[0] is _EXC[jid],
           f"{jid}: the logged crash is {jid}'s own exception type")

    _recorder.records.clear()
    music_gen.replenish = _raising_replenish
    settings.bgm_dir = str(tmp / "does-not-exist")  # pool 0 < min
    contained = True
    try:
        jobs["music_replenish"].func()
    except Exception:
        contained = False
    ok(contained, "music_replenish: registered job is _safe-wrapped (crash contained)")
    ok(len(_replenish_calls) == 1,
       "the music_replenish job drives _music_replenish_tick for real")
    errs2 = _recorder.errors()
    ok(len(errs2) == 1 and "music_replenish tick failed" in errs2[0].getMessage(),
       "music_replenish: the contained crash is logged naming the tick")
    ok(errs2[0].exc_info is not None and errs2[0].exc_info[0] is _EXC["music_replenish"],
       "music_replenish: the logged crash is the replenish stub's own exception type")

    # --- idempotence + shutdown/restart --------------------------------------
    print("idempotence, shutdown, restart")

    scheduler.start()
    ok(scheduler._scheduler is sch, "a second start() keeps the same scheduler")
    ok(len(sch.get_jobs()) == 6, "a second start() registers no duplicate jobs")

    scheduler.shutdown()
    ok(not sch.running, "shutdown() actually stops the scheduler, not just the global")
    ok(scheduler._scheduler is None, "shutdown() clears the global")
    try:
        scheduler.shutdown()
        double_ok = True
    except Exception:
        double_ok = False
    ok(double_ok, "a second shutdown() is a harmless no-op")

    scheduler.start()
    ok(scheduler._scheduler is not None and scheduler._scheduler is not sch,
       "start() after shutdown() builds a fresh scheduler")
    ok(len(scheduler._scheduler.get_jobs()) == 6,
       "the fresh scheduler carries all six jobs again")
    scheduler.shutdown()
    ok(scheduler._scheduler is None, "final shutdown leaves no scheduler behind")
finally:
    if scheduler._scheduler is not None:  # a failed check above may leave it up
        scheduler.shutdown()
    render_loop.tick = _ORIG["render"]
    publish_loop.tick = _ORIG["publish"]
    metrics_loop.tick = _ORIG["metrics"]
    analytics_loop.tick = _ORIG["analytics"]
    autofill_loop.tick = _ORIG["autofill"]
    music_gen.replenish = _ORIG["replenish"]
    for k, v in _ORIG_SETTINGS.items():
        setattr(settings, k, v)
    if _orig_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _orig_tz
    time.tzset()
    if _tmp is not None:
        shutil.rmtree(_tmp, ignore_errors=True)
    _log.removeHandler(_recorder)
    _log.setLevel(_orig_level)

print(f"\nALL {_checks} CHECKS PASSED")
