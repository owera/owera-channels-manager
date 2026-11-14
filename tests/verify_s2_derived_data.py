"""S2 — PO CM 14/11 (3:52 + 3:53): disconnect scope, manual withdraw, derived data.

Run: PYTHONPATH=. uv run python tests/verify_s2_derived_data.py

In-memory SQLite + TestClient, temp storage dirs. No network, no Google, no
Stripe call, no real LLM (``llm._complete_api`` mocked, CLI raises). Covers:
  (1) 2-channel workspace: disconnecting one keeps the workspace consent, and
      that channel's data is out of the payload at once (youtube_context, ideas
      "existing" list) with the #214 7-day clock set; the other channel's data
      still flows. Disconnecting the LAST connected channel withdraws consent.
  (2) manual 'Withdraw AI consent' cuts the LLM for the WHOLE workspace even with
      a channel still connected; only a new ack brings it back. Footer link
      (always visible after the ack, independent of Settings) + 1-step dialog.
  (3) script / storyboard only for the same draft + workspace: another draft's
      or another workspace's script never enters a prompt (external: refused;
      internal OS/RR: logged only, never blocked); own script allowed.
  (4) #214 scope: deletion request / workspace closure purge scripts +
      storyboards through youtube_purge.purge_channel; a plain disconnect keeps them.
  (4b) same path, PO CM round 3: render media (mp4, narration, thumbnails, other
      artifacts) of NOT-published drafts deleted; published kept; path-traversal /
      symlink / shared-handle guards; other workspaces + internal OS/RR untouched;
      dry-run logs the plan and deletes nothing.
  (4c) MPT task-folder retention (external only): finished > 7 d deleted, < 7 d,
      running / queued, internal ws 1/2, shared, orphan, symlink and traversal kept;
      dry-run; scheduler job on the 6 h cycle.
  (5) no-channel-numbers instruction: external workspaces only; OS/RR system
      prompts byte-identical.
  (6) stats echo: post-generation warning (never blocks) when a raw channel
      statistic appears literally in the script / storyboard text.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as appdb
import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, ChannelMetric, OAuthStatus, Settings, Topic, Video, VideoStatus,
                        Workspace)
from app.services import (google_limited_use as glu, llm, llm_context, llm_derived, llm_guard, notify,
                          stripe_provision, youtube, youtube_purge)
from app.services.engines import worker

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


SHA_EN = hashlib.sha256(glu.TEXT_EN.encode()).hexdigest()
NOW = datetime.now(timezone.utc)
HF = tempfile.mkdtemp(prefix="s2-hf-")
SD = tempfile.mkdtemp(prefix="s2-storage-")
MP = tempfile.mkdtemp(prefix="s2-mpt-")

eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
patches = [patch.object(appdb, "engine", eng),
           patch.object(settings, "hyperframes_storage_dir", HF),
           patch.object(settings, "storage_dir", SD),
           patch.object(settings, "mpt_storage_dir", MP),
           patch.object(notify, "email_access_lost", lambda *a, **k: None),
           patch.object(youtube, "disconnect_channel", lambda s, ch: None),
           patch.object(llm, "_complete_api", lambda *a, **k: "ok"),
           patch.object(llm, "_complete_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("CLI")))]
for p in patches:
    p.start()
import app.services.llm_derived as _ld_mod  # noqa: E402  (uses app.db.engine at call time)

with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Internal"))
    s.add(Settings(id=1, render_concurrency=4))
    s.commit()


def provision(email, cs):
    with Session(eng) as s:
        wid = stripe_provision.handle_checkout_completed(
            s, {"id": cs, "mode": "subscription", "customer": f"cus_{cs}",
                "subscription": f"sub_{cs}", "customer_details": {"email": email}},
            event_id=f"evt_{cs}", event_created=int(NOW.timestamp()))
        s.commit()
        w = s.get(Workspace, wid)
        w.terms_accepted_at = NOW
        w.privacy_accepted_at = NOW
        s.add(w); s.commit()
        return wid, w.slug


WA, SLUG_A = provision("buyer-a@example.com", "cs_a")
WB, SLUG_B = provision("buyer-b@example.com", "cs_b")
SLUG = {1: "owera", WA: SLUG_A, WB: SLUG_B}


def _override_session():
    with Session(eng) as session:
        yield session


settings.multi_workspace = True
settings.allow_workspace_header = True
settings.app_password = ""
main.app.dependency_overrides[get_session] = _override_session
client = TestClient(main.app)
H = lambda ws: {"X-Workspace-Slug": SLUG[ws]}  # noqa: E731
ON = dict(llm_external_release=True, llm_backend="api", xai_data_retention_ack="30d_no_training",
          llm_internal_workspace_ids="1")


@contextlib.contextmanager
def cfg(**kw):
    with contextlib.ExitStack() as st:
        for k, v in kw.items():
            st.enter_context(patch.object(settings, k, v))
        yield


def row(ws):
    with Session(eng) as s:
        return s.get(Workspace, ws)


def refused(ws, ct="ideas", fields=("topic",)):
    try:
        llm_guard.require(ct, workspace_id=ws, fields=fields)
        return None
    except llm_guard.LLMBlocked as e:
        return e.reason


def ack(ws):
    r = client.post("/api/onboarding/ai-data/ack", headers=H(ws),
                    json={"ai_data": True, "text_sha": SHA_EN, "locale": "en"})
    assert r.status_code == 200, r.text


def mk_channel(ws, slug, connected=True):
    with Session(eng) as s:
        ch = Channel(workspace_id=ws, slug=slug, name=slug, daily_render_budget=2, daily_publish_budget=2,
                     oauth_status=OAuthStatus.CONNECTED if connected else OAuthStatus.DISCONNECTED)
        s.add(ch); s.commit(); s.refresh(ch)
        s.add(ChannelMetric(channel_id=ch.id, subscriber_count=12345, view_count=987654, video_count=42))
        t = Topic(channel_id=ch.id, name=f"{slug} topic", theme_prompt="x")
        s.add(t); s.commit(); s.refresh(t)
        s.add(Video(channel_id=ch.id, topic_id=t.id, subject=f"{slug} uploaded title", title=f"{slug} uploaded title",
                    yt_video_id=f"yt-{slug}", status=VideoStatus.PUBLISHED))
        s.add(Video(channel_id=ch.id, topic_id=t.id, subject=f"{slug} idea", status=VideoStatus.DRAFT))
        s.commit()
        return ch.id, t.id


def ctx_titles(ws, ch_id):
    with Session(eng) as s, llm_guard.scope(ws, "ideas"):
        return llm_context.youtube_context(s, ch_id).get("video_title", [])


def existing(ch_id, topic_id):
    with Session(eng) as s:
        return llm_derived.idea_existing_subjects(s, topic_id, s.get(Channel, ch_id))


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(); self.rows = []

    def emit(self, rec):
        self.rows.append((rec.levelno, rec.getMessage()))


# ---- (1) per-channel disconnect -------------------------------------------------------
print("(1) disconnect one of two channels: consent stays, its data leaves the payload at once")
C1, T1 = mk_channel(WA, "a-one")
C2, T2 = mk_channel(WA, "a-two")
with cfg(**ON):
    ack(WA)
    ok(refused(WA) is None, "workspace A consented, LLM allowed")
    ok("a-one uploaded title" in ctx_titles(WA, C1) and "a-one uploaded title" in existing(C1, T1),
       "before: channel 1's titles are in youtube_context + ideas list")
    r = client.post(f"/api/channels/{C1}/disconnect", headers=H(WA))
    ok(r.status_code == 200, "disconnect channel 1 (200)")
    with Session(eng) as s:
        c1 = s.get(Channel, C1)
        ok(c1.access_lost_at is not None and c1.access_lost_reason == notify.REASON_DISCONNECTED
           and youtube_purge.retention_delta(c1.access_lost_reason) == timedelta(days=7),
           "#214 7-day clock running for channel 1 (reason disconnected)")
    ok(row(WA).ai_data_ack_at is not None and refused(WA) is None,
       "workspace consent KEPT (channel 2 still connected), LLM allowed")
    ok(ctx_titles(WA, C1) == [], "youtube_context(ch1) → no titles")
    with Session(eng) as s, llm_guard.scope(WA, "ideas"):
        ok(llm_context.youtube_context(s, C1) == {}, "no titles, descriptions, tags or stats of channel 1")
    ex1 = existing(C1, T1)
    ok("a-one uploaded title" not in ex1 and "a-one idea" in ex1,
       "ideas prompt: channel 1's YouTube titles dropped at once (own CM ideas kept)")
    ok("a-two uploaded title" in ctx_titles(WA, C2) and "a-two uploaded title" in existing(C2, T2),
       "channel 2's data still flows")
    # the actual prompt built by the ideas endpoint never carries channel 1's YouTube titles
    seen = []
    with patch.object(llm, "_complete_api", lambda prompt, *a, **k: (seen.append(prompt), "New idea\n")[1]):
        rr = client.post(f"/api/topics/{T1}/generate", headers=H(WA), json={"count": 1}); print("    generate:", rr.status_code)
    ok(seen and all("a-one uploaded title" not in p for p in seen),
       f"ideas prompt for channel 1 has no YouTube title of it ({len(seen)} call)")
    ok(row(WA).ai_data_ack_withdrawn_at is None, "no withdraw stamped on the workspace")
    r = client.post(f"/api/channels/{C2}/disconnect", headers=H(WA))
    w = row(WA)
    ok(r.status_code == 200 and w.ai_data_ack_at is None and refused(WA) == "ai_data_not_acknowledged",
       "disconnecting the LAST connected channel withdraws the consent (LLM cut)")
    from app.models import GoogleLimitedUseConsentEvent as Ev
    with Session(eng) as s:
        last = s.exec(select(Ev).where(Ev.workspace_id == WA).order_by(Ev.id.desc())).first()
    ok(last.action == "withdraw" and last.reason == "disconnect", "withdraw event reason=disconnect")

# request-deletion follows the same rule
print("    request-deletion: same rule (consent kept while another channel is connected)")
C3, T3 = mk_channel(WB, "b-one")
C4, T4 = mk_channel(WB, "b-two")
with cfg(**ON):
    ack(WB)
    client.post(f"/api/channels/{C3}/request-deletion", headers=H(WB))
    ok(row(WB).ai_data_ack_at is not None and ctx_titles(WB, C3) == [],
       "request-deletion of 1 of 2: consent kept, channel's data excluded")

# ---- (2) manual withdraw: whole workspace -------------------------------------------
print("(2) manual 'Withdraw AI consent' cuts the whole workspace, channels still connected")
with cfg(**ON):
    with Session(eng) as s:
        ok(s.get(Channel, C4).oauth_status == OAuthStatus.CONNECTED, "workspace B still has a connected channel")
    r = client.post("/api/onboarding/ai-data/withdraw", headers=H(WB))
    ok(r.status_code == 200 and r.json()["valid"] is False and row(WB).ai_data_ack_at is None,
       "POST /api/onboarding/ai-data/withdraw clears the consent")
    ok(refused(WB) == "ai_data_not_acknowledged" and refused(WB, "render", ("script",)) == "ai_data_not_acknowledged",
       "LLM refused for the WHOLE workspace (ideas + render) though a channel is connected")
    with Session(eng) as s, llm_guard.scope(WB, "ideas"):
        try:
            llm.complete("x", fields=("topic",))
            got = None
        except llm_guard.LLMBlocked as e:
            got = e.reason
    ok(got == "ai_data_not_acknowledged", "llm.complete itself refused right after the withdraw")
    ok(refused(WB) == "ai_data_not_acknowledged", "still refused until a new ack (no auto re-consent)")
    ack(WB)
    ok(refused(WB) is None, "a new ack brings the LLM back")
app_src = (ROOT / "frontend/src/App.tsx").read_text()
page = (ROOT / "frontend/src/pages/AiData.tsx").read_text()
i18n = (ROOT / "frontend/src/wizardI18n.ts").read_text()
link = page[page.index("export function WithdrawAiConsentLink"):page.index("export function GoogleLimitedUseSettings")]
dlg = page[page.index("export function WithdrawAiConsentDialog"):page.index("export function WithdrawAiConsentLink")]
ok(app_src.count("<WithdrawAiConsentLink") == 2 and "function Sidebar" in app_src
   and app_src.index("<WithdrawAiConsentLink") > app_src.index("function Sidebar"),
   "footer link in the sidebar account area + the mobile footer (outside Settings)")
ok("data.exempt || !data.valid) return null" in link and "first" not in link.lower(),
   "link shown whenever the consent is current (no first-draft condition)")
ok('t("glu.footer.withdraw")' in link and '"glu.footer.withdraw": { en: "Withdraw AI consent", pt: "Retirar consentimento de IA" }' in i18n,
   "label EN 'Withdraw AI consent' / PT 'Retirar consentimento de IA'")
ok("setAsking(true)" in link and "<WithdrawAiConsentDialog" in link and "withdraw.mutate" not in link,
   "1st click opens the confirmation, it does not withdraw")
ok("withdraw.mutate" in dlg and 'role="alertdialog"' in dlg and 't("settings.glu.dialog_withdraw")' in dlg
   and 't("settings.glu.dialog_keep")' in dlg and 't("settings.glu.dialog_title")' in dlg,
   "1-step dialog: Withdraw / Keep (CMO v2 copy)")
for k, en, pt in (("settings.glu.dialog_withdraw", "Withdraw", "Retirar"), ("settings.glu.dialog_keep", "Keep", "Manter")):
    ok(f'"{k}": {{ en: "{en}", pt: "{pt}" }}' in i18n, f"{k} EN/PT")
sett = page[page.index("export function GoogleLimitedUseSettings"):]
ok("<WithdrawAiConsentDialog" in sett and "withdraw.mutate" not in sett,
   "Settings button uses the same dialog (no 1-click withdraw)")

# ---- (3) same draft + workspace -------------------------------------------------------
print("(3) a script / storyboard never enters another draft's or workspace's prompt")
SCRIPT_A = ("Most people boil pasta wrong and it is not about the salt at all, it is the pot size "
            "and the timing that ruin the texture every single night.")
SCRIPT_B = ("Your phone battery dies early because three background settings drain it while you sleep, "
            "and turning them off takes ten seconds in the settings app.")
with Session(eng) as s:
    va = Video(channel_id=C2, topic_id=T2, subject="pasta", script=SCRIPT_A, status=VideoStatus.REVIEW)
    va2 = Video(channel_id=C2, topic_id=T2, subject="pasta two", status=VideoStatus.QUEUED)
    vb = Video(channel_id=C4, topic_id=T4, subject="battery", script=SCRIPT_B, status=VideoStatus.REVIEW)
    s.add(va); s.add(va2); s.add(vb); s.commit()
    VA, VA2, VB = va.id, va2.id, vb.id
with Session(eng) as s:
    w = s.get(Workspace, WA)
    glu.record_ack(s, w, user=None, locale="en", sha=SHA_EN)


def run(ws, draft, prompt, fields=("script", "storyboard")):
    with llm_guard.scope(ws, "render"), llm_derived.draft_scope(draft):
        try:
            llm.complete(prompt, fields=fields)
            return None
        except llm_guard.LLMBlocked as e:
            return e.reason


with cfg(**ON):
    ok(run(WA, VA, f"Build the storyboard for this script:\n{SCRIPT_A}") is None,
       "own draft's script in its own storyboard prompt → allowed")
    ok(run(WA, VA2, f"Build the storyboard for this script:\n{SCRIPT_A}") == "foreign_draft_data",
       "another draft's script (same workspace) → refused (foreign_draft_data)")
    ok(run(WA, VA2, f"Write a script. Style example:\n{SCRIPT_B}") == "foreign_draft_data",
       "another workspace's script → refused")
    ok(run(WA, None, f"Metadata for:\n{SCRIPT_A}") == "foreign_draft_data",
       "unbound draft carrying a stored script → refused")
    ok(run(WA, VA2, "Write a script about pasta two.") is None, "fresh prompt (no other script) → allowed")
    with patch.object(llm_guard, "release_block_reason", lambda *a, **k: None):
        ok(run(1, VA2, f"x\n{SCRIPT_B}") is None, "internal OS/RR workspace: never blocked (logged only)")
    ok(run(WA, VA2, f"x\n{SCRIPT_A}", fields=("topic",)) is None,
       "calls not declaring script/storyboard skip the scan (no OS/RR cost on ideas)")
    ok(issubclass(llm_guard.LLMCrossDraft, llm_guard.LLMBlocked), "LLMCrossDraft is an LLMBlocked")
# wiring: each derived call site binds its draft
rl = (ROOT / "app/services/render_loop.py").read_text()
sc = (ROOT / "app/services/script_compose.py").read_text()
wk = (ROOT / "app/services/engines/worker.py").read_text()
ok('params["video_id"] = video.id' in rl and "_derived.draft_scope(video.id)" in rl,
   "render_loop: render params carry video_id; metadata bound to its draft")
ok("llm_derived.draft_scope(video_id)" in sc, "script_compose bound to its draft")
ok('_derived.bind_draft(params.get("video_id"))' in wk and "_derived.unbind_draft(_draft_tok)" in wk,
   "worker.run_job binds the draft for script + storyboard calls")
ok("llm_derived.check_prompt(prompt, system, decl, ws)" in (ROOT / "app/services/llm.py").read_text(),
   "llm.complete runs the same-draft check on every call")

# ---- (4) #214 purge scope --------------------------------------------------------------
print("(4) deletion request / workspace closure purge scripts + storyboards (7 days)")


def job(handle, script):
    d = Path(HF) / handle
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<html>storyboard</html>")
    (d / "narration_words.json").write_text("[]")
    (d / "status.json").write_text(json.dumps({"state": "done", "script": script}))
    (d / "final.mp4").write_bytes(b"\0")
    return d


with Session(eng) as s:
    for cid, tid in ((C3, T3), (C4, T4), (C1, T1)):
        s.add(Video(channel_id=cid, topic_id=tid, subject=f"d{cid}", script=f"script of {cid}",
                    creation_config=json.dumps({"beats": ["x"]}), mpt_task_id=f"job-{cid}",
                    status=VideoStatus.REVIEW))
    s.commit()
d3, d1 = job(f"job-{C3}", f"script of {C3}"), job(f"job-{C1}", f"script of {C1}")
with Session(eng) as s:
    c3, c1 = s.get(Channel, C3), s.get(Channel, C1)
    ok(c3.access_lost_reason == notify.REASON_DELETION_REQUESTED, "channel b-one: deletion_requested clock")
    later = (c3.access_lost_at.replace(tzinfo=timezone.utc) if c3.access_lost_at.tzinfo is None
             else c3.access_lost_at) + timedelta(days=8)
    ok(youtube_purge.is_due(c3, later) and youtube_purge.is_due(c1, later), "both due after 7 days")
    r3 = youtube_purge.purge_channel(s, c3, later)
    r1 = youtube_purge.purge_channel(s, c1, later)
with Session(eng) as s:
    v3 = s.exec(select(Video).where(Video.channel_id == C3, Video.mpt_task_id == f"job-{C3}")).one()
    v1 = s.exec(select(Video).where(Video.channel_id == C1, Video.mpt_task_id == f"job-{C1}")).one()
    ok(v3.script is None and v3.creation_config is None and r3.get("derived_videos", 0) >= 1,
       f"deletion_requested: Video.script + creation_config cleared ({r3})")
    ok(not (d3 / "index.html").exists() and not (d3 / "narration_words.json").exists()
       and not (d3 / "status.json").exists() and not (d3 / "final.mp4").exists(),
       "deletion_requested (unpublished draft): storyboard, words, status script and the render removed")
    ok(youtube_purge._purge_job_text is not None, "text purge kept for published drafts' job dirs")
    ok(v1.script == f"script of {C1}" and v1.creation_config is not None and "derived_videos" not in r1
       and (d1 / "index.html").exists(), "plain disconnect: the draft is kept (only YouTube API data purged)")
# workspace closure → same purge
WC, SLUG_C = provision("buyer-c@example.com", "cs_c")
SLUG[WC] = SLUG_C
C5, T5 = mk_channel(WC, "c-one")
with Session(eng) as s:
    s.add(Video(channel_id=C5, topic_id=T5, subject="c", script="script c", creation_config="{}", mpt_task_id=f"job-{C5}"))
    s.commit()
    notify.mark_workspace_closed(s, s.get(Workspace, WC))
with Session(eng) as s:
    c5 = s.get(Channel, C5)
    ok(c5.access_lost_reason == notify.REASON_WORKSPACE_CLOSED, "workspace closure stamps the 7-day reason")
    youtube_purge.purge_channel(s, c5, datetime.now(timezone.utc) + timedelta(days=8))
with Session(eng) as s:
    ok(all(v.script is None and v.creation_config is None
           for v in s.exec(select(Video).where(Video.channel_id == C5)).all()),
       "workspace_closed: scripts + storyboards purged by the same #214 path")
ok("purge_derived" in (ROOT / "app/services/youtube_purge.py").read_text()
   and "def purge_derived" not in (ROOT / "app/services/google_limited_use.py").read_text(),
   "one purge path (youtube_purge), no duplicate")


# ---- (4b) media of NOT-published drafts ------------------------------------------------
print("(4b) deletion request: unpublished render media deleted, published kept, guards")
import os  # noqa: E402

VROOT = Path(SD) / "videos"


def media(vid, handle, extra=True):
    vd = VROOT / str(vid)
    (vd / "sub").mkdir(parents=True, exist_ok=True)
    for n in ("video.mp4", "thumb.jpg", "thumb_provided.png"):
        (vd / n).write_bytes(b"\0")
    if extra:
        (vd / "sub" / "artifact.bin").write_bytes(b"\0")
    files = [vd / "video.mp4", vd / "thumb.jpg", vd / "thumb_provided.png"]
    if handle and "/" not in handle:
        jd = Path(HF) / handle
        jd.mkdir(parents=True, exist_ok=True)
        for n in ("final.mp4", "narration.mp3", "index.html"):
            (jd / n).write_bytes(b"\0")
        files += [jd / "final.mp4", jd / "narration.mp3"]
        md = Path(MP) / handle                                    # MPT task folder (round 4)
        (md / "clips").mkdir(parents=True, exist_ok=True)
        for n in ("final-1.mp4", "audio.mp3", "script.json", "clips/c1.mp4"):
            (md / n).write_bytes(b"\0")
        files += [md / "final-1.mp4", md / "audio.mp3", md / "script.json", md / "clips/c1.mp4"]
    return files


WD, SLUG_D = provision("buyer-d@example.com", "cs_d")
WE, SLUG_E = provision("buyer-e@example.com", "cs_e")
SLUG[WD], SLUG[WE] = SLUG_D, SLUG_E
CD, TD = mk_channel(WD, "d-one")
CE, TE = mk_channel(WE, "e-one")
CI, TI = mk_channel(1, "os-int")
outside = Path(tempfile.mkdtemp(prefix="s2-outside-"))
(outside / "victim.mp4").write_bytes(b"\0")
os.symlink(outside, Path(HF) / "linkjob")                      # symlinked job dir
os.symlink(outside, Path(MP) / "linkjob")                      # symlinked MPT task folder
with Session(eng) as s:
    def V(ch, tp, **kw):
        v = Video(channel_id=ch, topic_id=tp, subject=kw.pop("subject", "v"), **kw)
        s.add(v); s.commit(); s.refresh(v)
        return v.id
    U = V(CD, TD, status=VideoStatus.REVIEW, mpt_task_id="job-u", video_path="x", thumb_path="y")
    P = V(CD, TD, status=VideoStatus.PUBLISHED, yt_video_id="yt-p", mpt_task_id="job-p")
    P2 = V(CD, TD, status=VideoStatus.APPROVED, published_at=NOW, mpt_task_id="job-p2")   # published earlier
    TR = V(CD, TD, status=VideoStatus.FAILED, mpt_task_id="../" + outside.name)          # traversal handle
    SL = V(CD, TD, status=VideoStatus.FAILED, mpt_task_id="linkjob")                     # symlink job dir
    SH = V(CD, TD, status=VideoStatus.REVIEW, mpt_task_id="job-shared")
    O = V(CE, TE, status=VideoStatus.REVIEW, mpt_task_id="job-o")                        # other workspace
    O2 = V(CE, TE, status=VideoStatus.REVIEW, mpt_task_id="job-shared")                  # shares SH's handle
    I = V(CI, TI, status=VideoStatus.REVIEW, mpt_task_id="job-i")                        # internal OS/RR
f_u = media(U, "job-u")
f_p, f_p2 = media(P, "job-p"), media(P2, "job-p2")
media(TR, None); media(SL, None)
f_sh = media(SH, "job-shared")
f_o, f_i = media(O, "job-o"), media(I, "job-i")
# a symlink inside U's output dir that points outside must not be followed
os.symlink(outside / "victim.mp4", VROOT / str(U) / "evil.mp4")

cap2 = _Cap()
logging.getLogger("manager.youtube_purge").addHandler(cap2)
logging.getLogger("manager.youtube_purge").setLevel(logging.INFO)
with Session(eng) as s:
    notify.mark_deletion_requested(s, s.get(Channel, CD))
with Session(eng) as s:
    ch = s.get(Channel, CD)
    dry = youtube_purge.purge_media(s, ch, dry_run=True)
ok(dry["media_dry_run"] is True and dry["media_files"] > 0 and all(f.exists() for f in f_u)
   and any("would_delete" in m and str(VROOT / str(U)) in m for _, m in cap2.rows)
   and any("youtube_purge_media plan" in m and f"video_id={U}" in m for _, m in cap2.rows),
   f"dry-run: plan + would_delete logged, nothing deleted ({dry})")
with Session(eng) as s, patch.object(settings, "purge_media_dry_run", True):
    youtube_purge.purge_media(s, s.get(Channel, CD))
ok(all(f.exists() for f in f_u), "MANAGER_PURGE_MEDIA_DRY_RUN=1 → nothing deleted")
cap2.rows.clear()
with Session(eng) as s:
    ch = s.get(Channel, CD)
    la = ch.access_lost_at if ch.access_lost_at.tzinfo else ch.access_lost_at.replace(tzinfo=timezone.utc)
    ok(not youtube_purge.is_due(ch, la + timedelta(days=6)) and youtube_purge.is_due(ch, la + timedelta(days=7, seconds=1)),
       "same 7-day clock (not due at 6 d, due after 7 d)")
    res = youtube_purge.purge_channel(s, ch, la + timedelta(days=7, seconds=1))
ok(not any(f.exists() for f in f_u) and not (VROOT / str(U)).exists() and not (Path(HF) / "job-u").exists(),
   f"unpublished draft: video.mp4, thumb.jpg, provided thumb, sub/artifact, final.mp4, narration.mp3 deleted ({res})")
with Session(eng) as s:
    vu = s.get(Video, U)
    ok(vu.video_path is None and vu.thumb_path is None and vu.script is None, "row paths cleared")
    ok(s.get(Video, P).yt_video_id == "yt-p", "nothing on YouTube touched (no API call, yt id kept)")
ok(all(f.exists() for f in f_p + f_p2) and res.get("media_published_kept") == 3,
   "published videos (status PUBLISHED / yt_video_id / published_at) keep every file")
ok((outside / "victim.mp4").exists() and outside.exists(),
   "path traversal: '../x' handle never resolves outside; evil.mp4 symlink removed, its target kept")
ok((outside / "victim.mp4").exists() and (Path(HF) / "linkjob").is_symlink()
   and (Path(MP) / "linkjob").is_symlink(), "symlinked job dir / MPT task folder is not followed or deleted")
ok(all(f.exists() for f in f_sh[-2:]) and not (VROOT / str(SH)).exists(),
   "job dir shared with another workspace's video kept; the draft's own output dir deleted")
ok(not (Path(MP) / "job-u").exists(), "unpublished draft: its MPT task folder deleted too")
ok(all(f.exists() for f in f_o) and (Path(MP) / "job-o" / "final-1.mp4").exists(),
   "another workspace's media untouched (incl. its MPT task folder)")
ok(all(f.exists() for f in f_i) and (Path(MP) / "job-i" / "final-1.mp4").exists(),
   "internal ws 1 OS/RR media untouched (incl. its MPT task folder)")
ok((Path(MP) / "job-p" / "final-1.mp4").exists(), "published draft's MPT task folder kept")
ok(youtube_purge._safe_dir(str(VROOT), "..") is None and youtube_purge._safe_dir(str(VROOT), "../videos") is None
   and youtube_purge._safe_dir(str(Path(HF)), "linkjob") is None and youtube_purge._safe_dir("", "1") is None
   and youtube_purge._safe_dir(MP, "linkjob") is None and youtube_purge._safe_dir(MP, "a/b") is None,
   "_safe_dir rejects '..', multi-segment names, symlinks and an unset root")
# plain disconnect (and the 30-day dead-token reason) never deletes media
with Session(eng) as s:
    s.get(Channel, CE)
    ce = s.get(Channel, CE)
    notify.mark_operator_disconnect(s, ce)
with Session(eng) as s:
    ce = s.get(Channel, CE)
    la = ce.access_lost_at if ce.access_lost_at.tzinfo else ce.access_lost_at.replace(tzinfo=timezone.utc)
    r_e = youtube_purge.purge_channel(s, ce, la + timedelta(days=8))
ok(all(f.exists() for f in f_o) and "media_videos" not in r_e, "plain disconnect: media kept")
# the internal workspace's own deletion request is the only way its media goes
with Session(eng) as s:
    notify.mark_deletion_requested(s, s.get(Channel, CI))
with Session(eng) as s:
    ci = s.get(Channel, CI)
    la = ci.access_lost_at if ci.access_lost_at.tzinfo else ci.access_lost_at.replace(tzinfo=timezone.utc)
    youtube_purge.purge_channel(s, ci, la + timedelta(days=8))
ok(not any(f.exists() for f in f_i) and not (Path(MP) / "job-i").exists() and all(f.exists() for f in f_o),
   "internal ws 1 media (incl. MPT folder) deleted only by ws 1's own deletion request (others untouched)")


# ---- (4c) MPT task-folder retention (external, 7 d after the job finished) ------------
print("(4c) MPT retention: external task folders deleted 7 d after the job finished")
from app.services import mpt_retention  # noqa: E402

RNOW = datetime.now(timezone.utc)
WR, SLUG_R = provision("buyer-r@example.com", "cs_r")
SLUG[WR] = SLUG_R
ok(WR > 2, "test buyer workspace is not ws 1 / 2")
CR, TR_ = mk_channel(WR, "r-one")
ok(WA == 2, "ws 2 exists here (the first provisioned buyer) — it must still count as internal")
CA2, TA2 = mk_channel(2, "ws2-ch")


def task(handle, age_days):
    d = Path(MP) / handle
    (d / "sub").mkdir(parents=True, exist_ok=True)
    for n in ("final-1.mp4", "audio.mp3", "sub/clip.mp4"):
        (d / n).write_bytes(b"\0")
        t_ = (RNOW - timedelta(days=age_days)).timestamp()
        os.utime(d / n, (t_, t_))
    return d


with Session(eng) as s:
    def RV(ch, tp, status, handle):
        v = Video(channel_id=ch, topic_id=tp, subject=handle, status=status, mpt_task_id=handle)
        s.add(v); s.commit()
    RV(CR, TR_, VideoStatus.REVIEW, "ret-old-ok")
    RV(CR, TR_, VideoStatus.FAILED, "ret-old-failed")
    RV(CR, TR_, VideoStatus.PUBLISHED, "ret-old-published")
    RV(CR, TR_, VideoStatus.REVIEW, "ret-new")
    RV(CR, TR_, VideoStatus.RENDERING, "ret-running")
    RV(CR, TR_, VideoStatus.QUEUED, "ret-queued")
    RV(CI, TI, VideoStatus.REVIEW, "ret-internal1")
    RV(CA2, TA2, VideoStatus.REVIEW, "ret-ws2")
    RV(CR, TR_, VideoStatus.REVIEW, "ret-shared")
    RV(CE, TE, VideoStatus.REVIEW, "ret-shared")
    RV(CR, TR_, VideoStatus.REVIEW, "linkret")
    RV(CR, TR_, VideoStatus.REVIEW, "../" + outside.name)
dirs = {h: task(h, a) for h, a in (("ret-old-ok", 8), ("ret-old-failed", 8), ("ret-old-published", 8),
                                    ("ret-new", 6), ("ret-running", 9), ("ret-queued", 9),
                                    ("ret-internal1", 30), ("ret-ws2", 30), ("ret-shared", 30),
                                    ("ret-orphan", 30))}
old_out = (RNOW - timedelta(days=30)).timestamp()
os.utime(outside / "victim.mp4", (old_out, old_out))
os.symlink(outside, Path(MP) / "linkret")
cap3 = _Cap()
logging.getLogger("manager.mpt_retention").addHandler(cap3)
logging.getLogger("manager.mpt_retention").setLevel(logging.INFO)
n_dry = mpt_retention.tick(RNOW, dry_run=True)
ok(n_dry == 3 and all(d.exists() for d in dirs.values())
   and any("would_delete" in m for _, m in cap3.rows) and any("mpt_task_retention plan" in m for _, m in cap3.rows),
   f"dry-run: plan + would_delete logged for 3 folders, nothing deleted ({n_dry})")
with patch.object(settings, "purge_media_dry_run", True):
    mpt_retention.tick(RNOW)
ok(all(d.exists() for d in dirs.values()), "MANAGER_PURGE_MEDIA_DRY_RUN=1 → nothing deleted")
n = mpt_retention.tick(RNOW)
ok(n == 3, f"3 folders deleted ({n})")
ok(not dirs["ret-old-ok"].exists() and not dirs["ret-old-failed"].exists(),
   "external job finished > 7 d ago (ok and failed): task folder deleted")
ok(not dirs["ret-old-published"].exists() and Path(SD).exists(),
   "published > 7 d: MPT scratch deleted (the Manager's own copy in storage/videos is not touched)")
ok(dirs["ret-new"].exists(), "finished < 7 d ago: kept")
ok(dirs["ret-running"].exists() and dirs["ret-queued"].exists(), "rendering / queued job: kept (even if old)")
ok(dirs["ret-internal1"].exists() and dirs["ret-ws2"].exists(), "internal ws 1 and ws 2 (OS/RR): kept")
ok(dirs["ret-shared"].exists() and dirs["ret-orphan"].exists(), "folder shared by two videos / without a video row: kept")
ok((outside / "victim.mp4").exists() and (Path(MP) / "linkret").is_symlink(),
   "symlinked task folder and '../' traversal handle: blocked, target untouched")
with patch.object(settings, "llm_internal_workspace_ids", f"1,{WR}"):
    task("ret-env-internal", 9)
    with Session(eng) as s:
        s.add(Video(channel_id=CR, topic_id=TR_, subject="e", status=VideoStatus.REVIEW,
                    mpt_task_id="ret-env-internal")); s.commit()
    mpt_retention.tick(RNOW)
    ok((Path(MP) / "ret-env-internal").exists(), "a workspace in MANAGER_LLM_INTERNAL_WORKSPACE_IDS is internal: kept")
ok(mpt_retention.TICK_HOURS == 6 and mpt_retention.RETENTION_DAYS == 7, "6 h cycle, 7-day retention")
sch_src = (ROOT / "app/services/scheduler.py").read_text()
ok('id="mpt_task_retention"' in sch_src and "hours=mpt_retention.TICK_HOURS" in sch_src,
   "registered as scheduler job mpt_task_retention (6 h)")

# ---- (5) no-channel-numbers instruction --------------------------------------------
print("(5) no-channel-numbers instruction: external only, OS/RR byte-identical")
SYS = worker._SYSTEM if hasattr(worker, "_SYSTEM") else "craft system prompt"
got = {}


def capture(prompt, system=None, *a, **k):
    got["system"] = system
    return "ok"


with cfg(**ON), patch.object(llm, "complete", capture):
    with llm_guard.scope(1, "render"):
        worker._llm("u", system="CRAFT SYSTEM", max_tokens=10)
    ok(got["system"] == "CRAFT SYSTEM", "internal (OS/RR) _llm: system prompt unchanged")
    with llm_guard.scope(1, "render"):
        worker._llm_compose("u", system="GATE B SYSTEM", max_tokens=10)
    ok(got["system"] == "GATE B SYSTEM", "internal (OS/RR) _llm_compose: Gate B system prompt unchanged")
    worker._llm("u", system="CRAFT SYSTEM", max_tokens=10)
    ok(got["system"] == "CRAFT SYSTEM", "unbound (tests / CLI): unchanged")
    with llm_guard.scope(WA, "render"):
        worker._llm("u", system="CRAFT SYSTEM", max_tokens=10)
    ok(got["system"] == "CRAFT SYSTEM\n\n" + llm_derived.NO_CHANNEL_NUMBERS_RULE,
       "external _llm: rule appended after the unchanged craft prompt")
    with llm_guard.scope(WA, "render"):
        worker._llm_compose("u", system="GATE B SYSTEM", max_tokens=10)
    ok(got["system"].startswith("GATE B SYSTEM") and got["system"].endswith(llm_derived.NO_CHANNEL_NUMBERS_RULE),
       "external _llm_compose (storyboard): rule appended")
r_ = llm_derived.NO_CHANNEL_NUMBERS_RULE.lower()
ok(all(w in r_ for w in ("views", "subscribers", "retention", "script", "storyboard", "do not copy")),
   "rule names views, subscribers, retention, script and storyboard")

# ---- (6) stats echo ---------------------------------------------------------------------
print("(6) stats echo: warning only, never blocks")
st = {"subscriber_count": 12345, "view_count": 987654, "video_count": 42}
ok(llm_derived.stats_echo("We hit 12,345 subscribers!", st) == ["subscriber_count"], "12,345 (grouped) flagged")
ok(llm_derived.stats_echo("987654 views later", st) == ["view_count"], "987654 (plain) flagged")
ok(llm_derived.stats_echo("987.654 visualizações", st) == ["view_count"], "987.654 (PT grouping) flagged")
ok(llm_derived.stats_echo("42 tips, 1234567 grains, 123450 steps", st) == [],
   "small numbers (<100) and longer numbers containing the digits are not flagged")


cap = _Cap()
logging.getLogger("manager.llm_derived").addHandler(cap)
hits = llm_derived.flag_stats_echo("Join our 12,345 subscribers", kind="script", channel_id=C2, video_id=VA,
                                   workspace_id=WA)
ok(hits == ["subscriber_count"] and any(l == logging.WARNING and "llm_stats_echo" in m and "kind=script" in m
                                        and f"video_id={VA}" in m and "subscriber_count" in m for l, m in cap.rows),
   "flag_stats_echo logs a WARNING llm_stats_echo with workspace/channel/video/kind/stat (stats from ChannelMetric)")
cap.rows.clear()
html = "<html><script>var n=987654;</script><div>Now 987,654 views</div></html>"
ok(llm_derived.flag_stats_echo(llm_derived.html_text(html), kind="storyboard", stats=st) == ["view_count"]
   and any("kind=storyboard" in m for _, m in cap.rows), "storyboard visible text checked (kind=storyboard)")
with patch.object(llm_derived, "stats_echo", lambda *a: (_ for _ in ()).throw(RuntimeError("x"))):
    ok(llm_derived.flag_stats_echo("x", kind="script", stats=st) == [], "a failing check never raises")
ok(wk.count("_derived.flag_stats_echo(") == 2 and "llm_derived.flag_stats_echo(" in sc,
   "hooked after the script and the storyboard (worker) and after compose (script_compose)")

for p in patches:
    p.stop()
print(f"\nALL {_checks} CHECKS PASSED")
