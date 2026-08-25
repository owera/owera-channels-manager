"""Regression checks for POST /api/trends/{id}/adopt board-horizon guard.

The 2026-08-25 flood: 10 watching trends were adopted in 21s against an already
full ch2 bench (pending 10 → 90) because adopt created a new topic + 8 ideas
with no cap, unlike POST /topics/{id}/generate and autofill which both stop at
``daily_render_budget × board_horizon_days``. These checks pin that adopt 409s
when the bench is full (no topic, no videos, trend stays watching) and that
under-capacity adopts still seed, clamping idea_count to remaining seats.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no network,
no LLM). ``video_gen.generate_ideas`` is stubbed. Exits non-zero on the first
failed assertion.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, JobRun, OAuthStatus, Topic, TrendSignal,
                        TrendStatus, Video, VideoStatus)
from app.routers import trends as trends_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(trends_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "trends module loaded from this tree")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

# budget 5 × horizon 2 = 10 seats (matches production).
with Session(engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5))
    s.commit()
    s.add(Topic(channel_id=1, name="Filler", theme_prompt="filler",
                weight=2, content_format="short"))          # id 1
    s.commit()
    for i in range(10):
        s.add(Video(channel_id=1, topic_id=1, subject=f"full-{i}",
                    status=VideoStatus.DRAFT))
    s.add(TrendSignal(term="Full Bench Trend", term_norm="full bench trend",
                      channel_id=1, status=TrendStatus.WATCHING, score=85,
                      description="should not land"))
    s.add(TrendSignal(term="Room For Two", term_norm="room for two",
                      channel_id=1, status=TrendStatus.WATCHING, score=80,
                      description="clamped adopt"))
    s.add(TrendSignal(term="Empty Board Trend", term_norm="empty board trend",
                      channel_id=1, status=TrendStatus.WATCHING, score=90,
                      description="full adopt"))
    s.add(TrendSignal(term="Already In", term_norm="already in",
                      channel_id=1, status=TrendStatus.ADOPTED, score=70,
                      adopted_topic_id=1, description="already adopted"))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

_orig_ideas = trends_router.video_gen.generate_ideas
_orig_lang = trends_router.video_gen.channel_language
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


def fake_lang(_session, _channel_id):
    return "en"


trends_router.video_gen.generate_ideas = fake_ideas
trends_router.video_gen.channel_language = fake_lang


def adopt(trend_id, body=None):
    return client.post(f"/api/trends/{trend_id}/adopt", auth=auth,
                       json=body or {"idea_count": 8, "produce_count": 3})


def pending():
    with Session(engine) as s:
        return s.exec(select(func.count(Video.id)).where(
            Video.channel_id == 1,
            Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED]),
        )).one()


def topic_count():
    with Session(engine) as s:
        return s.exec(select(func.count(Topic.id))).one()


def trend_row(tid):
    with Session(engine) as s:
        return s.get(TrendSignal, tid)


def adopt_runs():
    with Session(engine) as s:
        return s.exec(select(JobRun).where(JobRun.kind == "trend_adopt")).all()


try:
    print("POST /api/trends/{id}/adopt: full board 409s and writes nothing")
    topics_before = topic_count()
    r = adopt(1)
    ok(r.status_code == 409, "full-board adopt returns 409")
    ok("board at capacity" in r.text, "409 names the horizon, not a generic conflict")
    ok(calls == [], "full-board adopt never called generate_ideas")
    ok(topic_count() == topics_before, "full-board adopt creates no topic")
    ok(pending() == 10, "full-board pending stays at the horizon")
    t = trend_row(1)
    ok(t.status == TrendStatus.WATCHING and t.adopted_topic_id is None,
       "full-board trend stays watching (not adopted)")
    ok(adopt_runs() == [], "full-board adopt writes no JobRun")

    print("POST /api/trends/{id}/adopt: clamps idea_count to remaining seats")
    with Session(engine) as s:
        extras = s.exec(select(Video).where(
            Video.channel_id == 1, Video.topic_id == 1)).all()
        # Drop 2 drafts so board_space = 2.
        for v in extras[:2]:
            s.delete(v)
        s.commit()
    ok(pending() == 8, "setup left 2 seats")
    r = adopt(2)
    ok(r.status_code == 200, "under-capacity adopt returns 200")
    body = r.json()
    ok(body.get("ideas") == 2, "idea_count 8 clamped to remaining 2 seats")
    ok(body.get("producing") == 2, "produce_count 3 clamped to the 2 ideas")
    ok(len(calls) == 1 and calls[0]["n"] == 2,
       "generate_ideas asked for the clamped 2, not 8")
    ok(pending() == 10, "clamped adopt fills exactly to the horizon")
    t = trend_row(2)
    ok(t.status == TrendStatus.ADOPTED and t.adopted_topic_id is not None,
       "under-capacity trend is adopted")
    runs = adopt_runs()
    ok(len(runs) == 1 and runs[0].status == "success",
       "exactly one trend_adopt JobRun on the clamped path")

    print("POST /api/trends/{id}/adopt: empty board still seeds the ask")
    with Session(engine) as s:
        for v in s.exec(select(Video).where(Video.channel_id == 1)).all():
            s.delete(v)
        s.commit()
    ok(pending() == 0, "setup emptied the bench")
    n_before = len(calls)
    r = adopt(3)
    ok(r.status_code == 200, "empty-board adopt returns 200")
    body = r.json()
    ok(body.get("ideas") == 8, "empty board keeps the requested 8 ideas")
    ok(body.get("producing") == 3, "empty board auto-produces the requested 3")
    ok(len(calls) == n_before + 1 and calls[-1]["n"] == 8,
       "empty-board generate_ideas asked for 8")
    ok(pending() == 8, "8 new videos landed")
    with Session(engine) as s:
        queued = s.exec(select(func.count(Video.id)).where(
            Video.channel_id == 1, Video.status == VideoStatus.QUEUED)).one()
        drafts = s.exec(select(func.count(Video.id)).where(
            Video.channel_id == 1, Video.status == VideoStatus.DRAFT)).one()
    ok(queued == 3 and drafts == 5, "3 queued + 5 draft (produce_count then remainder)")
    t = trend_row(3)
    ok(t.status == TrendStatus.ADOPTED and t.adopted_topic_id is not None,
       "empty-board trend is adopted")

    print("POST /api/trends/{id}/adopt: already-adopted / auth")
    n_before = len(calls)
    pending_before = pending()
    r = adopt(4)
    ok(r.status_code == 409, "already-adopted trend is 409")
    ok("already adopted" in r.text, "already-adopted 409 names the existing topic")
    ok(len(calls) == n_before, "already-adopted never called generate_ideas")
    ok(pending() == pending_before, "already-adopted writes no videos")

    r = client.post("/api/trends/3/adopt", json={"idea_count": 1})
    ok(r.status_code == 401, "adopt still requires auth")
finally:
    trends_router.video_gen.generate_ideas = _orig_ideas
    trends_router.video_gen.channel_language = _orig_lang
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
