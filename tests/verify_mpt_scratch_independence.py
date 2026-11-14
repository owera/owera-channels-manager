"""S2 / PO CM 14/11 — nothing after an MPT render depends on the MPT scratch folder.

Run: MANAGER_DIR=$(mktemp -d) PYTHONPATH=. uv run python tests/verify_mpt_scratch_independence.py

MPT retention (app/services/mpt_retention.py) deletes an EXTERNAL workspace's
``<mpt_storage_dir>/<task_id>/`` 7 days after the job finished, published
videos included. PO CM's condition: no re-render, edit, publish / upload,
thumbnail, chapters, Shorts endcard, analytics, Gate B / craft check or
activity link may read that folder after the render. This test:

1. renders an MPT video through the real ``render_loop._finalize`` (real ffmpeg
   on a 3 s clip, no LLM: metadata already generated), so the artifacts land in
   ``storage/videos/<id>/`` (video.mp4, thumb.jpg, and index.html if MPT wrote one);
2. records each flow's result with the scratch present;
3. deletes the scratch through ``mpt_retention.tick`` (8 days later, published);
4. re-runs every flow (YouTube / thumbnail LLM / MPT HTTP mocked; no network,
   no paid call) and checks the same result, read from ``storage/videos/<id>``;
5. a static audit: the only post-render readers of the MPT scratch are the
   render-completion copy and nothing else.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

os.environ.setdefault("MANAGER_TOPIC_FLAGS", '{"vm_pass_required": []}')

import app.db as appdb  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (Channel, OAuthStatus, Topic, Video, VideoStatus, Workspace,  # noqa: E402
                        utcnow)
from app.services import (chapters, craft, mpt_retention, publish_loop, render_loop,  # noqa: E402
                          review_guard, youtube)
from app.services.engines import get_engine, storyboard, theme  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


TMP = Path(tempfile.mkdtemp(prefix="s2-scratch-"))
STORAGE, MPT = TMP / "storage", TMP / "mpt-tasks"
(STORAGE / "videos").mkdir(parents=True)
MPT.mkdir()
settings.storage_dir = str(STORAGE)
settings.mpt_storage_dir = str(MPT)
settings.purge_media_dry_run = False

eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
appdb.engine = eng
s = Session(eng)

WS = 7   # external workspace (not 1/2, not in MANAGER_LLM_INTERNAL_WORKSPACE_IDS)
s.add(Workspace(id=WS, slug="buyer-x", name="Buyer X"))
ch = Channel(slug="buyer-x-ch", name="Buyer X channel", workspace_id=WS, oauth_status=OAuthStatus.CONNECTED)
s.add(ch); s.commit(); s.refresh(ch)
tp = Topic(channel_id=ch.id, name="Cache tips", content_format="long")
s.add(tp); s.commit(); s.refresh(tp)


def mp4(path: Path, seconds: int = 3) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, timeout=60)


# A long-form storyboard index.html (the chapters source), as if MPT wrote one.
BEATS = [{"type": "hook", "cue": "", "text": "Cache misses cost money", "emoji": "", "start": 0.0, "dur": 12.0},
         {"type": "statement", "cue": "", "text": "Measure first", "w": 2, "emoji": "", "start": 12.0, "dur": 12.0},
         {"type": "quote", "cue": "", "text": "Then migrate", "attribution": "", "start": 24.0, "dur": 12.0},
         {"type": "cta", "cue": "", "text": "Follow", "sub": "", "start": 36.0, "dur": 9.0}]
HTML = storyboard.build_index_html(BEATS, theme.resolve(3, "scratch test"), "1080p", 1920, 1080, 45.0)
EXPECTED_CHAPTERS = chapters.chapter_lines(HTML)


def render(task: str, title: str) -> Video:
    """MPT task folder → real render_loop._finalize (no LLM: metadata already set)."""
    d = MPT / task
    d.mkdir()
    mp4(d / "final-1.mp4")
    (d / "audio.mp3").write_bytes(b"ID3")
    (d / "script.json").write_text("{}")
    (d / "index.html").write_text(HTML)
    v = Video(channel_id=ch.id, topic_id=tp.id, subject="Cache misses", title=title,
              script="Cache misses cost money. Measure first. Then migrate.",
              description="Why cache misses cost money.", tags_json='["cache"]',
              status=VideoStatus.RENDERING, engine="mpt", mpt_task_id=task, metadata_generated=True)
    s.add(v); s.commit(); s.refresh(v)
    render_loop._finalize(s, v, ch, get_engine("mpt"), {"state": 4, "script": v.script})
    s.add(v); s.commit(); s.refresh(v)
    return v


print("1. render completion copies everything later flows need into storage/videos/<id>")
v = render("task-aaa111", "Cache misses cost money · Cache tips 1")
VD = STORAGE / "videos" / str(v.id)
ok(v.video_path == str(VD / "video.mp4") and Path(v.video_path).is_file(), "video.mp4 in storage/videos/<id>")
ok(v.thumb_path == str(VD / "thumb.jpg") and Path(v.thumb_path).is_file(), "render still thumb.jpg in storage/videos/<id>")
ok((VD / "index.html").read_text() == HTML, "MPT index.html (chapters source) copied next to video.mp4")
ok(v.status in (VideoStatus.REVIEW, VideoStatus.APPROVED), f"finalized normally (status={v.status})")


def flows(video: Video) -> dict:
    """Every post-render flow, with YouTube / LLM / MPT HTTP mocked."""
    out = {}
    out["chapters"] = chapters.chapter_lines_for_video(video)
    out["has_audio"] = craft.probe_has_audio(video.video_path)
    out["craft_gate"] = craft.publish_craft_block_reason(
        title=video.title, script=video.script, creation_config=video.creation_config,
        content_format="long", video_path=video.video_path, description=video.description,
        description_required=False, video_id=video.id, video=video, brand=None)
    out["fingerprint_ok"] = review_guard.artifact_fingerprint(video.video_path) is not None
    out["stale_marker"] = json.loads(craft.mark_stale_render(
        video.creation_config, video_path=video.video_path, fields=["script"]))[craft.STALE_RENDER_KEY]["video_path"]
    from app.routers import media
    out["stream"] = media.stream_video(video.id, session=s, ws=s.get(Workspace, WS)).path
    return out


before = flows(v)
ok(before["chapters"] == EXPECTED_CHAPTERS and before["chapters"], "chapters from storage index.html")
ok(before["has_audio"] is True, "Gate B mute probe reads storage video.mp4")

print("2. MPT retention deletes the scratch of the (published) finished video")
v.status, v.yt_video_id, v.published_at = VideoStatus.PUBLISHED, "yt-old", utcnow()
s.add(v); s.commit()
old = (utcnow() - timedelta(days=8)).timestamp()
for f in (MPT / "task-aaa111").rglob("*"):
    os.utime(f, (old, old))
n = mpt_retention.tick(dry_run=False)
ok(n >= 1 and not (MPT / "task-aaa111").exists(), "scratch mpt_storage_dir/task-aaa111 deleted")
ok(Path(v.video_path).is_file() and (VD / "index.html").is_file() and Path(v.thumb_path).is_file(),
   "storage/videos/<id> untouched")
s.refresh(v)

print("3. every flow still works, from storage/videos/<id>")
after = flows(v)
for k in before:
    ok(after[k] == before[k], f"{k}: same result without the scratch ({str(after[k])[:60]})")
ok(after["stream"] == v.video_path, "review player / activity media link streams storage/videos/<id>/video.mp4")

# Thumbnail regen (custom thumbnail at publish) writes next to video.mp4.
_thumb_out = []


def _fake_png(subject, title, out_png, **k):
    Path(out_png).write_bytes(b"\x89PNG")
    _thumb_out.append(Path(out_png))
    return Path(out_png)


with patch.object(publish_loop.thumbnail, "make_thumbnail_png", _fake_png), \
     patch.object(youtube, "set_thumbnail", lambda *a, **k: None, create=True):
    publish_loop._set_custom_thumbnail(s, object(), ch, v, "yt-old")
ok(_thumb_out and _thumb_out[0].parent == VD, "thumbnail regen writes storage/videos/<id>/thumb_custom.png")

# Re-upload / publish of a second finished video whose scratch is gone too.
print("4. publish / upload of a finished video with its scratch already deleted")
v2 = render("task-bbb222", "Measure first then migrate · Cache tips 2")
shutil.rmtree(MPT / "task-bbb222")
v2.status, v2.craft_review = VideoStatus.APPROVED, craft.CRAFT_REVIEW_PASS
s.add(v2); s.commit()
_up = {}


def _upload(service, path, title, desc, tags, privacy, **k):
    _up.update(path=path, exists=Path(path).is_file(), desc=desc)
    return "yt-new"


with patch.object(publish_loop.craft, "publish_craft_block_reason", lambda **k: None), \
     patch("app.services.episode_guard.duplicate_episode_reason", lambda *a, **k: None), \
     patch.object(review_guard, "vm_pass_required_reason", lambda v: None), \
     patch.object(youtube, "get_service", lambda *a, **k: object()), \
     patch.object(youtube, "upload_video", _upload), \
     patch.object(youtube, "insert_comment", lambda *a, **k: "c1"), \
     patch.object(youtube, "set_thumbnail", lambda *a, **k: None, create=True), \
     patch("app.services.topic_playlist.ensure_topic_playlist", lambda *a, **k: None), \
     patch.object(publish_loop.video_gen, "channel_language_code", lambda *a, **k: "en"), \
     patch.object(publish_loop.thumbnail, "make_thumbnail_png", _fake_png):
    publish_loop._publish_one(s, ch, v2)
s.refresh(v2)
ok(v2.status == VideoStatus.PUBLISHED and v2.yt_video_id == "yt-new", "published")
ok(_up["path"] == v2.video_path and _up["exists"], "upload_video got storage/videos/<id>/video.mp4")
ok(all(ln in (_up["desc"] or "") for ln in EXPECTED_CHAPTERS), "long-form chapters in the description (storage index.html)")

print("5. re-render: a requeue clears the handle; the new render uses a NEW task folder")
from app.routers.videos import _clear_render_artifact  # noqa: E402
fields = _clear_render_artifact(v2)
ok(fields["mpt_task_id"] is None and fields["video_path"] is None, "requeue drops the old MPT handle (never reused)")
for k_, val in fields.items():
    setattr(v2, k_, val)
v2.status, v2.engine, v2.mpt_task_id, v2.metadata_generated = VideoStatus.RENDERING, "mpt", "task-ccc333", True
d3 = MPT / "task-ccc333"
d3.mkdir()
mp4(d3 / "final-1.mp4")
s.add(v2); s.commit()
render_loop._finalize(s, v2, ch, get_engine("mpt"), {"state": 4, "script": v2.script})
s.commit(); s.refresh(v2)
ok(v2.video_path == str(STORAGE / "videos" / str(v2.id) / "video.mp4") and Path(v2.video_path).is_file(),
   "re-render finalizes from the new task folder into storage/videos/<id>")

print("6. static audit: nothing after the render reads the MPT scratch")
APP = ROOT / "app"
pat = re.compile(r"mpt_storage_dir|local_final_path|final_path\(")
hits = {}
for f in APP.rglob("*.py"):
    for i, line in enumerate(f.read_text().splitlines(), 1):
        if pat.search(line):
            hits.setdefault(str(f.relative_to(ROOT)), []).append(line.strip())
ALLOWED = {
    "app/config.py",                       # the setting
    "app/services/mpt_client.py",          # local_final_path definition
    "app/services/engines/base.py",        # protocol doc
    "app/services/engines/mpt.py",         # adapter: final_path → local_final_path
    "app/services/engines/hyperframes.py",  # HF job dir (not MPT scratch)
    "app/services/render_loop.py",         # _finalize: the render-completion copy
    "app/services/chapters.py",            # HyperFrames branch only (MPT reads storage)
    "app/services/youtube_purge.py",       # deletion purge
    "app/services/mpt_retention.py",       # retention
}
ok(set(hits) <= ALLOWED, f"only the known modules touch the scratch path ({sorted(set(hits) - ALLOWED)})")
rl = inspect.getsource(render_loop)
ok(rl.count("final_path(") == 1 and "final_path(" in inspect.getsource(render_loop._finalize),
   "render_loop: final_path only in _finalize (render completion, status still RENDERING)")
ch_src = inspect.getsource(chapters.chapter_lines_for_video)
ok('== "mpt"' in ch_src and "Path(vp).parent" in ch_src, "chapters: MPT reads storage/videos/<id>/index.html")
ok(VideoStatus.RENDERING in mpt_retention.RUNNING and VideoStatus.QUEUED in mpt_retention.RUNNING,
   "retention never deletes the folder of a queued / rendering job (the only time it is read)")
for mod in ("publish_loop", "metadata", "thumbnail", "provided_thumb", "craft", "review_guard",
            "gate_b_preflight", "gate_grandfather", "analytics", "metrics", "issues", "activation",
            "engines/worker", "engines/storyboard", "sentence_pace", "video_gen"):
    p = APP / "services" / f"{mod}.py"
    if p.exists():
        ok(not pat.search(p.read_text()), f"{mod}: no MPT scratch read")
for r in (APP / "routers").glob("*.py"):
    ok(not pat.search(r.read_text()), f"routers/{r.name}: no MPT scratch read")
fe = "\n".join(p.read_text() for p in (ROOT / "frontend/src").rglob("*.tsx"))
fe_all = fe + (ROOT / "frontend/src/api.ts").read_text()
ok("mpt_task_id" not in fe and "storage/tasks" not in fe_all and "mpt_storage" not in fe_all,
   "SPA / activity log: no page or link uses the MPT task folder (api.ts only types the opaque id)")

print(f"\nALL {_checks} CHECKS PASSED")
