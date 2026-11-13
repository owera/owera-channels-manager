"""Dependency-free regression checks for app/services/llm.py.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_llm.py

``llm.complete`` is the only manager completion client: topic autogen, script /
metadata generation, HyperFrames motion/LLM steps, and thumbnail hooks all go
through it. It must spawn ``grok -p`` (Grok Build CLI headless), use the
machine's OAuth login, and never require XAI_API_KEY / Anthropic / LiteLLM.

Covers, dependency-free (no network, no real grok, no Anthropic):
  - config defaults: grok_bin='grok', timeout 600 + light 180; litellm_model / anthropic_api_key gone
  - ``build_prompt`` / ``build_cmd``: ``-p``, system prepend, no max_tokens flag
  - ``complete`` (subprocess stubbed): argv, cwd=scratch, timeout, env strips
    XAI_API_KEY / GROK_API_KEY / ANTHROPIC_API_KEY and keeps HOME
  - FileNotFoundError / nonzero / TimeoutExpired → GrokCLIError (grok.Timeout token)
  - stdout strip; empty stdout → ""
  - call-site wiring: video_gen / metadata / worker._llm import complete, not litellm

Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from app.services import llm, metadata, video_gen
from app.services.engines import worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
# Config: grok -p is the LLM, not LiteLLM / Anthropic / XAI_API_KEY
# ---------------------------------------------------------------------------
print("config: grok CLI is the manager LLM")

ok(settings.grok_bin == "grok", "default grok_bin is 'grok' (PATH lookup)")
ok(settings.grok_timeout_seconds == 600, "default grok timeout is 600s (long storyboards)")
ok(settings.grok_timeout_seconds_light == 180,
   "default light timeout is 180s (ideas/metadata)")
ok(not hasattr(settings, "litellm_model"),
   "litellm_model setting is gone (no anthropic/claude default)")
ok(not hasattr(settings, "anthropic_api_key"),
   "anthropic_api_key setting is gone (no MANAGER_ANTHROPIC_API_KEY bridge)")
ok("XAI_API_KEY" in llm._STRIP_ENV and "ANTHROPIC_API_KEY" in llm._STRIP_ENV,
   "child env strips XAI_API_KEY and ANTHROPIC_API_KEY")


# ---------------------------------------------------------------------------
# build_prompt / build_cmd
# ---------------------------------------------------------------------------
print("build_prompt / build_cmd")

ok(llm.build_prompt("hello") == "hello", "user-only prompt is passed through")
ok(llm.build_prompt("user", system="sys") == "sys\n\nuser",
   "system prepends the user prompt with a blank line")
ok(llm.build_prompt("user", system="  ") == "user",
   "whitespace-only system is ignored")
ok(llm.build_prompt("user", system=None) == "user", "None system is ignored")

cmd = llm.build_cmd("Write a title")
ok(cmd[0] == settings.grok_bin, "argv0 is settings.grok_bin")
ok(cmd == [settings.grok_bin, "-p", "Write a title"],
   "headless invocation is exactly grok -p <prompt> (no extra flags)")
ok("--single" not in cmd, "-p is the flag; --single is the same switch, not passed twice")
ok("max_tokens" not in " ".join(cmd) and "--max-tokens" not in cmd,
   "max_tokens is not a grok CLI flag (accepted by complete, ignored)")
ok("--no-auto-update" not in cmd and "--permission-mode" not in cmd,
   "do not invent flags beyond grok 1.0.5 -p / --single")

cmd_sys = llm.build_cmd("user bit", system="system bit")
ok(cmd_sys == [settings.grok_bin, "-p", "system bit\n\nuser bit"],
   "system+user flattened into the single -p argument")


# Model / reasoning-effort pins (grok >= 1.0.41 `-m` / `--reasoning-effort`).
# Assert the CODE default (Settings field), not the live value: claw0's .env
# pins MANAGER_GROK_MODEL=grok-4.7 on purpose (approved by Rodrigo), so
# settings.grok_model is not "" in production and must not fail this suite.
from app.config import Settings as _Settings
ok(_Settings.model_fields["grok_model"].default == "",
   "default grok_model is empty (CLI default model, no -m); .env may pin it")
ok(_Settings.model_fields["grok_compose_reasoning_effort"].default == "medium",
   "default compose reasoning effort is medium (2026-09-22 grok-4.7 compose timeouts)")
ok(_Settings.model_fields["grok_effort_ideas"].default == "low",
   "default ideas/metadata reasoning effort is low (grok-4.7 high blows the light timeout)")
cmd_pin = llm.build_cmd("u", system="s", model="grok-4.6", reasoning_effort="medium")
ok(cmd_pin == [settings.grok_bin, "-m", "grok-4.6", "--reasoning-effort", "medium",
               "-p", "s\n\nu"],
   "model/effort add -m / --reasoning-effort before -p; prompt stays last")
ok(llm.build_cmd("u", model="", reasoning_effort="") == [settings.grok_bin, "-p", "u"],
   "empty model/effort add no flags")
ok(llm.build_cmd("u", reasoning_effort="low")[-2:] == ["-p", "u"]
   and "-m" not in llm.build_cmd("u", reasoning_effort="low"),
   "effort alone adds only --reasoning-effort")

# ---------------------------------------------------------------------------
# complete — subprocess stubbed
# ---------------------------------------------------------------------------
print("complete: subprocess contract (mocked; never calls real grok)")

_captured: dict = {}


def _run_ok(cmd, capture_output=True, text=True, timeout=None, env=None, cwd=None):
    _captured["cmd"] = list(cmd)
    _captured["timeout"] = timeout
    _captured["env"] = dict(env or {})
    _captured["cwd"] = cwd
    _captured["capture_output"] = capture_output
    _captured["text"] = text
    return SimpleNamespace(returncode=0, stdout="  the reply  \n", stderr="")


_orig_bin = settings.grok_bin
_orig_timeout = settings.grok_timeout_seconds
_orig_storage = settings.storage_dir
with tempfile.TemporaryDirectory() as td:
    settings.storage_dir = td
    settings.grok_bin = "/opt/fake/grok"
    settings.grok_timeout_seconds = 42
    poison = {
        "XAI_API_KEY": "xai-should-never-reach-child",
        "GROK_API_KEY": "grok-should-never-reach-child",
        "ANTHROPIC_API_KEY": "sk-ant-should-never-reach-child",
        "HOME": "/Users/claw0",
        "PATH": "/usr/bin",
        "UNRELATED": "keep-me",
    }
    with patch.object(llm.subprocess, "run", side_effect=_run_ok), \
            patch.dict("os.environ", poison, clear=True):
        out = llm.complete("ping the model", system="be brief", max_tokens=99)
    ok(out == "the reply", "stdout is stripped; surrounding whitespace dropped")
    ok(_captured["cmd"][0] == "/opt/fake/grok", "uses settings.grok_bin (absolute ok)")
    ok(_captured["cmd"][_captured["cmd"].index("-p") + 1] == "be brief\n\nping the model",
       "-p payload is the flattened system+user prompt")
    ok("99" not in _captured["cmd"], "max_tokens is not forwarded to argv")
    ok(_captured["timeout"] == 42, "timeout is settings.grok_timeout_seconds")
    ok(_captured["capture_output"] is True and _captured["text"] is True,
       "capture_output + text so we read stdout")
    scratch = Path(td) / "grok-scratch"
    ok(scratch.is_dir(), "scratch cwd is created under storage_dir")
    ok(Path(_captured["cwd"]) == scratch,
       "subprocess cwd is the isolated grok-scratch (not the manager repo)")
    env = _captured["env"]
    ok("XAI_API_KEY" not in env, "XAI_API_KEY stripped from grok child env")
    ok("GROK_API_KEY" not in env, "GROK_API_KEY stripped from grok child env")
    ok("ANTHROPIC_API_KEY" not in env, "ANTHROPIC_API_KEY stripped from grok child env")
    ok(env.get("HOME") == "/Users/claw0",
       "HOME is kept so grok finds ~/.grok OIDC cache")
    ok(env.get("UNRELATED") == "keep-me", "unrelated env is inherited")
settings.grok_bin = _orig_bin
settings.grok_timeout_seconds = _orig_timeout
settings.storage_dir = _orig_storage

# Explicit timeout= kwarg overrides settings (light path for ideas/metadata).
_captured.clear()
with tempfile.TemporaryDirectory() as td2:
    settings.storage_dir = td2
    with patch.object(llm.subprocess, "run", side_effect=_run_ok):
        out = llm.complete("short", timeout=180)
ok(out == "the reply", "timeout= override still returns stdout")
ok(_captured["timeout"] == 180, "timeout= kwarg wins over settings.grok_timeout_seconds")
settings.storage_dir = _orig_storage

# video_gen / metadata call sites pass the light budget.
src_vg = inspect.getsource(video_gen.generate_ideas)
ok("grok_timeout_seconds_light" in src_vg,
   "generate_ideas uses grok_timeout_seconds_light")
ok("grok_effort_ideas" in src_vg and "reasoning_effort" in src_vg,
   "generate_ideas pins reasoning_effort from settings.grok_effort_ideas")
src_meta = inspect.getsource(metadata._llm_fallback)
ok("grok_timeout_seconds_light" in src_meta,
   "metadata._llm_fallback uses grok_timeout_seconds_light")
ok("grok_effort_ideas" in src_meta and "reasoning_effort" in src_meta,
   "metadata._llm_fallback pins reasoning_effort from settings.grok_effort_ideas")
src_worker = inspect.getsource(worker._llm)
ok("timeout" not in src_worker.split("complete(")[1].split(")")[0],
   "worker._llm keeps default (storyboard/script) timeout — no light override")


def _run_empty(cmd, **kw):
    return SimpleNamespace(returncode=0, stdout=None, stderr="")


with patch.object(llm.subprocess, "run", side_effect=_run_empty):
    ok(llm.complete("x") == "", "None stdout → empty string (not None)")


def _run_fail(cmd, **kw):
    return SimpleNamespace(returncode=2, stdout="", stderr="auth: not logged in\n")


raised = False
try:
    with patch.object(llm.subprocess, "run", side_effect=_run_fail):
        llm.complete("x")
except llm.GrokCLIError as e:
    raised = True
    ok("exited 2" in str(e) and "not logged in" in str(e),
       "nonzero grok → GrokCLIError with stderr tail")
    ok("grok login" in str(e) and "XAI_API_KEY" in str(e),
       "nonzero grok tells operator to refresh OIDC, no API-key fallback")
ok(raised, "nonzero grok raises (does not swallow)")


def _run_missing(*a, **k):
    raise FileNotFoundError("grok")


raised = False
try:
    with patch.object(llm.subprocess, "run", side_effect=_run_missing):
        llm.complete("x")
except llm.GrokCLIError as e:
    raised = True
    msg = str(e)
    ok("not found" in msg.lower(), "missing binary names the grok CLI")
    ok("~/.local/bin" in msg and "MANAGER_GROK_BIN" in msg,
       "missing-binary hint names ~/.local/bin (live PATH) and MANAGER_GROK_BIN")
    ok("grok login" in msg and "XAI_API_KEY" in msg,
       "missing-binary hint says refresh OIDC, no XAI_API_KEY fallback")
ok(raised, "FileNotFoundError → GrokCLIError")


def _run_hang(*a, **k):
    raise subprocess.TimeoutExpired(cmd="grok", timeout=300)


raised = False
try:
    with patch.object(llm.subprocess, "run", side_effect=_run_hang):
        llm.complete("x")
except llm.GrokCLIError as e:
    raised = True
    ok("grok.Timeout" in str(e),
       "TimeoutExpired → grok.Timeout (transient token, not npx TimeoutExpired)")
ok(raised, "TimeoutExpired raises GrokCLIError")


# ---------------------------------------------------------------------------
# Call sites: no remaining litellm.completion in manager LLM paths
# ---------------------------------------------------------------------------
print("call sites wired through llm.complete")

ok(video_gen.complete is llm.complete,
   "video_gen.generate_ideas uses llm.complete (imported)")
ok(metadata.complete is llm.complete,
   "metadata fallback uses llm.complete (imported)")
src_worker = inspect.getsource(worker._llm)
ok("from app.services.llm import complete" in src_worker
   and "complete(" in src_worker,
   "worker._llm delegates to llm.complete")
ok("litellm" not in inspect.getsource(video_gen.generate_ideas),
   "generate_ideas source has no litellm")
ok("litellm" not in inspect.getsource(metadata._llm_fallback),
   "metadata._llm_fallback source has no litellm")
ok("litellm" not in inspect.getsource(worker._llm),
   "worker._llm source has no litellm")
ok("import litellm" not in inspect.getsource(video_gen)
   and "import litellm" not in inspect.getsource(metadata)
   and "import litellm" not in inspect.getsource(worker._llm),
   "no `import litellm` on the three manager completion modules/functions")

# worker._llm is the HyperFrames seam (script, storyboard, thumbnail). It must
# forward to llm.complete at runtime, not a leftover litellm.completion.
with patch.object(llm, "complete", return_value="  from grok  ") as m:
    got = worker._llm("user prompt", system="sys prompt", max_tokens=1500)
ok(got == "  from grok  ", "worker._llm returns complete() as-is (no extra strip)")
ok(m.call_args.kwargs["system"] == "sys prompt"
   and m.call_args.kwargs["max_tokens"] == 1500
   and m.call_args.args[0] == "user prompt",
   "worker._llm forwards prompt/system/max_tokens to llm.complete")



# complete(): settings.grok_model is the default -m; explicit args win; the storyboard
# compose seam pins --reasoning-effort and keeps the HARD (default) timeout.
_captured.clear()
_orig_model = settings.grok_model
with tempfile.TemporaryDirectory() as td3:
    settings.storage_dir = td3
    # Pin the code default for this leg: a .env MANAGER_GROK_MODEL pin
    # (grok-4.7 on claw0) must not change what this unit test asserts.
    settings.grok_model = _Settings.model_fields["grok_model"].default
    with patch.object(llm.subprocess, "run", side_effect=_run_ok):
        llm.complete("plain")
    ok(_captured["cmd"] == [settings.grok_bin, "-p", "plain"],
       "default complete() argv unchanged (no -m / --reasoning-effort)")
    settings.grok_model = "grok-4.6"
    with patch.object(llm.subprocess, "run", side_effect=_run_ok):
        llm.complete("pinned")
    ok(_captured["cmd"][1:3] == ["-m", "grok-4.6"], "settings.grok_model → -m <model>")
    with patch.object(llm.subprocess, "run", side_effect=_run_ok):
        llm.complete("override", model="grok-4.7", reasoning_effort="low")
    ok(_captured["cmd"] == [settings.grok_bin, "-m", "grok-4.7", "--reasoning-effort", "low",
                            "-p", "override"], "explicit model/effort kwargs win")
settings.grok_model = _orig_model
settings.storage_dir = _orig_storage

with patch.object(llm, "complete", return_value="{}") as m:
    worker._llm_compose("sb user", system="sb sys", max_tokens=1500)
ok(m.call_args.kwargs.get("reasoning_effort") == settings.grok_compose_reasoning_effort,
   "worker._llm_compose pins reasoning_effort = settings.grok_compose_reasoning_effort")
ok("timeout" not in m.call_args.kwargs,
   "worker._llm_compose keeps the default HARD timeout (no LIGHT override)")
_orig_eff = settings.grok_compose_reasoning_effort
settings.grok_compose_reasoning_effort = ""
with patch.object(llm, "complete", return_value="{}") as m:
    worker._llm_compose("sb user")
ok(m.call_args.kwargs.get("reasoning_effort") is None,
   "empty grok_compose_reasoning_effort → no flag (kill switch)")
settings.grok_compose_reasoning_effort = _orig_eff
with patch.object(llm, "complete", return_value="x") as m:
    worker._llm("script prompt")
ok("reasoning_effort" not in m.call_args.kwargs,
   "worker._llm (script/thumb) does not pin effort — compose and light ideas/metadata do")
src_gc = inspect.getsource(worker._generate_composition)
ok("llm=_llm_compose" in src_gc, "storyboard.compose is wired to worker._llm_compose")

# ---------------------------------------------------------------------------
# Call-type effort pins, asserted at the mocked subprocess argv
# (ideas / metadata / compose / script). Never calls a real grok.
# ---------------------------------------------------------------------------
print("call-type effort pins (subprocess argv)")

import os as _os

# env: MANAGER_GROK_EFFORT_IDEAS (prefix + field name). A live .env must not
# hide this: env vars win over env_file in pydantic-settings.
with patch.dict(_os.environ, {"MANAGER_GROK_EFFORT_IDEAS": "minimal"}):
    _from_env = _Settings()
ok(_from_env.grok_effort_ideas == "minimal",
   "MANAGER_GROK_EFFORT_IDEAS is read onto Settings.grok_effort_ideas")


def _argv_for(thunk):
    """Run thunk with subprocess.run mocked; return (argv, timeout)."""
    _captured.clear()
    _storage = settings.storage_dir
    _model = settings.grok_model
    with tempfile.TemporaryDirectory() as _td:
        settings.storage_dir = _td
        settings.grok_model = _Settings.model_fields["grok_model"].default
        with patch.object(llm.subprocess, "run", side_effect=_run_ok):
            thunk()
    settings.storage_dir = _storage
    settings.grok_model = _model
    return _captured["cmd"], _captured["timeout"]


def _effort_of(cmd):
    if "--reasoning-effort" not in cmd:
        return None
    return cmd[cmd.index("--reasoning-effort") + 1]


# Pin the code defaults for this block so a claw0 .env (compose=medium already,
# ideas possibly unset) cannot change what we assert about argv.
_orig_ideas = settings.grok_effort_ideas
_orig_compose = settings.grok_compose_reasoning_effort
settings.grok_effort_ideas = _Settings.model_fields["grok_effort_ideas"].default
settings.grok_compose_reasoning_effort = _Settings.model_fields["grok_compose_reasoning_effort"].default

cmd, timeout = _argv_for(lambda: video_gen.generate_ideas("T", None, [], n=1))
ok(_effort_of(cmd) == "low",
   "generate_ideas (autofill/trends) argv has --reasoning-effort low")
ok(timeout == settings.grok_timeout_seconds_light,
   "generate_ideas keeps the light timeout")
ok(cmd[-2] == "-p", "generate_ideas still ends with -p <prompt>")

cmd, timeout = _argv_for(lambda: metadata._llm_fallback("S", "script text"))
ok(_effort_of(cmd) == "low",
   "metadata._llm_fallback argv has --reasoning-effort low")
ok(timeout == settings.grok_timeout_seconds_light,
   "metadata._llm_fallback keeps the light timeout")

cmd, timeout = _argv_for(lambda: worker._llm_compose("sb user", system="sb sys"))
ok(_effort_of(cmd) == "medium",
   "worker._llm_compose argv still has --reasoning-effort medium")
ok(timeout == settings.grok_timeout_seconds,
   "worker._llm_compose keeps the HARD timeout")

cmd, timeout = _argv_for(lambda: worker._llm("script prompt"))
ok(_effort_of(cmd) is None,
   "worker._llm (script/thumb) argv has no --reasoning-effort")
ok(timeout == settings.grok_timeout_seconds,
   "worker._llm keeps the HARD timeout")

# Kill switch and override, same mechanism as compose.
settings.grok_effort_ideas = ""
cmd, _ = _argv_for(lambda: video_gen.generate_ideas("T", None, [], n=1))
ok(_effort_of(cmd) is None,
   "empty grok_effort_ideas → generate_ideas adds no --reasoning-effort (kill switch)")
cmd, _ = _argv_for(lambda: metadata._llm_fallback("S", "script text"))
ok(_effort_of(cmd) is None,
   "empty grok_effort_ideas → metadata adds no --reasoning-effort (kill switch)")

settings.grok_effort_ideas = "medium"
cmd, _ = _argv_for(lambda: video_gen.generate_ideas("T", None, [], n=1))
ok(_effort_of(cmd) == "medium",
   "MANAGER_GROK_EFFORT_IDEAS / settings.grok_effort_ideas override reaches argv")

settings.grok_effort_ideas = _orig_ideas
settings.grok_compose_reasoning_effort = _orig_compose

print()
print(f"ALL {_checks} CHECKS PASSED")
