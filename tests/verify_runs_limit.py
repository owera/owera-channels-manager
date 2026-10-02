"""Regression checks for GET /api/runs and /api/agent/state query limits.

SQLite treats LIMIT -1 as "no upper bound". Both handlers passed the query
int straight to .limit(), so limit=-1 returned every JobRun. The dashboard
asks for 60 and the growth playbook for 100; those stay inside 1..100.
limit=0 is an empty page that looks like a quiet audit log, so it is 400
too. True is an int equal to 1 and must not become LIMIT 1.

Run: PYTHONPATH=. uv run python tests/verify_runs_limit.py

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, lifespan/scheduler never started). Exits non-zero on the first
failure.
"""
import inspect
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus
from app.routers import queue as queue_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(queue_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "queue module loaded from this tree")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

base = datetime(2026, 10, 1, tzinfo=timezone.utc)
with Session(engine) as s:
    # Disconnected so agent_state does not call YouTube analytics.
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.DISCONNECTED))
    s.add(Channel(slug="b", name="B", oauth_status=OAuthStatus.DISCONNECTED))
    s.commit()
    for i in range(3):
        s.add(JobRun(channel_id=1, kind="render", status="success",
                     detail=f"r{i}", created_at=base + timedelta(minutes=i)))
    s.add(JobRun(channel_id=2, kind="render", status="success", detail="other",
                 created_at=base + timedelta(minutes=10)))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
_orig_bgm = settings.bgm_dir
settings.app_password = "testpw"
# pool_count on agent_state must not scan the live music pool.
settings.bgm_dir = str(Path(tempfile.mkdtemp(prefix="verify-runs-limit-")) / "missing")
client = TestClient(main.app)
auth = ("x", "testpw")


def _status(resp_or_exc) -> str:
    if isinstance(resp_or_exc, HTTPException):
        return f"HTTP {resp_or_exc.status_code}"
    if isinstance(resp_or_exc, BaseException):
        return type(resp_or_exc).__name__
    return f"HTTP {resp_or_exc.status_code}"


def _get(url: str, **kwargs):
    try:
        return client.get(url, auth=auth, **kwargs)
    except Exception as e:
        return e


try:
    print("GET /api/runs rejects an unbounded limit")
    r = _get("/api/runs?limit=-1")
    ok(_status(r) == "HTTP 400",
       f"limit=-1 is 400, not every JobRun ({_status(r)})")
    if hasattr(r, "json"):
        ok("unbounded" in str(r.json().get("detail", "")),
           "limit=-1 names the SQLite unbounded LIMIT")
    else:
        ok(False, "limit=-1 did not return an HTTP response")

    r = _get("/api/runs?limit=0")
    ok(_status(r) == "HTTP 400",
       f"limit=0 is 400, not an empty audit page ({_status(r)})")

    r = _get("/api/runs?limit=101")
    ok(_status(r) == "HTTP 400",
       f"limit=101 is 400, past the documented ceiling ({_status(r)})")

    r = client.get("/api/runs?limit=-1")
    ok(r.status_code == 401, "unauthenticated limit=-1 is 401")

    r = _get("/api/runs?limit=true")
    ok(_status(r) == "HTTP 422",
       f"limit=true is 422, not coerced to 1 ({_status(r)})")

    print("GET /api/runs still pages inside 1..100")
    r = client.get("/api/runs?limit=2", auth=auth)
    ok(r.status_code == 200, "limit=2 is 200")
    details = [row["detail"] for row in r.json()]
    ok(details == ["other", "r2"],
       f"limit=2 is the two newest rows (got {details})")

    r = client.get("/api/runs?limit=60", auth=auth)
    ok(r.status_code == 200 and len(r.json()) == 4,
       "the dashboard's limit=60 still returns every row we have")

    r = client.get("/api/runs?limit=100", auth=auth)
    ok(r.status_code == 200 and len(r.json()) == 4,
       "the playbook's limit=100 still returns every row we have")

    r = client.get("/api/runs", auth=auth)
    ok(r.status_code == 200 and len(r.json()) == 4,
       "the default limit (100) still returns every row we have")

    r = client.get("/api/runs?limit=10&channel_id=1", auth=auth)
    ok(r.status_code == 200, "channel filter with a legal limit is 200")
    details = [row["detail"] for row in r.json()]
    ok(details == ["r2", "r1", "r0"],
       f"channel_id=1 still excludes the other channel (got {details})")

    print("GET /api/agent/state uses the same ceiling")
    r = _get("/api/agent/state?runs_limit=-1")
    ok(_status(r) == "HTTP 400",
       f"runs_limit=-1 is 400 ({_status(r)})")

    r = client.get("/api/agent/state?runs_limit=1", auth=auth)
    ok(r.status_code == 200, f"runs_limit=1 is 200 ({r.status_code})")
    recent = r.json().get("recent_runs") or []
    ok(len(recent) == 1 and recent[0]["detail"] == "other",
       f"runs_limit=1 is the newest row only (got {[row.get('detail') for row in recent]})")

    r = client.get("/api/agent/state", auth=auth)
    ok(r.status_code == 200 and len(r.json().get("recent_runs") or []) == 4,
       "the default runs_limit (40) still returns every row we have")

    print("direct callers")
    with Session(engine) as s:
        try:
            queue_router.runs(limit=True, session=s)
            bool_result = "returned"
        except HTTPException as e:
            bool_result = f"HTTP {e.status_code}"
        ok(bool_result == "HTTP 400",
           f"runs(limit=True) is 400, not LIMIT 1 ({bool_result})")

    runs_src = inspect.getsource(queue_router.runs)
    state_src = inspect.getsource(queue_router.agent_state)
    ok(runs_src.index("_bounded_runs_limit") < runs_src.index(".limit("),
       "runs bounds the limit before the query")
    ok(state_src.index("_bounded_runs_limit") < state_src.index(".limit("),
       "agent_state bounds runs_limit before the query")
    ok(queue_router._RUNS_LIMIT_MAX == 100,
       "the ceiling stays 100 (dashboard 60 and playbook 100 fit)")
finally:
    settings.app_password = _orig_pw
    settings.bgm_dir = _orig_bgm

print(f"ALL {_checks} CHECKS PASSED")
