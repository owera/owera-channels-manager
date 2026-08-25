"""Regression checks for POST /api/topics/{id}/generate.

The 2026-08-24 overflow skip left a residual: generate still used
``t.weight or 1``, so a parked (weight=0) topic could be hand-filled up
to the 1× ceiling — undoing a park via the dashboard button AND the
growth agent's Feed-winners call (``POST /api/topics/{id}/generate``).
These checks pin that generate uses the same ``weight <= 0`` gate as
autofill / overflow, and that live topics still generate.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, no LLM). ``video_gen.generate_ideas`` is stubbed and recorded;
the app lifespan/scheduler are never started. Exits non-zero on the
first failed assertion.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.routers import topics as topics_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# Isolated-copy batteries pin this so a stale pyc / wrong PYTHONPATH cannot
# silently test a different checkout (08-01 lesson).
ok(Path(topics_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "topics module loaded from this tree")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

# High render budget so the board-horizon cap (budget × 2 = 40) does not
# bind before the per-topic idea ceiling (target 6 × weight).
with Session(engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=20))
    s.commit()
    s.add(Topic(channel_id=1, name="Parked", theme_prompt="parked theme",
                weight=0, content_format="short"))          # id 1
    s.add(Topic(channel_id=1, name="Live", theme_prompt="live theme",
                weight=1, content_format="short"))          # id 2
    s.add(Topic(channel_id=1, name="Heavy", theme_prompt="heavy theme",
                weight=2, content_format="long"))           # id 3
    s.add(Topic(channel_id=1, name="Neg", theme_prompt="neg theme",
                weight=-1, content_format="short"))         # id 4
    s.add(Topic(channel_id=1, name="Full", theme_prompt="full theme",
                weight=1, content_format="short"))          # id 5
    s.commit()
    for i in range(6):
        s.add(Video(channel_id=1, topic_id=5, subject=f"full-draft-{i}",
                    status=VideoStatus.DRAFT))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

_orig_ideas = topics_router.video_gen.generate_ideas
calls: list[dict] = []


def fake_ideas(topic_name, theme_prompt, existing, n=8, content_format="short",
               language=None):
    calls.append({
        "topic_name": topic_name,
        "theme_prompt": theme_prompt,
        "existing": list(existing),
        "n": n,
        "content_format": content_format,
        "language": language,
    })
    return [f"{topic_name}-idea-{i}" for i in range(n)]


topics_router.video_gen.generate_ideas = fake_ideas


def post_generate(topic_id, count=8):
    return client.post(f"/api/topics/{topic_id}/generate", auth=auth,
                       json={"count": count})


def draft_count(topic_id):
    with Session(engine) as s:
        return s.exec(select(func.count(Video.id)).where(
            Video.topic_id == topic_id, Video.status == VideoStatus.DRAFT
        )).one()


def generate_runs():
    with Session(engine) as s:
        return s.exec(select(JobRun).where(JobRun.kind == "generate")).all()


try:
    print("POST /api/topics/{id}/generate: parked weight<=0 writes nothing")
    r = post_generate(1, count=8)
    ok(r.status_code == 200, "parked generate returns 200 (same shape as other no-ops)")
    body = r.json()
    ok(body.get("generated") == 0,
       "weight=0 generate writes zero ideas (or-1 would fill to the 1× ceiling)")
    ok("parked" in (body.get("reason") or "").lower(),
       "parked reason names the park, not 'idea ceiling reached'")
    ok(calls == [], "parked generate never called generate_ideas")
    ok(draft_count(1) == 0, "parked topic still has zero drafts")
    ok(generate_runs() == [], "parked generate writes no JobRun")

    r = post_generate(4, count=8)
    ok(r.status_code == 200, "weight=-1 generate returns 200")
    body = r.json()
    ok(body.get("generated") == 0,
       "weight=-1 generate writes zero ideas")
    ok("parked" in (body.get("reason") or "").lower(),
       "weight=-1 uses the parked reason (discriminates if-not-weight, which is 0-only)")
    ok(calls == [], "weight=-1 generate never called generate_ideas")
    ok(draft_count(4) == 0, "weight=-1 topic still has zero drafts")

    print("POST /api/topics/{id}/generate: live topics still generate")
    r = post_generate(2, count=8)
    ok(r.status_code == 200, "live generate returns 200")
    body = r.json()
    # ceiling_base = max(3, 6) = 6; weight=1 → ceiling 6; body.count 8 clamps to 6.
    ok(body.get("generated") == 6,
       "weight=1 generate clamps to the 1× ceiling (6), not body.count 8")
    ok(len(calls) == 1, "live generate called generate_ideas once")
    ok(calls[0]["topic_name"] == "Live" and calls[0]["theme_prompt"] == "live theme",
       "generate_ideas received the live topic name and theme")
    ok(calls[0]["n"] == 6, "generate_ideas asked for the clamped 6, not 8")
    ok(calls[0]["content_format"] == "short", "short topic forwards content_format=short")
    ok(draft_count(2) == 6, "six draft rows landed on the live topic")
    runs = generate_runs()
    ok(len(runs) == 1, "exactly one generate JobRun on the live path")
    ok(runs[0].channel_id == 1 and runs[0].status == "success",
       "generate JobRun carries channel_id and success")
    ok("Live" in (runs[0].detail or "") and "6" in (runs[0].detail or ""),
       "generate JobRun names the topic and the idea count")

    print("POST /api/topics/{id}/generate: weight multiplier and isolation")
    n_before = len(calls)
    r = post_generate(3, count=20)
    ok(r.status_code == 200, "heavy generate returns 200")
    # weight=2 → ceiling 12; board cap 40 does not bind.
    ok(r.json().get("generated") == 12,
       "weight=2 generate clamps to 2× ceiling (12), not 1× (or-1 residual) or 20")
    ok(len(calls) == n_before + 1, "heavy generate called generate_ideas once")
    ok(calls[-1]["n"] == 12 and calls[-1]["content_format"] == "long",
       "heavy generate_ideas asked for 12 longs")
    ok(draft_count(3) == 12, "twelve drafts landed on the heavy topic")
    ok(draft_count(1) == 0, "parked sibling still empty after live generates")

    print("POST /api/topics/{id}/generate: idea ceiling is a distinct no-op")
    n_before = len(calls)
    runs_before = len(generate_runs())
    r = post_generate(5, count=8)
    ok(r.status_code == 200, "at-ceiling generate returns 200")
    body = r.json()
    ok(body.get("generated") == 0, "six drafts on a weight-1 topic is the ceiling")
    ok(body.get("reason") == "idea ceiling reached",
       "at-ceiling reason is the existing ceiling string, not parked")
    ok(len(calls) == n_before, "at-ceiling generate never called generate_ideas")
    ok(len(generate_runs()) == runs_before, "at-ceiling generate writes no JobRun")
    ok(draft_count(5) == 6, "at-ceiling topic draft count unchanged")

    print("POST /api/topics/{id}/generate: 404 / 502 / auth")
    n_before = len(calls)
    r = post_generate(99999, count=8)
    ok(r.status_code == 404, "missing topic is 404")
    ok(len(calls) == n_before, "404 never called generate_ideas")

    def boom(*_a, **_k):
        raise RuntimeError("llm down")

    topics_router.video_gen.generate_ideas = boom
    with Session(engine) as s:
        s.add(Topic(channel_id=1, name="Boom", theme_prompt="boom theme",
                    weight=1, content_format="short"))
        s.commit()
        boom_id = s.exec(select(Topic).where(Topic.name == "Boom")).one().id
    r = post_generate(boom_id, count=1)
    ok(r.status_code == 502, "generate_ideas raise on a live under-ceiling topic is 502")
    ok("idea generation failed" in r.text and "llm down" in r.text,
       "502 wraps the generate_ideas error")
    ok(draft_count(boom_id) == 0, "a 502 writes no draft rows")

    r = client.post("/api/topics/2/generate", json={"count": 1})
    ok(r.status_code == 401, "generate still requires auth")
finally:
    topics_router.video_gen.generate_ideas = _orig_ideas
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
