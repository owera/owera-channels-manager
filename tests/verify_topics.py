"""Regression checks for POST /api/topics/{id}/generate and PATCH
``content_format``.

The 2026-08-24 overflow skip left a residual: generate still used
``t.weight or 1``, so a parked (weight=0) topic could be hand-filled up
to the 1× ceiling — undoing a park via the dashboard button AND the
growth agent's Feed-winners call (``POST /api/topics/{id}/generate``).
These checks pin that generate uses the same ``weight <= 0`` gate as
autofill / overflow, and that live topics still generate.

#23–#26 mopped leftover formats (empty / ``"LONG"`` / ``"medium"``) at
every consumer via ``== "long"`` else-short. Create already writes that
gate; PATCH ``setattr``s the raw body, so a growth-agent / curl leftover
is how those formats enter the DB. These checks pin PATCH to the same
gate: leftovers persist as ``"short"``, canonical ``"long"`` stays long,
omitted format stays put, a sibling is untouched.

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no
network, no LLM). ``video_gen.generate_ideas`` is stubbed and recorded;
the app lifespan/scheduler are never started. Exits non-zero on the
first failed assertion.
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


def patch_topic(topic_id, **body):
    return client.patch(f"/api/topics/{topic_id}", auth=auth, json=body)


def topic_format(topic_id):
    with Session(engine) as s:
        return s.get(Topic, topic_id).content_format


def topic_name(topic_id):
    with Session(engine) as s:
        return s.get(Topic, topic_id).name


def topic_weight(topic_id):
    with Session(engine) as s:
        return s.get(Topic, topic_id).weight


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

    print("PATCH /api/topics/{id}: leftover formats persist as short (create's gate)")
    # Topic 2 is Live / short; topic 3 is Heavy / long.
    ok(topic_format(2) == "short", "precondition: topic 2 is canonical short")
    ok(topic_format(3) == "long", "precondition: topic 3 is canonical long")

    sibling_before = topic_format(3)
    name_before = topic_name(2)

    r = patch_topic(2, content_format="LONG")
    ok(r.status_code == 200, "PATCH content_format=LONG is 200")
    ok(r.json().get("content_format") == "short",
       "LONG leftover response is short (create would write short; "
       ".lower()=='long' would persist long)")
    ok(topic_format(2) == "short",
       "LONG leftover persisted as short, not LONG")
    ok(topic_format(3) == sibling_before,
       "PATCH format on topic 2 left sibling topic 3 untouched")

    r = patch_topic(2, content_format="")
    ok(r.status_code == 200, "PATCH content_format='' is 200")
    ok(topic_format(2) == "short",
       "empty-format leftover persisted as short")

    r = patch_topic(2, content_format="medium")
    ok(r.status_code == 200, "PATCH content_format=medium is 200")
    ok(topic_format(2) == "short",
       "medium leftover persisted as short "
       "(an allowlist of short+empty+LONG would miss this)")

    r = patch_topic(2, content_format=None)
    ok(r.status_code == 200, "PATCH content_format=null is 200")
    ok(r.json().get("content_format") == "short",
       "null leftover response is short, not None")
    ok(topic_format(2) == "short",
       "null leftover persisted as short, not SQL NULL")
    with Session(engine) as s:
        ok(s.get(Topic, 2).content_format is not None,
           "null PATCH did not persist SQL NULL into content_format")

    print("PATCH /api/topics/{id}: canonical long/short still persist")
    r = patch_topic(2, content_format="long")
    ok(r.status_code == 200, "PATCH content_format=long is 200")
    ok(topic_format(2) == "long",
       "canonical long persisted (always-short mutant dies here)")
    ok(topic_format(3) == "long", "canonical long PATCH left sibling long")

    r = patch_topic(2, content_format="short")
    ok(r.status_code == 200, "PATCH content_format=short is 200")
    ok(topic_format(2) == "short",
       "canonical short persisted (always-long mutant dies here)")

    r = patch_topic(3, content_format="short")
    ok(r.status_code == 200, "PATCH long topic down to short is 200")
    ok(topic_format(3) == "short", "canonical short overwrites a long")
    r = patch_topic(3, content_format="long")
    ok(r.status_code == 200, "restore topic 3 to long is 200")
    ok(topic_format(3) == "long", "topic 3 restored to long")

    print("PATCH /api/topics/{id}: omitted format stays put; mixed body writes both")
    r = patch_topic(2, name="Live-renamed")
    ok(r.status_code == 200, "name-only PATCH is 200")
    ok(topic_name(2) == "Live-renamed", "name-only PATCH persisted the name")
    ok(topic_format(2) == "short",
       "name-only PATCH left content_format (exclude_unset)")
    ok(topic_format(3) == "long", "name-only PATCH left sibling format")

    r = patch_topic(2, name=name_before, content_format="LONG")
    ok(r.status_code == 200, "mixed name + LONG leftover is 200")
    ok(topic_name(2) == name_before, "mixed PATCH restored the original name")
    ok(topic_format(2) == "short",
       "mixed PATCH still canonicalized LONG → short")

    # Vacuous-pin class: name-only / null on a topic that is ALREADY short
    # cannot kill always-_canonical_format(fields.get(...)) (omitted → short)
    # or skip-None (null leaves long). Drive both against canonical long.
    heavy_name = topic_name(3)
    ok(topic_format(3) == "long", "precondition: topic 3 is still canonical long")
    r = patch_topic(3, name="Heavy-renamed")
    ok(r.status_code == 200, "name-only PATCH on a long topic is 200")
    ok(topic_name(3) == "Heavy-renamed", "name-only PATCH on long persisted the name")
    ok(topic_format(3) == "long",
       "name-only PATCH on a long topic left content_format long "
       "(always _canonical_format(fields.get(...)) clobbers omitted to short)")

    r = patch_topic(3, content_format=None)
    ok(r.status_code == 200, "PATCH content_format=null on a long topic is 200")
    ok(r.json().get("content_format") == "short",
       "null leftover on a long topic response is short")
    ok(topic_format(3) == "short",
       "null leftover on a long topic persisted as short "
       "(skip-None / exclude_none leaves long)")
    ok(topic_name(3) == "Heavy-renamed",
       "null-format PATCH on long left the renamed name")

    r = patch_topic(3, name=heavy_name, content_format="LONG")
    ok(r.status_code == 200, "mixed name + LONG leftover on a long topic is 200")
    ok(topic_name(3) == heavy_name, "mixed PATCH on long restored the original name")
    ok(topic_format(3) == "short",
       "mixed PATCH on a long topic canonicalized LONG → short "
       "(dropping content_format from the mixed body would leave long)")
    r = patch_topic(3, content_format="long")
    ok(r.status_code == 200, "restore topic 3 to long after leftover pins")
    ok(topic_format(3) == "long", "topic 3 restored to long")

    print("PATCH /api/topics/{id}: 404 / auth")
    r = patch_topic(99999, content_format="short")
    ok(r.status_code == 404, "PATCH missing topic is 404")
    r = client.patch("/api/topics/2", json={"content_format": "short"})
    ok(r.status_code == 401, "PATCH still requires auth")

    ok(topics_router._canonical_format("long") == "long",
       "_canonical_format(long) is long")
    ok(topics_router._canonical_format("short") == "short",
       "_canonical_format(short) is short")
    ok(topics_router._canonical_format("LONG") == "short",
       "_canonical_format(LONG) is short (.lower()==long would return long)")
    ok(topics_router._canonical_format("") == "short",
       "_canonical_format('') is short")
    ok(topics_router._canonical_format(None) == "short",
       "_canonical_format(None) is short")
    ok(topics_router._canonical_format("medium") == "short",
       "_canonical_format(medium) is short")
    ok("_canonical_format" in inspect.getsource(topics_router.update_topic),
       "update_topic canonicalizes through _canonical_format "
       "(setattr of the raw body is how leftovers enter the DB)")
    ok("_canonical_format" in inspect.getsource(topics_router.create_topic),
       "create_topic canonicalizes through _canonical_format "
       "(a PATCH-only helper would let create drift)")

    print("POST /api/topics: leftover formats persist as short (helper wiring)")
    r = client.post("/api/topics", auth=auth, json={
        "channel_id": 1, "name": "CreateLeftover", "content_format": "LONG"})
    ok(r.status_code == 201, "create LONG leftover is 201")
    ok(r.json().get("content_format") == "short",
       "create LONG leftover persisted as short "
       "(helper name in source with unused/constant call would write LONG)")
    create_id = r.json()["id"]
    ok(topic_format(create_id) == "short", "create LONG leftover row is short")

    r = client.post("/api/topics", auth=auth, json={
        "channel_id": 1, "name": "CreateLong", "content_format": "long"})
    ok(r.status_code == 201, "create canonical long is 201")
    ok(r.json().get("content_format") == "long",
       "create canonical long persisted "
       "(_canonical_format('short') constant would write short)")
    ok(topic_format(r.json()["id"]) == "long", "create canonical long row is long")

    print("PATCH /api/topics/{id}: weight floor (null/bool/negative 400; 0 parks)")
    # Topic 1 is parked weight=0; topic 2 is live weight=1; topic 3 is heavy weight=2.
    ok(topic_weight(1) == 0, "precondition: topic 1 is parked at weight=0")
    ok(topic_weight(2) == 1, "precondition: topic 2 is live at weight=1")
    ok(topic_weight(3) == 2, "precondition: topic 3 is heavy at weight=2")
    gen_src = inspect.getsource(topics_router.generate_videos)
    ok("t.weight if t.weight is not None else 1" in gen_src,
       "generate still treats weight is None as 1 (null PATCH would unpark)")

    r = patch_topic(2, weight=3)
    ok(r.status_code == 200, "PATCH weight=3 is 200")
    ok(topic_weight(2) == 3, "weight=3 persisted")
    ok(topic_weight(1) == 0, "weight PATCH on topic 2 left parked sibling at 0")
    r = patch_topic(2, weight=0)
    ok(r.status_code == 200, "PATCH weight=0 is 200 (legal park)")
    ok(topic_weight(2) == 0, "weight=0 persisted (legal park)")
    r = patch_topic(2, weight=1)
    ok(r.status_code == 200, "restore topic 2 to weight=1")
    ok(topic_weight(2) == 1, "topic 2 restored to 1")

    before_name = topic_name(1)
    r = patch_topic(1, weight=None)
    ok(r.status_code == 400, "PATCH weight=null on a parked topic is 400")
    ok("weight" in r.text.lower(), "null-weight 400 names the field")
    ok(topic_weight(1) == 0,
       "null PATCH on parked weight=0 writes nothing "
       "(setattr None then None->1 would unpark)")
    with Session(engine) as s:
        ok(s.get(Topic, 1).weight is not None,
           "null PATCH did not persist SQL NULL into weight")
    ok(topic_name(1) == before_name, "null-weight 400 left the name")

    r = patch_topic(2, weight=None)
    ok(r.status_code == 400, "PATCH weight=null on a live topic is 400")
    ok(topic_weight(2) == 1, "null PATCH on live weight=1 writes nothing")

    r = patch_topic(2, weight=-1)
    ok(r.status_code == 400, "PATCH weight=-1 is 400")
    ok(topic_weight(2) == 1, "negative weight writes nothing")
    ok(topic_weight(3) == 2, "negative-weight 400 left sibling weight")

    r = patch_topic(2, weight=False)
    ok(r.status_code in (400, 422),
       "PATCH weight=false is 4xx (must not coerce to 0 park)")
    ok(topic_weight(2) == 1, "false did not persist a 0 park")

    r = patch_topic(1, weight=True)
    ok(r.status_code in (400, 422),
       "PATCH weight=true is 4xx (must not coerce to 1 unpark)")
    ok(topic_weight(1) == 0, "true did not persist a 1 unpark on a parked topic")

    r = patch_topic(2, name="Live-smuggle", weight=-1)
    ok(r.status_code == 400, "mixed name + weight=-1 is 400")
    ok(topic_name(2) != "Live-smuggle",
       "mixed 400 writes none of the fields (name not smuggled)")
    ok(topic_weight(2) == 1, "mixed 400 left live weight=1")

    r = patch_topic(2, name=topic_name(2))
    ok(r.status_code == 200, "name-only PATCH (weight omitted) is 200")
    ok(topic_weight(2) == 1,
       "name-only PATCH left weight (exclude_unset; always-floor would 400)")

    upd_src = inspect.getsource(topics_router.update_topic)
    ok("_require_int" in upd_src,
       "update_topic floors weight through _require_int")
    ok(upd_src.index("_require_int") < upd_src.index("setattr"),
       "_require_int runs before setattr "
       "(setattr-then-400 would smuggle other fields if the session committed)")
finally:
    topics_router.video_gen.generate_ideas = _orig_ideas
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
