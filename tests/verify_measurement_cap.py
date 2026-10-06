"""Measurement-fail cap (CTO council 06/10 item 2c).

Run: PYTHONPATH=. .venv/bin/python tests/verify_measurement_cap.py

#70 split Gate B/C outcomes into kind="real" (a craft defect → FAIL) and
kind="measurement" (the measuring step could not verify the check → "not
verifiable (not failed)", so the gate may still PASS). Pins the ceiling:
  * 1 render with a measurement entry behaves as today (PASS → skip-gate
    auto-approve still approves);
  * 2 consecutive renders with a measurement entry → craft gate FAIL with the
    "Measurement cap" reason → the row parks in review (route to VM review);
  * a real pass (no measurement entry) in between resets the counter;
  * after 2 consecutive, no path yields PASS: skip-gate auto-approve, approve,
    publish gate, review_ready digest, a 3rd measurement render, a real-FAIL
    render, requeue, and a PATCH /craft that rewrites creation_config all keep
    it blocked; only the VM's Gate B PASS recorded on THAT render (vm-pass) or
    a later fully-measured render releases it;
  * longs exempt; streak persisted on Video.measurement_streak (+ db column).
The Video Maker gate itself is stubbed per render (kinds under test); the
render is driven through the real render_loop._finalize. In-memory DB.
"""
import inspect
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.db as app_db
import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, issues, render_loop

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


MEAS = {"result": "PASS", "checks": {"A": "PASS", "B": "PASS", "C": "PASS"}, "reasons": [],
        "kinds": [{"check": "card_sync", "kind": "measurement", "card": 2,
                   "status": "unmatched", "why": "words_not_in_tts"}]}
CLEAN = {"result": "PASS", "checks": {"A": "PASS", "B": "PASS", "C": "PASS"}, "reasons": []}
REAL = {"result": "FAIL", "checks": {"B": "FAIL"}, "reasons": ["[B] Card sync: FAIL — card 3 late"],
        "kinds": [{"check": "card_sync", "kind": "real", "card": 3, "status": "late"}]}
REAL_AND_MEAS = {**REAL, "kinds": REAL["kinds"] + MEAS["kinds"]}

print("helpers")
ok(craft.gate_measurement_kinds(MEAS) == MEAS["kinds"] and craft.gate_measurement_kinds(CLEAN) == []
   and craft.gate_measurement_kinds(REAL) == [] and craft.gate_measurement_kinds(None) == [],
   "gate_measurement_kinds picks only kind=measurement")
ok(craft.MEASUREMENT_CAP_N == 2, "cap is 2 consecutive renders")
ok("measurement_streak" in inspect.getsource(app_db._add_missing_columns),
   "db migration adds video.measurement_streak (existing _add_missing_columns pattern)")
ok(Video(channel_id=1, topic_id=1, subject="x").measurement_streak == 0, "model default 0")

_TMP = Path(tempfile.mkdtemp(prefix="meas-cap-"))
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
TITLE = "Chat paged Lee · Agent traps 4"
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED,
                  default_skip_gate=True))                      # skip-gate auto-approve on
    s.commit()
    s.add(Topic(channel_id=1, name="Agent traps", content_format="short"))
    s.add(Topic(channel_id=1, name="Deep dives", content_format="long"))
    s.commit()
    for i in range(4):
        s.add(Video(channel_id=1, topic_id=2 if i == 3 else 1, subject=TITLE, title=TITLE,
                    script="Chat paged Lee.", metadata_generated=True,
                    status=VideoStatus.QUEUED, mpt_task_id=f"t{i}"))
    s.commit()


def _sess():
    with Session(engine) as s:
        yield s


def get(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


_n = [0]


def render(vid, gate):
    """One render of video ``vid`` through the real _finalize; ``gate`` is what
    the Video Maker gate returns for this render's board."""
    _n[0] += 1
    src = _TMP / f"out-{_n[0]}.mp4"
    src.write_bytes(b"mp4")

    class _Eng:
        def final_path(self, task_id):
            return src

    cc = {"beats": [{"type": "hook", "text": "Chat paged Lee"}, {"type": "statement", "text": "x"}],
          "craft_gate": gate, "render_n": _n[0]}
    _sd = settings.storage_dir
    settings.storage_dir = str(_TMP / f"storage-{_n[0]}")
    try:
        with patch.object(craft, "video_maker_gate", return_value=gate), \
                patch.object(render_loop, "_has_visible_frames", return_value=True), \
                patch.object(render_loop, "_make_thumbnail", return_value=False):
            with Session(engine) as s:
                v = s.get(Video, vid)
                v.status = VideoStatus.RENDERING
                render_loop._finalize(s, v, s.get(Channel, 1), _Eng(), {"creation_config": cc})
                s.add(v)
                s.commit()
    finally:
        settings.storage_dir = _sd
    return get(vid)


def gate_reason(v, gate):
    with patch.object(craft, "video_maker_gate", return_value=gate):
        return craft.video_maker_gate_reason(v.creation_config, "short")


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)
auth = ("x", "pw")
stubs = [patch.object(craft, "title_gate_reason", return_value=None),
         patch.object(craft, "nonsense_title_reason", return_value=None),
         patch.object(craft, "probe_has_audio", return_value=True)]
for p in stubs:
    p.start()
try:
    print("1 measurement fail behaves as today")
    v = render(1, MEAS)
    ok(v.measurement_streak == 1, f"streak 1 ({v.measurement_streak})")
    ok(craft.MEASUREMENT_CAP_KEY not in json.loads(v.creation_config), "no cap marker")
    ok(v.status == VideoStatus.APPROVED and v.craft_review == craft.CRAFT_REVIEW_PASS,
       f"skip-gate auto-approve still approves a single measurement render ({v.status})")
    ok(gate_reason(v, MEAS) is None, "gate reason None (unverifiable ≠ failed)")

    print("2 consecutive → FAIL / VM review")
    v = render(1, MEAS)
    cap = json.loads(v.creation_config).get(craft.MEASUREMENT_CAP_KEY)
    ok(v.measurement_streak == 2 and cap and cap["streak"] == 2 and cap["checks"] == ["card_sync"]
       and cap["video_path"] == v.video_path, f"streak 2, cap bound to this render: {cap}")
    ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_FAIL
       and "Measurement cap" in (v.error or ""),
       f"2nd consecutive: not auto-approved; parked in review with craft FAIL ({v.status}, {v.error!r:.80})")
    ok("Measurement cap" in (gate_reason(v, MEAS) or ""), "video_maker_gate_reason → Measurement cap FAIL")
    with patch.object(craft, "video_maker_gate", return_value=MEAS):
        ok("Measurement cap" in (craft.publish_craft_block_reason(
            title=TITLE, script=v.script, creation_config=v.creation_config,
            video_path=v.video_path) or ""), "publish craft gate blocks it")
        r = client.post("/api/videos/1/approve", auth=auth)
        ok(r.status_code == 409 and "Measurement cap" in r.json()["detail"],
           f"approve → 409 ({r.status_code})")
        with Session(engine) as s:
            ok(issues.review_ready_reason(s, s.get(Video, 1)) is not None,
               "review_ready digest: not ready (auto-approve never sees it)")
        r = client.patch("/api/videos/1/craft", auth=auth,
                         json={"creation_config": {"beats": [{"type": "hook", "text": "x"}]}})
        ok(r.status_code == 200 and craft.MEASUREMENT_CAP_KEY in json.loads(get(1).creation_config),
           "PATCH /craft rewriting creation_config cannot drop the cap")
        ok("Measurement cap" in (gate_reason(get(1), MEAS) or ""), "still capped after the PATCH")

    print("no PASS after 2 consecutive")
    v = render(1, MEAS)
    ok(v.measurement_streak == 3 and v.status == VideoStatus.REVIEW
       and v.craft_review == craft.CRAFT_REVIEW_FAIL, "3rd measurement render: still capped")
    v = render(1, REAL_AND_MEAS)
    ok(v.measurement_streak == 4 and v.status == VideoStatus.REVIEW
       and v.craft_review == craft.CRAFT_REVIEW_FAIL, "real FAIL + measurement render: FAIL, streak grows")
    v = render(1, REAL)
    ok(v.measurement_streak == 0 and v.status == VideoStatus.REVIEW
       and v.craft_review == craft.CRAFT_REVIEW_FAIL and "Card sync" in (v.error or ""),
       "a real-FAIL render without measurement resets the streak but is still a real FAIL")
    v = render(1, MEAS)
    v = render(1, MEAS)
    ok(v.measurement_streak == 2 and v.status == VideoStatus.REVIEW, "capped again after 2 more")
    r = client.post("/api/videos/1/requeue", auth=auth)
    ok(r.status_code == 200 and get(1).measurement_streak == 2,
       "requeue keeps the streak (render of record, not the text)")
    v = render(1, MEAS)
    ok(v.measurement_streak == 3 and v.status == VideoStatus.REVIEW,
       "the re-render after requeue with a measurement entry is still capped")

    print("route to VM review: only the VM's PASS on THIS render releases it")
    with patch.object(craft, "video_maker_gate", return_value=MEAS):
        r = client.post("/api/videos/1/vm-pass", auth=auth, headers={"X-Actor": "growth"})
        ok(r.status_code == 403, "growth cannot record vm_pass")
        ok(client.post("/api/videos/1/approve", auth=auth).status_code == 409,
           "still 409 before VM review")
        r = client.post("/api/videos/1/vm-pass", auth=auth, headers={"X-Actor": "vm"},
                        json={"note": "eyeballed card sync"})
        ok(r.status_code == 200, f"VM records Gate B PASS on this render ({r.status_code})")
        ok(gate_reason(get(1), MEAS) is None, "cap released by the VM PASS bound to this render")
        r = client.post("/api/videos/1/approve", auth=auth)
        ok(r.status_code == 200 and r.json()["status"] == VideoStatus.APPROVED,
           f"approve after VM review → 200 ({r.status_code})")
    with Session(engine) as s:
        vv = s.get(Video, 1)
        vv.status = VideoStatus.QUEUED
        s.add(vv)
        s.commit()
    v = render(1, MEAS)
    ok(v.measurement_streak == 4 and "vm_pass" not in json.loads(v.creation_config)
       and v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_FAIL,
       "a vm_pass from an older render never carries over to a new capped render")

    print("a real pass in between resets the counter")
    v = render(2, MEAS)
    ok(v.measurement_streak == 1 and v.status == VideoStatus.APPROVED, "video 2: measurement #1 → approved")
    with Session(engine) as s:
        vv = s.get(Video, 2)
        vv.status = VideoStatus.QUEUED
        s.add(vv)
        s.commit()
    v = render(2, CLEAN)
    ok(v.measurement_streak == 0 and v.status == VideoStatus.APPROVED, "real pass → streak 0")
    with Session(engine) as s:
        vv = s.get(Video, 2)
        vv.status = VideoStatus.QUEUED
        s.add(vv)
        s.commit()
    v = render(2, MEAS)
    ok(v.measurement_streak == 1 and v.status == VideoStatus.APPROVED
       and craft.MEASUREMENT_CAP_KEY not in json.loads(v.creation_config),
       "meas, PASS, meas → not consecutive: streak 1, no cap, approved")
    v = render(1, CLEAN)
    ok(v.measurement_streak == 0 and craft.MEASUREMENT_CAP_KEY not in json.loads(v.creation_config)
       and v.status == VideoStatus.APPROVED, "a capped video released by a fully-measured re-render")

    print("longs exempt")
    v = render(4, MEAS)
    v = render(4, MEAS)
    ok(craft.MEASUREMENT_CAP_KEY not in json.loads(v.creation_config)
       and craft.measurement_cap_reason(v.creation_config, "long") is None,
       "long-format renders are never capped (Gate B/C are shorts-only)")
finally:
    for p in stubs:
        p.stop()
    settings.app_password = _pw
    main.app.dependency_overrides.pop(get_session, None)

print()
print(f"ALL {_checks} CHECKS PASSED")
