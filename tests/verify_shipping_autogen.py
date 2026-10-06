"""Shipping autogen guard (CMO decision 2026-10-06, OS Gate B).

Run: PYTHONPATH=. .venv/bin/python tests/verify_shipping_autogen.py

#1400-#1402 were created by idea autogen on topic t45 "Shipping" with no
merged Channels Manager PR behind them; VM Gate B failed and Channels
rejected them. Shipping rows exist only for a real PR (the #37/#39 flow: the
teaser is created from the merged PR with a provided script). Pins:
  * review_guard.autogen_block_reason: Shipping topic → reason; others → None;
  * review_guard.drop_teaser_ideas drops "· Shipping N" / "Channels Manager"
    ideas, keeps the rest;
  * POST /api/topics/{id}/generate on Shipping: generated 0 + reason, no LLM
    call, no row; other topics still generate (teaser-shaped ideas dropped);
  * POST /api/trends/{id}/adopt of a "Shipping" trend: 409, no topic/row/LLM;
  * the PR-backed path (POST /api/videos with a provided script) still works.
(The autofill tick is pinned in verify_autofill.py.)
In-memory DB + TestClient; generate_ideas stubbed. No network.
"""
import json
import sys

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, OAuthStatus, Topic, TrendSignal, TrendStatus, Video,
                        VideoStatus)
from app.routers import topics as topics_router
from app.routers import trends as trends_router
from app.services import review_guard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("helpers")
ok(review_guard.autogen_block_reason("Shipping") == review_guard.AUTOGEN_SHIPPING_REASON,
   "Shipping topic → autogen blocked")
ok(review_guard.autogen_block_reason("shipping") is not None, "case-insensitive topic match")
for name in ("Agent memory", "Agent traps", "CrewAI", "IA", "Local", None, ""):
    ok(review_guard.autogen_block_reason(name) is None, f"topic {name!r} → autogen allowed")
ok("source Channels Manager PR" in review_guard.AUTOGEN_SHIPPING_REASON,
   "reason names the missing source PR")
ideas = ["Your caption saves itself · Shipping 3",          # #1400 shape
         "Channels Manager queues your render",              # names the product
         "Memory died between chats · Agent memory 2",
         "Chat paged Lee · Agent traps 4"]
ok(review_guard.drop_teaser_ideas(ideas) == ideas[2:], "teaser-shaped ideas dropped, rest kept")
ok(review_guard.drop_teaser_ideas([]) == [] and review_guard.drop_teaser_ideas(None) == [],
   "empty input → empty list")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5))
    s.commit()
    s.add(Topic(channel_id=1, name="Shipping", theme_prompt="CM teasers", weight=1,
                content_format="short"))                      # id 1 (t45 analogue)
    s.add(Topic(channel_id=1, name="Agent traps", theme_prompt="traps", weight=1,
                content_format="short"))                      # id 2
    s.commit()
    s.add(TrendSignal(term="Shipping", term_norm="shipping", channel_id=1,
                      status=TrendStatus.WATCHING, score=90, description="x"))   # id 1
    s.add(TrendSignal(term="Agent sandboxes", term_norm="agent sandboxes", channel_id=1,
                      status=TrendStatus.WATCHING, score=80, description="y"))   # id 2
    s.commit()


def _sess():
    with Session(engine) as s:
        yield s


calls = []


def fake_ideas(topic_name, theme_prompt, existing, n=8, content_format="short", language=None):
    calls.append(topic_name)
    return ["You clicked and got a teaser · Shipping 5", f"{topic_name} real idea one",
            f"{topic_name} real idea two"][:max(n, 1)]


def rows(topic_id=None):
    with Session(engine) as s:
        q = select(func.count(Video.id))
        if topic_id:
            q = q.where(Video.topic_id == topic_id)
        return s.exec(q).one()


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)
auth = ("x", "pw")
_orig_t, _orig_tr = topics_router.video_gen.generate_ideas, trends_router.video_gen.generate_ideas
_orig_lang = trends_router.video_gen.channel_language
topics_router.video_gen.generate_ideas = fake_ideas
trends_router.video_gen.channel_language = lambda s, cid: "en"
try:
    print("POST /api/topics/{id}/generate")
    r = client.post("/api/topics/1/generate", auth=auth, json={"count": 3})
    ok(r.status_code == 200 and r.json()["generated"] == 0
       and "source Channels Manager PR" in r.json()["reason"],
       f"Shipping generate → generated 0 + reason ({r.status_code} {r.text[:120]})")
    ok(calls == [] and rows(1) == 0, "Shipping generate: no LLM call, no draft row")
    r = client.post("/api/topics/2/generate", auth=auth, json={"count": 3})
    with Session(engine) as s:
        subs = [v.subject for v in s.exec(select(Video).where(Video.topic_id == 2)).all()]
    ok(r.status_code == 200 and r.json()["generated"] == 2 and calls == ["Agent traps"],
       f"other topic still generates ({r.text[:80]})")
    ok(not any("Shipping" in x for x in subs), f"teaser-shaped idea dropped on other topics: {subs}")

    print("POST /api/trends/{id}/adopt")
    calls.clear()
    with Session(engine) as s:
        n_topics = s.exec(select(func.count(Topic.id))).one()
    r = client.post("/api/trends/1/adopt", auth=auth, json={"idea_count": 3, "produce_count": 0})
    with Session(engine) as s:
        n_topics2 = s.exec(select(func.count(Topic.id))).one()
        tr = s.get(TrendSignal, 1)
    ok(r.status_code == 409 and "source Channels Manager PR" in r.json()["detail"],
       f"adopting a 'Shipping' trend → 409 ({r.status_code})")
    ok(calls == [] and n_topics2 == n_topics and tr.status == TrendStatus.WATCHING,
       "refused adopt: no LLM call, no topic, trend untouched")
    r = client.post("/api/trends/2/adopt", auth=auth, json={"idea_count": 3, "produce_count": 0})
    ok(r.status_code == 200 and r.json().get("ideas") == 2,
       f"other trend adopts; teaser-shaped idea dropped ({r.status_code} {r.text[:120]})")

    print("PR-backed path still works")
    before = rows(1)
    r = client.post("/api/videos", auth=auth, json={
        "topic_id": 1, "subject": "Your thumbnail, not a template · Shipping 9",
        "script": "Your thumbnail, not a template. Channels Manager now takes your PNG."})
    ok(r.status_code in (200, 201) and rows(1) == before + 1,
       f"POST /api/videos with a provided script on Shipping still creates the row ({r.status_code})")
    with Session(engine) as s:
        v = s.exec(select(Video).where(Video.topic_id == 1)).first()
        cc = json.loads(v.creation_config or "{}")
    ok(cc.get("script_source") == "provided", "the PR teaser keeps script_source=provided")
finally:
    topics_router.video_gen.generate_ideas = _orig_t
    trends_router.video_gen.generate_ideas = _orig_tr
    trends_router.video_gen.channel_language = _orig_lang
    settings.app_password = _pw
    main.app.dependency_overrides.pop(get_session, None)

print()
print(f"ALL {_checks} CHECKS PASSED")
