"""Dependency-free regression checks for app/services/engines/hyperframes.py
(backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_hyperframes.py

``HyperFramesEngine`` is the local HTML/CSS → MP4 render adapter: submit spawns a
daemon thread running worker.run_job, poll reads the status.json the worker
writes, and final_path names the muxed MP4. render_loop compares on the shared
STATE_* constants and trusts poll's {state, progress, script} shape; a regression
here either blocks every HyperFrames render (missing/corrupt status treated as
failed) or never finishes (complete never surfaces). Previously only MPT's
engine adapter had direct coverage (verify_mpt_client); hyperframes had none.

Covers, dependency-free (no network, no HyperFrames CLI, no ffmpeg, no LLM/TTS):
  - module/engine contracts: name, STATE_* identity with base, final_path shape
  - ``_job_dir`` / ``_status_path`` under settings.hyperframes_storage_dir
  - ``write_status``: create, merge-preserve, corrupt-JSON recovery
  - ``poll``: missing file / corrupt JSON → PROCESSING+0; happy path field
    forwarding (error, creation_config); progress int coercion + null→0;
    default state when key missing
  - ``submit``: uuid hex handle, job dir + initial status, daemon thread args
    (handle, job_dir, subject, params COPY), unique handles, no ORM touch
  - registry: get_engine("hyperframes") returns the HyperFrames adapter

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.services.engines import base as base_mod
from app.services.engines import hyperframes as hf
from app.services.engines import get_engine
from app.services.engines.base import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_PROCESSING,
)

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
# Module / engine contracts
# ---------------------------------------------------------------------------
print("module contracts: name, STATE_* identity, final_path shape")

ok(hf.HyperFramesEngine.name == "hyperframes",
   "engine name is the registry key 'hyperframes'")
ok(hf.STATE_PROCESSING is base_mod.STATE_PROCESSING
   or hf.STATE_PROCESSING == STATE_PROCESSING,
   "hyperframes imports STATE_PROCESSING from base (shared constants)")
ok(STATE_FAILED == -1 and STATE_COMPLETE == 1 and STATE_PROCESSING == 4,
   "STATE_* stay pinned to MPT's values (render_loop compares on these)")

engine = hf.HyperFramesEngine()
ok(engine.name == "hyperframes", "instance .name matches class")
ok(engine.final_path("abc123")
   == Path(settings.hyperframes_storage_dir) / "abc123" / "final.mp4",
   "final_path is <storage>/<handle>/final.mp4 (worker mux target)")

# Registry wiring: render_loop resolves engines by name through get_engine.
reg = get_engine("hyperframes")
ok(reg.name == "hyperframes", "get_engine('hyperframes') returns HyperFrames adapter")
ok(type(reg).__name__ == "HyperFramesEngine",
   "registry singleton is HyperFramesEngine (not a miswired MPT)")


# ---------------------------------------------------------------------------
# Path helpers — pin against settings so a renamed storage root can't silently
# point status/final paths at a different tree than worker.run_job uses.
# ---------------------------------------------------------------------------
print("_job_dir / _status_path under hyperframes_storage_dir")

_orig_storage = settings.hyperframes_storage_dir
with tempfile.TemporaryDirectory() as td:
    settings.hyperframes_storage_dir = td
    try:
        ok(hf._job_dir("h1") == Path(td) / "h1",
           "_job_dir joins storage_dir / handle")
        ok(hf._status_path("h1") == Path(td) / "h1" / "status.json",
           "_status_path is job_dir/status.json (the poll contract surface)")
        # Nested handle-looking names must not escape the storage root.
        ok(hf._job_dir("a/b").is_relative_to(Path(td))
           or str(hf._job_dir("a/b")).startswith(td),
           "job_dir stays under storage root even with slashy handles")
    finally:
        settings.hyperframes_storage_dir = _orig_storage


# ---------------------------------------------------------------------------
# write_status — merge semantics the worker and submit both depend on
# ---------------------------------------------------------------------------
print("write_status: create, merge-preserve, corrupt-JSON recovery")

with tempfile.TemporaryDirectory() as td:
    settings.hyperframes_storage_dir = td
    try:
        handle = "ws-create"
        (Path(td) / handle).mkdir()
        hf.write_status(handle, state=STATE_PROCESSING, progress=0, script=None)
        path = Path(td) / handle / "status.json"
        ok(path.is_file(), "write_status creates status.json")
        data = json.loads(path.read_text())
        ok(data == {"state": STATE_PROCESSING, "progress": 0, "script": None},
           "initial write carries exactly the provided fields")

        # Merge: later progress must not wipe state/script the worker set earlier.
        hf.write_status(handle, progress=42, script="narration")
        data = json.loads(path.read_text())
        ok(data["state"] == STATE_PROCESSING,
           "merge preserves unmentioned keys (state kept)")
        ok(data["progress"] == 42 and data["script"] == "narration",
           "merge overwrites provided keys")

        # Corrupt JSON must not raise and must not leave the file unreadable —
        # a half-written status would strand poll forever if it raised.
        path.write_text("{not json")
        hf.write_status(handle, state=STATE_COMPLETE, progress=100)
        data = json.loads(path.read_text())
        ok(data["state"] == STATE_COMPLETE and data["progress"] == 100,
           "corrupt JSON treated as empty dict, then fields written")
        ok("script" not in data,
           "corrupt recovery does not invent keys from the bad file")

        # Overwrite an existing key with None (explicit clear).
        hf.write_status(handle, error="boom")
        hf.write_status(handle, error=None)
        data = json.loads(path.read_text())
        ok(data.get("error") is None,
           "explicit error=None is written (clears a prior error)")
    finally:
        settings.hyperframes_storage_dir = _orig_storage


# ---------------------------------------------------------------------------
# poll — the render_loop contract surface
# ---------------------------------------------------------------------------
print("poll: missing / corrupt / happy path / progress coercion")

with tempfile.TemporaryDirectory() as td:
    settings.hyperframes_storage_dir = td
    try:
        eng = hf.HyperFramesEngine()

        # Missing status: in-flight between submit mkdir and first write_status,
        # or a wiped job dir — must look like still processing, never failed.
        missing = eng.poll("no-such-handle")
        ok(missing == {"state": STATE_PROCESSING, "progress": 0, "script": None},
           "missing status.json → PROCESSING progress=0 script=None")
        ok("error" not in missing and "creation_config" not in missing,
           "missing-status poll omits optional keys (error/creation_config)")

        handle = "poll-ok"
        (Path(td) / handle).mkdir()
        status_path = Path(td) / handle / "status.json"

        # Corrupt JSON: same safe fallback as missing (do not fail the render).
        status_path.write_text("<<<")
        corrupt = eng.poll(handle)
        ok(corrupt == {"state": STATE_PROCESSING, "progress": 0, "script": None},
           "corrupt status.json → same PROCESSING fallback as missing")

        # Happy path: full worker payload including optional fields the loop
        # and _finalize read (error on fail, creation_config on complete).
        status_path.write_text(json.dumps({
            "state": STATE_COMPLETE,
            "progress": 100,
            "script": "hello world",
            "error": None,
            "creation_config": {"beats": 3},
            "extra_ignored_by_shape": True,
        }))
        got = eng.poll(handle)
        ok(got["state"] == STATE_COMPLETE, "poll forwards state")
        ok(got["progress"] == 100, "poll forwards progress")
        ok(got["script"] == "hello world", "poll forwards script")
        ok(got["error"] is None, "poll forwards error (even when None)")
        ok(got["creation_config"] == {"beats": 3},
           "poll forwards creation_config for _finalize")
        # Extra keys in the file are NOT required to be dropped — but the
        # required keys must be present. Pin that the contract keys exist.
        for k in ("state", "progress", "script", "error", "creation_config"):
            ok(k in got, f"poll result includes contract key {k!r}")

        # progress coercion: worker always writes int, but a hand-edited or
        # partial write must not blow up int() or leave a non-int for the loop.
        status_path.write_text(json.dumps({
            "state": STATE_PROCESSING, "progress": "37", "script": None,
        }))
        ok(eng.poll(handle)["progress"] == 37,
           "string progress coerces via int()")
        status_path.write_text(json.dumps({
            "state": STATE_PROCESSING, "progress": None, "script": None,
        }))
        ok(eng.poll(handle)["progress"] == 0,
           "null progress → 0 (int(None or 0))")
        status_path.write_text(json.dumps({
            "state": STATE_PROCESSING, "script": "x",
        }))
        ok(eng.poll(handle)["progress"] == 0,
           "missing progress key → 0")
        # Default state when the key is absent: still processing, not failed.
        status_path.write_text(json.dumps({"progress": 10, "script": None}))
        ok(eng.poll(handle)["state"] == STATE_PROCESSING,
           "missing state key defaults to STATE_PROCESSING")

        # STATE_FAILED path: loop must see the error string.
        status_path.write_text(json.dumps({
            "state": STATE_FAILED,
            "progress": 55,
            "script": "partial",
            "error": "RuntimeError: boom",
        }))
        failed = eng.poll(handle)
        ok(failed["state"] == STATE_FAILED, "failed state forwarded")
        ok(failed["error"] == "RuntimeError: boom",
           "failed error string forwarded for JobRun detail")
        ok(failed["script"] == "partial",
           "partial script preserved on failure (debug/forensics)")
    finally:
        settings.hyperframes_storage_dir = _orig_storage


# ---------------------------------------------------------------------------
# submit — async shape: return immediately, daemon thread runs the pipeline
# ---------------------------------------------------------------------------
print("submit: handle, initial status, thread args, params copy")

_run_job_calls: list[tuple] = []
_run_job_started = threading.Event()


def _fake_run_job(handle, job_dir, subject, params):
    """Stand-in for worker.run_job: record args, never touch LLM/ffmpeg.

    Records the params object BY IDENTITY (no re-copy) so a submit that
    forgets ``dict(params)`` and hands the caller's dict through is caught —
    a re-copy here would make that mutant survive.
    """
    _run_job_calls.append((handle, job_dir, subject, params))
    _run_job_started.set()
    # Simulate a brief in-flight so poll can observe PROCESSING if needed.
    time.sleep(0.05)


with tempfile.TemporaryDirectory() as td:
    settings.hyperframes_storage_dir = td
    try:
        eng = hf.HyperFramesEngine()
        video = SimpleNamespace(subject="Cache invalidation that actually works")
        params = {"voice_name": "pt-BR-AntonioNeural", "content_format": "short",
                  "video_aspect": "9:16"}

        with patch.object(hf, "run_job", side_effect=_fake_run_job):
            _run_job_calls.clear()
            _run_job_started.clear()
            handle = eng.submit(video, params)

            ok(isinstance(handle, str) and len(handle) == 32,
               "submit returns a 32-char uuid4().hex handle")
            ok(all(c in "0123456789abcdef" for c in handle),
               "handle is lowercase hex (uuid4().hex)")

            job_dir = Path(td) / handle
            ok(job_dir.is_dir(), "submit mkdir's the job dir under storage")
            status_path = job_dir / "status.json"
            ok(status_path.is_file(), "submit writes initial status.json")
            initial = json.loads(status_path.read_text())
            ok(initial["state"] == STATE_PROCESSING,
               "initial state is STATE_PROCESSING")
            ok(initial["progress"] == 0, "initial progress is 0")
            ok(initial["script"] is None, "initial script is None")
            ok(initial.get("error") is None, "initial error is None")

            # Thread must start (daemon) and receive the captured plain values —
            # never the ORM video object.
            ok(_run_job_started.wait(timeout=2.0),
               "run_job thread started within 2s (submit is non-blocking)")
            ok(len(_run_job_calls) == 1, "exactly one run_job invocation")
            got_handle, got_dir, got_subject, got_params = _run_job_calls[0]
            ok(got_handle == handle, "run_job receives the returned handle")
            ok(Path(got_dir) == job_dir, "run_job receives the job_dir Path")
            ok(got_subject == "Cache invalidation that actually works",
               "run_job receives video.subject as a plain str (no ORM)")
            ok(got_params == {"voice_name": "pt-BR-AntonioNeural",
                              "content_format": "short",
                              "video_aspect": "9:16"},
               "run_job receives a params dict matching the submit input")
            # Identity pin: submit must pass dict(params), not the caller's
            # object — otherwise a later mutation races the worker thread.
            ok(got_params is not params,
               "run_job params is a copy (not the caller's dict object)")

            # Value pin too: even if identity were faked, the snapshot at
            # thread-start must be the pre-mutation values.
            params["voice_name"] = "MUTATED"
            ok(got_params["voice_name"] == "pt-BR-AntonioNeural",
               "caller mutation after submit does not alter the worker's params")

            # poll right after submit (before worker finishes) is PROCESSING.
            polled = eng.poll(handle)
            ok(polled["state"] == STATE_PROCESSING and polled["progress"] == 0,
               "poll after submit (pre-worker progress) is PROCESSING/0")

            # Two submits → two distinct handles and two job dirs.
            with patch.object(hf, "run_job", side_effect=_fake_run_job):
                h2 = eng.submit(video, {"content_format": "long"})
            ok(h2 != handle, "second submit returns a distinct handle")
            ok((Path(td) / h2).is_dir(), "second submit has its own job dir")

            # final_path for this handle points at the mux target under the
            # same job dir (even if the file does not exist yet).
            ok(eng.final_path(handle) == job_dir / "final.mp4",
               "final_path for submitted handle is job_dir/final.mp4")
            ok(not eng.final_path(handle).exists(),
               "final.mp4 does not exist until the worker finishes (pre-mux)")
    finally:
        settings.hyperframes_storage_dir = _orig_storage


# ---------------------------------------------------------------------------
# submit does not raise if the video only has .subject (minimal protocol)
# ---------------------------------------------------------------------------
print("submit: minimal video protocol + thread is daemon")

_thread_info: dict = {}


def _capture_thread_run_job(handle, job_dir, subject, params):
    # The calling thread IS the worker thread HyperFramesEngine started.
    t = threading.current_thread()
    _thread_info["daemon"] = t.daemon
    _thread_info["name"] = t.name
    _thread_info["handle"] = handle


with tempfile.TemporaryDirectory() as td:
    settings.hyperframes_storage_dir = td
    try:
        eng = hf.HyperFramesEngine()
        video = SimpleNamespace(subject="x")
        with patch.object(hf, "run_job", side_effect=_capture_thread_run_job):
            handle = eng.submit(video, {})
            # Give the thread a moment to run.
            deadline = time.time() + 2.0
            while "daemon" not in _thread_info and time.time() < deadline:
                time.sleep(0.01)
        ok(_thread_info.get("daemon") is True,
           "worker thread is daemon (scheduler tick must never join it)")
        ok(_thread_info.get("name", "").startswith("hyperframes-"),
           "thread name is hyperframes-<handle[:8]> for log forensics")
        ok(_thread_info.get("name") == f"hyperframes-{handle[:8]}",
           "thread name embeds the handle prefix")
    finally:
        settings.hyperframes_storage_dir = _orig_storage


# ---------------------------------------------------------------------------
# write_status is what poll reads — end-to-end without run_job
# ---------------------------------------------------------------------------
print("write_status → poll round-trip (the worker→loop surface)")

with tempfile.TemporaryDirectory() as td:
    settings.hyperframes_storage_dir = td
    try:
        eng = hf.HyperFramesEngine()
        handle = "roundtrip"
        (Path(td) / handle).mkdir()
        # Mimic worker progress updates.
        hf.write_status(handle, state=STATE_PROCESSING, progress=0, script=None,
                        error=None)
        ok(eng.poll(handle)["progress"] == 0, "round-trip: progress 0")
        hf.write_status(handle, progress=50, script="partial script")
        mid = eng.poll(handle)
        ok(mid["progress"] == 50 and mid["script"] == "partial script",
           "round-trip: mid-pipeline progress + script")
        hf.write_status(handle, state=STATE_COMPLETE, progress=100,
                        creation_config={"w": 1080})
        done = eng.poll(handle)
        ok(done["state"] == STATE_COMPLETE and done["progress"] == 100,
           "round-trip: complete")
        ok(done["creation_config"] == {"w": 1080},
           "round-trip: creation_config survives merge + poll")
        ok(done["script"] == "partial script",
           "round-trip: earlier script preserved across complete write")
    finally:
        settings.hyperframes_storage_dir = _orig_storage


print()
print(f"ALL {_checks} CHECKS PASSED")
