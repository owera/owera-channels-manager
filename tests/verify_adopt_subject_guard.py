"""Adopt must not queue a subject the pre-produce guard holds.

POST /api/trends/{id}/adopt used to mark the first produce_count ideas
QUEUED in order. generate_ideas keeps an R$N / $N title on purpose
(is_title_fragment carves that out) and a stub / future prompt can still
emit a lowercase fragment. Auto-produce already skip-and-continues those
drafts (_hold_invalid_subject); adopt queued them anyway and spent a
render slot on a video the publish craft gate will not ship.

Same skip-and-continue as auto-produce: a held subject stays DRAFT with
video.error = the guard reason; later legal ideas fill produce_count.

In-memory SQLite, TestClient, generate_ideas stubbed. No live DB, no
network, no LLM. Exits non-zero on the first failed assertion.
"""
import inspect
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, JobRun, OAuthStatus, Topic, TrendSignal,
                        TrendStatus, Video, VideoStatus)
from app.routers import trends as trends_router
from app.services.subject_guard import HOLD_PREFIX, subject_guard_reason

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

HELD_LOWER = "camadas na GPU, o resto no CPU, não é engenharia. · IA 209"
HELD_CURRENCY = "O agente me cobrou $47 no terminal. · Claude Code 10"
HELD_UNIT = "B em Q4 rodou na 24GB. O Q8, não. · Local 56"
LEGAL_A = "node_modules no contexto não é engenharia. · IA 206"
LEGAL_B = "Chat targeted staging. Prod wrote production. · Agent memory 35"
LEGAL_C = "Ollama parado prende 7GB de VRAM, não é engenharia. · IA 207"

ok(subject_guard_reason(HELD_LOWER) and subject_guard_reason(HELD_LOWER).startswith(HOLD_PREFIX),
   "precondition: lowercase fragment is held")
ok(subject_guard_reason(HELD_CURRENCY) and subject_guard_reason(HELD_CURRENCY).startswith(HOLD_PREFIX),
   "precondition: currency value is held")
ok(subject_guard_reason(HELD_UNIT) and subject_guard_reason(HELD_UNIT).startswith(HOLD_PREFIX),
   "precondition: bare unit is held")
ok(subject_guard_reason(LEGAL_A) is None, "precondition: node_modules allowlist is legal")
ok(subject_guard_reason(LEGAL_B) is None, "precondition: EN OS title is legal")
ok(subject_guard_reason(LEGAL_C) is None, "precondition: Ollama title is legal")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Channel(slug="b", name="B", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5))
    s.commit()
    ch_b = s.exec(select(Channel).where(Channel.slug == "b")).one().id
    s.add(TrendSignal(term="Guard Mix", term_norm="guard mix",
                      channel_id=ch_b, status=TrendStatus.WATCHING, score=90,
                      description="mixed held+legal"))
    s.add(TrendSignal(term="Guard Legal", term_norm="guard legal",
                      channel_id=ch_b, status=TrendStatus.WATCHING, score=80,
                      description="all legal"))
    s.add(TrendSignal(term="Guard Zero", term_norm="guard zero",
                      channel_id=ch_b, status=TrendStatus.WATCHING, score=70,
                      description="produce_count 0"))
    s.add(TrendSignal(term="Guard Auth", term_norm="guard auth",
                      channel_id=ch_b, status=TrendStatus.WATCHING, score=60,
                      description="unauth"))
    s.commit()
    mix_id = s.exec(select(TrendSignal).where(TrendSignal.term_norm == "guard mix")).one().id
    legal_id = s.exec(select(TrendSignal).where(TrendSignal.term_norm == "guard legal")).one().id
    zero_id = s.exec(select(TrendSignal).where(TrendSignal.term_norm == "guard zero")).one().id
    auth_id = s.exec(select(TrendSignal).where(TrendSignal.term_norm == "guard auth")).one().id


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
_orig_hook = trends_router.video_gen.idea_hook_kwargs
_next_ideas: list[str] = []


def fake_ideas(topic_name, theme_prompt, existing, n=8, content_format="short",
               language=None):
    return list(_next_ideas)[:n]


def fake_lang(_session, _channel_id):
    return "en"


trends_router.video_gen.generate_ideas = fake_ideas
trends_router.video_gen.channel_language = fake_lang
trends_router.video_gen.idea_hook_kwargs = lambda *a, **k: None


def adopt(trend_id, body):
    return client.post(f"/api/trends/{trend_id}/adopt", auth=auth, json=body)


def videos_for_topic(topic_id):
    with Session(engine) as s:
        return s.exec(
            select(Video).where(Video.topic_id == topic_id).order_by(Video.position, Video.id)
        ).all()


def produce_runs(channel_id):
    with Session(engine) as s:
        return s.exec(
            select(JobRun).where(JobRun.kind == "produce", JobRun.channel_id == channel_id)
            .order_by(JobRun.id)
        ).all()


try:
    print("POST /api/trends/{id}/adopt: held subjects stay DRAFT; later legal fill produce_count")
    _next_ideas[:] = [HELD_LOWER, HELD_CURRENCY, LEGAL_A, LEGAL_B, LEGAL_C]
    r = adopt(mix_id, {"idea_count": 5, "produce_count": 2})
    ok(r.status_code == 200, f"mixed adopt returns 200 ({r.status_code} {r.text[:200]})")
    body = r.json()
    ok(body.get("ideas") == 5, f"five ideas kept (held are not dropped): {body.get('ideas')}")
    ok(body.get("producing") == 2,
       f"producing is 2 legal queued, not the two held prefix ({body.get('producing')})")
    rows = videos_for_topic(body["topic_id"])
    ok(len(rows) == 5, f"five video rows ({len(rows)})")
    # renumber_series rewrites the "· Series NN" tail; match on the head.
    heads = [v.subject.split("·", 1)[0] for v in rows]
    ok("camadas na GPU" in heads[0], f"row 0 is the lowercase fragment ({rows[0].subject!r})")
    ok("$47" in rows[1].subject, f"row 1 is the currency subject ({rows[1].subject!r})")
    ok("node_modules" in rows[2].subject, f"row 2 is LEGAL_A ({rows[2].subject!r})")
    ok("Chat targeted" in rows[3].subject, f"row 3 is LEGAL_B ({rows[3].subject!r})")
    ok("Ollama" in rows[4].subject, f"row 4 is LEGAL_C ({rows[4].subject!r})")
    ok([v.status for v in rows] == [VideoStatus.DRAFT, VideoStatus.DRAFT,
                                    VideoStatus.QUEUED, VideoStatus.QUEUED,
                                    VideoStatus.DRAFT],
       f"held stay DRAFT; later legal fill the 2 slots "
       f"(prefix queue would be Q,Q,D,D,D); got {[v.status for v in rows]}")
    ok((rows[0].error or "").startswith(HOLD_PREFIX),
       "held lowercase records the guard reason on video.error")
    ok((rows[1].error or "").startswith(HOLD_PREFIX),
       "held currency records the guard reason on video.error")
    ok(rows[2].error is None and rows[3].error is None,
       "queued legal ideas have no guard error")
    held_runs = produce_runs(ch_b)
    ok(len(held_runs) == 2, f"two produce-error JobRuns while slots were open ({len(held_runs)})")
    ok(all(j.status == "error" and "held draft" in (j.detail or "") for j in held_runs),
       "produce JobRuns are errors naming the hold")
    ok({j.video_id for j in held_runs} == {rows[0].id, rows[1].id},
       "produce JobRuns point at the two held videos, not a hardcoded id=1")

    print("POST /api/trends/{id}/adopt: all-legal still queues a prefix")
    n_runs = len(produce_runs(ch_b))
    _next_ideas[:] = [LEGAL_A, LEGAL_B, LEGAL_C]
    r = adopt(legal_id, {"idea_count": 3, "produce_count": 2})
    ok(r.status_code == 200, "all-legal adopt returns 200")
    ok(r.json().get("producing") == 2, "all-legal producing is still 2")
    rows = videos_for_topic(r.json()["topic_id"])
    ok([v.status for v in rows] == [VideoStatus.QUEUED, VideoStatus.QUEUED, VideoStatus.DRAFT],
       "all-legal: first produce_count queued, remainder draft")
    ok(all(v.error is None for v in rows), "all-legal rows have no guard error")
    ok(len(produce_runs(ch_b)) == n_runs, "all-legal writes no produce-error JobRun")

    print("POST /api/trends/{id}/adopt: produce_count=0 keeps a legal idea as DRAFT")
    _next_ideas[:] = [LEGAL_A, HELD_UNIT]
    r = adopt(zero_id, {"idea_count": 2, "produce_count": 0})
    ok(r.status_code == 200, "produce_count=0 adopt returns 200")
    ok(r.json().get("producing") == 0, "produce_count=0 producing is 0")
    rows = videos_for_topic(r.json()["topic_id"])
    ok(all(v.status == VideoStatus.DRAFT for v in rows),
       "produce_count=0 does not queue, even a legal idea")
    ok(rows[0].error is None, "legal draft under produce_count=0 has no guard error")
    ok((rows[1].error or "").startswith(HOLD_PREFIX),
       "held draft under produce_count=0 still records the reason")
    # No slot was open, so the hold is left for auto-produce's next tick.
    ok(all(j.video_id != rows[1].id for j in produce_runs(ch_b)),
       "held idea after produce_count is filled does not write a produce JobRun")

    print("POST /api/trends/{id}/adopt: unauthenticated 401 writes nothing")
    topics_before = None
    with Session(engine) as s:
        topics_before = len(s.exec(select(Topic)).all())
        videos_before = len(s.exec(select(Video)).all())
    _next_ideas[:] = [LEGAL_A]
    r = client.post(f"/api/trends/{auth_id}/adopt",
                    json={"idea_count": 1, "produce_count": 1})
    ok(r.status_code == 401, "unauthenticated adopt is 401")
    with Session(engine) as s:
        ok(len(s.exec(select(Topic)).all()) == topics_before, "401 writes no topic")
        ok(len(s.exec(select(Video)).all()) == videos_before, "401 writes no video")
        t = s.get(TrendSignal, auth_id)
        ok(t.status == TrendStatus.WATCHING and t.adopted_topic_id is None,
           "401 leaves the trend watching")

    src = inspect.getsource(trends_router.adopt_trend)
    ok("subject_guard_reason" in src,
       "adopt_trend calls subject_guard_reason (a hand-rolled status=QUEUED prefix fails)")
    ok("produced < produce_count" in src,
       "adopt fills produce_count from later legal ideas (not i < produce_count)")
finally:
    trends_router.video_gen.generate_ideas = _orig_ideas
    trends_router.video_gen.channel_language = _orig_lang
    trends_router.video_gen.idea_hook_kwargs = _orig_hook
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
