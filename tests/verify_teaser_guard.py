"""CM teaser approve guard + review-gate actor audit (2026-10-03).

Run: PYTHONPATH=. .venv/bin/python tests/verify_teaser_guard.py

#1385 (Shipping 8, a Channels Manager teaser) was published with a Video
Maker Gate B FAIL after a non-Channels approve. Pins:
  * teaser detection (Shipping series / "Channels Manager" / cm_pr);
  * approve of a CM teaser: 403 for a non-Channels actor (including the
    Video Maker — CoS 2026-10-06: the VM records vm_pass, never approves),
    409 without the VM's vm_pass on THIS render, 200 for Channels with vm_pass;
  * vm_pass: Channels/VM only, review + current artifact only, recorded
    with actor + timestamp, cleared by a new render artifact;
  * requeue of a CM teaser by a non-Channels actor → 403 (blocked);
  * non-teasers unchanged for any actor;
  * every approve / requeue / reject / vm_pass JobRun carries actor=<name>
    (X-Actor header, else the Basic auth username).
In-memory DB + TestClient; craft publish gate stubbed (not under test).
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, review_guard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


_ART = Path(tempfile.mkdtemp(prefix="teaser-guard-"))


def art(name):
    f = _ART / f"{name}.mp4"
    f.write_bytes(b"mp4")
    return str(f)


print("teaser detection")
V = lambda **k: SimpleNamespace(**{"title": None, "subject": "", "script": None,
                                   "overrides_json": None, "creation_config": None, **k})
ok(review_guard.is_cm_teaser(V(title="Your thumbnail, not a template · Shipping 8")),
   "Shipping series title → teaser")
ok(review_guard.is_cm_teaser(V(subject="Thumbs"), topic_name="Shipping"), "topic Shipping → teaser")
ok(review_guard.is_cm_teaser(V(subject="x", script="Channels Manager now takes your PNG.")),
   "provided script naming Channels Manager → teaser")
ok(review_guard.is_cm_teaser(V(subject="x", overrides_json=json.dumps({"cm_pr": 53}))),
   "cm_pr in overrides → teaser")
ok(not review_guard.is_cm_teaser(V(title="Modelo em swap não pensa · IA 7", script="Swap.")),
   "RR IA explainer → not a teaser")

print("actor resolution")
R = lambda h: SimpleNamespace(headers=h)
ok(review_guard.actor_of(R({"X-Actor": "Channels"})) == "channels", "X-Actor header wins (lower-cased)")
ok(review_guard.actor_of(R({"Authorization": "Basic Z3Jvd3RoOnB3"})) == "growth",
   "fallback: Basic auth username")
ok(review_guard.actor_of(R({})) == "unknown", "no header, no auth → unknown")
ok(review_guard.actor_of(R({"X-Actor": "a b;c" * 20})) .count(" ") == 0
   and len(review_guard.actor_of(R({"X-Actor": "a b;c" * 20}))) <= 40, "actor sanitised + bounded")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
TEASER = "Your thumbnail, not a template · Shipping 8"
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="Shipping"))
    s.add(Topic(channel_id=1, name="Agent traps"))
    s.commit()
    s.add(Video(channel_id=1, topic_id=1, subject=TEASER, title=TEASER, script="s",
                status=VideoStatus.REVIEW, video_path=art(1)))          # 1 teaser
    s.add(Video(channel_id=1, topic_id=2, subject="Chat paged Lee · Agent traps 4",
                title="Chat paged Lee · Agent traps 4", script="s",
                status=VideoStatus.REVIEW, video_path=art(2)))          # 2 non-teaser
    s.add(Video(channel_id=1, topic_id=1, subject=TEASER, title=TEASER, script="s",
                status=VideoStatus.REVIEW, video_path=art(3)))          # 3 teaser (requeue)
    s.add(Video(channel_id=1, topic_id=2, subject="Chat paged Ana · Agent traps 5",
                title="Chat paged Ana · Agent traps 5", script="s",
                status=VideoStatus.REVIEW, video_path=art(4)))          # 4 non-teaser (requeue)
    s.commit()


def _sess():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)
GROWTH = ("growth", "pw")
CH = {"X-Actor": "channels"}


def runs(kind, vid):
    with Session(engine) as s:
        return s.exec(select(JobRun).where(JobRun.kind == kind, JobRun.video_id == vid)).all()


try:
    with patch.object(craft, "publish_craft_block_reason", return_value=None):
        print("approve guard")
        r = client.post("/api/videos/1/approve", auth=GROWTH)
        ok(r.status_code == 403 and "only Channels approves" in r.json()["detail"],
           f"growth agent approving a CM teaser → 403 ({r.status_code})")
        r = client.post("/api/videos/1/approve", auth=GROWTH, headers=CH)
        ok(r.status_code == 409 and "vm-pass" in r.json()["detail"],
           f"Channels without vm_pass → 409 with the reason ({r.status_code})")
        ok(runs("approve", 1) == [], "refused approves write no JobRun")

        print("vm_pass endpoint")
        r = client.post("/api/videos/1/vm-pass", auth=GROWTH)
        ok(r.status_code == 403, "growth cannot set vm_pass")
        r = client.post("/api/videos/1/vm-pass", auth=GROWTH, headers={"X-Actor": "vm"},
                        json={"note": "Gate B PASS 2026-10-04 final render"})
        ok(r.status_code == 200 and r.json()["vm_pass"]["actor"] == "vm"
           and r.json()["vm_pass"]["result"] == "PASS" and r.json()["vm_pass"]["at"],
           "VM sets vm_pass: recorded with actor + timestamp")
        with Session(engine) as s:
            cc = json.loads(s.get(Video, 1).creation_config)
        ok(cc["vm_pass"]["video_path"].endswith("1.mp4"), "vm_pass bound to the current artifact")
        vr = runs("vm_pass", 1)
        ok(len(vr) == 1 and "actor=vm" in vr[0].detail, "vm_pass JobRun carries the actor")
        r = client.post("/api/videos/2/vm-pass", auth=GROWTH, headers={"X-Actor": "vm"})
        ok(r.status_code == 200, "vm_pass may be recorded on any review render")

        r = client.post("/api/videos/1/approve", auth=GROWTH)
        ok(r.status_code == 403, "even with vm_pass, the growth agent cannot approve a CM teaser")
        # CoS 2026-10-06: approve is Channels-only — the VM records PASS, never approves.
        for vm_actor in ("vm", "video-maker", "videomaker", "VM"):
            r = client.post("/api/videos/1/approve", auth=GROWTH, headers={"X-Actor": vm_actor})
            ok(r.status_code == 403 and "only Channels approves" in r.json()["detail"],
               f"Video Maker ({vm_actor}) approving a CM teaser with vm_pass → 403 ({r.status_code})")
        ok(runs("approve", 1) == [], "refused VM approves write no JobRun")
        with Session(engine) as s:
            ok(s.get(Video, 1).status == VideoStatus.REVIEW, "teaser still in review after VM approve attempts")
        ok(review_guard.can_record_vm_pass("vm") and review_guard.can_record_vm_pass("channels")
           and not review_guard.can_record_vm_pass("growth"), "vm_pass allowlist: Channels + VM, not growth")
        ok(review_guard.is_channels_actor("channels") and not review_guard.is_channels_actor("vm")
           and not review_guard.is_channels_actor("video-maker"), "approve allowlist: Channels only")
        r = client.post("/api/videos/1/vm-pass", auth=GROWTH, headers={"X-Actor": "video-maker"},
                        json={"note": "re-check"})
        ok(r.status_code == 200 and r.json()["vm_pass"]["actor"] == "video-maker",
           "VM (video-maker) can still record the PASS")
        r = client.post("/api/videos/1/approve", auth=GROWTH, headers=CH)
        ok(r.status_code == 200 and r.json()["status"] == VideoStatus.APPROVED,
           "Channels + vm_pass on this render → approved")
        ar = runs("approve", 1)
        ok(len(ar) == 1 and ar[0].detail.endswith("actor=channels") and ar[0].created_at,
           "approve JobRun records actor=channels + timestamp")

        print("stale vm_pass (new render) does not count")
        with Session(engine) as s:
            v = s.get(Video, 3)
            review_guard.set_vm_pass(v, "vm")
            v.video_path = art("3-rerender")   # a new render replaced the artifact
            s.add(v)
            s.commit()
        r = client.post("/api/videos/3/approve", auth=GROWTH, headers=CH)
        ok(r.status_code == 409, "vm_pass recorded for an older render → 409")

        print("requeue guard (chosen: blocked for non-Channels actors)")
        r = client.post("/api/videos/3/requeue", auth=GROWTH)
        ok(r.status_code == 403 and "only Channels requeues" in r.json()["detail"],
           "growth requeueing a CM teaser → 403")
        ok(runs("requeue", 3) == [], "refused requeue writes nothing")
        r = client.post("/api/videos/3/requeue", auth=GROWTH, headers=CH)
        ok(r.status_code == 200 and r.json()["status"] == VideoStatus.QUEUED, "Channels requeue → 200")
        rq = runs("requeue", 3)
        ok(len(rq) == 1 and rq[0].detail.endswith("actor=channels"), "requeue JobRun records the actor")

        print("non-teasers unchanged")
        r = client.post("/api/videos/2/approve", auth=GROWTH)
        ok(r.status_code == 200, "growth approves a non-teaser as before")
        ok(runs("approve", 2)[0].detail.endswith("actor=growth"), "actor falls back to the Basic username")
        r = client.post("/api/videos/4/requeue", auth=GROWTH)
        ok(r.status_code == 200, "growth requeues a non-teaser as before")
        r = client.post("/api/videos/4/reject", auth=GROWTH, json={"reason": "weak"})
        ok(r.status_code in (200, 409), "reject reachable")
        with Session(engine) as s:
            v4 = s.get(Video, 4)
        if r.status_code == 200:
            ok(runs("reject", 4)[0].detail.endswith("actor=growth"), "reject JobRun records the actor")
        else:  # queued rows may not be rejectable; reject a review row instead
            with Session(engine) as s:
                s.add(Video(channel_id=1, topic_id=2, subject="r", title="r", script="s",
                            status=VideoStatus.REVIEW, video_path=art(5)))
                s.commit()
            r = client.post("/api/videos/5/reject", auth=GROWTH, headers={"X-Actor": "cto"},
                            json={"reason": "weak"})
            ok(r.status_code == 200 and runs("reject", 5)[0].detail.endswith("actor=cto"),
               "reject JobRun records the actor (X-Actor)")
finally:
    settings.app_password = _pw
    main.app.dependency_overrides.pop(get_session, None)

print()
print(f"ALL {_checks} CHECKS PASSED")
