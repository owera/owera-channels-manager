"""Stale render after PATCH /craft (2026-10-06, OS Agent traps #1319/#1328).

Run: PYTHONPATH=. .venv/bin/python tests/verify_stale_render.py

Loophole: PATCH /api/videos/{id}/craft re-scored craft_review from the SAVED
text, so after a text fix a video could read status=review +
craft_review=pass while its mp4 was still the OLD render (cards cut) that the
VM failed, and approve / auto-approve could approve a VM-failed master.
Pins:
  * an edit on a rendered video writes creation_config.stale_render and the
    re-score yields craft_review=fail (never pass) with the stale reason;
  * approve → 409; review_ready digest → not ready; publish gate rejects;
  * an approved video drops back to review (approved_at cleared);
  * a later PATCH cannot clear the marker; a no-op PATCH / a video without an
    mp4 is not marked;
  * a completed re-render (_finalize) clears it and approve works again.
In-memory DB + TestClient. Title/Gate/audio probes stubbed (not under test).
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import os as _os_vmp; _os_vmp.environ["MANAGER_TOPIC_FLAGS"] = '{"vm_pass_required": []}'  # test-only: pre-vm_pass-lock flow (PR #87 default = all topics; lock pinned in verify_vm_pass_topics)
import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, issues, publish_loop, render_loop

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


_TMP = Path(tempfile.mkdtemp(prefix="stale-render-"))


def art(name):
    f = _TMP / f"{name}.mp4"
    f.write_bytes(b"mp4")
    return str(f)


TITLE = "Chat paged Lee · Agent traps 4"
CC = {"beats": [{"type": "hook", "text": "Chat paged Lee"}]}

print("craft helpers")
ok(craft.stale_render_of(None) is None and craft.stale_render_of("{}") is None,
   "no marker → None")
marked = craft.mark_stale_render(json.dumps(CC), video_path="/x.mp4", fields=["script"])
ok(craft.stale_render_of(marked)["fields"] == ["script"], "mark_stale_render writes the marker")
ok(json.loads(craft.clear_stale_render(marked)) == CC, "clear_stale_render drops only the marker")
ok(craft.clear_stale_render(json.dumps(CC)) == json.dumps(CC), "clear is a no-op without a marker")
ok(craft.publish_craft_block_reason(title=TITLE, script="s", creation_config=marked,
                                    check_audio=False) == craft.STALE_RENDER_REASON,
   "publish craft gate blocks a stale render first")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="Agent traps", content_format="short"))
    s.commit()
    for i, st in enumerate((VideoStatus.REVIEW, VideoStatus.APPROVED, VideoStatus.REVIEW), 1):
        s.add(Video(channel_id=1, topic_id=1, subject=TITLE, title=TITLE, script="old script",
                    creation_config=json.dumps(CC), status=st, video_path=art(i),
                    craft_review=craft.CRAFT_REVIEW_PASS, metadata_generated=True,
                    mpt_task_id=f"t{i}"))
    s.add(Video(channel_id=1, topic_id=1, subject=TITLE, title=TITLE, script="old",
                status=VideoStatus.RENDERED, video_path=None))          # 4: no mp4
    s.commit()


def _sess():
    with Session(engine) as s:
        yield s


def get(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)
auth = ("x", "pw")
stubs = [patch.object(craft, "review_gate_reason", return_value=None),
         patch.object(craft, "nonsense_title_reason", return_value=None),
         patch.object(craft, "probe_has_audio", return_value=True)]
for p in stubs:
    p.start()
try:
    print("baseline: the same video is approvable before the edit")
    ok(craft.publish_craft_block_reason(title=TITLE, script="old script",
                                        creation_config=json.dumps(CC)) is None,
       "un-edited render clears the (stubbed) gate")

    print("PATCH /craft on a rendered review video → stale, never pass")
    r = client.patch("/api/videos/1/craft", auth=auth, json={"script": "fixed card text script"})
    v = get(1)
    ok(r.status_code == 200 and craft.stale_render_of(v.creation_config) is not None,
       "text fix on a rendered video writes stale_render")
    ok(craft.stale_render_of(v.creation_config)["video_path"] == v.video_path
       and craft.stale_render_of(v.creation_config)["fields"] == ["script"],
       "marker records the stale mp4 and the edited field")
    ok(v.craft_review == craft.CRAFT_REVIEW_FAIL, f"craft_review re-score is fail, not pass ({v.craft_review})")
    ok(v.status == VideoStatus.REVIEW and v.video_path.endswith("1.mp4"),
       "status / video_path untouched (no re-render burnt)")
    with Session(engine) as s:
        jr = s.exec(select(JobRun).where(JobRun.kind == "craft_persist",
                                         JobRun.video_id == 1)).all()
    ok(jr and "render marked stale" in jr[-1].detail and "render is stale" in jr[-1].detail,
       "craft_persist JobRun says why")

    r = client.post("/api/videos/1/approve", auth=auth)
    ok(r.status_code == 409 and "render is stale" in r.json()["detail"],
       f"approve of a stale render → 409 ({r.status_code})")
    ok(get(1).status == VideoStatus.REVIEW, "still in review after the refused approve")
    with Session(engine) as s:
        ok(issues.review_ready_reason(s, s.get(Video, 1)) is not None,
           "review_ready digest: stale render is not ready (auto-approve never sees it)")

    print("a later PATCH cannot clear the marker")
    r = client.patch("/api/videos/1/craft", auth=auth, json={"creation_config": CC})
    ok(r.status_code == 200 and craft.stale_render_of(get(1).creation_config) is not None,
       "PATCH creation_config without the marker keeps the render stale")
    ok(get(1).craft_review == craft.CRAFT_REVIEW_FAIL, "still craft_review=fail")

    print("approved video → back to review")
    r = client.patch("/api/videos/2/craft", auth=auth,
                     json={"creation_config": {"beats": [{"type": "hook", "text": "New card"}]}})
    v2 = get(2)
    ok(r.status_code == 200 and v2.status == VideoStatus.REVIEW and v2.approved_at is None,
       "approved + beats edit → review, approved_at cleared")
    ok(craft.stale_render_of(v2.creation_config)["fields"] == ["creation_config"],
       "marker names creation_config")

    print("publish loop refuses a stale render")
    with Session(engine) as s:
        v = s.get(Video, 2)
        v.status = VideoStatus.APPROVED            # e.g. a row approved before this fix
        v.craft_review = craft.CRAFT_REVIEW_PASS
        s.add(v)
        s.commit()
        publish_loop._publish_one(s, s.get(Channel, 1), s.get(Video, 2))
    v2 = get(2)
    ok(v2.status == VideoStatus.REJECTED and "render is stale" in (v2.rejected_reason or ""),
       f"publish gate: stale render never uploads ({v2.status})")

    print("no-op / no-mp4 edits are not marked")
    r = client.patch("/api/videos/3/craft", auth=auth, json={"script": "old script"})
    ok(r.status_code == 200 and craft.stale_render_of(get(3).creation_config) is None,
       "same script (no change) → not stale")
    r = client.patch("/api/videos/4/craft", auth=auth, json={"script": "new"})
    ok(r.status_code == 200 and craft.stale_render_of(get(4).creation_config) is None,
       "no rendered mp4 → nothing to mark")

    print("a completed re-render clears the marker")
    src = Path(art("render-out"))

    class _Eng:
        def final_path(self, task_id):
            return src

    _sd = settings.storage_dir
    settings.storage_dir = str(_TMP / "storage")
    try:
        with patch.object(render_loop, "_has_visible_frames", return_value=True), \
                patch.object(render_loop, "_make_thumbnail", return_value=False):
            with Session(engine) as s:
                v = s.get(Video, 1)
                v.status = VideoStatus.RENDERING
                render_loop._finalize(s, v, s.get(Channel, 1), _Eng(), {"creation_config": None})
                s.add(v)
                s.commit()
    finally:
        settings.storage_dir = _sd
    v = get(1)
    ok(craft.stale_render_of(v.creation_config) is None, "re-render dropped stale_render")
    ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_PENDING,
       f"re-rendered video back in review, pending ({v.status}, {v.craft_review})")
    ok(json.loads(v.creation_config) == CC, "rest of creation_config kept")
    r = client.post("/api/videos/1/approve", auth=auth)
    ok(r.status_code == 200 and r.json()["status"] == VideoStatus.APPROVED,
       f"approve works again after the re-render ({r.status_code} {r.text[:120]})")
finally:
    for p in stubs:
        p.stop()
    settings.app_password = _pw
    main.app.dependency_overrides.pop(get_session, None)

print()
print(f"ALL {_checks} CHECKS PASSED")
