"""Generics v1 bar: niche on channel, topic reorder, generate steering body.

In-memory SQLite + TestClient; stubs ``video_gen.generate_ideas`` (no LLM).

Run: ``cd manager && python tests/verify_generics_v1.py``
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus, Workspace
from app.routers import topics as topics_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(topics_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "topics module loaded from this tree")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(slug="gv1", name="GV1", workspace_id=1, oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=20, niche="Homelab Kubernetes for working parents"))
    s.commit()
    s.add(Topic(channel_id=1, name="A", theme_prompt="a", weight=1, position=1, content_format="short"))
    s.add(Topic(channel_id=1, name="B", theme_prompt="b", weight=1, position=2, content_format="short"))
    s.add(Topic(channel_id=1, name="C", theme_prompt="c", weight=1, position=3, content_format="short"))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("owera", "testpw")

calls = []
_orig_ideas = topics_router.video_gen.generate_ideas


def fake_ideas(topic_name, theme_prompt, existing, n=8, content_format="short",
               language=None, niche=None, steer_notes=None):
    calls.append({
        "topic_name": topic_name,
        "theme_prompt": theme_prompt,
        "niche": niche,
        "steer_notes": steer_notes,
        "n": n,
    })
    return [f"{topic_name}-{i}" for i in range(min(n, 3))]


topics_router.video_gen.generate_ideas = fake_ideas

try:
    print("PATCH channel niche")
    r = client.patch("/api/channels/1", auth=auth, json={"niche": "  Edge ML on a shoestring  "})
    ok(r.status_code == 200, "patch niche 200")
    ok(r.json().get("niche") == "Edge ML on a shoestring", "niche trimmed")

    print("POST /api/topics/reorder")
    r = client.post("/api/topics/reorder", auth=auth,
                    json={"channel_id": 1, "ordered_ids": [3, 1, 2]})
    ok(r.status_code == 200, "reorder 200")
    with Session(engine) as s:
        pos = {t.id: t.position for t in s.exec(select(Topic)).all()}
    ok(pos[3] == 0 and pos[1] == 1 and pos[2] == 2, "positions 0,1,2")

    print("POST /api/topics/{id}/generate steering")
    calls.clear()
    r = client.post("/api/topics/1/generate", auth=auth, json={
        "count": 2,
        "steer_angle": "cost-sensitive",
        "steer_title": "Why X bites in prod",
    })
    ok(r.status_code == 200, "generate 200")
    ok(len(calls) == 1, "one generate_ideas call")
    ok("cost-sensitive" in (calls[0]["steer_notes"] or ""), "steer_notes mentions angle")
    ok("Why X bites in prod" in (calls[0]["steer_notes"] or ""), "steer_notes mentions title")
    ok(calls[0]["niche"] == "Edge ML on a shoestring", "niche passed through")

    with Session(engine) as s:
        n_drafts = s.exec(
            select(Video).where(Video.topic_id == 1, Video.status == VideoStatus.DRAFT)
        ).all()
    ok(len(n_drafts) >= 1, "draft rows created")
finally:
    topics_router.video_gen.generate_ideas = _orig_ideas
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw

print(f"ALL {_checks} CHECKS PASSED")
