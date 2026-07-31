"""Dependency-free regression checks for app/services/mpt_client.py.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_mpt_client.py

mpt_client is the render pipeline's HTTP seam to MoneyPrinterTurbo: render_loop
merges every param layer through build_video_params before each submit, and the
engine adapter + metadata + settings-status all lean on this module's error
contracts (submit/poll raise so the loop can log-and-retry; social_metadata and
list_musics swallow so a dead MPT can never fail a publish or a settings page).
None of that had direct coverage. HTTP behaviour is checked against a real local
mock of MPT's REST API (same approach as verify_reconnect.py) — no network, no
real MPT. Exits non-zero on the first failed assertion.
"""
import json as jsonlib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.config import settings
from app.services import mpt_client
from app.services.mpt_client import (
    DEFAULT_PARAMS,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_PROCESSING,
    MPTClient,
    MPTError,
    build_video_params,
)

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------- mock MPT API

class _MPTHandler(BaseHTTPRequestHandler):
    """Scriptable stand-in for MPT's REST API: routes maps (method, path) to a
    (status, payload) pair; every request lands in seen for assertions."""
    routes: dict = {}
    seen: list = []

    def _handle(self, method):
        split = urlsplit(self.path)
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            try:
                body = jsonlib.loads(raw)
            except Exception:
                body = raw
        _MPTHandler.seen.append((method, split.path, body, parse_qs(split.query)))
        status, payload = _MPTHandler.routes.get((method, split.path),
                                                 (404, {"message": "no such route"}))
        data = jsonlib.dumps(payload).encode() if isinstance(payload, (dict, list)) \
            else payload
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, *args):  # keep the suite output readable
        pass


server = HTTPServer(("127.0.0.1", 0), _MPTHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"


def script(routes):
    _MPTHandler.routes = routes
    _MPTHandler.seen = []


def last_request():
    return _MPTHandler.seen[-1]


def dead_base():
    """A base URL nothing listens on (bind, note the port, close)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


client = MPTClient(base_url=BASE)


# ------------------------------------------------------------ state constants

print("case: MPT task-state constants stay pinned to MPT's const.py values")
ok(STATE_FAILED == -1, "STATE_FAILED is -1")
ok(STATE_COMPLETE == 1, "STATE_COMPLETE is 1")
ok(STATE_PROCESSING == 4, "STATE_PROCESSING is 4 (render_loop/engines compare on these)")


# --------------------------------------------------------- build_video_params

print("case: no layers -> defaults plus the forced subject")
p = build_video_params("My subject")
ok(p["video_subject"] == "My subject", "subject set")
ok(p["video_language"] == "en-US" and p["video_aspect"] == "9:16",
   "defaults present")
ok(p["paragraph_number"] == 2 and p["bgm_type"] == "random",
   "proven produce.py defaults carried over")

print("case: later layers override earlier (profile -> topic -> video order)")
p = build_video_params(
    "s",
    {"voice_name": "profile-voice", "bgm_volume": 0.5},
    {"voice_name": "topic-voice"},
    {"video_aspect": "16:9"},
)
ok(p["voice_name"] == "topic-voice", "later layer wins the shared key")
ok(p["bgm_volume"] == 0.5, "earlier layer's uncontested key survives")
ok(p["video_aspect"] == "16:9", "last layer applied")

print("case: None-valued keys never clobber an earlier layer")
p = build_video_params("s", {"voice_name": "keep-me"}, {"voice_name": None})
ok(p["voice_name"] == "keep-me", "explicit None in a later layer is stripped")
p = build_video_params("s", {"video_language": None})
ok(p["video_language"] == "en-US", "None cannot erase a default either")

print("case: falsy-but-real values DO override (0 / False / empty string)")
p = build_video_params("s", {"bgm_volume": 0, "subtitle_enabled": False,
                             "bgm_type": ""})
ok(p["bgm_volume"] == 0, "0 overrides the 0.2 default")
ok(p["subtitle_enabled"] is False, "False overrides the True default")
ok(p["bgm_type"] == "", "empty string overrides")

print("case: absent layers (None) are skipped — render_loop passes None for "
      "missing profiles/overrides")
p = build_video_params("s", None, {"voice_name": "v"}, None)
ok(p["voice_name"] == "v", "real layer applied around the Nones")

print("case: subject always wins last")
p = build_video_params("forced", {"video_subject": "layer tries to sneak in"})
ok(p["video_subject"] == "forced", "a layer cannot override the subject")

print("case: merging never mutates DEFAULT_PARAMS or the input layers")
before = dict(DEFAULT_PARAMS)
layer = {"voice_name": "x", "dead": None}
p = build_video_params("s", layer)
p["video_language"] = "mutated-by-caller"
p["injected"] = True
ok(DEFAULT_PARAMS == before, "DEFAULT_PARAMS unchanged after merge + caller mutation")
ok(layer == {"voice_name": "x", "dead": None}, "input layer unchanged")


# ------------------------------------------------------------ local_final_path

print("case: local_final_path is <mpt_storage_dir>/<task>/final-<n>.mp4")
ok(client.local_final_path("task-abc")
   == Path(settings.mpt_storage_dir) / "task-abc" / "final-1.mp4",
   "default index 1")
ok(client.local_final_path("task-abc", index=3).name == "final-3.mp4",
   "index selects final-<n>.mp4")


# ------------------------------------------------------- base_url normalisation

print("case: base_url is rstripped and /api/v1 appended")
c2 = MPTClient(base_url=BASE + "///")
ok(c2.api == BASE + "/api/v1", "trailing slashes never double up in the API root")
script({("GET", "/api/v1/tasks"): (200, {"data": {}})})
ok(c2.ping() is True, "a trailing-slash base still reaches the API")


# ----------------------------------------------------------------------- ping

print("case: ping is the cheap /tasks liveness probe")
script({("GET", "/api/v1/tasks"): (200, {"data": {"tasks": []}})})
ok(client.ping() is True, "200 -> True")
method, path, _, query = last_request()
ok((method, path) == ("GET", "/api/v1/tasks"), "probes GET /api/v1/tasks")
ok(query.get("page") == ["1"] and query.get("page_size") == ["1"],
   "asks for a single-item page (cheapest possible probe)")
script({("GET", "/api/v1/tasks"): (500, {"message": "boom"})})
ok(client.ping() is False, "non-200 -> False")
ok(MPTClient(base_url=dead_base()).ping() is False,
   "unreachable MPT -> False, never raises (settings page depends on this)")


# --------------------------------------------------------------------- submit

print("case: submit posts the params and returns the task_id")
script({("POST", "/api/v1/videos"): (200, {"data": {"task_id": "tid-1"}})})
params = build_video_params("Subject", {"voice_name": "v"})
ok(client.submit(params) == "tid-1", "task_id extracted")
method, path, body, _ = last_request()
ok((method, path) == ("POST", "/api/v1/videos"), "posts to /api/v1/videos")
ok(body == params, "params arrive as the JSON body, unmodified")

print("case: submit raises MPTError when the response has no task_id")
for payload, label in [({"data": {}}, "empty data"),
                       ({"data": None}, "null data"),
                       ({"data": {"task_id": ""}}, "empty task_id")]:
    script({("POST", "/api/v1/videos"): (200, payload)})
    try:
        client.submit({"video_subject": "s"})
        ok(False, f"submit must raise on {label}")
    except MPTError:
        ok(True, f"{label} -> MPTError")

print("case: submit propagates HTTP errors (render_loop logs 'submit failed')")
script({("POST", "/api/v1/videos"): (500, {"message": "boom"})})
try:
    client.submit({"video_subject": "s"})
    ok(False, "submit must raise on a 500")
except httpx.HTTPStatusError:
    ok(True, "500 -> HTTPStatusError propagates to the loop's log-and-skip")


# ----------------------------------------------------------------------- poll

print("case: poll returns the task dict")
script({("GET", "/api/v1/tasks/tid-1"): (200, {"data": {"state": 4, "progress": 60}})})
t = client.poll("tid-1")
ok(t == {"state": 4, "progress": 60}, "data dict returned")
ok(t["state"] == STATE_PROCESSING, "state readable against the pinned constants")

print("case: poll normalises null data to {} (engine calls .get on it)")
script({("GET", "/api/v1/tasks/tid-1"): (200, {"data": None})})
ok(client.poll("tid-1") == {}, "null data -> {} not None")

print("case: poll propagates HTTP errors (loop retries next tick)")
script({})  # 404 on every route
try:
    client.poll("gone")
    ok(False, "poll must raise on a 404")
except httpx.HTTPStatusError:
    ok(True, "404 -> HTTPStatusError propagates")


# ------------------------------------------------------------- social_metadata

print("case: social_metadata returns MPT's metadata dict")
script({("POST", "/api/v1/social-metadata"):
        (200, {"data": {"title": "T", "caption": "C", "hashtags": ["#x"]}})})
meta = client.social_metadata("Subject", "the script", platform="youtube_shorts",
                              language="pt-BR")
ok(meta == {"title": "T", "caption": "C", "hashtags": ["#x"]}, "data returned")
_, path, body, _ = last_request()
ok(path == "/api/v1/social-metadata", "posts to /api/v1/social-metadata")
ok(body == {"video_subject": "Subject", "video_script": "the script",
            "language": "pt-BR", "platform": "youtube_shorts"},
   "payload carries subject/script/language/platform")

print("case: social_metadata swallows every failure to None "
      "(metadata.py falls back; a dead MPT can't fail a publish)")
script({("POST", "/api/v1/social-metadata"): (500, {"message": "boom"})})
ok(client.social_metadata("s", "x") is None, "500 -> None")
script({("POST", "/api/v1/social-metadata"): (200, b"<html>not json</html>")})
ok(client.social_metadata("s", "x") is None, "non-JSON body -> None")
script({("POST", "/api/v1/social-metadata"): (200, {"data": None})})
ok(client.social_metadata("s", "x") is None, "null data -> None")
ok(MPTClient(base_url=dead_base()).social_metadata("s", "x") is None,
   "unreachable -> None")


# ----------------------------------------------------------------- list_musics

print("case: list_musics returns the files list, [] on any failure")
script({("GET", "/api/v1/musics"): (200, {"data": {"files": [{"name": "a.mp3"}]}})})
ok(client.list_musics() == [{"name": "a.mp3"}], "files list returned")
script({("GET", "/api/v1/musics"): (200, {"data": None})})
ok(client.list_musics() == [], "null data -> []")
script({("GET", "/api/v1/musics"): (500, {"message": "boom"})})
ok(client.list_musics() == [], "500 -> []")
ok(MPTClient(base_url=dead_base()).list_musics() == [], "unreachable -> []")


# ------------------------------------------------------ MPTEngine thin adapter

print("case: MPTEngine.submit strips the manager-internal content_format hint")
from app.services.engines import mpt as engine_mod

_real_singleton = engine_mod.mpt
engine_mod.mpt = client
try:
    engine = engine_mod.MPTEngine()
    script({("POST", "/api/v1/videos"): (200, {"data": {"task_id": "tid-9"}})})
    ok(engine.submit(None, {"video_subject": "s", "content_format": "long",
                            "voice_name": "v"}) == "tid-9",
       "delegates to MPTClient.submit")
    _, _, body, _ = last_request()
    ok("content_format" not in body, "content_format never reaches MPT's VideoParams")
    ok(body["voice_name"] == "v", "every other key passes through")

    print("case: MPTEngine.poll normalises to the {state, progress, script} contract")
    script({("GET", "/api/v1/tasks/tid-9"):
            (200, {"data": {"state": 1, "progress": None, "script": "words",
                            "videos": ["ignored"]}})})
    ok(engine.poll("tid-9") == {"state": 1, "progress": 0, "script": "words"},
       "null progress -> 0; extra MPT fields dropped")
    ok(engine.final_path("tid-9")
       == Path(settings.mpt_storage_dir) / "tid-9" / "final-1.mp4",
       "final_path delegates to local_final_path")
finally:
    engine_mod.mpt = _real_singleton

server.shutdown()
print(f"\nALL {_checks} CHECKS PASSED")
