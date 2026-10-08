"""vm_pass_required topics (VM FAIL P0 08/10: #1449/#1450 auto-approved and
published without Gate B).

Run: PYTHONPATH=. .venv/bin/python tests/verify_vm_pass_topics.py

Topics listed under settings.topic_flags["vm_pass_required"] (env
MANAGER_TOPIC_FLAGS, default [47] = OS "named tool") need the Video Maker's
Gate B PASS on the CURRENT render — the CM teaser lock (#71/#73) without its
Channels-only actor rule. Pins:
  * config: default [47]; an env object without the flag keeps the default;
    an override replaces the list;
  * manual approve: 409 without vm_pass; 200 with vm_pass on this render (any
    actor); a vm_pass from an older render (other path, or the same path
    overwritten by a re-render) → 409; a non-listed topic is unaffected;
  * retry-republish (publish-side failure → approved): 409 without vm_pass;
  * skip-gate auto-approve at render finalize: parked in review (pending,
    error names the lock); a finalize drops any vm_pass carried over;
    a non-listed topic still auto-approves;
  * publish loop: an approved row without vm_pass is parked in review before
    PUBLISHING (never uploaded); with vm_pass it proceeds.
In-memory DB + TestClient; craft publish gate stubbed (not under test).
"""
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import Settings, settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, publish_loop, render_loop, review_guard, topic_flags

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("config")
ok(Settings().topic_flags.get("vm_pass_required") == [47]
   and topic_flags.DEFAULTS[topic_flags.VM_PASS_REQUIRED] == (47,),
   "default vm_pass_required topics = [47] (settings + code default)")
ok(topic_flags.has(topic_flags.VM_PASS_REQUIRED, 47) and not topic_flags.has(topic_flags.VM_PASS_REQUIRED, 3)
   and not topic_flags.has(topic_flags.VM_PASS_REQUIRED, None),
   "topic 47 flagged; others / None not")
_orig_flags = settings.topic_flags
settings.topic_flags = {"some_other_flag": [9]}
ok(topic_flags.topics_with(topic_flags.VM_PASS_REQUIRED) == frozenset({47}),
   "env object without the flag keeps the default [47]")
settings.topic_flags = {"vm_pass_required": [47, "52", "junk"]}
ok(topic_flags.topics_with(topic_flags.VM_PASS_REQUIRED) == frozenset({47, 52}),
   "override replaces the list (ints / numeric strings; junk ignored)")
settings.topic_flags = {"vm_pass_required": []}
ok(not topic_flags.has(topic_flags.VM_PASS_REQUIRED, 47), "an empty list turns the lock off")
settings.topic_flags = _orig_flags
os.environ["MANAGER_TOPIC_FLAGS"] = '{"vm_pass_required": [47, 61]}'
try:
    ok(Settings().topic_flags["vm_pass_required"] == [47, 61], "MANAGER_TOPIC_FLAGS env (JSON object) is read")
finally:
    os.environ.pop("MANAGER_TOPIC_FLAGS", None)

_ART = Path(tempfile.mkdtemp(prefix="vm-pass-topics-"))


def art(name, data=b"mp4"):
    f = _ART / f"{name}.mp4"
    f.write_bytes(data)
    return str(f)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED,
                  default_skip_gate=True))
    s.commit()
    s.add(Topic(id=3, channel_id=1, name="Agent memory", content_format="short"))
    s.add(Topic(id=47, channel_id=1, name="Agent traps", content_format="short"))
    s.commit()


def add(**kw):
    with Session(engine) as s:
        v = Video(**{"channel_id": 1, "topic_id": 47, "subject": kw.get("title") or "x",
                     "script": "x.", "metadata_generated": True, **kw})
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def get(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


def _sess():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)
GROWTH = ("growth", "pw")
VM = {"X-Actor": "vm"}

try:
    with patch.object(craft, "publish_craft_block_reason", return_value=None):
        print("manual approve")
        a = add(title="Cursor timed out on French Windows. · Agent traps 9",
                status=VideoStatus.REVIEW, video_path=art("a"))
        r = client.post(f"/api/videos/{a}/approve", auth=GROWTH)
        ok(r.status_code == 409 and "requires the VM's Gate B PASS" in r.json()["detail"]
           and "topic 47" in r.json()["detail"],
           f"topic 47 without vm_pass → 409 with the reason ({r.status_code})")
        with Session(engine) as s:
            ok(s.exec(select(JobRun).where(JobRun.kind == "approve", JobRun.video_id == a)).all() == [],
               "refused approve writes no JobRun")
        ok(get(a).status == VideoStatus.REVIEW, "still in review")
        r = client.post(f"/api/videos/{a}/vm-pass", auth=GROWTH)
        ok(r.status_code == 403, "growth still cannot record vm_pass")
        r = client.post(f"/api/videos/{a}/vm-pass", auth=GROWTH, headers=VM,
                        json={"note": "Gate B PASS"})
        ok(r.status_code == 200 and r.json()["vm_pass"]["artifact"],
           "VM records vm_pass (bound to path + artifact fingerprint)")
        r = client.post(f"/api/videos/{a}/approve", auth=GROWTH)
        ok(r.status_code == 200 and r.json()["status"] == VideoStatus.APPROVED,
           "vm_pass on the current render → approve OK (any actor, not a CM teaser)")

        print("stale vm_pass (older render)")
        b = add(title="Stale path. · Agent traps 10", status=VideoStatus.REVIEW, video_path=art("b"))
        with Session(engine) as s:
            v = s.get(Video, b)
            review_guard.set_vm_pass(v, "vm")
            v.video_path = art("b-rerender")
            s.add(v)
            s.commit()
        r = client.post(f"/api/videos/{b}/approve", auth=GROWTH)
        ok(r.status_code == 409, "vm_pass recorded for another path → 409")
        c = add(title="Stale same path. · Agent traps 11", status=VideoStatus.REVIEW,
                video_path=art("c"))
        with Session(engine) as s:
            v = s.get(Video, c)
            review_guard.set_vm_pass(v, "vm")
            s.add(v)
            s.commit()
        time.sleep(0.01)
        art("c", b"mp4-rerendered-bytes")  # re-render overwrites the same storage path
        ok(review_guard.vm_pass_of(get(c)) is None,
           "same path overwritten by a re-render → vm_pass no longer current")
        r = client.post(f"/api/videos/{c}/approve", auth=GROWTH)
        ok(r.status_code == 409, "…and approve → 409")
        legacy = add(title="Legacy record. · Agent traps 12", status=VideoStatus.REVIEW,
                     video_path=art("legacy"))
        with Session(engine) as s:
            v = s.get(Video, legacy)
            v.creation_config = json.dumps({"vm_pass": {"result": "PASS", "actor": "vm",
                                                        "video_path": v.video_path}})
            s.add(v)
            s.commit()
        ok(review_guard.vm_pass_of(get(legacy)) is not None,
           "a pre-fingerprint record keeps the path-only check (no retroactive breakage)")

        print("non-listed topic unaffected")
        n = add(topic_id=3, title="Chat paged Lee. · Agent memory 4", status=VideoStatus.REVIEW,
                video_path=art("n"))
        ok(review_guard.vm_pass_required_reason(get(n)) is None, "topic 3 has no vm_pass lock")
        r = client.post(f"/api/videos/{n}/approve", auth=GROWTH)
        ok(r.status_code == 200, "topic 3 approves without vm_pass as before")

        print("retry-republish")
        f = add(title="Upload stalled. · Agent traps 13", status=VideoStatus.FAILED,
                video_path=art("f"), error="upload failed: socket")
        r = client.post(f"/api/videos/{f}/retry", auth=GROWTH)
        ok(r.status_code == 409 and "requires the VM's Gate B PASS" in r.json()["detail"],
           f"retry of a publish-side failure without vm_pass → 409 ({r.status_code})")
        ok(get(f).status == VideoStatus.FAILED, "row unchanged")

        print("skip-gate auto-approve at render finalize")
        src = _ART / "out.mp4"
        src.write_bytes(b"rendered")

        class _Eng:
            def final_path(self, task_id):
                return src

        def finalize(vid, task_cc=None):
            _sd = settings.storage_dir
            settings.storage_dir = str(_ART / f"st-{vid}")
            try:
                with patch.object(render_loop, "_has_visible_frames", return_value=True), \
                        patch.object(render_loop, "_make_thumbnail", return_value=False):
                    with Session(engine) as s:
                        v = s.get(Video, vid)
                        v.status = VideoStatus.RENDERING
                        task = {"creation_config": task_cc} if task_cc is not None else {}
                        render_loop._finalize(s, v, s.get(Channel, 1), _Eng(), task)
                        s.add(v)
                        s.commit()
            finally:
                settings.storage_dir = _sd
            return get(vid)

        q = add(title="Render. · Agent traps 14", status=VideoStatus.QUEUED, mpt_task_id="t-q")
        v = finalize(q, {"beats": []})
        ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_PENDING
           and v.approved_at is None and "requires the VM's Gate B PASS" in (v.error or ""),
           "skip-gate channel: a topic-47 render is NOT auto-approved (review, pending, reason)")
        # engine without a task creation_config: the old blob (with a vm_pass on the
        # same storage path) must not let the new render auto-approve
        q2 = add(title="Render again. · Agent traps 15", status=VideoStatus.QUEUED, mpt_task_id="t-q2")
        dest = _ART / f"st-{q2}" / "videos" / str(q2) / "video.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"old render")
        with Session(engine) as s:
            v = s.get(Video, q2)
            v.video_path = str(dest)
            review_guard.set_vm_pass(v, "vm")
            s.add(v)
            s.commit()
        v = finalize(q2)
        ok(v.status == VideoStatus.REVIEW and "vm_pass" not in json.loads(v.creation_config or "{}"),
           "finalize drops a carried-over vm_pass (same path) → stays in review")
        q3 = add(topic_id=3, title="Render fine. · Agent memory 5", status=VideoStatus.QUEUED,
                 mpt_task_id="t-q3")
        v = finalize(q3, {"beats": []})
        ok(v.status == VideoStatus.APPROVED and v.craft_review == craft.CRAFT_REVIEW_PASS,
           "non-listed topic still auto-approves on a skip-gate channel")

        print("publish loop (defense in depth)")
        p = add(title="Publish. · Agent traps 16", status=VideoStatus.APPROVED,
                craft_review=craft.CRAFT_REVIEW_PASS, video_path=art("p"))
        uploaded = []
        with patch.object(publish_loop.youtube, "get_service",
                          side_effect=lambda *a, **k: uploaded.append(1) or (_ for _ in ()).throw(RuntimeError("stop"))):
            with Session(engine) as s:
                publish_loop._publish_one(s, s.get(Channel, 1), s.get(Video, p))
        v = get(p)
        ok(v.status == VideoStatus.REVIEW and v.approved_at is None
           and v.craft_review == craft.CRAFT_REVIEW_PENDING
           and "requires the VM's Gate B PASS" in (v.error or "") and not uploaded,
           "approved topic-47 row without vm_pass → parked in review, never reaches upload")
        with Session(engine) as s:
            ok(any("publish vm_pass gate" in (j.detail or "") for j in
                   s.exec(select(JobRun).where(JobRun.kind == "publish", JobRun.video_id == p)).all()),
               "publish JobRun logs the vm_pass gate")
        src_p = inspect.getsource(publish_loop._publish_one)
        ok(src_p.index("vm_pass_required_reason") < src_p.index("VideoStatus.PUBLISHING"),
           "publish loop checks vm_pass before flipping to PUBLISHING")
        p2 = add(title="Publish ok. · Agent traps 17", status=VideoStatus.APPROVED,
                 craft_review=craft.CRAFT_REVIEW_PASS, video_path=art("p2"))
        with Session(engine) as s:
            v = s.get(Video, p2)
            review_guard.set_vm_pass(v, "vm")
            s.add(v)
            s.commit()
        with patch.object(publish_loop.youtube, "get_service",
                          side_effect=lambda *a, **k: uploaded.append(1) or (_ for _ in ()).throw(RuntimeError("stop"))):
            with Session(engine) as s:
                publish_loop._publish_one(s, s.get(Channel, 1), s.get(Video, p2))
        _v2 = get(p2)
        ok(uploaded == [1] and _v2.status in (VideoStatus.APPROVED, VideoStatus.PUBLISHING)
           and "vm_pass" not in (_v2.error or ""),
           f"with vm_pass on the current render it proceeds to upload ({_v2.status}, stub stops it)")
finally:
    settings.app_password = _pw
    settings.topic_flags = _orig_flags
    main.app.dependency_overrides.pop(get_session, None)

print()
print(f"ALL {_checks} CHECKS PASSED")
