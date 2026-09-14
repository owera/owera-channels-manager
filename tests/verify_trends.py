"""Regression checks for POST /api/trends/{id}/adopt.

Board-horizon guard (2026-08-25 flood): adopt 409s when the bench is full
(no topic, no videos, trend stays watching) and under-capacity adopts still
seed, clamping idea_count to remaining seats.

idea_count floor (backlog #43): ``max(1, body.idea_count)`` turned 0 /
negative into a one-idea adopt. Generate already 400s count<=0 (#41); adopt
is the same growth-agent path. produce_count 0 is legal (drafts only);
negative / bool 4xx.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no network,
no LLM). ``video_gen.generate_ideas`` is stubbed. Exits non-zero on the first
failed assertion.
"""
import inspect
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
from app.schemas import TrendAdoptBody

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
    s.add(TrendSignal(term="Count Floor", term_norm="count floor",
                      channel_id=1, status=TrendStatus.WATCHING, score=60,
                      description="idea_count floor"))
    s.add(TrendSignal(term="Omitted Count", term_norm="omitted count",
                      channel_id=1, status=TrendStatus.WATCHING, score=55,
                      description="default idea_count"))
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

    print("POST /api/trends/{id}/adopt: idea_count<=0 is 400 (not a silent 1-idea adopt)")
    # Defect: idea_count = max(1, body.idea_count) turned 0 / negative into a
    # one-idea adopt (topic + generate_ideas(n=1) + optional produce). Generate
    # already 400s count<=0 (#41); adopt is the same growth-agent path.
    n_before = len(calls)
    pending_before = pending()
    topics_before = topic_count()
    runs_before = len(adopt_runs())
    r = adopt(5, {"idea_count": 0, "produce_count": 0})
    ok(r.status_code == 400, "idea_count=0 on an under-ceiling watching trend is 400")
    ok(">= 1" in r.text, "400 names the idea_count floor")
    ok(len(calls) == n_before, "idea_count=0 never called generate_ideas")
    ok(topic_count() == topics_before, "idea_count=0 creates no topic")
    ok(pending() == pending_before, "idea_count=0 writes no videos")
    t = trend_row(5)
    ok(t.status == TrendStatus.WATCHING and t.adopted_topic_id is None,
       "idea_count=0 left Count Floor watching")
    ok(len(adopt_runs()) == runs_before, "idea_count=0 writes no JobRun")

    r = adopt(5, {"idea_count": -1, "produce_count": 0})
    ok(r.status_code == 400, "idea_count=-1 is 400")
    ok(len(calls) == n_before, "idea_count=-1 never called generate_ideas")
    ok(topic_count() == topics_before, "negative idea_count creates no topic")

    r = client.post("/api/trends/5/adopt", auth=auth, json={"idea_count": False})
    ok(r.status_code in (400, 422),
       "idea_count=false is 4xx (must not coerce to 0 then max(1,0)=1)")
    ok(len(calls) == n_before, "idea_count=false never called generate_ideas")
    ok(topic_count() == topics_before, "false did not create a topic")

    r = client.post("/api/trends/5/adopt", auth=auth, json={"idea_count": True})
    ok(r.status_code in (400, 422),
       "idea_count=true is 4xx (must not coerce to 1 and generate)")
    ok(len(calls) == n_before, "idea_count=true never called generate_ideas")
    ok(topic_count() == topics_before, "true did not create a one-idea topic")

    r = client.post("/api/trends/5/adopt", auth=auth, json={"idea_count": None})
    ok(r.status_code in (400, 422), "idea_count=null is 4xx")
    ok(len(calls) == n_before, "idea_count=null never called generate_ideas")

    r = adopt(4, {"idea_count": 0, "produce_count": 0})
    ok(r.status_code == 409, "already-adopted + idea_count=0 is still 409 (not 400)")
    ok("already adopted" in r.text, "already-adopted wins over the count floor")
    ok(len(calls) == n_before, "already-adopted + count=0 never called generate_ideas")

    r = adopt(5, {"idea_count": 1, "produce_count": -1})
    ok(r.status_code == 400, "produce_count=-1 is 400 (0 is legal; negative is not)")
    ok(len(calls) == n_before, "produce_count=-1 never called generate_ideas")
    ok(topic_count() == topics_before, "negative produce_count creates no topic")
    t = trend_row(5)
    ok(t.status == TrendStatus.WATCHING, "produce_count=-1 left Count Floor watching")

    r = client.post("/api/trends/5/adopt", auth=auth,
                    json={"idea_count": 1, "produce_count": False})
    ok(r.status_code in (400, 422),
       "produce_count=false is 4xx (must not coerce to 0)")
    ok(len(calls) == n_before, "produce_count=false never called generate_ideas")

    r = client.post("/api/trends/5/adopt", auth=auth,
                    json={"idea_count": 1, "produce_count": True})
    ok(r.status_code in (400, 422),
       "produce_count=true is 4xx (must not coerce to 1 and auto-produce)")
    ok(len(calls) == n_before, "produce_count=true never called generate_ideas")

    r = adopt(5, {"idea_count": 1, "produce_count": 0})
    ok(r.status_code == 200, "idea_count=1 is 200")
    ok(r.json().get("ideas") == 1, "idea_count=1 seeded exactly one idea")
    ok(r.json().get("producing") == 0, "produce_count=0 queued nothing")
    ok(len(calls) == n_before + 1, "idea_count=1 called generate_ideas once")
    ok(calls[-1]["n"] == 1, "generate_ideas asked for n=1")
    t = trend_row(5)
    ok(t.status == TrendStatus.ADOPTED and t.adopted_topic_id is not None,
       "idea_count=1 adopted Count Floor")

    # idea_count=1 left 9 pending / 1 seat — ideas==1 would pass a default of 1.
    # Leave 5 seats (not 1, not 8) so omitted {} must be default 8 clamped to 5,
    # and producing must be min(3, 5)=3 (a default of 0 would produce 0).
    with Session(engine) as s:
        extras = s.exec(select(Video).where(
            Video.channel_id == 1,
            Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED]))).all()
        for v in extras[: max(0, len(extras) - 5)]:
            s.delete(v)
        s.commit()
    ok(pending() == 5, "setup left 5 pending / 5 seats for omitted-default pin")

    r = client.post("/api/trends/6/adopt", auth=auth, json={})
    ok(r.status_code == 200, "omitted counts still 200 (TrendAdoptBody defaults)")
    ok(r.json().get("ideas") == 5,
       "omitted idea_count defaults to 8 and clamps to remaining 5, not 400 "
       "(remaining=1 made a default of 1 look like 8)")
    ok(r.json().get("producing") == 3,
       "omitted produce_count defaults to 3 (min(3, remaining 5)); "
       "a default of 0 would produce 0")
    ok(len(calls) == n_before + 2, "omitted counts called generate_ideas")
    ok(calls[-1]["n"] == 5, "omitted idea_count asked for remaining 5")
    t = trend_row(6)
    ok(t.status == TrendStatus.ADOPTED, "omitted-count trend is adopted")
    ok(pending() == 10, "omitted fill lands on the horizon")
    ok(TrendAdoptBody.model_fields["idea_count"].default == 8,
       "TrendAdoptBody.idea_count default is 8")
    ok(TrendAdoptBody.model_fields["produce_count"].default == 3,
       "TrendAdoptBody.produce_count default is 3")

    n_before = len(calls)
    r = adopt(1, {"idea_count": 0, "produce_count": 0})
    ok(r.status_code == 400,
       "full-board + idea_count=0 is 400 (count floor before horizon 409)")
    ok(">= 1" in r.text, "full-board count=0 names the floor, not capacity")
    ok(len(calls) == n_before, "full-board count=0 never called generate_ideas")
    t = trend_row(1)
    ok(t.status == TrendStatus.WATCHING, "full-board count=0 left trend 1 watching")

    adopt_src = inspect.getsource(trends_router.adopt_trend)
    ok("_require_int" in adopt_src,
       "adopt_trend floors idea_count through _require_int")
    ok(adopt_src.index("already adopted") < adopt_src.index("_require_int"),
       "already-adopted 409 runs before the count floor "
       "(floor-first would 400 already-adopted+count=0)")
    ok("max(1, body.idea_count)" not in adopt_src,
       "idea_count is no longer max(1, ...) (that is the silent-1 defect)")
    ok(adopt_src.index("_require_int") < adopt_src.index("board at capacity"),
       "_require_int runs before the horizon clamp "
       "(a late floor after 409 would 409 count=0 on a full board)")
    ok(adopt_src.index("_require_int") < adopt_src.index("generate_ideas"),
       "_require_int runs before generate_ideas")
    body_src = inspect.getsource(TrendAdoptBody)
    ok('_reject_bool_count' in body_src and 'mode="before"' in body_src,
       "TrendAdoptBody._reject_bool_count is mode=before "
       "(a comment containing 'before' is not the decorator)")
finally:
    trends_router.video_gen.generate_ideas = _orig_ideas
    trends_router.video_gen.channel_language = _orig_lang
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
