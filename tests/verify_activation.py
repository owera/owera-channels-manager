"""S1 activation instrumentation (A0–A6): paid_at, first_draft_at, approved_by,
render-failed signal, onboarding first_draft step + timestamps, activation_report.

Run: PYTHONPATH=. uv run python tests/verify_activation.py
In-memory DB + TestClient; craft publish gate stubbed (not under test).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MANAGER_SPA_GOOGLE_LOGIN", "0")
_TMP = Path(tempfile.mkdtemp(prefix="verify-activation-"))
os.environ["MANAGER_DB_PATH"] = str(_TMP / "cli.db")   # CLI part only; never the real DB

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus, Workspace,
                        utcnow)
from app.services import activation, craft, onboarding, render_loop, review_guard, stripe_provision

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def art(name, data=b"mp4"):
    f = _TMP / f"{name}.mp4"
    f.write_bytes(data)
    return str(f)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.commit()
    s.add(Channel(id=1, workspace_id=1, slug="owera", name="Owera Software",
                  oauth_status=OAuthStatus.CONNECTED, default_skip_gate=True))
    s.add(Topic(id=3, channel_id=1, name="Agent memory", content_format="short"))
    s.commit()


def _sess():
    with Session(engine) as s:
        yield s


def get(model, pk):
    with Session(engine) as s:
        o = s.get(model, pk)
        s.expunge(o)
        return o


def add_video(**kw):
    with Session(engine) as s:
        d = {"channel_id": 1, "topic_id": 3, "subject": kw.get("title") or "x",
             "script": "x.", "metadata_generated": True}
        v = Video(**{**d, **kw})
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def _aw(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── schema ────────────────────────────────────────────────────────────────
print("schema")
for col in ("is_test", "paid_at", "first_draft_at"):
    ok(col in Workspace.model_fields, f"Workspace.{col}")
ok("approved_by" in Video.model_fields, "Video.approved_by")
from app import db as _db
import inspect as _inspect
_src = _inspect.getsource(_db._add_missing_columns)
for col in ("is_test", "paid_at", "first_draft_at", "approved_by"):
    ok(f'"{col}"' in _src, f"_add_missing_columns adds {col} (SQLite migration)")

# ── A0: paid_at from checkout event.created ───────────────────────────────
print("A0 paid_at")
settings.stripe_cm_payment_links = "plink_cm"
T_PAID = 1_790_000_000
ev = {"id": "evt_a0", "type": "checkout.session.completed", "created": T_PAID,
      "data": {"object": {"id": "cs_a0", "mode": "subscription", "payment_link": "plink_cm",
                          "customer": "cus_a0", "subscription": "sub_a0", "created": T_PAID - 99,
                          "customer_details": {"email": "Buyer@Example.com"}}}}
with patch.object(stripe_provision.stripe_email, "send_welcome_email", return_value=True) as _w:
    with Session(engine) as s:
        ok(stripe_provision.process_event(s, ev) == 200, "checkout processed")
    ok(_w.call_count == 1, "welcome still sent once (unchanged)")
with Session(engine) as s:
    buyer = s.exec(select(Workspace).where(Workspace.owner_email == "buyer@example.com")).one()
    BUYER = buyer.id
    ok(_aw(buyer.paid_at) == datetime.fromtimestamp(T_PAID, tz=timezone.utc),
       "paid_at = event.created (not data.object.created, not now)")
    ok(buyer.is_test is False, "paid buyer is not is_test")
# second checkout for the same workspace never moves paid_at
ev2 = json.loads(json.dumps(ev))
ev2["id"], ev2["created"] = "evt_a0b", T_PAID + 86400
ev2["data"]["object"]["id"] = "cs_a0b"
with patch.object(stripe_provision.stripe_email, "send_welcome_email", return_value=True):
    with Session(engine) as s:
        stripe_provision.process_event(s, ev2)
ok(_aw(get(Workspace, BUYER).paid_at) == datetime.fromtimestamp(T_PAID, tz=timezone.utc),
   "re-checkout keeps the first paid_at")
ok(stripe_provision._paid_at(None, {"created": T_PAID}) ==
   datetime.fromtimestamp(T_PAID, tz=timezone.utc), "fallback: session.created")
_now = stripe_provision._paid_at("junk", {})
ok(abs((_now - utcnow()).total_seconds()) < 5 and _now.tzinfo is not None,
   "fallback: now (tz-aware) when no created")

# ── A3: first_draft_at stamped at render finalize; A4 auto approver ───────
print("A3 first_draft_at + skip-gate auto approver")
settings.topic_flags = {"vm_pass_required": []}
src = _TMP / "out.mp4"
src.write_bytes(b"rendered")


class _Eng:
    def final_path(self, task_id):
        return src


def finalize(vid):
    _sd = settings.storage_dir
    settings.storage_dir = str(_TMP / f"st-{vid}")
    try:
        with patch.object(render_loop, "_has_visible_frames", return_value=True), \
                patch.object(render_loop, "_make_thumbnail", return_value=False), \
                patch.object(craft, "publish_craft_block_reason", return_value=None):
            with Session(engine) as s:
                v = s.get(Video, vid)
                v.status = VideoStatus.RENDERING
                render_loop._finalize(s, v, s.get(Channel, 1), _Eng(), {})
                s.add(v)
                s.commit()
    finally:
        settings.storage_dir = _sd
    return get(Video, vid)


ok(get(Workspace, 1).first_draft_at is None, "no first_draft_at before any render")
with Session(engine) as s:
    r = onboarding.evaluate(s, s.get(Workspace, 1))
st = {x["id"]: x for x in r["steps"]}
ok("first_draft" in st and st["first_draft"]["done"] is False and st["first_draft"]["at"] is None,
   "onboarding: first_draft step present, not done")
ok([x["id"] for x in r["steps"]] ==
   ["channel", "youtube", "topic", "budgets", "ideas", "first_draft", "live"],
   "step order: first_draft between ideas and live")

v1 = add_video(title="First render", status=VideoStatus.QUEUED, mpt_task_id="t1")
v = finalize(v1)
ok(v.status == VideoStatus.APPROVED and v.approved_by == render_loop.AUTO_APPROVER,
   f"skip-gate auto-approve stamps approved_by={render_loop.AUTO_APPROVER!r}")
fd1 = _aw(get(Workspace, 1).first_draft_at)
ok(fd1 is not None, "first successful render stamps workspace.first_draft_at")
v2 = add_video(title="Second render", status=VideoStatus.QUEUED, mpt_task_id="t2")
finalize(v2)
ok(_aw(get(Workspace, 1).first_draft_at) == fd1, "second render does not move first_draft_at")
with Session(engine) as s:
    r = onboarding.evaluate(s, s.get(Workspace, 1))
st = {x["id"]: x for x in r["steps"]}
ok(st["first_draft"]["done"] and st["first_draft"]["at"], "onboarding: first_draft done + at")
ok(r["timestamps"]["first_draft_at"] == st["first_draft"]["at"], "timestamps.first_draft_at")
ok(set(r["timestamps"]) >= {"created_at", "paid_at", "first_draft_at", "first_approved_at",
                             "first_published_at"}, "timestamps block keys")
ok(r["onboarding_complete"] is False, "onboarding_complete unchanged semantics (ideas missing)")
ok("render_failed" in r and r["is_test"] is False, "render_failed signal + is_test in status")

# stamp_first_draft is null-safe
with Session(engine) as s:
    ok(activation.stamp_first_draft(s, None) is False, "stamp_first_draft(None) no-op")

# ── A4: approve endpoint records actor ────────────────────────────────────
print("A4 approved_by")
main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
client = TestClient(main.app)
try:
    with patch.object(craft, "publish_craft_block_reason", return_value=None):
        settings.app_password = ""
        a = add_video(title="Approve me", status=VideoStatus.REVIEW, video_path=art("a"))
        rr = client.post(f"/api/videos/{a}/approve", headers={"X-Actor": "Buyer"})
        ok(rr.status_code == 200, f"approve 200 ({rr.status_code} {rr.text[:120]})")
        ok(get(Video, a).approved_by == "buyer", "approved_by = request actor (X-Actor, normalized)")
        b = add_video(title="Approve anon", status=VideoStatus.REVIEW, video_path=art("b"))
        rr = client.post(f"/api/videos/{b}/approve")
        ok(rr.status_code == 200 and get(Video, b).approved_by is None,
           "no identity → approved_by NULL (null-safe), approve still works")
        settings.app_password = "pw"
        c = add_video(title="Approve basic", status=VideoStatus.REVIEW, video_path=art("c"))
        rr = client.post(f"/api/videos/{c}/approve", auth=("channels", "pw"))
        ok(rr.status_code == 200 and get(Video, c).approved_by == "channels",
           "Basic user → approved_by")
        # retry-republish re-approves with the actor
        f = add_video(title="Upload stalled", status=VideoStatus.FAILED, video_path=art("f"),
                      error="upload failed: socket")
        rr = client.post(f"/api/videos/{f}/retry", auth=("channels", "pw"))
        ok(rr.status_code == 200 and get(Video, f).approved_by == "channels",
           f"retry-republish → approved_by = actor ({rr.status_code})")
finally:
    settings.app_password = _pw
ok(review_guard.approver_of("unknown") is None and review_guard.approver_of(None) is None
   and review_guard.approver_of("x" * 99) == "x" * 64, "approver_of null-safe + bounded")
with Session(engine) as s:
    vv = s.get(Video, a)
    from app.services.gate_grandfather import park_os_held
    park_os_held(vv, "held: test")
    ok(vv.approved_at is None and vv.approved_by is None, "park_os_held clears approved_by")

# ── A5: render-failed signal per workspace ────────────────────────────────
print("A5 render-failed signal")
now = utcnow()
with Session(engine) as s:
    s.add(Channel(id=2, workspace_id=BUYER, slug="b1", name="Buyer ch",
                  oauth_status=OAuthStatus.CONNECTED, niche="cooking"))
    for i in range(3):
        s.add(Topic(id=20 + i, channel_id=2, name=f"T{i}"))
    s.commit()
fa = add_video(channel_id=2, topic_id=20, title="old fail", status=VideoStatus.FAILED,
               updated_at=now - timedelta(hours=30))
# A days-old idea that failed 1 h ago: updated_at is stale (the render loop
# doesn't touch it on failure), the error JobRun is what dates the failure.
fb = add_video(channel_id=2, topic_id=20, title="new fail", status=VideoStatus.FAILED,
               updated_at=now - timedelta(hours=72))
with Session(engine) as s:
    s.add(JobRun(kind="render", status="error", channel_id=2, video_id=fa,
                 created_at=now - timedelta(hours=30)))
    s.add(JobRun(kind="render", status="error", channel_id=2, video_id=fb,
                 created_at=now - timedelta(hours=1)))
    s.commit()
    sig = activation.render_failed_signal(s, s.get(Workspace, BUYER), now)
    sig1 = activation.render_failed_signal(s, s.get(Workspace, 1), now)
ok(sig["failed_now"] == 2 and sig["failed_over_24h"] == 1 and sig["render_errors_24h"] == 1,
   f"buyer signal counts; fail age from the error JobRun, not stale updated_at ({sig})")
ok(sig["last_render_failed_at"] is not None, "last_render_failed_at")
ok(sig1["failed_now"] == 0 and sig1["failed_over_24h"] == 0,
   "ws1 signal is separate (buyer FAILED rows not counted)")

# ── activation report (buyer) ─────────────────────────────────────────────
print("activation report")
for i in range(5):
    vid = add_video(channel_id=2, topic_id=21, title=f"d{i}", status=VideoStatus.REVIEW)
    with Session(engine) as s:
        s.add(JobRun(kind="render", status="success", channel_id=2, video_id=vid,
                     created_at=now - timedelta(hours=10 - i)))
        s.commit()
add_video(channel_id=2, topic_id=21, title="buyer approved", status=VideoStatus.PUBLISHED,
          approved_at=now - timedelta(hours=3), approved_by="buyer",
          published_at=now - timedelta(hours=2), yt_video_id="yt1")
with Session(engine) as s:
    rep = activation.report(s, s.get(Workspace, BUYER), now)
ok(all(k in rep for k in ("A0", "A1", "A2", "A3", "A4", "A5", "A6")), "A0–A6 keys")
ok(all(set(rep[k]) >= {"done", "at"} for k in ("A0", "A1", "A2", "A3", "A4", "A5", "A6")),
   "each step has done/at")
ok(rep["A0"]["done"] and rep["A0"]["paid_at"] and rep["paid"] is True, "A0 done + paid_at")
ok(rep["A1"]["done"] and rep["A1"]["connected_channels"] == 1, "A1 connected")
ok(rep["A2"]["done"] and rep["A2"]["active_topics"] == 3, "A2 niche + 3 topics")
ok(rep["A3"]["done"] and rep["A3"]["drafts"] == 5 and rep["A3"]["first_draft_source"] == "jobrun",
   "A3 5 drafts; pre-S1 first draft derived from JobRun")
ok(rep["A4"]["done"] and rep["A4"]["approved_by"] == "buyer", "A4 buyer approval")
ok(rep["A5"]["done"] is False and rep["A5"]["failed_over_24h"] == 1, "A5 not done: failed > 24 h")
ok(rep["A6"]["done"] and rep["A6"]["published"] == 1, "A6 published")
with Session(engine) as s:
    rep1 = activation.report(s, s.get(Workspace, 1), now)
ok(rep1["A4"]["done"] and rep1["A4"]["auto_approvals"] >= 2 and rep1["A4"]["approved_by"] != "auto:skip_gate",
   "auto approvals are counted apart and never satisfy A4 as a buyer")
ok(rep1["A3"]["first_draft_source"] == "stamped", "stamped first_draft_at wins")
json.dumps(rep)  # serializable

# ── S5 test workspace in the report: is_test flagged, paid_at null ────────
print("is_test workspace (S5) in report")
from app.provision_test_workspace import provision
with Session(engine) as s:
    out = provision(s, "e2e.tester+test@owera.com")
    ok(out["ok"] and out["created"] and out["is_test"] and out["paid_at"] is None,
       "S5 provision: is_test, paid_at null")
    tw = s.get(Workspace, out["workspace_id"])
    rt = activation.report(s, tw, now)
    ok(rt["is_test"] is True and rt["paid_at"] is None and rt["paid"] is False,
       "report flags is_test and paid_at=null")
    ok(rt["A0"]["done"] and rt["A0"]["paid_at"] is None and rt["A0"]["provision_lag_seconds"] is None,
       "A0 for test ws: provisioned, no payment lag")
    allr = activation.report_all(s, now=now)
    ok([r["workspace_slug"] for r in allr if r["is_test"]] == [tw.slug]
       and all(r["workspace_id"] != 1 for r in allr), "report_all: test included, owera excluded")
    ok(all(not r["is_test"] for r in activation.report_all(s, include_test=False, now=now)),
       "report_all(include_test=False) drops test ws")
    r = onboarding.evaluate(s, tw)
    ok(r["is_test"] is True and r["timestamps"]["paid_at"] is None, "onboarding status: is_test, paid_at null")

# ── onboarding over HTTP (route) ──────────────────────────────────────────
print("GET /api/onboarding/status")
settings.app_password = ""
try:
    rr = client.get("/api/onboarding/status")
finally:
    settings.app_password = _pw
ok(rr.status_code == 200 and "timestamps" in rr.json()
   and any(x["id"] == "first_draft" for x in rr.json()["steps"]), "route returns first_draft + timestamps")

# ── CLI (file DB under tmp) ───────────────────────────────────────────────
print("activation_report CLI")
from app import activation_report as ar
from app.db import engine as cli_engine
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ar.main([])
ok(rc == 2 and "missing" in json.loads(buf.getvalue()), "CLI refuses (exit 2) on un-migrated DB, no ALTER")
from sqlalchemy import text as _t
with cli_engine.connect() as c:
    cols = {r[1] for r in c.execute(_t("PRAGMA table_info(workspace)"))}
ok("paid_at" not in cols, "CLI did not migrate the DB")
_db.init_db()
with Session(cli_engine) as s:
    provision(s, "cli.tester+test@owera.com")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ar.main([])
out = json.loads(buf.getvalue())
ok(rc == 0 and out["ok"] and len(out["workspaces"]) == 1 and out["workspaces"][0]["is_test"],
   "CLI JSON: one test workspace, owera excluded by default")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ar.main(["--include-owera", "--workspace", "1"])
ok(rc == 0 and json.loads(buf.getvalue())["workspaces"][0]["workspace_id"] == 1, "CLI --workspace by id")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ar.main(["--workspace", "nope"])
ok(rc == 2, "CLI unknown workspace → exit 2")

print("A5 llm_blocked (S8 guard refusal) read")
from types import SimpleNamespace as _NS
from unittest.mock import patch as _patch
from app.services import llm_guard as _lg
# Pre-S8 fallback path: force "no blocked_status" so these checks hold whether or
# not S8 (#229) is already merged (with S8 and the release flag OFF, blocked_status
# returns None by design, which is covered below).
with _patch.object(_lg, "blocked_status", None, create=True):
    ok(activation.llm_blocked(_NS()) is None, "no llm_blocked columns (pre-S8) → None")
    ok(activation.llm_blocked(_NS(llm_blocked_reason=None)) is None, "no reason → None")
    _b = activation.llm_blocked(_NS(llm_blocked_reason="not_paid_or_test",
                                    llm_blocked_at=datetime(2026, 11, 14, 3, 0)))
    ok(_b == {"reason": "not_paid_or_test", "step": None, "at": "2026-11-14T03:00:00Z"},
       "raw columns read (reason, at ISO UTC)")
with _patch.object(_lg, "blocked_status", create=True,
                   new=lambda ws: {"reason": "terms_privacy_not_accepted", "step": "terms_privacy",
                                   "message": "Drafts paused: …", "at": "2026-11-14T03:00:00+00:00"}):
    _b = activation.llm_blocked(_NS())
    ok(_b == {"reason": "terms_privacy_not_accepted", "step": "terms_privacy",
              "at": "2026-11-14T03:00:00+00:00"}, "after S8 merge: uses llm_guard.blocked_status")
    with Session(engine) as s:
        _r = activation.report(s, s.get(Workspace, 1), now=now)
    ok(_r["A5"]["done"] is False and _r["A5"]["llm_blocked"]["reason"] == "terms_privacy_not_accepted",
       "A5 not done while a guard refusal is active")
with _patch.object(_lg, "blocked_status", create=True, new=lambda ws: None):
    with Session(engine) as s:
        _r = activation.report(s, s.get(Workspace, 1), now=now)
    ok(_r["A5"]["llm_blocked"] is None, "A5 llm_blocked null when allowed / flag OFF")

print(f"\nALL {_checks} CHECKS PASSED")
