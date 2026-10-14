"""Approve guard: VM Gate B PASS mandatory, NO exceptions + approval audit (14/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_approve_guard.py

Pins:
  * every approve path refuses a video without the VM PASS on its current
    render with 409 and detail starting "held: needs vm_pass": POST approve
    (growth agent / dashboard / Channels / unknown API caller), with any
    force/admin/grandfather/exempt query or body noise, retry-republish;
    PATCH /videos/{id} {"status": "approved"} and PATCH /craft never approve;
  * legacy topic_flags vm_pass_exempt / vm_pass_required[] (settings + env)
    are ignored;
  * with the PASS → 200 and one ApprovalAudit row (actor, actor_kind, path,
    video id, timestamp); refused calls write no audit row.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["MANAGER_TOPIC_FLAGS"] = '{"vm_pass_exempt": ["*"], "vm_pass_required": []}'

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import ApprovalAudit, Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, review_guard

_n = 0


def ok(cond, msg):
    global _n
    _n += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(settings.topic_flags.get("vm_pass_exempt") == ["*"], "env sets legacy exempt ['*'] (must be ignored)")
_ART = Path(tempfile.mkdtemp(prefix="approve-guard-"))
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(id=3, channel_id=1, name="Agent memory", content_format="short"))
    s.commit()


_ep = [0]


def add(name, status=VideoStatus.REVIEW, **kw):
    _ep[0] += 1
    f = _ART / f"{name}.mp4"
    f.write_bytes(b"mp4-" + name.encode())
    with Session(engine) as s:
        v = Video(channel_id=1, topic_id=3, subject=name, title=f"{name}. · Agent memory {_ep[0]}",
                  script="x.", metadata_generated=True, status=status, video_path=str(f), **kw)
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def get(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


def audits(vid=None):
    with Session(engine) as s:
        q = select(ApprovalAudit)
        if vid is not None:
            q = q.where(ApprovalAudit.video_id == vid)
        return s.exec(q).all()


def give_pass(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        review_guard.set_vm_pass(v, "vm")
        s.add(v)
        s.commit()


def _sess():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)


def held(r):
    return r.status_code == 409 and str(r.json().get("detail", "")).startswith("held: needs vm_pass — ")


try:
    with patch.object(craft, "publish_craft_block_reason", return_value=None):
        print("approve without PASS → 409 held, every actor / every knob")
        a = add("nopass")
        for who, kw in [("growth agent", dict(auth=("growth", "pw"))),
                        ("dashboard", dict(auth=("rodrigo", "pw"), headers={"X-Actor": "dashboard"})),
                        ("channels", dict(auth=("x", "pw"), headers={"X-Actor": "channels"})),
                        ("api/unknown", dict(auth=("", "pw")))]:
            r = client.post(f"/api/videos/{a}/approve", **kw)
            ok(held(r), f"{who}: 409 held ({r.status_code} {r.text[:80]})")
        for q in ("force=true", "admin=1", "grandfather=true", "vm_pass_exempt=true", "skip_gate=true"):
            r = client.post(f"/api/videos/{a}/approve?{q}", auth=("growth", "pw"),
                            json={"force": True, "vm_pass": "PASS", "admin": True})
            ok(held(r), f"?{q} + body force/admin/vm_pass → still 409 held")
        ok(get(a).status == VideoStatus.REVIEW and not audits(a), "refused approve wrote nothing")

        print("alternate paths never approve")
        r = client.patch(f"/api/videos/{a}", auth=("growth", "pw"), json={"status": "approved",
                                                                         "approved_at": "2026-10-14T00:00:00"})
        ok(get(a).status == VideoStatus.REVIEW and get(a).approved_at is None,
           f"PATCH /videos/{{id}} status=approved ignored ({r.status_code})")
        r = client.patch(f"/api/videos/{a}/craft", auth=("growth", "pw"), json={"status": "approved"})
        ok(get(a).status == VideoStatus.REVIEW, f"PATCH /craft never approves ({r.status_code})")
        f = add("failedpub", status=VideoStatus.FAILED, error="upload failed: boom")
        with Session(engine) as s:
            s.add(JobRun(video_id=f, channel_id=1, kind="publish", status="error", detail="upload failed"))
            s.commit()
        r = client.post(f"/api/videos/{f}/retry", auth=("growth", "pw"))
        ok(held(r) and get(f).status == VideoStatus.FAILED, "retry-republish without PASS → 409 held")
        ok(review_guard.vm_pass_required_reason(get(a)).startswith("held: needs vm_pass — "),
           "guard ignores legacy vm_pass_exempt ['*'] / vm_pass_required []")

        print("with PASS → ok + audit")
        for name, kw, kind in [("p-growth", dict(auth=("growth", "pw")), "growth_agent"),
                               ("p-dash", dict(auth=("rodrigo", "pw"), headers={"X-Actor": "dashboard"}), "user"),
                               ("p-api", dict(auth=("cto", "pw")), "api")]:
            v = add(name)
            give_pass(v)
            r = client.post(f"/api/videos/{v}/approve", **kw)
            ok(r.status_code == 200 and r.json()["status"] == VideoStatus.APPROVED, f"{name}: 200 approved")
            rows = audits(v)
            ok(len(rows) == 1 and rows[0].actor_kind == kind and rows[0].path == "approve"
               and rows[0].video_id == v and rows[0].created_at and rows[0].vm_pass_actor == "vm",
               f"{name}: audit row actor={rows[0].actor if rows else None} kind={kind}")
        give_pass(f)
        r = client.post(f"/api/videos/{f}/retry", auth=("growth", "pw"))
        rows = audits(f)
        ok(r.status_code == 200 and len(rows) == 1 and rows[0].path == "retry"
           and rows[0].actor_kind == "growth_agent", "retry-republish with PASS → audit path=retry")
        ok(review_guard.actor_kind("skip-gate") == "system", "skip-gate auto-approve audited as system")
finally:
    settings.app_password = _pw
    main.app.dependency_overrides.clear()

print(f"\nALL {_n} CHECKS PASSED")
