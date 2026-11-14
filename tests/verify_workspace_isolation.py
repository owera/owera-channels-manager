"""S4: workspace isolation on every channel/video/topic-scoped route.

Buyer A (ws "buyer-a") tries buyer B's (ws "buyer-b") resources on each route:
every one must answer 404 (never 403 — existence must not leak) and must not
touch B's rows, tokens or the LLM. Settings: only the admin workspace (ws 1,
owera) reads/writes the global row; a buyer gets a filtered view and may only
pause its own workspace. ws 1 / admin keep their current behaviour.

In-memory SQLite + TestClient; no network, no LLM, no YouTube.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import channel_workspace_paused, get_session, paused_workspace_ids
from app.models import (Channel, ChannelMetric, JobRun, OAuthStatus, Playlist, RenderProfile,
                        Settings, Topic, TrendSignal, Video, VideoStatus, Workspace)
from app.services import analytics_loop, metrics_loop, video_gen, youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---- fixtures -----------------------------------------------------------------
_tmp = Path(tempfile.mkdtemp(prefix="s4-iso-"))
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="buyer-a", name="Buyer A"))
    s.add(Workspace(id=3, slug="buyer-b", name="Buyer B"))
    s.add(Settings(id=1))
    s.commit()
    ch1 = Channel(workspace_id=1, slug="own1", name="Owera 1",
                  oauth_status=OAuthStatus.CONNECTED, yt_channel_id="UC1")
    cha = Channel(workspace_id=2, slug="cha", name="A",
                  oauth_status=OAuthStatus.CONNECTED, yt_channel_id="UCA")
    chb = Channel(workspace_id=3, slug="chb", name="B",
                  oauth_status=OAuthStatus.CONNECTED, yt_channel_id="UCB")
    s.add_all([ch1, cha, chb])
    s.commit()
    for c in (ch1, cha, chb):
        s.refresh(c)
    CH1, CHA, CHB = ch1.id, cha.id, chb.id
    tb = Topic(channel_id=CHB, name="B topic", theme_prompt="b", weight=1)
    ta = Topic(channel_id=CHA, name="A topic", theme_prompt="a", weight=1)
    t1 = Topic(channel_id=CH1, name="Owera topic", theme_prompt="o", weight=1)
    s.add_all([tb, ta, t1])
    s.commit()
    for t in (tb, ta, t1):
        s.refresh(t)
    TB, TA, T1 = tb.id, ta.id, t1.id
    vid_file = _tmp / "b.mp4"
    vid_file.write_bytes(b"\x00" * 64)
    thumb_file = _tmp / "b.jpg"
    thumb_file.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)
    vb = Video(channel_id=CHB, topic_id=TB, subject="B video", status=VideoStatus.APPROVED,
               video_path=str(vid_file), thumb_path=str(thumb_file))
    s.add(vb)
    s.add(ChannelMetric(channel_id=CHB, subscriber_count=7, view_count=1, video_count=1))
    plb = Playlist(channel_id=CHB, yt_playlist_id="PLb", title="B list")
    pb = RenderProfile(name="B look", channel_id=CHB, params_json="{}")
    p_shared = RenderProfile(name="Default", channel_id=None, params_json="{}")
    trb = TrendSignal(term="B trend", term_norm="b trend", channel_id=CHB, score=50)
    tr_null = TrendSignal(term="Research", term_norm="research", channel_id=None, score=10)
    tr1 = TrendSignal(term="Owera trend", term_norm="owera trend", channel_id=CH1, score=5)
    tra = TrendSignal(term="A trend", term_norm="a trend", channel_id=CHA, score=1)
    s.add_all([plb, pb, p_shared, trb, tr_null, tr1, tra])
    s.commit()
    for o in (vb, plb, pb, p_shared, trb, tr_null, tr1, tra):
        s.refresh(o)
    VB, PLB, PB, PSH, TRB, TRNULL, TR1, TRA = (vb.id, plb.id, pb.id, p_shared.id,
                                              trb.id, tr_null.id, tr1.id, tra.id)
    # JobRun has no workspace_id: ownership is the channel, else the video's channel.
    s.add_all([
        JobRun(channel_id=CHB, kind="render", status="success", detail="B channel run"),
        JobRun(channel_id=None, video_id=VB, kind="delete", status="success",
               detail="B video-only run"),
        JobRun(channel_id=CHA, kind="render", status="success", detail="A channel run"),
        JobRun(channel_id=None, video_id=None, kind="music_replenish", status="success",
               detail="global run"),
    ])
    s.commit()


def _override_session():
    with Session(engine) as session:
        yield session


_saved = (settings.multi_workspace, settings.allow_workspace_header, settings.app_password)
settings.multi_workspace = True
settings.allow_workspace_header = True
settings.app_password = ""
main.app.dependency_overrides[get_session] = _override_session
client = TestClient(main.app)

# Any token / YouTube / LLM touch on a foreign resource is a leak: fail loudly.
_touched: list[str] = []


def _boom(name):
    def f(*a, **k):
        _touched.append(name)
        raise AssertionError(f"{name} must not run for a foreign workspace")
    return f


_orig = (youtube.get_service, analytics_loop._snapshot_channel,
         metrics_loop.record_snapshot, video_gen.generate_ideas)
youtube.get_service = _boom("youtube.get_service")
analytics_loop._snapshot_channel = _boom("analytics_loop._snapshot_channel")
metrics_loop.record_snapshot = _boom("metrics_loop.record_snapshot")
video_gen.generate_ideas = _boom("video_gen.generate_ideas")

A = {"X-Workspace-Slug": "buyer-a"}
B = {"X-Workspace-Slug": "buyer-b"}
ADMIN = {"X-Workspace-Slug": "owera"}


def _d(row):
    # updated_at excluded: the admin's own no-op rename of Default bumps it.
    return {k: v for k, v in row.model_dump().items() if k != "updated_at"}


def snapshot():
    with Session(engine) as s:
        return {
            "topic_b": _d(s.get(Topic, TB)),
            "video_b": _d(s.get(Video, VB)),
            "profile_b": _d(s.get(RenderProfile, PB)),
            "profile_shared": _d(s.get(RenderProfile, PSH)),
            "trend_b": _d(s.get(TrendSignal, TRB)),
            "trend_null": _d(s.get(TrendSignal, TRNULL)),
            "settings": s.get(Settings, 1).model_dump(),
            "playlists_b": len(s.exec(select(Playlist).where(Playlist.channel_id == CHB)).all()),
            "topics_b": len(s.exec(select(Topic).where(Topic.channel_id == CHB)).all()),
            "ws_b": s.get(Workspace, 3).scheduler_paused,
        }


before = snapshot()


def cross(method, path, label, **kw):
    r = client.request(method, path, headers=A, **kw)
    ok(r.status_code == 404, f"A→B {label}: {method} {path} is 404 (got {r.status_code})")
    return r


# ---- youtube_admin (12 routes) ------------------------------------------------
print("youtube_admin: buyer A on buyer B's channel")
c = f"/api/channels/{CHB}"
cross("GET", f"{c}/youtube", "youtube overview")
cross("PUT", f"{c}/branding", "branding", json={"title": "pwned"})
cross("GET", f"{c}/metrics", "metrics")
cross("POST", f"{c}/metrics/refresh", "metrics refresh")
# #216 gates subscriptions/subscribers behind a global flag (404 when OFF).
# Turn it ON here so these 404s prove the workspace check, not the flag.
_subs_flag_before = settings.youtube_subscriptions_admin
settings.youtube_subscriptions_admin = True
cross("GET", f"{c}/subscriptions", "subscriptions list")
cross("POST", f"{c}/subscriptions", "subscribe", json={"channel": "@x"})
cross("DELETE", f"{c}/subscriptions/sub123", "unsubscribe")
cross("GET", f"{c}/subscribers", "subscribers")
settings.youtube_subscriptions_admin = _subs_flag_before
cross("GET", f"{c}/monetization", "monetization")
cross("GET", f"{c}/video-analytics", "video analytics")
cross("GET", f"{c}/video-analytics/by-topic", "video analytics by topic")
cross("POST", f"{c}/video-analytics/refresh", "video analytics refresh")
r_missing = client.get("/api/channels/999999/metrics", headers=A)
r_foreign = client.get(f"{c}/metrics", headers=A)
ok(r_missing.status_code == r_foreign.status_code == 404
   and r_missing.json() == r_foreign.json(),
   "foreign channel 404 body == missing channel 404 body (no existence leak)")
r = client.get(f"{c}/metrics", headers=B)
ok(r.status_code == 200 and r.json()["latest"]["subscriber_count"] == 7,
   "positive control: B reads its own metrics (200)")
r = client.get(f"{c}/monetization", headers=B)
# S8a (L4) removed the YPP milestones: B's own channel answers 410 Gone (the
# workspace check runs first, so A→B above is still the 404).
ok(r.status_code == 410, "own channel: B's /monetization is 410 Gone after S8a (L4)")

# ---- topics -----------------------------------------------------------------
print("topics: buyer A on buyer B's topic/channel")
cross("GET", f"/api/topics?channel_id={CHB}", "topics list")
cross("POST", "/api/topics", "topic create on B's channel",
      json={"channel_id": CHB, "name": "x"})
cross("GET", f"/api/topics/{TB}", "topic get")
cross("PATCH", f"/api/topics/{TB}", "topic patch", json={"weight": 0})
cross("DELETE", f"/api/topics/{TB}", "topic delete")
cross("POST", "/api/topics/reorder", "topic reorder",
      json={"channel_id": CHB, "ordered_ids": [TB]})
cross("POST", f"/api/topics/{TB}/generate", "topic generate", json={"count": 1})
cross("POST", "/api/topics", "own topic linking B's playlist",
      json={"channel_id": CHA, "name": "steal-pl", "playlist_id": PLB})
cross("POST", "/api/topics", "own topic linking B's render profile",
      json={"channel_id": CHA, "name": "steal-prof", "render_profile_id": PB})
cross("PATCH", f"/api/topics/{TA}", "own topic re-pointed at B's playlist",
      json={"playlist_id": PLB})
r = client.delete("/api/topics/999999", headers=A)
ok(r.status_code == 404, "missing topic delete is the same 404 (no 204-vs-404 leak)")
r = client.get(f"/api/topics?channel_id={CHA}", headers=A)
ok(r.status_code == 200 and [t["id"] for t in r.json()] == [TA],
   "positive control: A lists only its own topic")
r = client.get(f"/api/topics/{TB}", headers=B)
ok(r.status_code == 200 and r.json()["name"] == "B topic", "positive control: B gets its topic")
r = client.post("/api/topics", headers=A,
                json={"channel_id": CHA, "name": "shared-prof", "render_profile_id": PSH})
ok(r.status_code == 201, "A may link the shared (channel-less) render profile")

# ---- playlists ----------------------------------------------------------------
print("playlists: buyer A on buyer B's channel")
cross("GET", f"/api/channels/{CHB}/playlists", "playlists list")
cross("POST", f"/api/channels/{CHB}/playlists/sync", "playlists sync")
cross("POST", f"/api/channels/{CHB}/playlists", "playlist create", json={"title": "x"})
cross("POST", f"/api/channels/{CHB}/request-deletion", "request deletion (drops token, starts purge)")
r = client.get(f"/api/channels/{CHB}/playlists", headers=B)
ok(r.status_code == 200 and len(r.json()) == 1, "positive control: B lists its playlist")

# ---- media --------------------------------------------------------------------
print("media: buyer A on buyer B's video files")
cross("GET", f"/api/videos/{VB}/video", "video stream")
cross("GET", f"/api/videos/{VB}/thumb", "thumb")
r = client.get(f"/api/videos/{VB}/video", headers=B)
ok(r.status_code == 200, "positive control: B streams its own video")

# ---- thumbnails ---------------------------------------------------------------
print("thumbnails: buyer A on buyer B's video")
cross("POST", f"/api/videos/{VB}/thumbnail", "provided thumbnail set",
      json={"path": "nope.png"})
cross("DELETE", f"/api/videos/{VB}/thumbnail", "provided thumbnail clear")

# ---- runs (GET /api/runs) ----------------------------------------------------
print("runs: buyer A does not see buyer B's runs")
r = client.get("/api/runs", headers=A)
_details = {x["detail"] for x in r.json()} if r.status_code == 200 else set()
ok(r.status_code == 200 and _details == {"A channel run"},
   f"A's /api/runs lists only A's run (got {sorted(_details)})")
cross("GET", f"/api/runs?channel_id={CHB}", "runs filtered by B's channel")
cross("GET", f"/api/runs?video_id={VB}", "runs filtered by B's video")
r_missing = client.get("/api/runs?channel_id=999999", headers=A)
r_foreign = client.get(f"/api/runs?channel_id={CHB}", headers=A)
ok(r_missing.status_code == r_foreign.status_code == 404
   and r_missing.json() == r_foreign.json(),
   "runs: foreign channel filter 404 body == missing channel 404 body")
r = client.get("/api/runs", headers=B)
_details = {x["detail"] for x in r.json()}
ok(r.status_code == 200 and _details == {"B channel run", "B video-only run"},
   "positive control: B sees its channel run and its channel-less video run, not the global one")
r = client.get("/api/runs", headers=ADMIN)
_details = {x["detail"] for x in r.json()}
ok(r.status_code == 200 and _details == {"global run"},
   "ws 1: only its own + global (channel- and video-less) runs, no buyer rows")
r = client.get("/api/agent/state", headers=A)
ok(r.status_code == 200 and {x["detail"] for x in r.json()["recent_runs"]} == {"A channel run"},
   "agent/state recent_runs uses the same scope (A sees only A's run)")
ok(r.status_code == 200 and {x["term"] for x in r.json()["trends"]} == {"A trend"},
   "agent/state trends: A sees only A's trend (no B, no admin research)")
r = client.get("/api/agent/state", headers=ADMIN)
ok(r.status_code == 200 and {x["term"] for x in r.json()["trends"]} == {"Research", "Owera trend"},
   "agent/state trends: ws 1 sees its own + channel-less research, no buyer rows")

# ---- DELETE /api/videos/{id}: missing == foreign ------------------------------
print("videos: DELETE on a foreign id and on a missing id look the same")
r_missing = client.delete("/api/videos/999999", headers=A)
r_foreign = client.delete(f"/api/videos/{VB}", headers=A)
ok(r_missing.status_code == r_foreign.status_code == 404
   and r_missing.json() == r_foreign.json(),
   "DELETE video: foreign 404 body == missing 404 body (missing was 204)")

# ---- music (shared BGM pool, admin-only) --------------------------------------
print("music: the shared BGM pool is admin-only")
cross("GET", "/api/music", "music list")
cross("POST", "/api/music/generate", "music generate", json={"count": 1})
cross("DELETE", "/api/music/x.wav", "music delete")

# ---- trends -------------------------------------------------------------------
print("trends: buyer A on buyer B's / admin's trends")
r = client.get("/api/trends", headers=A)
ids = {t["id"] for t in r.json()}
ok(r.status_code == 200 and ids == {TRA},
   "A's trend list has only A's trend (no B, no admin research)")
cross("GET", f"/api/trends?channel_id={CHB}", "trends filtered by B's channel")
cross("GET", f"/api/trends/{TRB}", "trend get")
cross("PATCH", f"/api/trends/{TRB}", "trend patch", json={"score": 0})
cross("POST", f"/api/trends/{TRB}/adopt", "trend adopt", json={})
cross("GET", f"/api/trends/{TRNULL}", "admin research trend get")
cross("POST", "/api/trends", "trend upsert onto B's channel",
      json={"term": "zzz", "channel_id": CHB})
cross("PATCH", f"/api/trends/{TRA}", "own trend moved to B's channel",
      json={"channel_id": CHB})
cross("POST", f"/api/trends/{TRA}/adopt", "own trend adopted into B's channel",
      json={"channel_id": CHB})
r = client.post("/api/trends", headers=A, json={"term": "B Trend", "channel_id": CHA,
                                                 "score": 99})
ok(r.status_code == 201 and r.json()["id"] != TRB,
   "A upserting B's term creates A's own row (dedupe is per workspace)")
r = client.post("/api/trends", headers=A, json={"term": "orphan"})
ok(r.status_code == 400, "A cannot create a channel-less (admin research) trend")
r = client.get("/api/trends", headers=ADMIN)
ids = {t["id"] for t in r.json()}
ok(TRNULL in ids and TR1 in ids and TRB not in ids and TRA not in ids,
   "admin list: own + channel-less research, no buyer rows")

# ---- profiles -----------------------------------------------------------------
print("profiles: buyer A on buyer B's / shared profiles")
r = client.get("/api/profiles", headers=A)
names = {p["name"] for p in r.json()}
ok(r.status_code == 200 and "B look" not in names and "Default" in names,
   "A's profile list: shared yes, B's no")
cross("GET", f"/api/profiles?channel_id={CHB}", "profiles filtered by B's channel")
cross("POST", "/api/profiles", "profile create on B's channel",
      json={"name": "x", "channel_id": CHB})
cross("GET", f"/api/profiles/{PB}", "profile get")
cross("PATCH", f"/api/profiles/{PB}", "profile patch", json={"name": "pwned"})
cross("DELETE", f"/api/profiles/{PB}", "profile delete")
cross("PATCH", f"/api/profiles/{PSH}", "shared profile patch by a buyer", json={"name": "pwned"})
cross("DELETE", f"/api/profiles/{PSH}", "shared profile delete by a buyer")
r = client.get(f"/api/profiles/{PSH}", headers=A)
ok(r.status_code == 200, "A can read the shared profile")
r = client.post("/api/profiles", headers=A, json={"name": "shared?"})
ok(r.status_code == 400, "A cannot create a shared (channel-less) profile")
r = client.get(f"/api/profiles/{PB}", headers=B)
ok(r.status_code == 200, "positive control: B reads its own profile")

# ---- settings -----------------------------------------------------------------
print("settings: global row is admin-only; buyers pause only themselves")
r = client.get("/api/settings", headers=A)
body = r.json()
ok(r.status_code == 200 and body.get("scope") == "workspace"
   and "render_concurrency" not in body and "mpt_base_url" not in body
   and body["scheduler_paused"] is False,
   "A GET /api/settings is a filtered workspace view (no global knobs)")
for key, val in (("render_concurrency", 4), ("publish_drip_minutes", 0),
                 ("topic_autogen_enabled", True), ("topic_autogen_target", 99)):
    r = client.patch("/api/settings", headers=A, json={key: val})
    ok(r.status_code == 404, f"A PATCH global {key} is 404")
r = client.patch("/api/settings", headers=A,
                 json={"scheduler_paused": True, "render_concurrency": 3})
ok(r.status_code == 404, "A PATCH mixing a global key is 404 (nothing applied)")
with Session(engine) as s:
    ok(s.get(Workspace, 2).scheduler_paused is False, "…and A's pause was not applied either")
r = client.patch("/api/settings", headers=A, json={"scheduler_paused": True})
ok(r.status_code == 200 and r.json()["scheduler_paused"] is True,
   "A PATCH scheduler_paused pauses its own workspace")
with Session(engine) as s:
    ok(s.get(Settings, 1).scheduler_paused is False, "global scheduler stays running")
    ok(s.get(Workspace, 3).scheduler_paused is False, "B's workspace stays running")
    ok(paused_workspace_ids(s) == {2}, "paused_workspace_ids == {A}")
    ok(channel_workspace_paused(s, s.get(Channel, CHA)), "A's channel is paused")
    ok(not channel_workspace_paused(s, s.get(Channel, CHB)), "B's channel is not")
    ok(not channel_workspace_paused(s, s.get(Channel, CH1)), "ws 1's channel is not")
r = client.get("/api/settings", headers=B)
ok(r.json()["scheduler_paused"] is False, "B's view shows B not paused")
r = client.get("/api/agent/state", headers=A)
ok(r.status_code == 200 and "render_concurrency" not in r.json()["settings"],
   "A's /api/agent/state carries the filtered settings, not the global row")
with Session(engine) as s:
    qa = Video(channel_id=CHA, topic_id=TA, subject="A queued", status=VideoStatus.QUEUED)
    s.add(qa)
    s.commit()
    s.refresh(qa)
    QA = qa.id
r = client.get(f"/api/videos/queue-plan?channel_id={CHA}", headers=A)
ok(r.status_code == 200 and r.json().get(str(QA), {}).get("reason") == "workspace paused",
   "A's queue plan labels its queued card 'workspace paused'")

print("loops: a paused workspace is skipped, the others are not")
from app.services import render_loop  # noqa: E402
SUBJ_A = "Vector search beats keyword search for bots"
SUBJ_B = "How small teams ship weekly videos without burning out"
with Session(engine) as s:
    s.delete(s.get(Video, QA))           # A's queue empty: only the pause can hold it
    s.add(Video(channel_id=CHA, topic_id=TA, subject=SUBJ_A, status=VideoStatus.DRAFT))
    s.add(Video(channel_id=CHB, topic_id=TB, subject=SUBJ_B, status=VideoStatus.DRAFT))
    s.commit()
    render_loop._auto_produce(s)
    s.commit()
    a_draft = s.exec(select(Video).where(Video.subject == SUBJ_A)).one()
    b_draft = s.exec(select(Video).where(Video.subject == SUBJ_B)).one()
    ok(a_draft.status == VideoStatus.DRAFT,
       "render auto-produce leaves the paused workspace's draft alone")
    ok(b_draft.status == VideoStatus.QUEUED,
       "render auto-produce still queues the running workspace's draft")

r = client.patch("/api/settings", headers=A, json={"scheduler_paused": False})
ok(r.status_code == 200 and r.json()["scheduler_paused"] is False, "A resumes itself")
with Session(engine) as s:
    render_loop._auto_produce(s)
    s.commit()
    a_draft = s.exec(select(Video).where(Video.subject == SUBJ_A)).one()
    ok(a_draft.status == VideoStatus.QUEUED,
       "control: once A resumes, the same draft is auto-produced")
    for v in s.exec(select(Video).where(Video.subject.in_([SUBJ_A, SUBJ_B]))).all():
        s.delete(v)
    s.commit()

print("settings: admin (ws 1) unchanged")
r = client.get("/api/settings", headers=ADMIN)
body = r.json()
ok(r.status_code == 200 and body.get("scope") == "global" and "render_concurrency" in body
   and "mpt_base_url" in body and "youtube_quota_reset_at" in body,
   "admin GET is the full global row (+ scope=global)")
r = client.patch("/api/settings", headers=ADMIN, json={"render_concurrency": 2})
ok(r.status_code == 200 and r.json()["render_concurrency"] == 2, "admin PATCH global knob")
r = client.patch("/api/settings", headers=ADMIN, json={"render_concurrency": 0})
ok(r.status_code == 400, "admin validation unchanged (render_concurrency 0 → 400)")
r = client.patch("/api/settings", headers=ADMIN, json={"scheduler_paused": True})
ok(r.status_code == 200 and r.json()["scheduler_paused"] is True,
   "admin scheduler_paused is still the global kill switch")
client.patch("/api/settings", headers=ADMIN, json={"scheduler_paused": False,
                                                   "render_concurrency": 1})
r = client.get("/api/agent/state", headers=ADMIN)
ok(r.status_code == 200 and "render_concurrency" in r.json()["settings"],
   "admin /api/agent/state still carries the global settings")

# ---- ws 1 regression ----------------------------------------------------------
print("ws 1 (owera) regression")
r = client.get(f"/api/topics?channel_id={CH1}", headers=ADMIN)
ok(r.status_code == 200 and [t["id"] for t in r.json()] == [T1], "ws 1 lists its topics")
r = client.get(f"/api/channels/{CH1}/metrics", headers=ADMIN)
ok(r.status_code == 200, "ws 1 reads its channel metrics")
r = client.patch(f"/api/profiles/{PSH}", headers=ADMIN, json={"name": "Default"})
ok(r.status_code == 200, "ws 1 (admin) still edits the shared Default profile")
r = client.get(f"/api/topics/{TB}", headers=ADMIN)
ok(r.status_code == 404, "ws 1 does not see buyer topics either (strict isolation)")

# ---- nothing of B was touched -------------------------------------------------
after = snapshot()
for k in before:
    if k == "settings":
        continue  # admin PATCHed and restored it above
    ok(before[k] == after[k], f"B/shared state unchanged after the attack: {k}")
ok(after["settings"]["render_concurrency"] == 1 and not after["settings"]["scheduler_paused"],
   "global settings back to baseline (only the admin changed them)")
ok(_touched == [], "no YouTube token / analytics / LLM call for any foreign request")

# ---- restore ------------------------------------------------------------------
(youtube.get_service, analytics_loop._snapshot_channel,
 metrics_loop.record_snapshot, video_gen.generate_ideas) = _orig
settings.multi_workspace, settings.allow_workspace_header, settings.app_password = _saved
main.app.dependency_overrides.clear()
print(f"\nALL {_checks} CHECKS PASSED")
