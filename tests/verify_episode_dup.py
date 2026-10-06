"""Duplicate episode number check (P1, council 06/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_episode_dup.py

Episode numbers live in the title (else subject) suffix "· <Series> NN" (the
representation video_gen.renumber_series continues from). VM Gate B 06/10:
#1400 "duplicate Shipping 1 (#1313)", #1402 "duplicate Shipping 3 (#1311)".
Pins:
  * parsing: last "· Series N" group, label case/space-insensitive;
  * number repeated within the title → blocked (two suffixes, "Series N" in
    the head, "Ep N"/"Episode N"/"#N" marker in the head);
  * number already used in that series on the channel by an approved /
    publishing / published row → blocked; other channel, other series,
    rejected / failed / review siblings, longs with no number → not blocked;
  * wired at approve (409), render finalize (skip-gate auto-approve parks in
    review), publish loop (parks in review, never uploads), review_ready
    digest and retry-republish.
In-memory DB; the craft gate is stubbed to pass so only this check decides.
"""
import inspect
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.routers import videos as videos_router
from app.services import craft, episode_guard, issues, publish_loop, render_loop

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("parsing")
ok(episode_guard.episode_of("The preview passed. · Shipping 1") == ("shipping", "Shipping", 1),
   "'· Shipping 1' → (shipping, Shipping, 1)")
ok(episode_guard.episode_of("Chat chose daily digest. Prod sent each alert. · Agent memory 46")
   == ("agent memory", "Agent memory", 46), "multi-word series label")
ok(episode_guard.episode_of("x ·  agent   MEMORY  046 ")[0::2] == ("agent memory", 46),
   "case, extra spaces and zero padding normalised")
ok(episode_guard.episode_of("PDF escaneado não é engenharia. · IA 217")[2] == 217, "RR '· IA 217'")
ok(episode_guard.episode_of("Ollama idle holds 7GB") is None
   and episode_guard.episode_of("") is None and episode_guard.episode_of(None) is None,
   "no suffix → no episode (digits in the head are not an episode)")

print("number repeated within the title")
R = episode_guard.title_repeat_reason
ok(R("The preview passed. · Shipping 3") is None, "plain title → not repeated")
ok(R("Chat kept logs 7 days. Prod kept 90. · Agent memory 50") is None,
   "digits in the claim are not an episode marker")
ok(R("Chat stored UTC. · Agent memory 56 · Agent memory 56") is not None
   and "2 series numbers" in R("Chat stored UTC. · Agent memory 56 · Agent memory 56"),
   "two '· Series N' suffixes → repeated")
ok(R("x · Shipping 3 · Shipping 4") is not None, "two different suffix numbers → repeated")
ok(R("Shipping 3: the preview passed. · Shipping 3") is not None
   and "repeated in the title head" in R("Shipping 3: the preview passed. · Shipping 3"),
   "'Shipping 3' again in the head → repeated")
ok(R("shipping 03 — the preview passed · Shipping 3") is not None, "head repeat is case/zero-pad insensitive")
for head in ("Ep 3 — the preview passed", "Ep. 3: the preview passed", "Episode 3: the preview passed",
             "Episódio 3: o preview passou", "#3 The preview passed"):
    ok(R(head + " · Shipping 3") is not None, f"episode marker in head ({head.split()[0]}) → repeated")
ok(R("Shipping 4 lessons. · Shipping 3") is None,
   "a different number after the series word in the head is content, not a repeat")
ok(R("Shipping 3 the preview passed") is None, "no suffix → nothing to repeat")

print("number already used in the series on the channel")
import tempfile
from pathlib import Path
tmp = Path(tempfile.mkdtemp(prefix="ep-dup-"))
src = tmp / "out.mp4"
src.write_bytes(b"mp4")
MP4 = str(src)
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Channel(slug="owera", name="Owera Software", oauth_status=OAuthStatus.CONNECTED,
                  default_skip_gate=True))
    s.add(Channel(slug="rr", name="RR", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
    s.add(Topic(channel_id=1, name="Agent memory", content_format="short"))
    s.add(Topic(channel_id=2, name="IA", content_format="short"))
    s.commit()


def add(**kw):
    with Session(engine) as s:
        v = Video(**{"channel_id": 1, "topic_id": 1, "subject": kw.get("title") or "x",
                     "script": "x.", "metadata_generated": True, **kw})
        s.add(v)
        s.commit()
        s.refresh(v)
        return v.id


def reason(vid):
    with Session(engine) as s:
        return episode_guard.duplicate_episode_reason(s, s.get(Video, vid))


def get(vid):
    with Session(engine) as s:
        v = s.get(Video, vid)
        s.expunge(v)
        return v


pub1 = add(title="The preview passed. · Agent memory 1", status=VideoStatus.PUBLISHED)       # like #1313
dup1 = add(title="Caption typed, screen left. · Agent memory 1", status=VideoStatus.REVIEW,
           video_path=MP4)                                                         # like #1400
r = reason(dup1)
ok(r is not None and r.startswith("duplicate episode number") and f"#{pub1}" in r and "Agent memory 1" in r,
   "review row reusing a PUBLISHED 'Agent memory 1' → blocked, names the holder")
ok(reason(pub1) is None, "the published holder itself is never blocked by the newer duplicate")
lower = add(title="x · agent MEMORY 1", status=VideoStatus.REVIEW)
ok(reason(lower) is not None, "series label match is case-insensitive")
appr = add(title="One. · Agent memory 2", status=VideoStatus.APPROVED)
dup2 = add(title="Two. · Agent memory 2", status=VideoStatus.REVIEW)
ok(reason(dup2) is not None and "(approved)" in reason(dup2), "an APPROVED row holds its number")
pubing = add(title="Three. · Agent memory 3", status=VideoStatus.PUBLISHING)
ok(reason(add(title="Four. · Agent memory 3", status=VideoStatus.REVIEW)) is not None, "a PUBLISHING row holds its number")
for st in (VideoStatus.REJECTED, VideoStatus.FAILED, VideoStatus.REVIEW, VideoStatus.QUEUED, VideoStatus.DRAFT):
    add(title=f"Old {st}. · Agent memory 9", status=st)
ok(reason(add(title="New. · Agent memory 9", status=VideoStatus.REVIEW)) is None,
   "rejected / failed / review / queued / draft siblings do not hold the number")
ok(reason(add(title="Other series. · Agent traps 1", status=VideoStatus.REVIEW)) is None,
   "same number in another series → fine")
ok(reason(add(channel_id=2, topic_id=2, title="Outro canal. · Agent memory 1", status=VideoStatus.REVIEW)) is None,
   "same series + number on another channel → fine")
ok(reason(add(title=None, subject="Subject only. · Agent memory 1", status=VideoStatus.REVIEW)) is not None,
   "no title yet → the subject's suffix is checked")
ok(reason(add(title="No number at all", status=VideoStatus.REVIEW)) is None, "no episode number → fine")
ok(reason(add(title="Ep 7: x · Agent memory 7", status=VideoStatus.REVIEW)).startswith("duplicate episode number"),
   "a title repeat blocks even without a sibling")

print("approve (409)")


def _sess():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _sess
_pw = settings.app_password
settings.app_password = "pw"
client = TestClient(main.app)
AUTH = ("admin", "pw")
stubs = [patch.object(craft, "publish_craft_block_reason", return_value=None)]
for p in stubs:
    p.start()
try:
    resp = client.post(f"/api/videos/{dup1}/approve", auth=AUTH)
    ok(resp.status_code == 409 and "duplicate episode number" in resp.text,
       f"approve of the duplicate → 409 ({resp.status_code})")
    v = get(dup1)
    ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_FAIL,
       "the duplicate stays in review with craft_review=fail")
    a = add(title="Fresh. · Agent memory 10", status=VideoStatus.REVIEW, video_path=MP4)
    b = add(title="Twin. · Agent memory 10", status=VideoStatus.REVIEW, video_path=MP4)
    ok(client.post(f"/api/videos/{a}/approve", auth=AUTH).status_code == 200,
       "two review rows with the same number: the first approve wins")
    ok(client.post(f"/api/videos/{b}/approve", auth=AUTH).status_code == 409,
       "…and the second approve is blocked")
    with Session(engine) as s:
        vb = s.get(Video, b)
        vb.title = "Twin. · Agent memory 11"
        s.add(vb)
        s.commit()
    ok(client.post(f"/api/videos/{b}/approve", auth=AUTH).status_code == 200,
       "renumbered to a free number → approve passes")

    print("render finalize (skip-gate auto-approve)")

    class _Eng:
        def final_path(self, task_id):
            return src

    def finalize(vid):
        _sd = settings.storage_dir
        settings.storage_dir = str(tmp / f"st-{vid}")
        try:
            with patch.object(render_loop, "_has_visible_frames", return_value=True), \
                    patch.object(render_loop, "_make_thumbnail", return_value=False):
                with Session(engine) as s:
                    v = s.get(Video, vid)
                    v.status = VideoStatus.RENDERING
                    render_loop._finalize(s, v, s.get(Channel, 1), _Eng(),
                                          {"creation_config": {"beats": []}})
                    s.add(v)
                    s.commit()
        finally:
            settings.storage_dir = _sd
        return get(vid)

    rd = add(title="Render dup. · Agent memory 2", status=VideoStatus.QUEUED, mpt_task_id="t-rd")
    v = finalize(rd)
    ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_FAIL
       and "duplicate episode number" in (v.error or ""),
       "skip-gate channel: a duplicate render is parked in review (not auto-approved)")
    rf = add(title="Render fresh. · Agent memory 12", status=VideoStatus.QUEUED, mpt_task_id="t-rf")
    v = finalize(rf)
    ok(v.status == VideoStatus.APPROVED and v.craft_review == craft.CRAFT_REVIEW_PASS,
       "a free number still auto-approves on a skip-gate channel")

    print("publish loop")
    pd = add(title="Publish dup. · Agent memory 1", status=VideoStatus.APPROVED,
             craft_review=craft.CRAFT_REVIEW_PASS, video_path=str(src))
    uploaded = []
    with patch.object(publish_loop, "youtube_upload", create=True,
                      side_effect=lambda *a, **k: uploaded.append(1)):
        with Session(engine) as s:
            publish_loop._publish_one(s, s.get(Channel, 1), s.get(Video, pd))
    v = get(pd)
    ok(v.status == VideoStatus.REVIEW and v.craft_review == craft.CRAFT_REVIEW_FAIL
       and v.approved_at is None and "duplicate episode number" in (v.error or "") and not uploaded,
       "an approved duplicate is parked back in review before upload (never rejected, never uploaded)")
    ok("episode_guard.duplicate_episode_reason(session, video)" in inspect.getsource(publish_loop._publish_one)
       and inspect.getsource(publish_loop._publish_one).index("duplicate_episode_reason")
       < inspect.getsource(publish_loop._publish_one).index("VideoStatus.PUBLISHING"),
       "publish loop checks the episode before flipping to PUBLISHING")

    print("review_ready digest + retry")
    dg = add(title="Digest dup. · Agent memory 1", status=VideoStatus.REVIEW, video_path=MP4)
    with Session(engine) as s:
        rr = issues.review_ready_reason(s, s.get(Video, dg))
    ok(rr is not None and "duplicate episode number" in rr, "review_ready digest reports the duplicate")
    with Session(engine) as s:
        ok(issues.review_ready_reason(s, s.get(Video, add(title="Ready. · Agent memory 20",
                                                           status=VideoStatus.REVIEW,
                                                           video_path=MP4))) is None,
           "a free number is review-ready")
    src_r = inspect.getsource(videos_router.retry)
    ok("episode_guard.duplicate_episode_reason(session, v)" in src_r,
       "retry-republish (approved again with the artifact) runs the same check")
    ok("episode_guard.duplicate_episode_reason(session, v)" in inspect.getsource(videos_router.approve),
       "approve runs the check after the craft gate")
finally:
    for p in stubs:
        p.stop()
    settings.app_password = _pw
    main.app.dependency_overrides.pop(get_session, None)

print()
print(f"ALL {_checks} CHECKS PASSED")
