"""Regression checks for operator-provided thumbnails.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_provided_thumb.py

Covers POST/DELETE /api/videos/{id}/thumbnail (multipart + JSON path form),
validation (type sniffing, size floor, 25 MB input cap, >2 MB → JPEG
re-encode), status gating (409 once publishing/published), render finalize
never clobbering a provided thumb with the 1s still, publish uploading the
provided file and skipping template generation (absent → unchanged), PATCH
/craft keeping the marker, media GET content-type, and JobRun audit rows.

In-memory SQLite, throwaway storage_dir, no network. Exits non-zero on the
first failed assertion.
"""
from __future__ import annotations

import atexit
import inspect
import json
import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus, Workspace
from app.routers import thumbnails as thumb_router
from app.services import provided_thumb as pt
from app.services import publish_loop, render_loop, youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(pt.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "provided_thumb loaded from this tree")

_TMP = Path(tempfile.mkdtemp(prefix="verify-pthumb-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
_STORE = _TMP / "storage"
_STORE.mkdir()
_orig_storage = settings.storage_dir
settings.storage_dir = str(_STORE)
atexit.register(setattr, settings, "storage_dir", _orig_storage)


# --- image fixtures (pure Python) --------------------------------------------
def png_bytes(w, h, noise=False):
    raw = bytearray()
    row_len = w * 3
    for y in range(h):
        raw.append(0)
        if noise:
            raw += os.urandom(row_len)
        else:
            raw += bytes([(y * 7) & 0xFF, 80, 160]) * w

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 1)) + chunk(b"IEND", b""))


def jpeg_bytes(w, h):
    # Minimal header walk target: SOI, APP0, SOF0 with dims, EOI. sniff() only
    # needs the frame header; we never decode it (ffmpeg is not involved <2MB).
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


PORTRAIT_PNG = png_bytes(1080, 1920)
LAND_PNG = png_bytes(1280, 720)
PORTRAIT_JPG = jpeg_bytes(1080, 1920)

# --- pure helpers ---------------------------------------------------------------
print("sniff / validate")
ok(pt.sniff(PORTRAIT_PNG) == ("png", 1080, 1920), "PNG dims read from IHDR")
ok(pt.sniff(PORTRAIT_JPG) == ("jpeg", 1080, 1920), "JPEG dims read from SOF0")
for bad, why in ((b"GIF89a" + b"\x00" * 40, "GIF"), (b"<svg></svg>", "SVG"),
                 (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "WEBP")):
    try:
        pt.sniff(bad)
        ok(False, f"{why} rejected")
    except pt.ThumbError as e:
        ok(e.status == 400 and "PNG or JPEG" in str(e), f"{why} rejected (400)")
info = pt.validate(PORTRAIT_PNG, "short")
ok(info["converted"] is False and info["bytes"] == PORTRAIT_PNG and info["warnings"] == [],
   "1080x1920 PNG under 2 MB on a Short is stored as-is, no warning")
info = pt.validate(PORTRAIT_PNG, "long")
ok(info["warnings"] and "portrait" in info["warnings"][0],
   "portrait thumb on long-form is a warning, not a reject")
info = pt.validate(LAND_PNG, "short")
ok(info["warnings"] and "landscape" in info["warnings"][0],
   "landscape thumb on a Short is a warning, not a reject")

# --- app + DB -------------------------------------------------------------------
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    # id 1 / slug owera is the legacy default. Channel 2 is a spacer so
    # tenant-b's channel id is not equal to its workspace id.
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="tenant-b", name="Tenant B"))
    s.commit()
    s.add(Channel(id=1, slug="a", name="A", oauth_status=OAuthStatus.CONNECTED,
                  workspace_id=1))
    s.add(Channel(id=2, slug="a-extra", name="A2", oauth_status=OAuthStatus.CONNECTED,
                  workspace_id=1))
    s.add(Channel(id=3, slug="tb", name="TB", oauth_status=OAuthStatus.CONNECTED,
                  workspace_id=2))
    s.commit()
    s.add(Topic(channel_id=1, name="S", theme_prompt="x", content_format="short"))
    s.add(Topic(channel_id=1, name="L", theme_prompt="x", content_format="long"))
    s.add(Topic(channel_id=3, name="TB-S", theme_prompt="x", content_format="short"))
    s.commit()
    tb_topic = s.exec(select(Topic).where(Topic.name == "TB-S")).one()
    for st in (VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING,
               VideoStatus.RENDERED, VideoStatus.REVIEW, VideoStatus.APPROVED,
               VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
        s.add(Video(channel_id=1, topic_id=1, subject=f"v-{st}", status=st,
                    creation_config=json.dumps({"engine": "hf", "script_source": "provided"})))
    cc = json.dumps({"engine": "hf", "script_source": "provided"})
    s.add(Video(channel_id=3, topic_id=tb_topic.id, subject="tb-queued",
                status=VideoStatus.QUEUED, creation_config=cc))
    s.add(Video(channel_id=3, topic_id=tb_topic.id, subject="tb-publishing",
                status=VideoStatus.PUBLISHING, creation_config=cc))
    s.commit()
    tb_ch = s.get(Channel, 3)
    TB_QUEUED = s.exec(select(Video).where(Video.subject == "tb-queued")).one().id
    TB_PUB = s.exec(select(Video).where(Video.subject == "tb-publishing")).one().id
    ok(tb_ch is not None and tb_ch.id == 3 and tb_ch.workspace_id == 2
       and tb_ch.id != tb_ch.workspace_id and TB_QUEUED not in range(1, 9)
       and TB_PUB not in range(1, 9),
       "precondition: tenant videos sit on channel 3 / workspace 2, "
       "not on the owera status ids")
IDS = {st: i + 1 for i, st in enumerate(
    ("draft", "queued", "rendering", "rendered", "review", "approved", "publishing", "published"))}


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
_orig_mw = settings.multi_workspace
settings.app_password = "testpw"
# Legacy checks resolve the owera workspace. A developer .env with
# multi_workspace on would treat Basic username "x" as the tenant slug.
settings.multi_workspace = False
atexit.register(setattr, settings, "app_password", _orig_pw)
atexit.register(setattr, settings, "multi_workspace", _orig_mw)
client = TestClient(main.app)
auth = ("x", "testpw")


def post_file(vid, data, name="t.png", ctype="image/png"):
    return client.post(f"/api/videos/{vid}/thumbnail", auth=auth,
                       files={"file": (name, data, ctype)})


def post_path(vid, path):
    return client.post(f"/api/videos/{vid}/thumbnail", auth=auth, json={"path": path})


def runs(vid, kind="thumbnail_set"):
    with Session(engine) as s:
        return list(s.exec(select(JobRun).where(JobRun.kind == kind,
                                                JobRun.video_id == vid)))


def vid_row(vid):
    with Session(engine) as s:
        return s.get(Video, vid)


try:
    print("auth / 404 / content-type")
    r = client.post("/api/videos/1/thumbnail", files={"file": ("t.png", PORTRAIT_PNG, "image/png")})
    ok(r.status_code == 401, "POST thumbnail requires auth")
    r = client.delete("/api/videos/1/thumbnail")
    ok(r.status_code == 401, "DELETE thumbnail requires auth")
    ok(post_file(999, PORTRAIT_PNG).status_code == 404, "unknown video → 404")
    ok(client.delete("/api/videos/999/thumbnail", auth=auth).status_code == 404,
       "DELETE unknown video → 404")
    r = client.post("/api/videos/1/thumbnail", auth=auth, content=PORTRAIT_PNG,
                    headers={"content-type": "image/png"})
    ok(r.status_code == 415, "raw body (not multipart/JSON) → 415")
    r = client.post("/api/videos/1/thumbnail", auth=auth, files={"other": ("t.png", PORTRAIT_PNG)})
    ok(r.status_code == 400 and "'file'" in r.json()["detail"], "multipart without 'file' → 400")

    print("multipart PNG set on a queued video")
    vid = IDS["queued"]
    r = post_file(vid, PORTRAIT_PNG)
    ok(r.status_code == 200, f"PNG accepted ({r.status_code} {r.text[:200]})")
    body = r.json()
    dest = _STORE / "videos" / str(vid) / "thumb_provided.png"
    ok(body["thumb_path"] == str(dest) and dest.read_bytes() == PORTRAIT_PNG,
       "stored as-is at storage/videos/<id>/thumb_provided.png")
    ok(body["width"] == 1080 and body["height"] == 1920 and body["format"] == "png"
       and body["converted"] is False and body["thumb_source"] == "provided",
       "response reports dims/format/no conversion")
    v = vid_row(vid)
    cc = json.loads(v.creation_config)
    ok(v.thumb_path == str(dest) and cc["thumb_source"] == "provided"
       and cc["thumb_provided_path"] == str(dest) and cc["script_source"] == "provided"
       and cc["engine"] == "hf",
       "thumb_path + creation_config marker set; other cc keys kept")
    ok(v.status == VideoStatus.QUEUED, "status untouched")
    rr = runs(vid)
    ok(len(rr) == 1 and rr[0].status == "success" and "upload t.png" in rr[0].detail
       and "1080x1920" in rr[0].detail and rr[0].channel_id == 1,
       "JobRun thumbnail_set audit row with source + dims")
    r = client.get(f"/api/videos/{vid}/thumb", auth=auth)
    ok(r.status_code == 200 and r.headers["content-type"] == "image/png"
       and r.content == PORTRAIT_PNG, "GET /thumb serves the provided PNG as image/png")

    print("replace with JPEG removes the old PNG")
    r = post_file(vid, PORTRAIT_JPG, name="t.jpg", ctype="image/jpeg")
    ok(r.status_code == 200 and r.json()["format"] == "jpeg", "JPEG accepted")
    jdest = _STORE / "videos" / str(vid) / "thumb_provided.jpg"
    ok(jdest.is_file() and not dest.exists(), "old thumb_provided.png removed, .jpg stored")
    ok(vid_row(vid).thumb_path == str(jdest), "thumb_path points at the JPEG")
    r = client.get(f"/api/videos/{vid}/thumb", auth=auth)
    ok(r.headers["content-type"] == "image/jpeg", "GET /thumb serves JPEG as image/jpeg")

    print("validation errors")
    vid = IDS["draft"]
    r = post_file(vid, b"GIF89a" + b"\x00" * 64, name="t.gif", ctype="image/gif")
    ok(r.status_code == 400 and "PNG or JPEG" in r.json()["detail"], "GIF → 400")
    r = post_file(vid, PORTRAIT_PNG, name="lies.gif", ctype="image/gif")
    ok(r.status_code == 200, "type sniffed from bytes, not name/content-type")
    client.delete(f"/api/videos/{vid}/thumbnail", auth=auth)
    r = post_file(vid, b"")
    ok(r.status_code == 400 and "empty" in r.json()["detail"], "empty file → 400")
    r = post_file(vid, png_bytes(320, 180))
    ok(r.status_code == 400 and "too small" in r.json()["detail"], "320x180 → 400 too small")
    r = post_file(vid, PORTRAIT_PNG[:20])
    ok(r.status_code == 400 and "corrupt" in r.json()["detail"], "truncated PNG → 400")
    big = PORTRAIT_PNG + b"\x00" * (pt.MAX_INPUT_BYTES + 1 - len(PORTRAIT_PNG))
    r = post_file(vid, big)
    ok(r.status_code == 413, "input over 25 MB → 413")
    ok(vid_row(vid).thumb_path is None, "failed uploads leave nothing set")
    ok(not list((_STORE / "videos" / str(vid)).glob("thumb_provided.*")),
       "failed uploads leave no file")

    print(">2 MB PNG is re-encoded to JPEG ≤ 2 MB")
    if shutil.which("ffmpeg"):
        noisy = png_bytes(1080, 1920, noise=True)
        ok(len(noisy) > pt.YT_THUMB_MAX_BYTES, f"fixture is over 2 MB ({len(noisy)} bytes)")
        r = post_file(IDS["rendered"], noisy)
        ok(r.status_code == 200, f"oversize accepted after compression ({r.text[:200]})")
        b = r.json()
        ok(b["converted"] and b["format"] == "jpeg" and b["bytes"] <= pt.YT_THUMB_MAX_BYTES
           and b["original_bytes"] == len(noisy) and "JPEG" in b["conversion"],
           f"converted to JPEG {b['bytes']} bytes ({b['conversion']})")
        ok(b["thumb_path"].endswith("thumb_provided.jpg")
           and Path(b["thumb_path"]).stat().st_size == b["bytes"], "stored as .jpg")
        ok(b["width"] * b["height"] > 0 and b["height"] > b["width"], "portrait kept")
        rr = runs(IDS["rendered"])
        ok(rr and "re-encoded" in rr[-1].detail, "audit row records the conversion")
    else:
        print("  skip: ffmpeg not on PATH")
    saved_which = shutil.which
    try:
        pt.shutil.which = lambda name: None
        try:
            pt.validate(png_bytes(1080, 1920, noise=True), "short")
            ok(False, "no ffmpeg + >2MB rejected")
        except pt.ThumbError as e:
            ok(e.status == 400 and "2 MB" in str(e), "no ffmpeg + >2 MB → clear 400")
    finally:
        pt.shutil.which = saved_which

    print("JSON path form restricted to the media dir")
    vid = IDS["review"]
    inside = _STORE / "incoming" / "designer.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(LAND_PNG)
    r = post_path(vid, str(inside))
    ok(r.status_code == 200 and r.json()["width"] == 1280, "absolute path inside storage accepted")
    ok(r.json()["warnings"], "landscape-on-Short warning surfaced in the response")
    ok(inside.is_file(), "source file left in place (copied, not moved)")
    r = post_path(vid, "incoming/designer.png")
    ok(r.status_code == 200, "relative path resolved against storage dir")
    outside = _TMP / "outside.png"
    outside.write_bytes(LAND_PNG)
    r = post_path(vid, str(outside))
    ok(r.status_code == 400 and "media dir" in r.json()["detail"], "path outside storage → 400")
    r = post_path(vid, "../outside.png")
    ok(r.status_code == 400 and "media dir" in r.json()["detail"], "../ traversal → 400")
    link = _STORE / "incoming" / "link.png"
    link.symlink_to(outside)
    r = post_path(vid, str(link))
    ok(r.status_code == 400, "symlink escaping storage → 400")
    r = post_path(vid, str(_STORE / "incoming" / "nope.png"))
    ok(r.status_code == 400 and "not a readable file" in r.json()["detail"], "missing file → 400")
    r = client.post(f"/api/videos/{vid}/thumbnail", auth=auth, json={"file": "x"})
    ok(r.status_code == 400, "JSON without 'path' → 400")
    ok("path " in runs(vid)[0].detail, "audit row records path source")

    print("status gating")
    for st in ("draft", "queued", "rendering", "rendered", "review", "approved"):
        r = post_file(IDS[st], PORTRAIT_PNG)
        ok(r.status_code == 200, f"allowed from {st}")
    for st in ("publishing", "published"):
        r = post_file(IDS[st], PORTRAIT_PNG)
        ok(r.status_code == 409, f"POST 409 from {st}")
        r = client.delete(f"/api/videos/{IDS[st]}/thumbnail", auth=auth)
        ok(r.status_code == 409, f"DELETE 409 from {st}")
        ok(vid_row(IDS[st]).thumb_path is None, f"{st} untouched")

    print("DELETE clears, falls back to the render still")
    vid = IDS["approved"]
    vdir = _STORE / "videos" / str(vid)
    (vdir / "thumb.jpg").write_bytes(b"still")
    r = client.delete(f"/api/videos/{vid}/thumbnail", auth=auth)
    ok(r.status_code == 200 and r.json()["cleared"] is True, "DELETE reports cleared")
    v = vid_row(vid)
    cc = json.loads(v.creation_config)
    ok(v.thumb_path == str(vdir / "thumb.jpg"), "thumb_path falls back to thumb.jpg")
    ok("thumb_source" not in cc and "thumb_provided_path" not in cc
       and cc["script_source"] == "provided", "marker dropped, other cc keys kept")
    ok(not list(vdir.glob("thumb_provided.*")), "provided file removed")
    ok(runs(vid)[-1].detail == "provided thumbnail cleared via API", "audit row for clear")
    r = client.delete(f"/api/videos/{vid}/thumbnail", auth=auth)
    ok(r.status_code == 200 and r.json()["cleared"] is False
       and vid_row(vid).thumb_path == str(vdir / "thumb.jpg"),
       "second DELETE is a no-op that keeps the still")
    vid = IDS["draft"]
    r = client.delete(f"/api/videos/{vid}/thumbnail", auth=auth)
    ok(r.status_code == 200 and vid_row(vid).thumb_path is None,
       "DELETE without a still → thumb_path None")

    print("PATCH /craft replacing creation_config never un-provides the thumb")
    # The filename (thumb_path → thumb_provided.*) is the marker of record;
    # the creation_config keys are informational and re-added at finalize.
    vid = IDS["rendered"]
    post_file(vid, PORTRAIT_PNG)
    before = vid_row(vid).thumb_path
    r = client.patch(f"/api/videos/{vid}/craft", auth=auth,
                     json={"creation_config": {"engine": "hf", "new": 1}})
    ok(r.status_code == 200, f"PATCH craft ok ({r.status_code} {r.text[:200]})")
    v = vid_row(vid)
    ok(v.thumb_path == before and pt.is_provided(v) and pt.provided_path(v),
       "thumb_path kept and still provided after a wholesale cc replace")
    ok(json.loads(pt.carry_marker(v, v.creation_config)).get("thumb_source") == "provided",
       "carry_marker restores the informational marker (as finalize does)")

    print("workspace isolation: thumbnail POST/DELETE")
    auth_ow = ("owera", "testpw")
    auth_b = ("tenant-b", "testpw")

    def n_thumb_runs():
        with Session(engine) as s:
            return len(list(s.exec(select(JobRun).where(JobRun.kind == "thumbnail_set"))))

    def post_as(auth_pair, vid):
        return client.post(
            f"/api/videos/{vid}/thumbnail", auth=auth_pair,
            files={"file": ("t.png", PORTRAIT_PNG, "image/png")})

    def delete_as(auth_pair, vid):
        return client.delete(f"/api/videos/{vid}/thumbnail", auth=auth_pair)

    # Flag off: every authenticated caller is owera, whatever the Basic
    # username is. A guard that runs only when the flag is on would let
    # this write land.
    settings.multi_workspace = False
    flag_off_runs = n_thumb_runs()
    r = post_as(("x", "testpw"), TB_QUEUED)
    ok(r.status_code == 404 and "video not found" in r.text,
       "flag-off POST onto a tenant video is 404 (caller is owera)")
    ok(vid_row(TB_QUEUED).thumb_path is None
       and not (_STORE / "videos" / str(TB_QUEUED)).exists(),
       "flag-off POST wrote no tenant thumbnail")
    ok(n_thumb_runs() == flag_off_runs, "flag-off POST wrote no JobRun")
    r = delete_as(("tenant-b", "testpw"), TB_QUEUED)
    ok(r.status_code == 404 and "video not found" in r.text,
       "flag-off DELETE of a tenant video is 404 (username is not the tenant)")
    ok(n_thumb_runs() == flag_off_runs, "flag-off DELETE wrote no JobRun")

    settings.multi_workspace = True
    owera_queued = IDS["queued"]
    before_runs = n_thumb_runs()
    before_path = vid_row(owera_queued).thumb_path
    # Status gating already stored a provided file on this id. Compare bytes,
    # not "directory absent": a foreign write must not replace that file.
    before_bytes = Path(before_path).read_bytes()
    r = post_as(auth_b, owera_queued)
    ok(r.status_code == 404 and "video not found" in r.text,
       "tenant-b POST onto owera video is 404 video not found")
    ok(vid_row(owera_queued).thumb_path == before_path,
       "tenant-b POST left the owera thumb_path")
    ok(Path(before_path).is_file() and Path(before_path).read_bytes() == before_bytes,
       "tenant-b POST did not replace the owera thumbnail bytes")
    ok(n_thumb_runs() == before_runs, "tenant-b POST wrote no thumbnail_set JobRun")

    r = delete_as(auth_b, owera_queued)
    ok(r.status_code == 404 and "video not found" in r.text
       and vid_row(owera_queued).thumb_path == before_path
       and Path(before_path).read_bytes() == before_bytes,
       "tenant-b DELETE owera video is 404 and leaves the thumbnail")
    ok(n_thumb_runs() == before_runs, "tenant-b DELETE wrote no JobRun")

    r = post_as(auth_ow, TB_QUEUED)
    ok(r.status_code == 404 and "video not found" in r.text,
       "owera POST onto tenant video is 404 (both directions)")
    ok(vid_row(TB_QUEUED).thumb_path is None, "owera POST left the tenant thumb unset")
    ok(not (_STORE / "videos" / str(TB_QUEUED)).exists(),
       "owera POST created no tenant thumbnail directory")
    ok(n_thumb_runs() == before_runs, "owera POST wrote no JobRun")

    r = post_as(auth_b, TB_QUEUED)
    ok(r.status_code == 200 and r.json().get("thumb_source") == "provided",
       "tenant-b POST onto its own video is 200")
    own_path = vid_row(TB_QUEUED).thumb_path
    ok(own_path and Path(own_path).is_file() and Path(own_path).read_bytes() == PORTRAIT_PNG,
       "tenant-b own thumbnail file is the PNG")
    ok(n_thumb_runs() == before_runs + 1, "tenant-b own POST wrote one JobRun")

    r = delete_as(auth_ow, TB_QUEUED)
    ok(r.status_code == 404 and vid_row(TB_QUEUED).thumb_path == own_path
       and Path(own_path).is_file(),
       "owera DELETE tenant thumbnail is 404 and the file stays")
    ok(n_thumb_runs() == before_runs + 1, "owera DELETE wrote no JobRun")

    r = post_as(auth_ow, TB_PUB)
    ok(r.status_code == 404 and "video not found" in r.text
       and "cannot change" not in r.text,
       "owera POST on a tenant publishing video is 404, not the 409 status leak")
    ok(vid_row(TB_PUB).status == VideoStatus.PUBLISHING
       and vid_row(TB_PUB).thumb_path is None,
       "tenant publishing row stayed publishing with no thumb")

    r = post_as(auth_b, TB_PUB)
    ok(r.status_code == 409 and "cannot change" in r.text,
       "tenant-b POST on its own publishing video is still 409")
    ok(vid_row(TB_PUB).thumb_path is None, "own 409 wrote no thumb_path")

    r = delete_as(auth_b, TB_QUEUED)
    ok(r.status_code == 200 and r.json().get("cleared") is True
       and vid_row(TB_QUEUED).thumb_path is None,
       "tenant-b DELETE own thumbnail still clears")
    ok(not Path(own_path).exists(), "tenant-b DELETE removed its own file")

    r = client.post(
        f"/api/videos/{TB_QUEUED}/thumbnail",
        files={"file": ("t.png", PORTRAIT_PNG, "image/png")})
    ok(r.status_code == 401, "thumbnail POST on a tenant id still requires auth")
    ok(vid_row(TB_QUEUED).thumb_path is None, "unauth POST wrote nothing")

    src_tgt = inspect.getsource(thumb_router._thumb_target)
    src_set = inspect.getsource(thumb_router.set_provided_thumbnail)
    src_del = inspect.getsource(thumb_router.clear_provided_thumbnail)
    ok("require_video" in src_tgt and "session.get" not in src_tgt,
       "_thumb_target uses require_video (session.get is the cross-tenant write)")
    ok("current_workspace" in src_set and "_thumb_target(session, ws," in src_set,
       "POST thumbnail scopes the workspace before storing")
    ok("current_workspace" in src_del and "_thumb_target(session, ws," in src_del,
       "DELETE thumbnail scopes the workspace before clearing")
    settings.multi_workspace = False
finally:
    main.app.dependency_overrides.pop(get_session, None)

# --- render finalize never clobbers ------------------------------------------------
print("render finalize")
still_calls = []


def _fake_still(src, out):
    still_calls.append(Path(out))
    Path(out).write_bytes(b"still")
    return True


class _Eng:
    def __init__(self, p):
        self.p = p

    def final_path(self, task_id):
        return self.p


class _Meta:
    def generate(self, *a, **k):
        return {"title": "T", "description": "D", "tags": ["x"]}


_orig = (render_loop._has_visible_frames, render_loop._make_thumbnail, render_loop.metadata)
render_loop._has_visible_frames = lambda p: True
render_loop._make_thumbnail = _fake_still
render_loop.metadata = _Meta()
try:
    src = _TMP / "final.mp4"
    src.write_bytes(b"mp4")
    task = {"script": "s", "creation_config": {"engine": "hf", "fresh": True}}
    with Session(engine) as s:
        ch = s.get(Channel, 1)
        v = Video(channel_id=1, topic_id=1, subject="fin-provided",
                  status=VideoStatus.RENDERING, mpt_task_id="t1")
        s.add(v)
        s.commit()
        s.refresh(v)
        pt.store(v, pt.validate(PORTRAIT_PNG, "short"))
        provided = v.thumb_path
        render_loop._finalize(s, v, ch, _Eng(src), task)
        ok(v.video_path and Path(v.video_path).is_file(), "finalize still copies the render")
        ok(v.thumb_path == provided and Path(provided).read_bytes() == PORTRAIT_PNG,
           "provided thumb_path + file survive finalize")
        ok(still_calls == [], "1s still is not even generated when a thumb is provided")
        cc = json.loads(v.creation_config)
        ok(cc.get("fresh") is True and cc.get("thumb_source") == "provided"
           and cc.get("thumb_provided_path") == provided,
           "task creation_config replaced wholesale but marker carried")

        v2 = Video(channel_id=1, topic_id=1, subject="fin-plain",
                   status=VideoStatus.RENDERING, mpt_task_id="t2")
        s.add(v2)
        s.commit()
        s.refresh(v2)
        render_loop._finalize(s, v2, ch, _Eng(src), dict(task))
        exp = _STORE / "videos" / str(v2.id) / "thumb.jpg"
        ok(v2.thumb_path == str(exp) and still_calls == [exp],
           "no provided thumb → finalize writes thumb.jpg as before")
        ok("thumb_source" not in json.loads(v2.creation_config), "no marker invented")
finally:
    render_loop._has_visible_frames, render_loop._make_thumbnail, render_loop.metadata = _orig

# --- publish uses the provided file, skips the template -----------------------------
print("publish _set_custom_thumbnail")
make_calls, set_calls = [], []
_orig_p = (publish_loop.thumbnail.make_thumbnail_png, youtube.set_thumbnail)


def _mk(subject, title, out_png, **kw):
    make_calls.append(Path(out_png))
    Path(out_png).write_bytes(b"tmpl")
    return Path(out_png)


def _set(service, video_id, path):
    set_calls.append((video_id, path))


publish_loop.thumbnail.make_thumbnail_png = _mk
youtube.set_thumbnail = _set
try:
    with Session(engine) as s:
        ch = s.get(Channel, 1)

        def mkv(subject):
            v = Video(channel_id=1, topic_id=1, subject=subject, title="Title",
                      status=VideoStatus.APPROVED)
            s.add(v)
            s.commit()
            s.refresh(v)
            d = pt.video_dir(v.id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "video.mp4").write_bytes(b"mp4")
            v.video_path = str(d / "video.mp4")
            return v

        def thumb_runs(vid):
            return list(s.exec(select(JobRun).where(JobRun.kind == "thumbnail",
                                                    JobRun.video_id == vid)))

        v = mkv("pub-provided")
        pt.store(v, pt.validate(PORTRAIT_JPG, "short"))
        provided = v.thumb_path
        publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytP")
        ok(set_calls == [("ytP", provided)], "provided file uploaded to YouTube")
        ok(make_calls == [], "template generation skipped")
        ok(v.thumb_path == provided, "thumb_path still the provided file")
        tr = thumb_runs(v.id)
        ok(len(tr) == 1 and tr[0].status == "success" and "provided" in tr[0].detail
           and tr[0].quota_cost == youtube.QUOTA_THUMBNAIL_SET,
           "success JobRun says provided + logs quota cost")

        set_calls.clear()
        v = mkv("pub-plain")
        publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytT")
        tmpl = str(Path(v.video_path).parent / "thumb_custom.png")
        ok(len(make_calls) == 1 and set_calls == [("ytT", tmpl)] and v.thumb_path == tmpl,
           "no provided thumb → template generated + uploaded, thumb_path = template (unchanged)")

        make_calls.clear()
        set_calls.clear()
        v = mkv("pub-missing")
        pt.store(v, pt.validate(PORTRAIT_PNG, "short"))
        gone = v.thumb_path
        Path(gone).unlink()
        publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytM")
        tr = thumb_runs(v.id)
        ok(any(r.status == "error" and "missing" in r.detail for r in tr),
           "provided file missing → error JobRun")
        ok(len(make_calls) == 1 and len(set_calls) == 1,
           "…and falls back to the template so the upload still gets a thumb")
        ok(v.thumb_path == gone, "fallback does not overwrite the provided thumb_path")

        def _boom(*a):
            raise RuntimeError("403 forbidden")
        youtube.set_thumbnail = _boom
        make_calls.clear()
        v = mkv("pub-403")
        pt.store(v, pt.validate(PORTRAIT_PNG, "short"))
        publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytX")
        tr = thumb_runs(v.id)
        ok(len(tr) == 1 and tr[0].status == "error" and "403" in tr[0].detail
           and make_calls == [], "upload error is best-effort: logged, no raise, no template")
finally:
    publish_loop.thumbnail.make_thumbnail_png, youtube.set_thumbnail = _orig_p

print("youtube.set_thumbnail mimetype")
seen = []
_orig_mfu = youtube.MediaFileUpload


class _MFU:
    def __init__(self, path, mimetype=None, **kw):
        seen.append(mimetype)


class _Svc:
    def thumbnails(self):
        return self

    def set(self, **kw):
        return self

    def execute(self):
        return {}


youtube.MediaFileUpload = _MFU
try:
    youtube.set_thumbnail(_Svc(), "y", "/x/thumb_provided.jpg")
    youtube.set_thumbnail(_Svc(), "y", "/x/thumb_custom.png")
    ok(seen == ["image/jpeg", "image/png"], "JPEG uploaded as image/jpeg, PNG as image/png")
finally:
    youtube.MediaFileUpload = _orig_mfu

print(f"\nverify_provided_thumb: {_checks} checks passed")
