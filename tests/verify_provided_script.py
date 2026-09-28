"""Regression checks for provided scripts spoken verbatim (CoS item 8).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_provided_script.py

Before: ``worker.run_job`` always called ``_generate_script(subject)`` (grok -p),
``engine.submit`` only saw ``video.subject``, and ``_finalize`` did
``video.script = task.get("script") or video.script``. ``POST /api/videos``
took topic/subject/queue only, and PATCH …/craft 409s on drafts — so a
CMO-authored script could not reach the VO.

Pins:
- POST /api/videos accepts ``script`` (+ ``title``) and marks
  ``creation_config.script_source="provided"``; blank → 400, null → generate
- PATCH /api/videos/{id}/script sets / clears on draft|queued|failed-no-artifact;
  409 on review/approved/rendering/published; /craft + wide PATCH rules unchanged
- _submit_new passes the provided text to the engine; worker.run_job speaks it
  and never calls _generate_script (generated videos still do)
- series endcard ``Subscribe — next {series} {noun}.`` appended when missing,
  never duplicated / rewritten when present
- misaligned first spoken line fails the craft gate with a clear reason
  (pre-render FAILED, no slot / playlist / engine; publish gate too)
- _finalize never clobbers the provided script with an empty/None task script

In-memory SQLite, FastAPI TestClient, all I/O stubbed. Exits non-zero on the
first failed assertion.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import app_settings, get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.schemas import VideoCreate, VideoScriptSet, VideoUpdate
from app.services import craft, render_loop
from app.services.engines import STATE_COMPLETE, worker
from app.services.engines import mpt as mpt_engine_mod

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# One row of the CMO drafts.json (RR · IA hook double-down, A1), verbatim.
A1_SUBJECT = "PDF escaneado colado no chat não é engenharia. · IA 205"
A1_SCRIPT = (
    "PDF escaneado colado no chat não é engenharia. Não é. Página escaneada é foto. "
    "Não tem texto dentro. Você copia e vem vazio. Ou vem um OCR torto, com número "
    "trocado. E o modelo responde em cima disso com a mesma confiança. Eu testo antes, "
    "com pdftotext na página. Voltou vazio, a página vai como imagem pro modelo de "
    "visão. Voltou texto limpo, vai texto. Primeiro você descobre o que o arquivo é. "
    "Subscribe — next IA trap."
)
A1_NO_ENDCARD = A1_SCRIPT.rsplit(" Subscribe", 1)[0]
MISALIGNED = ("Hoje vamos falar de produtividade. Não é. Página escaneada é foto. "
              "Subscribe — next IA trap.")

# ---------------------------------------------------------------------------
print("schema surface")
ok("script" in VideoCreate.model_fields and "title" in VideoCreate.model_fields,
   "VideoCreate carries optional script + title")
ok(VideoCreate.model_fields["script"].default is None,
   "VideoCreate.script defaults to None (generate as before)")
ok("script" in VideoScriptSet.model_fields, "VideoScriptSet carries script")
ok("script" not in VideoUpdate.model_fields and "creation_config" not in VideoUpdate.model_fields,
   "VideoUpdate still omits script/creation_config (wide PATCH unchanged)")

# ---------------------------------------------------------------------------
print("craft helpers: endcard pin + hook alignment")
txt, edits = craft.prepare_provided_script(A1_SCRIPT, A1_SUBJECT, brand="rr")
ok(txt == A1_SCRIPT and edits == [],
   "script already ending on the series endcard is kept verbatim (no edits)")
ok(txt.count("Subscribe") == 1, "existing endcard not duplicated")
txt2, edits2 = craft.prepare_provided_script(A1_NO_ENDCARD, A1_SUBJECT, brand="rr")
ok(txt2 == A1_NO_ENDCARD + " Subscribe — next IA trap.",
   "missing endcard appended as 'Subscribe — next IA trap.' (series from · IA nn)")
ok(edits2 == ["endcard_appended"], "edit recorded as endcard_appended")
txt3, edits3 = craft.prepare_provided_script(txt2, A1_SUBJECT, brand="rr")
ok(txt3 == txt2 and edits3 == [], "prepare is idempotent (no second endcard)")
ok(txt2.startswith(A1_NO_ENDCARD), "body before the endcard is untouched")
custom = A1_NO_ENDCARD + " Subscribe — next IA receipt."
ok(craft.prepare_provided_script(custom, A1_SUBJECT, brand="rr")[0] == custom,
   "a present endcard with another allowed noun is kept as written (not rewritten)")
mid = ("PDF escaneado colado no chat não é engenharia. Subscribe to the channel now. "
       "Voltou vazio, a página vai como imagem.")
mtxt, medits = craft.prepare_provided_script(mid, A1_SUBJECT, brand="rr")
ok("Subscribe to the channel" not in mtxt and mtxt.endswith("Subscribe — next IA trap.")
   and "mid_subscribe_stripped" in medits,
   "miolo Subscribe dropped (same rule as the generated path), closer appended")
long_txt, long_edits = craft.prepare_provided_script(A1_NO_ENDCARD, A1_SUBJECT,
                                                     content_format="long")
ok(long_txt == A1_NO_ENDCARD and long_edits == [],
   "long-form: no endcard appended (matches _generate_script)")
ok(craft.provided_script_hook_reason(A1_SCRIPT, title=A1_SUBJECT) is None,
   "A1 first line == title head → aligned")
ok(craft.provided_script_hook_reason(A1_SCRIPT, subject=A1_SUBJECT) is None,
   "no title yet → subject head is the hook (aligned)")
r = craft.provided_script_hook_reason(MISALIGNED, title=A1_SUBJECT)
ok(r and r.startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
   "misaligned first line → PROVIDED_SCRIPT_HOOK_REASON")
ok(r and "PDF escaneado" in r and "Hoje vamos" in r and "never regenerated" in r,
   "reason names hook vs spoken and says it is not regenerated")
r = craft.provided_script_hook_reason("It costs seventy-nine dollars. More.",
                                      title="Copilot Credits burned $79 · Copilot Credits 12")
ok(r is not None, "$N stake in the title spelled out on the spoken line → fail")

# ---------------------------------------------------------------------------
print("publish craft gate: provided-script hook check")
cc_prov = json.dumps({"script_source": "provided"})
reason = craft.publish_craft_block_reason(
    title=A1_SUBJECT, script=MISALIGNED, creation_config=cc_prov,
    content_format="short", video_path=None)
ok(reason and reason.startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
   "misaligned provided script blocks publish with the hook reason")
ok(craft.publish_craft_block_reason(
    title=A1_SUBJECT, script=A1_SCRIPT, creation_config=cc_prov,
    content_format="short", video_path=None) is None,
   "aligned provided script clears the gate (title + A/B/C unchanged)")
ok(craft.publish_craft_block_reason(
    title=A1_SUBJECT, script=MISALIGNED,
    creation_config=json.dumps({"script_source": "generated"}),
    content_format="short", video_path=None) is None,
   "generated scripts are not subject to the new check (no behavior change)")
ok(craft.publish_craft_block_reason(
    title="no series here", script=A1_SCRIPT, creation_config=cc_prov,
    content_format="short", video_path=None) == craft.TITLE_GATE_REASON,
   "title gate still runs first for provided scripts (not weakened)")
status, why = craft.evaluate_craft_review(
    title=A1_SUBJECT, script=MISALIGNED, creation_config=cc_prov,
    content_format="short")
ok(status == craft.CRAFT_REVIEW_FAIL and why.startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
   "durable craft_review evaluates to fail with the reason")

# ---------------------------------------------------------------------------
print("API: POST /api/videos with script, PATCH /api/videos/{id}/script")
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="rr", name="RR", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="IA", content_format="short"))
    s.commit()
    for st, extra in ((VideoStatus.REVIEW, {"video_path": "/tmp/r.mp4"}),
                      (VideoStatus.APPROVED, {"video_path": "/tmp/a.mp4"}),
                      (VideoStatus.RENDERING, {"mpt_task_id": "h"}),
                      (VideoStatus.PUBLISHED, {"video_path": "/tmp/p.mp4", "yt_video_id": "yt"}),
                      (VideoStatus.FAILED, {"video_path": "/tmp/f.mp4"}),
                      (VideoStatus.FAILED, {})):
        s.add(Video(channel_id=1, topic_id=1, subject=f"seed {st}", status=st,
                    script="seed script", **extra))
    s.commit()
SEED = {"review": 1, "approved": 2, "rendering": 3, "published": 4,
        "failed_art": 5, "failed_noart": 6}


def _override_session():
    with Session(engine) as s:
        yield s


def row(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


def jobruns(kind):
    with Session(engine) as s:
        return list(s.exec(select(JobRun).where(JobRun.kind == kind)).all())


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")
try:
    r = client.post("/api/videos", auth=auth, json={
        "topic_id": 1, "subject": A1_SUBJECT, "title": A1_SUBJECT, "script": A1_SCRIPT})
    ok(r.status_code == 201, f"POST with script → 201 (got {r.status_code})")
    vid = r.json()["id"]
    v = row(vid)
    ok(v.script == A1_SCRIPT, "script persisted verbatim")
    ok(v.title == A1_SUBJECT, "title persisted")
    ok(v.status == VideoStatus.DRAFT, "queue omitted → draft")
    ok(json.loads(v.creation_config) == {"script_source": "provided"},
       "creation_config.script_source = provided")
    ok(v.error is None, "aligned hook → no error surfaced")
    ok(len(jobruns("script_set")) == 1 and jobruns("script_set")[0].video_id == vid,
       "create logs one script_set JobRun with the video id")

    r = client.post("/api/videos", auth=auth, json={"topic_id": 1, "subject": "plain idea"})
    v_plain = row(r.json()["id"])
    ok(r.status_code == 201 and v_plain.script is None and v_plain.creation_config is None,
       "POST without script unchanged (no script, no marker)")
    r = client.post("/api/videos", auth=auth, json={"topic_id": 1, "subject": "x", "script": None})
    ok(r.status_code == 201 and row(r.json()["id"]).script is None,
       "script: null behaves like omitted")
    r = client.post("/api/videos", auth=auth, json={"topic_id": 1, "subject": "x", "script": "   "})
    ok(r.status_code == 400, "blank script → 400")
    r = client.post("/api/videos", auth=auth, json={"topic_id": 1, "subject": "x", "title": " "})
    ok(r.status_code == 400, "blank title → 400")
    r = client.post("/api/videos", auth=auth, json={
        "topic_id": 1, "subject": A1_SUBJECT, "script": MISALIGNED, "queue": True})
    v_mis = row(r.json()["id"])
    ok(r.status_code == 201 and v_mis.status == VideoStatus.QUEUED,
       "misaligned script still creates (queue=true → queued); gate decides later")
    ok((v_mis.error or "").startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
       "misaligned hook surfaced on error at create time")

    # PATCH …/script on the draft
    r = client.patch(f"/api/videos/{v_plain.id}/script", auth=auth,
                     json={"script": "  " + A1_NO_ENDCARD + "  "})
    ok(r.status_code == 200, "PATCH script on draft → 200")
    v = row(v_plain.id)
    ok(v.script == A1_NO_ENDCARD and v.status == VideoStatus.DRAFT,
       "draft script set (stripped), status unchanged")
    ok(craft.is_provided_script(v.creation_config), "draft marked script_source=provided")
    r = client.patch(f"/api/videos/{v_mis.id}/script", auth=auth, json={"script": A1_SCRIPT})
    v = row(v_mis.id)
    ok(r.status_code == 200 and v.error is None and v.status == VideoStatus.QUEUED,
       "queued (not started) script fixed → error cleared, still queued")
    r = client.patch(f"/api/videos/{SEED['failed_noart']}/script", auth=auth,
                     json={"script": A1_SCRIPT})
    ok(r.status_code == 200 and row(SEED["failed_noart"]).status == VideoStatus.FAILED,
       "failed without artifact → allowed (fix, then /retry), status unchanged")
    for key in ("review", "approved", "rendering", "published", "failed_art"):
        r = client.patch(f"/api/videos/{SEED[key]}/script", auth=auth, json={"script": A1_SCRIPT})
        ok(r.status_code == 409 and row(SEED[key]).script == "seed script",
           f"PATCH script on {key} → 409, script untouched")
    r = client.patch(f"/api/videos/{v_plain.id}/script", auth=auth, json={})
    ok(r.status_code == 400, "missing script key → 400")
    r = client.patch(f"/api/videos/{v_plain.id}/script", auth=auth, json={"script": " "})
    ok(r.status_code == 400 and row(v_plain.id).script == A1_NO_ENDCARD,
       "blank script → 400, previous script kept")
    r = client.patch("/api/videos/9999/script", auth=auth, json={"script": "x"})
    ok(r.status_code == 404, "missing video → 404")
    r = client.patch(f"/api/videos/{v_plain.id}/script", json={"script": "x"})
    ok(r.status_code == 401, "PATCH script requires auth")
    r = client.patch(f"/api/videos/{vid}/craft", auth=auth, json={"script": "x"})
    ok(r.status_code == 409, "PATCH …/craft on a draft still 409 (rule unchanged)")
    r = client.patch(f"/api/videos/{vid}", auth=auth, json={"script": "wide"})
    ok(row(vid).script == A1_SCRIPT, "wide PATCH still ignores script")
    r = client.patch(f"/api/videos/{v_mis.id}/script", auth=auth, json={"script": None})
    v = row(v_mis.id)
    ok(r.status_code == 200 and v.script is None and v.creation_config is None,
       "script: null clears script + provenance → engine generates again")
    ok(len(jobruns("script_set")) >= 5, "every script set/clear writes a script_set JobRun")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


# ---------------------------------------------------------------------------
print("render loop: submit speaks the provided script, skips grok script gen")


def fresh_session() -> Session:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def seed(s, *, script=None, cc=None, subject=A1_SUBJECT, fmt="short", title=None):
    ch = Channel(slug="rr", name="RR", oauth_status=OAuthStatus.CONNECTED,
                 daily_render_budget=5)
    s.add(ch)
    s.commit()
    t = Topic(channel_id=ch.id, name="IA", theme_prompt="x", content_format=fmt)
    s.add(t)
    s.commit()
    v = Video(channel_id=ch.id, topic_id=t.id, subject=subject, title=title,
              status=VideoStatus.QUEUED, script=script,
              creation_config=json.dumps(cc) if cc else None)
    s.add(v)
    s.commit()
    s.refresh(v)
    return ch, t, v


_tmp = tempfile.mkdtemp(prefix="verify-provided-")
_orig_hf = settings.hyperframes_storage_dir
settings.hyperframes_storage_dir = _tmp


def _tts_write(text, voice, path: Path):
    tts_texts.append(text)
    path.write_bytes(b"mp3")
    return [{"text": "A", "start": 0.0, "dur": 0.4}]


def _render_write(job_dir, out: Path):
    out.write_bytes(b"mp4")


def _mux_write(*args):
    args[-1].write_bytes(b"final")


VALID_HTML = worker._fallback_composition("ok", "A. B. C. D.", "portrait", 1080, 1920, 12.0)
compose_scripts: list[str] = []


def _compose(subject, script, *a, **k):
    compose_scripts.append(script)
    return VALID_HTML


class SyncHFEngine:
    """HyperFrames contract, but runs worker.run_job inline (no thread)."""
    name = "hyperframes"

    def __init__(self):
        self.params = []

    def submit(self, video, params):
        self.params.append(dict(params))
        handle = f"h{video.id}"
        job_dir = Path(_tmp) / handle
        job_dir.mkdir(parents=True, exist_ok=True)
        worker.run_job(handle, job_dir, video.subject, dict(params))
        return handle

    def status(self, handle):
        return json.loads((Path(_tmp) / handle / "status.json").read_text())


playlist_calls = []
render_loop.ensure_topic_playlist = lambda session, topic, channel: playlist_calls.append(topic.id)
render_loop.resolve_engine = lambda session, video, topic, channel: "hyperframes"


def run_submit(s, eng, gen_return="generated words here"):
    render_loop.get_engine = lambda name: eng
    with patch.object(worker, "_generate_script", return_value=gen_return) as gen, \
            patch.object(worker, "_tts", side_effect=_tts_write), \
            patch.object(worker, "_probe_duration", return_value=10.0), \
            patch.object(worker, "_generate_composition", side_effect=_compose), \
            patch.object(worker, "_render", side_effect=_render_write), \
            patch.object(worker, "_has_visible_frames", return_value=True), \
            patch.object(worker, "_pick_bgm", return_value=None), \
            patch.object(worker, "_mux", side_effect=_mux_write):
        render_loop._submit_new(s)
        s.commit()
    return gen


# provided, endcard missing → appended once, spoken verbatim, grok not called
tts_texts, compose_scripts = [], []
s = fresh_session()
ch, t, v = seed(s, script=A1_NO_ENDCARD, cc={"script_source": "provided"})
eng = SyncHFEngine()
gen = run_submit(s, eng)
v = s.get(Video, v.id)
EXPECTED = A1_NO_ENDCARD + " Subscribe — next IA trap."
ok(gen.call_count == 0, "_generate_script NOT called for a provided script")
ok(v.status == VideoStatus.RENDERING and v.mpt_task_id == f"h{v.id}",
   "provided video submitted normally (rendering, handle stored)")
ok(eng.params and eng.params[0].get("provided_script") == EXPECTED,
   "engine params carry provided_script = provided + endcard")
ok(eng.params[0].get("video_script") == EXPECTED,
   "engine params carry video_script too (MPT native field)")
ok(eng.params[0].get("video_subject") == A1_SUBJECT, "video_subject unchanged")
ok(tts_texts == [EXPECTED], "TTS speaks the provided script verbatim (+ endcard)")
ok(compose_scripts == [EXPECTED], "storyboard compose receives the same verbatim script")
st = eng.status(v.mpt_task_id)
ok(st["state"] == STATE_COMPLETE and st["script"] == EXPECTED,
   "worker status script == provided (+ endcard)")
ok(st["creation_config"].get("script_source") == "provided",
   "worker creation_config.script_source = provided")
ok(v.script == EXPECTED and v.script.count("Subscribe") == 1,
   "row script = spoken text (endcard appended once)")
cc = json.loads(v.creation_config)
ok(cc["script_source"] == "provided" and cc["script_edits"] == ["endcard_appended"],
   "row provenance: provided + script_edits=[endcard_appended]")
ok(playlist_calls == [t.id], "normal submit side effects still run (topic playlist)")

# provided with endcard already present → no duplication
tts_texts, compose_scripts = [], []
s = fresh_session()
ch, t, v = seed(s, script=A1_SCRIPT, cc={"script_source": "provided"})
eng = SyncHFEngine()
gen = run_submit(s, eng)
v = s.get(Video, v.id)
ok(gen.call_count == 0 and tts_texts == [A1_SCRIPT],
   "endcard present → spoken exactly as provided, grok not called")
ok(v.script == A1_SCRIPT and json.loads(v.creation_config)["script_edits"] == [],
   "no duplicate endcard, no edits recorded")

# generated path unchanged
tts_texts, compose_scripts = [], []
s = fresh_session()
ch, t, v = seed(s)
eng = SyncHFEngine()
gen = run_submit(s, eng)
v = s.get(Video, v.id)
ok(gen.call_count == 1, "no provided script → _generate_script called once (unchanged)")
ok("provided_script" not in eng.params[0] and "video_script" not in eng.params[0],
   "generated path: no provided_script/video_script params")
ok(tts_texts == ["generated words here"], "generated path speaks the grok script")
ok(eng.status(v.mpt_task_id)["creation_config"].get("script_source") == "generated",
   "worker marks script_source = generated")

# legacy row with a script but no provided marker (e.g. a requeue) still regenerates
s = fresh_session()
ch, t, v = seed(s, script="old generated script", cc={"beats": []})
eng = SyncHFEngine()
gen = run_submit(s, eng)
ok(gen.call_count == 1, "script without script_source=provided → regenerated (requeue unchanged)")

# misaligned provided → FAILED pre-render with reason, nothing spent
playlist_calls.clear()
s = fresh_session()
ch, t, v = seed(s, script=MISALIGNED, cc={"script_source": "provided"})
eng = SyncHFEngine()
gen = run_submit(s, eng)
v = s.get(Video, v.id)
ok(gen.call_count == 0, "misaligned provided script is NOT silently regenerated")
ok(eng.params == [], "engine.submit never called (no render slot spent)")
ok(playlist_calls == [], "no topic-playlist side effect for a blocked video")
ok(v.status == VideoStatus.FAILED and v.craft_review == craft.CRAFT_REVIEW_FAIL,
   "misaligned → FAILED + craft_review=fail")
ok((v.error or "").startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
   "error carries the provided-script hook reason")
ok(v.script == MISALIGNED, "provided script left as written for the operator to fix")
runs = s.exec(select(JobRun).where(JobRun.kind == "render")).all()
ok(len(runs) == 1 and runs[0].status == "error" and "no slot used" in runs[0].detail,
   "one render error JobRun (not success — rendered_today untouched)")

# MPT adapter: native video_script, internal provided_script filtered out
sent = {}
with patch.object(mpt_engine_mod.mpt, "submit", side_effect=lambda p: sent.update(p) or "tid"):
    mpt_engine_mod.MPTEngine().submit(None, {"video_subject": "s", "content_format": "short",
                                             "provided_script": "X.", "video_script": "X."})
ok(sent.get("video_script") == "X." and "provided_script" not in sent
   and "content_format" not in sent,
   "MPT gets video_script verbatim; provided_script/content_format stripped")

# ---------------------------------------------------------------------------
print("finalize: provided script is never clobbered")
_orig_storage = settings.storage_dir
settings.storage_dir = tempfile.mkdtemp(prefix="verify-provided-store-")
render_loop._has_visible_frames = lambda p: True
render_loop._make_thumbnail = lambda src, out: False


class MetaStub:
    def generate(self, subject, script, content_format="short", language=None):
        return {"title": subject, "description": "d", "tags": ["t"]}


render_loop.metadata = MetaStub()


class FinEngine:
    def __init__(self, final):
        self.final = final

    def final_path(self, handle):
        return self.final


def finalize_with(task, script, cc):
    s = fresh_session()
    ch, t, v = seed(s, script=script, cc=cc, title=A1_SUBJECT)
    v.status = VideoStatus.RENDERING
    v.mpt_task_id = "fin"
    src = Path(settings.storage_dir) / f"src-{id(task)}.mp4"
    src.write_bytes(b"bytes")
    render_loop._finalize(s, v, s.get(Channel, ch.id), FinEngine(src), task)
    s.commit()
    return s.get(Video, v.id)


prov_cc = {"script_source": "provided", "script_edits": ["endcard_appended"]}
v = finalize_with({"state": STATE_COMPLETE, "script": None}, A1_SCRIPT, prov_cc)
ok(v.script == A1_SCRIPT, "task script None → provided script kept")
v = finalize_with({"state": STATE_COMPLETE, "script": ""}, A1_SCRIPT, prov_cc)
ok(v.script == A1_SCRIPT, "task script '' → provided script kept")
v = finalize_with({"state": STATE_COMPLETE, "script": "engine re-derived text",
                   "creation_config": {"beats": [{"type": "hook"}], "used_fallback": False}},
                  A1_SCRIPT, prov_cc)
ok(v.script == A1_SCRIPT, "task script differs → provided (+ endcard) still the text of record")
cc = json.loads(v.creation_config)
ok(cc.get("script_source") == "provided" and cc.get("script_edits") == ["endcard_appended"]
   and cc.get("beats") == [{"type": "hook"}],
   "engine creation_config stored, provenance + script_edits carried over")
ok(v.status == VideoStatus.REVIEW, "provided video still goes through the review gate")
v = finalize_with({"state": STATE_COMPLETE, "script": "fresh grok script"},
                  "old script", None)
ok(v.script == "fresh grok script", "generated video: task script still wins (unchanged)")
v = finalize_with({"state": STATE_COMPLETE, "script": None}, "old script", None)
ok(v.script == "old script", "generated video: empty task script keeps the old one (unchanged)")
# engine snapshot without beats → Gate A/B/C fail-open, so the hook check is
# the reason that surfaces (proves it runs inside the finalize gate).
v = finalize_with({"state": STATE_COMPLETE, "script": None,
                   "creation_config": {"voice": "pt-BR-AntonioNeural"}},
                  MISALIGNED, {"script_source": "provided"})
ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_FAIL
   and (v.error or "").startswith(craft.PROVIDED_SCRIPT_HOOK_REASON),
   "finalize craft gate fails a misaligned provided script with the reason")

settings.storage_dir = _orig_storage
settings.hyperframes_storage_dir = _orig_hf

print(f"\nALL {_checks} CHECKS PASSED")
