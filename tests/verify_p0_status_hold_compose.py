"""P0 2026-09-29 — review-gate state table, publish hold, compose-script.

Run: PYTHONPATH=. .venv/bin/python tests/verify_p0_status_hold_compose.py

(i) 2026-09-28: #1360 sat QUEUED with an old artifact; ``/retry`` never
checked status and flipped it to APPROVED unreviewed; ``/requeue`` never
cleared ``video_path``. Pins: retry/requeue/reject/approve only from the
allowed states (409 + no JobRun otherwise), requeue/retry clear the render
artifact but keep script / creation_config / provided thumbnail, approve
refuses a missing or stale artifact.
(ii) ``POST /api/videos/{id}/hold|unhold`` on APPROVED: the publish loop never
selects a held row (end-to-end through ``publish_loop.tick``), the craft sweep
never touches it, runway / dashboard / publish-plan / render mix exclude it.
(iii) ``POST /api/videos/{id}/compose-script``: grok (mocked at
``llm.complete``) with effort=medium, script saved as provided, no
daily_render_budget use, no JobRun kind=render, 409 while rendering or outside
draft/queued, and the later render speaks the saved script (no second grok).

In-memory SQLite, FastAPI TestClient, grok / engines / YouTube stubbed —
never the real manager.db, no network. Exits non-zero on the first failure.
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
# Duplicate-episode check is pinned in tests/verify_episode_dup.py; this suite's
# fixtures share one passing "· <Series> N" title across rows, so it is off here.
from app.services import episode_guard as _episode_guard  # noqa: E402
_episode_guard.duplicate_episode_reason = lambda session, video: None
from app.config import settings
from app.db import get_session
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus,
                        utcnow)
from app.routers import queue as queue_router
from app.routers import videos as videos_router
from app.services import craft, issues, publish_loop, quota, render_loop, script_compose
from app.services.engines import worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(videos_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "videos module loaded from this tree")

_OK_TITLE = "Cache miss re-bills the whole receipt · Copilot Credits 1"
_OK_SCRIPT = "It costs $79. Here is why Credits matter."
A1_SUBJECT = "PDF escaneado colado no chat não é engenharia. · IA 205"
A1_SCRIPT = (
    "PDF escaneado colado no chat não é engenharia. Não é. Página escaneada é foto. "
    "Não tem texto dentro. Você copia e vem vazio. Ou vem um OCR torto, com número "
    "trocado. E o modelo responde em cima disso com a mesma confiança. Eu testo antes, "
    "com pdftotext na página. Voltou vazio, a página vai como imagem pro modelo de "
    "visão. Voltou texto limpo, vai texto. Primeiro você descobre o que o arquivo é. "
    "Subscribe — next IA trap."
)
ART = Path(tempfile.mkdtemp(prefix="p0-artifacts-"))


def artifact(name: str) -> str:
    f = ART / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"fake mp4")
    return str(f)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")

with Session(engine) as s:
    s.add(Channel(slug="rr", name="RR", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5, daily_publish_budget=5))
    s.commit()
    s.add(Topic(channel_id=1, name="shorts", content_format="short"))
    s.add(Topic(channel_id=1, name="longs", content_format="long"))
    s.commit()


def mk(status, **kw) -> int:
    with Session(engine) as s:
        v = Video(channel_id=1, topic_id=kw.pop("topic_id", 1),
                  subject=kw.pop("subject", "subject"), status=status, **kw)
        s.add(v)
        s.commit()
        return v.id


def get(vid) -> Video:
    with Session(engine) as s:
        return s.get(Video, vid)


def runs(kind=None, vid=None):
    with Session(engine) as s:
        q = select(JobRun)
        if kind:
            q = q.where(JobRun.kind == kind)
        if vid:
            q = q.where(JobRun.video_id == vid)
        return s.exec(q).all()


ALL = [VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING, VideoStatus.RENDERED,
       VideoStatus.REVIEW, VideoStatus.APPROVED, VideoStatus.PUBLISHING,
       VideoStatus.PUBLISHED, VideoStatus.FAILED, VideoStatus.REJECTED]

# ---------------------------------------------------------------------------
print("(i) state table: forbidden transitions are 409 and write nothing")
ok(videos_router.RETRY_FROM == {VideoStatus.FAILED}, "retry only from failed")
ok(videos_router.REQUEUE_FROM == {VideoStatus.RENDERED, VideoStatus.REVIEW,
                                  VideoStatus.APPROVED, VideoStatus.FAILED,
                                  VideoStatus.REJECTED}, "requeue table")
ok(videos_router.REJECT_FROM == {VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERED,
                                 VideoStatus.REVIEW, VideoStatus.APPROVED,
                                 VideoStatus.FAILED}, "reject table")
ok(videos_router.APPROVE_FROM == {VideoStatus.RENDERED, VideoStatus.REVIEW}, "approve table")
table = {"retry": videos_router.RETRY_FROM, "requeue": videos_router.REQUEUE_FROM,
         "reject": videos_router.REJECT_FROM, "approve": videos_router.APPROVE_FROM}
for action, allowed in table.items():
    for st in ALL:
        if st in allowed:
            continue
        vid = mk(st, video_path=artifact(f"forbid/{action}-{st}.mp4"),
                 title=_OK_TITLE, script=_OK_SCRIPT, mpt_task_id="h-keep")
        before = len(runs())
        kw = {"json": {"reason": "x"}} if action == "reject" else {}
        r = client.post(f"/api/videos/{vid}/{action}", auth=auth, **kw)
        v = get(vid)
        ok(r.status_code == 409 and v.status == st and v.video_path and v.mpt_task_id == "h-keep"
           and len(runs()) == before,
           f"{action} from {st}: 409, row untouched, no JobRun")

print("(i) #1360 regression: queued row with a stale artifact")
v1360 = mk(VideoStatus.QUEUED, video_path=artifact("1360/video.mp4"),
           title=_OK_TITLE, script=_OK_SCRIPT)
r = client.post(f"/api/videos/{v1360}/retry", auth=auth)
ok(r.status_code == 409 and get(v1360).status == VideoStatus.QUEUED,
   "retry on QUEUED+artifact is 409 (was: flipped to approved unreviewed)")
r = client.post(f"/api/videos/{v1360}/approve", auth=auth)
ok(r.status_code == 409, "approve on QUEUED is 409")

print("(i) requeue clears the render artifact, keeps provided craft")
cc = {"script_source": "provided", "beats": ["hook"], "thumb_provided": "thumb_provided.png"}
for st in sorted(videos_router.REQUEUE_FROM):
    vid = mk(st, video_path=artifact(f"rq/{st}/video.mp4"),
             thumb_path=str(ART / f"rq/{st}/thumb.jpg"), mpt_task_id="h1",
             render_progress=100, error="old", craft_review=craft.CRAFT_REVIEW_PASS,
             approved_at=utcnow(), title=_OK_TITLE, script=A1_SCRIPT,
             creation_config=json.dumps(cc), retry_count=2,
             held=(st == VideoStatus.APPROVED), held_at=utcnow() if st == VideoStatus.APPROVED else None)
    r = client.post(f"/api/videos/{vid}/requeue", auth=auth)
    v = get(vid)
    ok(r.status_code == 200 and v.status == VideoStatus.QUEUED, f"requeue from {st} → queued")
    ok(v.video_path is None and v.thumb_path is None and v.mpt_task_id is None
       and v.render_progress == 0 and v.error is None and v.approved_at is None
       and v.craft_review == craft.CRAFT_REVIEW_PENDING and v.retry_count == 0
       and not v.held and v.held_at is None,
       f"requeue from {st}: video_path/thumb/handle/progress/approval/craft/hold cleared")
    ok(v.script == A1_SCRIPT and json.loads(v.creation_config) == cc and v.title == _OK_TITLE,
       f"requeue from {st}: script / creation_config / title kept")
vprov = mk(VideoStatus.REVIEW, video_path=artifact("prov/video.mp4"),
           thumb_path=str(ART / "prov/thumb_provided.png"))
client.post(f"/api/videos/{vprov}/requeue", auth=auth)
ok(get(vprov).thumb_path == str(ART / "prov/thumb_provided.png") and get(vprov).video_path is None,
   "an operator-provided thumbnail (not thumb.jpg/thumb_custom.png) survives requeue")
from app.services import provided_thumb  # noqa: E402  (#39, on main)
ok(provided_thumb.is_provided(get(vprov)),
   "provided_thumb.is_provided still true after requeue (#39 marker = file name kept)")
rq = runs("requeue", vprov)
ok(len(rq) == 1 and "artifact cleared" in rq[0].detail, "requeue JobRun says artifact cleared")

print("(i) retry: publish failure re-publishes; render failure re-renders + clears")
vpub = mk(VideoStatus.FAILED, video_path=artifact("pub/video.mp4"), title=_OK_TITLE,
          script=_OK_SCRIPT, error="upload failed: 500", retry_count=3)
with Session(engine) as s:
    quota.log(s, kind="render", status="success", video_id=vpub, channel_id=1)
    quota.log(s, kind="publish", status="error", video_id=vpub, channel_id=1, detail="upload failed")
    s.commit()
r = client.post(f"/api/videos/{vpub}/retry", auth=auth)
v = get(vpub)
ok(r.status_code == 200 and v.status == VideoStatus.APPROVED and v.video_path
   and v.retry_count == 0, "failed after publish error + artifact on disk → approved, artifact kept")
vblank = mk(VideoStatus.FAILED, video_path=artifact("blank/video.mp4"),
            thumb_path=str(ART / "blank/thumb.jpg"), title=_OK_TITLE, script=_OK_SCRIPT,
            error="post-render frames blank at finalize — not publishing")
with Session(engine) as s:
    quota.log(s, kind="render", status="error", video_id=vblank, channel_id=1,
              detail="post-render frames blank at finalize — not publishing")
    s.commit()
r = client.post(f"/api/videos/{vblank}/retry", auth=auth)
v = get(vblank)
ok(r.status_code == 200 and v.status == VideoStatus.QUEUED and v.video_path is None
   and v.thumb_path is None,
   "failed at render (blank frames) with a file → queued re-render, artifact cleared (was: approved)")
vgone = mk(VideoStatus.FAILED, video_path=str(ART / "gone/video.mp4"), error="upload failed: x",
           title=_OK_TITLE, script=_OK_SCRIPT)
r = client.post(f"/api/videos/{vgone}/retry", auth=auth)
ok(r.status_code == 200 and get(vgone).status == VideoStatus.QUEUED and get(vgone).video_path is None,
   "publish failure whose file vanished → re-render, not approved")

print("(i) approve needs a fresh artifact")
vnoart = mk(VideoStatus.REVIEW, title=_OK_TITLE, script=_OK_SCRIPT)
r = client.post(f"/api/videos/{vnoart}/approve", auth=auth)
ok(r.status_code == 409 and "artifact" in r.json()["detail"]
   and get(vnoart).status == VideoStatus.REVIEW and not runs("approve", vnoart),
   "review without video_path: 409, no JobRun")
vmiss = mk(VideoStatus.REVIEW, title=_OK_TITLE, script=_OK_SCRIPT,
           video_path=str(ART / "missing/video.mp4"))
r = client.post(f"/api/videos/{vmiss}/approve", auth=auth)
ok(r.status_code == 409 and "missing" in r.json()["detail"], "video_path not on disk: 409")
vstale = mk(VideoStatus.REVIEW, title=_OK_TITLE, script=_OK_SCRIPT,
            video_path=artifact("stale/video.mp4"))
with Session(engine) as s:
    s.add(JobRun(kind="render", status="success", video_id=vstale, channel_id=1,
                 created_at=utcnow() - timedelta(hours=2)))
    s.add(JobRun(kind="requeue", status="success", video_id=vstale, channel_id=1,
                 detail="requeued via API: approved -> queued (re-render)",
                 created_at=utcnow() - timedelta(hours=1)))
    s.commit()
r = client.post(f"/api/videos/{vstale}/approve", auth=auth)
ok(r.status_code == 409 and "predates" in r.json()["detail"],
   "legacy row: requeue logged after the last render success → 409 (stale artifact)")
with Session(engine) as s:
    quota.log(s, kind="render", status="success", video_id=vstale, channel_id=1)
    s.commit()
r = client.post(f"/api/videos/{vstale}/approve", auth=auth)
ok(r.status_code == 200 and get(vstale).status == VideoStatus.APPROVED,
   "after a new successful render the same row approves")

# ---------------------------------------------------------------------------
print("(ii) hold / unhold")
for st in ALL:
    if st == VideoStatus.APPROVED:
        continue
    vid = mk(st)
    ok(client.post(f"/api/videos/{vid}/hold", auth=auth).status_code == 409
       and client.post(f"/api/videos/{vid}/unhold", auth=auth).status_code == 409
       and not get(vid).held, f"hold/unhold from {st}: 409")
ok(client.post("/api/videos/99999/hold", auth=auth).status_code == 404, "hold missing video 404")
ok(client.post(f"/api/videos/{vpub}/hold").status_code == 401, "hold needs the same auth as other routes")

# Fresh channel so selection is not polluted by the rows above.
with Session(engine) as s:
    s.add(Channel(slug="os", name="OS", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=5, daily_publish_budget=5))
    s.commit()
    ch2 = s.exec(select(Channel).where(Channel.slug == "os")).one().id
    s.add(Topic(channel_id=ch2, name="s2", content_format="short"))
    s.add(Topic(channel_id=ch2, name="l2", content_format="long"))
    s.commit()
    t_s2 = s.exec(select(Topic).where(Topic.name == "s2")).one().id
    t_l2 = s.exec(select(Topic).where(Topic.name == "l2")).one().id
    # one prior publish so runway signals are "operating"
    s.add(Video(channel_id=ch2, topic_id=t_s2, subject="old", status=VideoStatus.PUBLISHED))
    s.commit()


def mk2(status, topic, **kw) -> int:
    with Session(engine) as s:
        v = Video(channel_id=ch2, topic_id=topic, subject=kw.pop("subject", "s"),
                  status=status, **kw)
        s.add(v)
        s.commit()
        return v.id


va = mk2(VideoStatus.APPROVED, t_s2, craft_review=craft.CRAFT_REVIEW_PASS, title=_OK_TITLE,
         script=_OK_SCRIPT, video_path=artifact("h/a.mp4"), approved_at=utcnow())
vl = mk2(VideoStatus.APPROVED, t_l2, craft_review=craft.CRAFT_REVIEW_PASS, title=_OK_TITLE,
         script=_OK_SCRIPT, video_path=artifact("h/l.mp4"), approved_at=utcnow())
vp = mk2(VideoStatus.APPROVED, t_s2, craft_review=craft.CRAFT_REVIEW_PENDING, title=_OK_TITLE,
         script="", video_path=artifact("h/p.mp4"), approved_at=utcnow())

r = client.post(f"/api/videos/{va}/hold", auth=auth)
ok(r.status_code == 200 and r.json()["held"] is True and r.json()["status"] == "approved"
   and r.json()["craft_review"] == "pass", "hold approved → held, status/craft unchanged")
ok(client.post(f"/api/videos/{va}/hold", auth=auth).status_code == 409, "double hold 409")
for vid in (vl, vp):
    client.post(f"/api/videos/{vid}/hold", auth=auth)
ok(len(runs("hold")) == 3, "each hold writes one JobRun kind=hold")

with Session(engine) as s:
    ok(publish_loop._next_approved(s, ch2) is None, "_next_approved never returns a held row")
    publish_loop._sweep_craft_reviews(s, ch2)
vpp = get(vp)
ok(vpp.status == VideoStatus.APPROVED and vpp.held and vpp.craft_review == craft.CRAFT_REVIEW_PENDING,
   "craft sweep skips held rows: not re-scored, not auto-rejected, hold intact")

published = []


@contextlib.contextmanager
def _scope():
    s = Session(engine)
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _fake_publish(session, channel, video):
    published.append(video.id)
    video.status = VideoStatus.PUBLISHED
    session.add(video)


with patch.object(publish_loop, "session_scope", _scope), \
        patch.object(publish_loop, "_publish_one", _fake_publish):
    for _ in range(3):
        publish_loop.tick()
ok(published == [], "publish_loop.tick over 3 ticks publishes nothing while every row is held")
ok(get(va).held and get(vl).held and get(vp).held, "ticks (sweep + selection) never undo the hold")

print("(ii) runway / counts exclude held")
with Session(engine) as s:
    ch = s.get(Channel, ch2)
    sig = issues.publish_signals(s, ch)
ok(sig["approved"] == 0 and sig["held"] == 3 and sig["runway"] == sig["published_today"],
   f"publish_signals: approved excludes held, held surfaced ({sig['approved']}/{sig['held']})")
r = client.get(f"/api/videos/publish-plan?channel_id={ch2}", auth=auth)
ok(r.status_code == 200 and r.json() == {}, "publish-plan has no ETA for held rows")
r = client.get("/api/dashboard", auth=auth)
row = [x for x in r.json() if x["channel"]["id"] == ch2][0]
ok(row["held"] == 3 and row["next_publish_eta"] is None and row["publish_hold"] is None,
   "dashboard: held count, no publish ETA from held rows")
with Session(engine) as s:
    q_long = mk2(VideoStatus.QUEUED, t_l2)
    q_short = mk2(VideoStatus.QUEUED, t_s2)
    order = [v.id for v in render_loop._queued_candidates(s) if v.channel_id == ch2]
ok(order[:1] == [q_long], "render mix: a held approved long is not 'banked' (queued long first)")
with Session(engine) as s:
    d = issues.detect(s)
ps = [x for x in d.get("pipeline_starved", []) if x.get("channel_id") == ch2]
inv = [x for x in d.get("board_inventory", []) if x.get("channel_id") == ch2]
ok(len(inv) == 1 and inv[0]["by_format"]["approved"] == {"long": 0, "short": 0},
   "issues board_inventory approved-by-format excludes held")
ok(not ps or ps[0].get("approved") == 0, "issues publish_starved counts no held stock")

print("(ii) unhold resumes the drip without re-render")
r = client.post(f"/api/videos/{va}/unhold", auth=auth)
ok(r.status_code == 200 and r.json()["held"] is False and r.json()["video_path"],
   "unhold → back in drip, same artifact")
ok(client.post(f"/api/videos/{va}/unhold", auth=auth).status_code == 409, "unhold of non-held 409")
with patch.object(publish_loop, "session_scope", _scope), \
        patch.object(publish_loop, "_publish_one", _fake_publish):
    publish_loop.tick()
ok(published == [va], "after unhold the next tick publishes exactly that row")
ok(get(vl).held and get(vp).held, "other held rows still held")
r = client.post(f"/api/videos/{vl}/reject", auth=auth, json={"reason": "no"})
ok(r.status_code == 200 and not get(vl).held, "reject of a held row clears the hold flag")
with Session(engine) as s:
    for q in (q_long, q_short):
        s.delete(s.get(Video, q))
    s.commit()

# ---------------------------------------------------------------------------
print("(iii) compose-script: grok mocked, effort medium, no render budget")
calls = []


def fake_complete(prompt, system=None, max_tokens=None, *, timeout=None, model=None,
                  reasoning_effort=None):
    calls.append({"effort": reasoning_effort, "timeout": timeout, "prompt": prompt})
    return A1_SCRIPT


def boom_llm(*a, **k):
    raise AssertionError("render-path _llm must not be used by compose-script")


with Session(engine) as s:
    s.add(Channel(slug="rr-compose", name="Recio Radar", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=3, daily_publish_budget=3))
    s.commit()
    ch3 = s.exec(select(Channel).where(Channel.slug == "rr-compose")).one().id
    s.add(Topic(channel_id=ch3, name="IA", content_format="short"))
    s.commit()
    t3 = s.exec(select(Topic).where(Topic.channel_id == ch3)).one().id


def mk3(status, **kw) -> int:
    with Session(engine) as s:
        v = Video(channel_id=ch3, topic_id=t3, subject=kw.pop("subject", A1_SUBJECT),
                  status=status, **kw)
        s.add(v)
        s.commit()
        return v.id


ok(settings.grok_compose_reasoning_effort == "medium", "compose effort pin default is medium")
vd = mk3(VideoStatus.DRAFT)
with patch("app.services.llm.complete", side_effect=fake_complete), \
        patch.object(worker, "_llm", side_effect=boom_llm):
    r = client.post(f"/api/videos/{vd}/compose-script?wait=true", auth=auth)
ok(r.status_code == 200, f"compose-script (wait) returns the video: {r.status_code} {r.text[:200]}")
ok(len(calls) == 1 and calls[0]["effort"] == "medium" and calls[0]["timeout"] is None,
   "one grok call, reasoning_effort=medium, default (existing) timeout")
v = get(vd)
cc3 = json.loads(v.creation_config)
ok(v.status == VideoStatus.DRAFT and v.script and "PDF escaneado" in v.script,
   "script stored on the draft; status unchanged")
ok(cc3.get("script_source") == craft.SCRIPT_SOURCE_PROVIDED and cc3.get("script_composed", {}).get("effort") == "medium",
   "stored as provided script (spoken verbatim at render) + compose marker")
ok(not runs("render", vd), "no JobRun kind=render for compose")
kinds = [(j.kind, j.status) for j in runs(vid=vd)]
ok(kinds == [("compose_script", "started"), ("compose_script", "success")],
   f"JobRun kind=compose_script started+success ({kinds})")
with Session(engine) as s:
    ok(quota.rendered_today(s, ch3) == 0 and quota.in_flight_renders(s, ch3) == 0
       and s.get(Channel, ch3).daily_render_budget == 3,
       "daily_render_budget untouched: rendered_today 0, in-flight 0, budget 3")
saved = v.script

print("(iii) compose-script refusals")
with patch("app.services.llm.complete", side_effect=fake_complete):
    n0 = len(calls)
    r = client.post(f"/api/videos/{vd}/compose-script?wait=true", auth=auth)
    ok(r.status_code == 409 and "saved script" in r.json()["detail"] and get(vd).script == saved,
       "existing saved script: 409 (no overwrite)")
    for st in (VideoStatus.RENDERING, VideoStatus.RENDERED, VideoStatus.REVIEW,
               VideoStatus.APPROVED, VideoStatus.PUBLISHED, VideoStatus.FAILED,
               VideoStatus.REJECTED, VideoStatus.PUBLISHING):
        vid = mk3(st)
        r = client.post(f"/api/videos/{vid}/compose-script?wait=true", auth=auth)
        ok(r.status_code == 409, f"compose from {st}: 409")
    vq_inflight = mk3(VideoStatus.QUEUED, mpt_task_id="h-live")
    r = client.post(f"/api/videos/{vq_inflight}/compose-script?wait=true", auth=auth)
    ok(r.status_code == 409 and "render in progress" in r.json()["detail"],
       "render handle present: 409 render in progress")
    vlock = mk3(VideoStatus.DRAFT)
    script_compose.claim(vlock)
    r = client.post(f"/api/videos/{vlock}/compose-script?wait=true", auth=auth)
    ok(r.status_code == 409 and "already running" in r.json()["detail"], "concurrent compose: 409")
    script_compose.release(vlock)
    ok(len(calls) == n0, "no grok call on any refused compose")
    r = client.post(f"/api/videos/{vd}/compose-script?wait=true&force=true", auth=auth)
    ok(r.status_code == 200 and len(calls) == n0 + 1, "force=true replaces the saved script")

print("(iii) grok failure")


def fail_complete(*a, **k):
    from app.services.llm import GrokCLIError
    raise GrokCLIError("grok.Timeout: grok -p timed out after 600s.")


vf = mk3(VideoStatus.QUEUED)
with patch("app.services.llm.complete", side_effect=fail_complete):
    r = client.post(f"/api/videos/{vf}/compose-script?wait=true", auth=auth)
ok(r.status_code == 502 and get(vf).script is None and get(vf).status == VideoStatus.QUEUED,
   "grok timeout → 502, no script written, status unchanged")
ok([j.status for j in runs("compose_script", vf)] == ["started", "error"],
   "failure logged as compose_script error")
ok(not script_compose.is_composing(vf), "claim released after failure")

print("(iii) async mode")
vasync = mk3(VideoStatus.QUEUED)
# Deterministic: this harness shares ONE SQLite connection (StaticPool) across
# threads. If the background thread starts while the request's session is
# still being torn down, that session's close() ROLLBACK lands on the shared
# connection and discards the thread's uncommitted JobRun/script writes (the
# old sleep-poll flaked: ['success'] only, or ['started'] only). Production
# (file DB, one pooled connection per session) has no such coupling. So:
# capture the background start, let the request finish, then run the REAL
# start_background and join its thread — awaiting the task, not sleeping.
_bg_calls = []
_real_start_background = script_compose.start_background


def _defer_start(bind, video_id, *, force=False):
    _bg_calls.append((bind, video_id, force))


with patch("app.services.llm.complete", side_effect=fake_complete):
    with patch.object(script_compose, "start_background", side_effect=_defer_start):
        r = client.post(f"/api/videos/{vasync}/compose-script", auth=auth)
    ok(r.status_code == 202 and r.json()["status"] == "composing", "default async: 202 composing")
    ok(len(_bg_calls) == 1 and _bg_calls[0][1] == vasync and script_compose.is_composing(vasync),
       "async: background compose handed off; claim held until the thread releases it")
    _bind, _vid, _force = _bg_calls[0]
    _t = _real_start_background(_bind, _vid, force=_force)
    _t.join(timeout=60)
    ok(not _t.is_alive() and not script_compose.is_composing(vasync),
       "background compose thread finished and released the claim")
ok(get(vasync).script and "PDF escaneado" in get(vasync).script
   and [j.status for j in runs("compose_script", vasync)] == ["started", "success"],
   "background compose stored the script and logged success")

print("(iii) render skips a composing row and later speaks the saved script")


class FakeEngine:
    def __init__(self):
        self.params = []

    def submit(self, video, params):
        self.params.append(dict(params))
        return f"h{video.id}"


eng = FakeEngine()
with Session(engine) as s:
    for c in s.exec(select(Channel).where(Channel.id != ch3)).all():
        c.paused = True
        s.add(c)
    # Global in-flight renders gate _submit_new: clear the state-table fixtures
    # so the checks below are not vacuous, and allow 2 concurrent renders.
    for o in s.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all():
        o.status = VideoStatus.FAILED
        s.add(o)
    for other in (vf, vasync, vq_inflight):
        o = s.get(Video, other)
        o.status = VideoStatus.DRAFT
        s.add(o)
    from app.db import app_settings
    app_settings(s).render_concurrency = 2
    s.commit()
client.post(f"/api/videos/{vd}/produce", auth=auth)
vother = mk3(VideoStatus.QUEUED, subject="Second queued idea · IA 206")
saved = get(vd).script
script_compose.claim(vd)
with patch.object(render_loop, "get_engine", lambda name: eng), \
        patch.object(render_loop, "resolve_engine", lambda *a: "hyperframes"), \
        patch.object(render_loop, "ensure_topic_playlist", lambda *a: None), \
        patch.object(worker, "_generate_script", return_value="generated"):
    with Session(engine) as s:
        render_loop._submit_new(s)
        s.commit()
ok(get(vd).status == VideoStatus.QUEUED and get(vother).status == VideoStatus.RENDERING
   and len(eng.params) == 1,
   "a row being composed is skipped by _submit_new (the other queued row renders)")
script_compose.release(vd)
with patch.object(render_loop, "get_engine", lambda name: eng), \
        patch.object(render_loop, "resolve_engine", lambda *a: "hyperframes"), \
        patch.object(render_loop, "ensure_topic_playlist", lambda *a: None), \
        patch.object(worker, "_generate_script", side_effect=AssertionError("regenerated")), \
        patch("app.services.llm.complete", side_effect=AssertionError("grok at render")):
    with Session(engine) as s:
        render_loop._submit_new(s)
        s.commit()
v = get(vd)
ok(v.status == VideoStatus.RENDERING and len(eng.params) == 2,
   "render submitted after compose released (no grok, no _generate_script)")
sent = eng.params[-1]
ok(sent.get("provided_script") and sent["provided_script"].startswith(saved.rsplit(" Subscribe", 1)[0][:40]),
   "render params carry the saved (composed) script as provided_script")
ok(sent.get("video_script") == sent.get("provided_script") == v.script,
   "spoken text == stored script (no re-generation, no overwrite)")
ok(len([j for j in runs("render", vd) if j.status == "started"]) == 1,
   "only the real render writes kind=render")

print("migration: _add_missing_columns adds held/held_at to a pre-P0 video table")
import app.db as app_db
from sqlalchemy import text as _sql
_mig = Path(tempfile.mkdtemp(prefix="p0-mig-")) / "old.db"
_old = create_engine(f"sqlite:///{_mig}")
with _old.begin() as c:
    c.execute(_sql("CREATE TABLE video (id INTEGER PRIMARY KEY, channel_id INTEGER, "
                   "topic_id INTEGER, subject VARCHAR, status VARCHAR)"))
    c.execute(_sql("INSERT INTO video (channel_id, topic_id, subject, status) "
                   "VALUES (1, 1, 'legacy', 'approved')"))
    for t in ("renderprofile", "videometric", "channel", "topic", "settings"):
        c.execute(_sql(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)"))
with patch.object(app_db, "engine", _old):
    app_db._add_missing_columns()
    app_db._add_missing_columns()          # idempotent
with _old.begin() as c:
    cols = {r[1] for r in c.execute(_sql("PRAGMA table_info(video)"))}
    row = c.execute(_sql("SELECT held, held_at FROM video")).one()
ok({"held", "held_at"} <= cols, "held + held_at columns added (idempotent)")
ok(row[0] == 0 and row[1] is None, "legacy approved row migrates as not held")
with Session(_old) as s2:
    ok(s2.exec(select(Video.id).where(Video.held.is_not(True))).all() == [1],
       "held IS NOT 1 filter keeps legacy rows publishable")

main.app.dependency_overrides.clear()
settings.app_password = _orig_pw
print(f"all {_checks} checks passed")
