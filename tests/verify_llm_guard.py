"""Dependency-free checks for the LLM hard guard (app/services/llm_guard.py).

Run: PYTHONPATH=. .venv/bin/python tests/verify_llm_guard.py

Rule (CTO/CoS 2026-11-13): no Google-sourced data from a non-Owera workspace may
enter an LLM prompt (consumer ``grok login`` → training allowed; Google Limited
Use). Covered, with no network, no real grok and no DB on disk:
  - allowlist parsing (default "1" = owera, empty / malformed → nothing allowed)
  - require(): external blocked, internal allowed, unknown / no scope blocked;
    the block log line carries the event + workspace id + call type, NO content
  - llm.complete: blocked before any subprocess; allowed → spawns grok
  - mpt_client submit / social_metadata: blocked before any HTTP
  - worker.run_job: external workspace → STATE_FAILED LLMBlocked, grok never run
  - render_loop._llm_guard_block: external → FAILED (not transient), internal → pass
  - agent_preflight (growth/code/run-check agents): scoped to
    MANAGER_AGENT_RUN_WORKSPACE_IDS (default "1"); a tenant workspace with a
    channel does not block the run; that workspace is excluded from context;
    unreadable DB / empty effective set → blocked; a configured id off the
    LLM allowlist is dropped (logged) and fail-closed if none remain
  - entry points bind a scope (ideas, metadata, thumbnail, compose, render, preflight)
  - STATIC: no new LLM / subprocess call site outside the choke point (grep)
"""
from __future__ import annotations

import inspect
import json
import os
import io
import logging
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.services import llm, llm_guard, mpt_client

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = _Capture()
logging.getLogger("manager.llm_guard").addHandler(cap)
logging.getLogger("manager.llm_guard").setLevel(logging.INFO)

SECRET_PROMPT = "PROMPT-CONTENT-must-never-be-logged channel stats 12345 views"

# --------------------------------------------------------------------------- allowlist
print("allowlist")
ok(settings.llm_internal_workspace_ids == "1", "default allowlist is '1' (workspace slug owera)")
ok(llm_guard.internal_workspace_ids() == frozenset({1}), "default parses to {1}")
with patch.object(settings, "llm_internal_workspace_ids", ""):
    ok(llm_guard.internal_workspace_ids() == frozenset(), "empty → nothing allowed")
    ok(not llm_guard.is_allowed(1), "empty allowlist blocks even owera")
with patch.object(settings, "llm_internal_workspace_ids", "1,x"):
    ok(llm_guard.internal_workspace_ids() == frozenset(), "malformed → nothing allowed (never widens)")
with patch.object(settings, "llm_internal_workspace_ids", " 1 , 7 "):
    ok(llm_guard.internal_workspace_ids() == frozenset({1, 7}), "spaces tolerated")
ok(not llm_guard.is_allowed(True), "bool is not a workspace id")
ok(not llm_guard.is_allowed("abc"), "garbage id blocked")

# --------------------------------------------------------------------------- require
print("require(): external / internal / unknown")
cap.lines.clear()
try:
    with llm_guard.scope(2, "ideas"):
        llm_guard.require()
    ok(False, "external workspace 2 must be blocked")
except llm_guard.LLMBlocked as e:
    ok(e.workspace_id == 2 and e.call_type == "ideas", "external workspace blocked (LLMBlocked)")
ok(any(l == "llm_blocked_external_google_data workspace_id=2 call_type=ideas" for l in cap.lines),
   "block log line: event + workspace id + call type")

with llm_guard.scope(1, "ideas"):
    ok(llm_guard.require() == 1, "internal workspace 1 allowed")

for ws in (None, 999, "nope"):
    try:
        with llm_guard.scope(ws, "metadata"):
            llm_guard.require()
        ok(False, f"unknown workspace {ws!r} must be blocked")
    except llm_guard.LLMBlocked:
        ok(True, f"unknown workspace {ws!r} blocked")

try:
    llm_guard.require("thumbnail")
    ok(False, "no scope must be blocked")
except llm_guard.LLMBlocked as e:
    ok(e.workspace_id is None and e.call_type == "thumbnail", "no scope at all → blocked (fail closed)")

ok(issubclass(llm_guard.LLMBlocked, llm.GrokCLIError),
   "LLMBlocked is a GrokCLIError (existing grok-failure paths fail hard, no fallback)")
from app.services import render_loop  # noqa: E402
ok(not any(sig in str(llm_guard.LLMBlocked(2, "render")) for sig in render_loop._TRANSIENT),
   "block message matches no render_loop._TRANSIENT signature (no retry loop)")
with llm_guard.scope(1, "render"):
    with llm_guard.scope(2, "thumbnail"):
        ok(llm_guard.current() == (2, "thumbnail"), "nested scope overrides")
    ok(llm_guard.current() == (1, "render"), "scope restored on exit")
ok(llm_guard.current() is None, "no scope leaks after with")

# --------------------------------------------------------------------------- llm.complete
print("llm.complete is guarded before spawning")
calls = []


def _fake_run(cmd, **kw):
    calls.append(cmd)
    return SimpleNamespace(returncode=0, stdout="ok out", stderr="")


cap.lines.clear()
with patch.object(subprocess, "run", side_effect=_fake_run):
    with llm_guard.scope(2, "storyboard_compose"):
        try:
            llm.complete(SECRET_PROMPT, system="sys")
            ok(False, "complete must raise for external workspace")
        except llm_guard.LLMBlocked:
            ok(True, "complete raises LLMBlocked for external workspace")
    ok(calls == [], "no subprocess spawned when blocked")
    try:
        llm.complete(SECRET_PROMPT)
        ok(False, "complete without scope must raise")
    except llm_guard.LLMBlocked:
        ok(calls == [], "complete without any scope → blocked, nothing spawned")
    with llm_guard.scope(1, "storyboard_compose"):
        out = llm.complete(SECRET_PROMPT)
    ok(out == "ok out" and len(calls) == 1 and calls[0][0] == settings.grok_bin,
       "internal workspace → grok spawned")
ok(all("PROMPT-CONTENT" not in l and "12345" not in l for l in cap.lines),
   "block log never contains prompt content")

# --------------------------------------------------------------------------- MPT
print("MoneyPrinterTurbo (remote LLM) is guarded before HTTP")


class _NoHTTP:
    def __init__(self, *a, **k):
        raise AssertionError("HTTP must not be attempted when blocked")


with patch.object(mpt_client.httpx, "Client", _NoHTTP):
    with llm_guard.scope(2, "render_submit"):
        for name, call in (("submit", lambda: mpt_client.mpt.submit({"video_subject": "x"})),
                           ("social_metadata", lambda: mpt_client.mpt.social_metadata("s", "x"))):
            try:
                call()
                ok(False, f"mpt.{name} must raise for external workspace")
            except llm_guard.LLMBlocked:
                ok(True, f"mpt.{name} blocked before HTTP (raises, not None)")
    try:
        mpt_client.mpt.social_metadata("s", "x")
        ok(False, "mpt.social_metadata without scope must raise")
    except llm_guard.LLMBlocked:
        ok(True, "mpt.social_metadata without scope blocked")

# --------------------------------------------------------------------------- worker.run_job
print("worker.run_job binds the channel workspace")
from app.services.engines import worker  # noqa: E402

statuses = []
calls.clear()
with patch.object(worker, "_status", side_effect=lambda h, **f: statuses.append(f)), \
        patch.object(subprocess, "run", side_effect=_fake_run):
    worker.run_job("h-ext", Path("/nonexistent-job"), "subject",
                   {"workspace_id": 2, "channel_id": 3, "content_format": "short"})
fail = [s for s in statuses if s.get("state") == worker.STATE_FAILED]
ok(fail and "LLMBlocked" in fail[-1].get("error", "")
   and "llm_blocked_external_google_data" in fail[-1]["error"],
   "external workspace render → STATE_FAILED LLMBlocked")
ok(not any(c and c[0] == settings.grok_bin for c in calls), "grok never spawned for external render")
statuses.clear()
with patch.object(worker, "_status", side_effect=lambda h, **f: statuses.append(f)), \
        patch.object(subprocess, "run", side_effect=_fake_run):
    worker.run_job("h-none", Path("/nonexistent-job"), "subject", {"content_format": "short"})
fail = [s for s in statuses if s.get("state") == worker.STATE_FAILED]
ok(fail and "LLMBlocked" in fail[-1].get("error", ""), "render without workspace_id → blocked")
ok(llm_guard.current() is None, "run_job does not leak its scope")

# --------------------------------------------------------------------------- render_loop
print("render_loop parks external-workspace rows FAILED before submit")
from app.models import VideoStatus  # noqa: E402

logged = []


class _Sess:
    def add(self, o):
        pass


with patch.object(render_loop.quota, "log", side_effect=lambda *a, **k: logged.append(k)):
    v = SimpleNamespace(id=10, status=VideoStatus.QUEUED, error=None)
    ok(render_loop._llm_guard_block(_Sess(), v, SimpleNamespace(id=3, workspace_id=2)) is True,
       "external channel → blocked")
    ok(v.status == VideoStatus.FAILED and "llm_blocked_external_google_data" in v.error,
       "row FAILED with the block reason")
    n = len(logged)
    render_loop._llm_guard_block(_Sess(), v, SimpleNamespace(id=3, workspace_id=2))
    ok(len(logged) == n, "no repeat JobRun while the row is already FAILED with that reason")
    v2 = SimpleNamespace(id=11, status=VideoStatus.QUEUED, error=None)
    ok(render_loop._llm_guard_block(_Sess(), v2, SimpleNamespace(id=1, workspace_id=1)) is False
       and v2.status == VideoStatus.QUEUED, "owera channel → not blocked")
src = inspect.getsource(render_loop._submit_new)
ok(src.index("_llm_guard_block(") < src.index("engine.submit("),
   "_submit_new checks the guard before submit")
ok('params["workspace_id"] = channel.workspace_id' in src, "render params carry workspace_id")

# --------------------------------------------------------------------------- agents
print("agent preflight + scoped context (growth / code / run-check)")
from app.models import Channel, Workspace  # noqa: E402
import app.db as _db  # noqa: E402

ok(settings.agent_run_workspace_ids == "1", "default agent-run env is '1'")
ok(llm_guard.agent_run_workspace_ids() == frozenset({1}), "default agent-run parses to {1}")

eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Channel(id=1, workspace_id=1, slug="os", name="Owera Software"))
    s.commit()
    cap.lines.clear()
    ok(llm_guard.agent_preflight("growth_agent", session=s) is True, "only owera channels → allowed")
    s.add(Workspace(id=2, slug="tenant-b", name="Tenant B"))
    s.add(Workspace(id=5, slug="buyer", name="Buyer"))
    s.commit()
    ok(llm_guard.agent_preflight("growth_agent", session=s) is True,
       "external workspace with no channel (no Google data yet) → allowed")
    s.add(Channel(id=9, workspace_id=2, slug="elen3x", name="TENANT-B-SECRET-CHANNEL"))
    s.add(Channel(id=10, workspace_id=5, slug="buyer-yt", name="Buyer YT SECRET"))
    s.commit()
    for ct in ("growth_agent", "code_agent", "run_check"):
        cap.lines.clear()
        ok(llm_guard.agent_preflight(ct, session=s) is True,
           f"{ct}: ws2 with a channel does not block a run scoped to ws1")
        ctx = llm_guard.agent_run_context(session=s)
        ok(ctx is not None and ctx["workspace_ids"] == [1],
           f"{ct}: context workspace_ids == [1]")
        ok(all(c["workspace_id"] == 1 for c in ctx["channels"]),
           f"{ct}: context channels are ws1 only")
        blob = json.dumps(ctx)
        ok("TENANT-B-SECRET-CHANNEL" not in blob and "Buyer YT SECRET" not in blob
           and "tenant-b" not in blob and not any(w["id"] in (2, 5) for w in ctx["workspaces"]),
           f"{ct}: ws2/buyer excluded from context")
        ok(llm_guard.workspace_of_channel(9, session=s) == 2, "workspace_of_channel still reads the DB")
    ok(llm_guard.workspace_of_channel(404, session=s) is None, "unknown channel → None (blocked)")

    # A configured value other than the default.
    s.add(Workspace(id=7, slug="internal-b", name="Internal B"))
    s.add(Channel(id=17, workspace_id=7, slug="ib", name="Internal B Channel"))
    s.commit()
    with patch.object(settings, "llm_internal_workspace_ids", "1,7"), \
            patch.object(settings, "agent_run_workspace_ids", "7"):
        for ct in ("growth_agent", "code_agent", "run_check"):
            ok(llm_guard.agent_preflight(ct, session=s) is True,
               f"{ct}: configured AGENT_RUN=7 is allowed when 7 is on the LLM allowlist")
            ctx = llm_guard.agent_run_context(session=s)
            ok(ctx["workspace_ids"] == [7], f"{ct}: configured context is ws7 only")
            names = {c["name"] for c in ctx["channels"]}
            slugs = {w["slug"] for w in ctx["workspaces"]}
            ok("Internal B Channel" in names and "Owera Software" not in names
               and "TENANT-B-SECRET-CHANNEL" not in names,
               f"{ct}: configured context has ws7 channel, not ws1/ws2")
            ok(slugs == {"internal-b"}, f"{ct}: configured context workspace slug is internal-b")

    # Defense: agent-run env includes a workspace off the LLM allowlist → drop it.
    cap.lines.clear()
    with patch.object(settings, "agent_run_workspace_ids", "1,2"):
        ok(llm_guard.agent_preflight("growth_agent", session=s) is True,
           "AGENT_RUN includes ws2 (not on LLM allowlist) → ws2 dropped, run continues")
        ok(any("workspace_id=2" in ln and "dropped_from_agent_run" in ln for ln in cap.lines),
           "dropped ws2 is logged with llm_blocked_external_google_data")
        ctx = llm_guard.agent_run_context(session=s)
        ok(ctx["workspace_ids"] == [1] and "TENANT-B-SECRET-CHANNEL" not in json.dumps(ctx),
           "dropped ws2 never enters the context")

    with patch.object(settings, "agent_run_workspace_ids", "2"):
        ok(llm_guard.agent_preflight("growth_agent", session=s) is False,
           "AGENT_RUN=2 only (off the LLM allowlist) → fail closed")
    with patch.object(settings, "agent_run_workspace_ids", ""):
        ok(llm_guard.agent_preflight("growth_agent", session=s) is False,
           "empty AGENT_RUN → fail closed")
    with patch.object(settings, "agent_run_workspace_ids", "1,foo"):
        ok(llm_guard.agent_preflight("growth_agent", session=s) is False,
           "malformed AGENT_RUN → fail closed (never widens)")

    # Subprocess mocked: agent-exec for all three call types, ws2 excluded.
    print("agent-exec (subprocess mocked): context is ws1 only")
    playbook = Path(os.environ.get("TMPDIR", "/tmp")) / "cm-agent-run-playbook.md"
    playbook.write_text("PLAYBOOK BODY\n", encoding="utf-8")
    with patch.object(_db, "engine", eng):
        for ct in ("growth_agent", "code_agent", "run_check"):
            with patch.object(os, "execvpe") as ex:
                rc = llm.main(["agent-exec", ct, "--", "grok", "--prompt-file", str(playbook)])
            ok(rc == 0 and ex.call_count == 1 and ex.call_args[0][0] == "grok",
               f"{ct}: agent-exec not blocked, execs grok (no real LLM)")
            argv, env = ex.call_args[0][1], ex.call_args[0][2]
            ok("--prompt-file" in argv, f"{ct}: prompt-file present")
            pf = Path(argv[argv.index("--prompt-file") + 1])
            text_pf = pf.read_text(encoding="utf-8")
            ok("PLAYBOOK BODY" in text_pf and "Owera Software" in text_pf
               and "slug=owera" in text_pf,
               f"{ct}: combined prompt has scope + playbook and ws1 data")
            ok("TENANT-B-SECRET-CHANNEL" not in text_pf and "Buyer YT SECRET" not in text_pf
               and "tenant-b" not in text_pf and "slug=buyer" not in text_pf,
               f"{ct}: combined prompt excludes ws2/buyer")
            ok(env.get("MANAGER_AGENT_RUN_WORKSPACE_IDS") == "1",
               f"{ct}: child env carries effective workspace ids = 1")
            ok("XAI_API_KEY" not in env, f"{ct}: API keys still stripped")
            try:
                pf.unlink()
            except OSError:
                pass
    try:
        playbook.unlink()
    except OSError:
        pass

    with patch.object(settings, "llm_internal_workspace_ids", "1,7"), \
            patch.object(settings, "agent_run_workspace_ids", "7"), \
            patch.object(_db, "engine", eng), \
            patch.object(os, "execvpe") as ex:
        playbook2 = Path(os.environ.get("TMPDIR", "/tmp")) / "cm-agent-run-playbook-7.md"
        playbook2.write_text("PLAYBOOK BODY\n", encoding="utf-8")
        rc = llm.main(["agent-exec", "growth_agent", "--", "grok", "--prompt-file", str(playbook2)])
        argv = ex.call_args[0][1]
        env = ex.call_args[0][2]
        text_pf = Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8")
        ok(rc == 0 and env.get("MANAGER_AGENT_RUN_WORKSPACE_IDS") == "7"
           and "Internal B Channel" in text_pf and "Owera Software" not in text_pf
           and "TENANT-B-SECRET-CHANNEL" not in text_pf,
           "configured AGENT_RUN=7: exec prompt has only ws7")
        try:
            playbook2.unlink()
        except OSError:
            pass


class _BadSession:
    def exec(self, *a, **k):
        raise RuntimeError("db unreadable")


ok(llm_guard.agent_preflight("code_agent", session=_BadSession()) is False,
   "unreadable DB → agent blocked (fail closed)")
with patch.object(llm_guard, "agent_preflight", return_value=True), \
        patch.object(llm_guard, "agent_run_context",
                     return_value={"workspace_ids": [1], "workspaces": [], "channels": []}):
    ok(llm_guard.main(["agent-preflight", "run_check"]) == 0, "CLI exit 0 when allowed")
with patch.object(llm_guard, "agent_preflight", return_value=False):
    ok(llm_guard.main(["agent-preflight", "run_check"]) == 3, "CLI exit 3 when blocked")
ok(llm_guard.main(["bogus"]) == 2, "CLI usage error → 2 (non-zero = blocked by the shell helper)")

# --------------------------------------------------------------------------- entry points
print("entry points bind a workspace scope")
from app.routers import topics, trends, videos  # noqa: E402
from app.services import autofill_loop, publish_loop, script_compose  # noqa: E402

for name, fn in (("topics.generate_videos", topics.generate_videos),
                 ("trends.adopt_trend", trends.adopt_trend),
                 ("autofill_loop._refill_topic", autofill_loop._refill_topic),
                 ("videos.regenerate_metadata", videos.regenerate_metadata),
                 ("script_compose.compose_into", script_compose.compose_into),
                 ("render_loop._submit_new", render_loop._submit_new),
                 ("render_loop (metadata after render)", render_loop),
                 ("publish_loop (thumbnail)", publish_loop)):
    ok("llm_guard.scope(" in inspect.getsource(fn), f"{name} binds llm_guard.scope")
ok("_llm_guard.bind(" in inspect.getsource(worker.run_job), "worker.run_job binds the scope")
rr = (ROOT / "run" / "rubric_review.py").read_text()
ok("llm_guard.scope(" in rr, "run/rubric_review.py binds llm_guard.scope")

# --------------------------------------------------------------------------- STATIC grep
print("static: no LLM / subprocess call site outside the choke point")


def _docstring_lines(src: str) -> set[int]:
    """Line numbers of bare string-expression statements (docstrings / notes)."""
    import ast
    out: set[int] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def _code_lines(path: Path):
    src = path.read_text(errors="replace")
    skip = _docstring_lines(src)
    for i, line in enumerate(src.splitlines(), 1):
        st = line.strip()
        if st.startswith("#") or i in skip:
            continue
        yield i, line.split("  # ")[0] if "  # " in line else line


PY_FILES = [p for d in ("app", "run", "scripts") for p in (ROOT / d).rglob("*.py")]
rel = lambda p: str(p.relative_to(ROOT))  # noqa: E731

# 1) every process spawn in app/ run/ scripts/ is a known, non-LLM command (or llm.py)
KNOWN_SPAWN = {
    "app/main.py": "sysctl / ps (health)",
    "app/services/render_loop.py": "ffmpeg still",
    "app/services/thumbnail.py": "npx hyperframes / ffmpeg",
    "app/services/craft.py": "ffprobe",
    "app/services/provided_thumb.py": "ffmpeg",
    "app/services/engines/worker.py": "ffprobe / ffmpeg / npx hyperframes",
    "app/services/llm.py": "grok -p (THE choke point)",
    "run/rubric_review.py": "ffmpeg frames",
}
spawn_re = re.compile(r"\b(subprocess\.(run|Popen|call|check_call|check_output|getoutput|getstatusoutput)"
                      r"|create_subprocess_(exec|shell)|os\.(system|popen|exec\w*|spawn\w*)|pty\.spawn)\b")
found = {}
for p in PY_FILES:
    for i, line in _code_lines(p):
        if spawn_re.search(line):
            found.setdefault(rel(p), []).append(i)
new_spawn = sorted(set(found) - set(KNOWN_SPAWN))
ok(not new_spawn, f"no process spawn outside the known non-LLM sites (new: {new_spawn})")

# 2) grok is only spawned by llm.py — no other file names the binary in an argv
grok_argv = re.compile(r"""(\[\s*["']grok["']|settings\.grok_bin|["']grok["']\s*,\s*["']-p["'])""")
grok_sites = sorted({rel(p) for p in PY_FILES for _, l in _code_lines(p) if grok_argv.search(l)})
ok(grok_sites == ["app/config.py", "app/services/llm.py"] or grok_sites == ["app/services/llm.py"],
   f"grok argv only in llm.py (+ the config default): {grok_sites}")
csrc = inspect.getsource(llm.complete)
ok(csrc.index("llm_guard.require(") < csrc.index("_complete_api(")
   and csrc.index("llm_guard.require(") < csrc.index("_complete_cli("),
   "llm.complete calls llm_guard.require before either backend (cli / api)")
ok("subprocess.run(" in inspect.getsource(llm._complete_cli)
   and "subprocess.run(" not in csrc, "grok -p spawn lives only in llm._complete_cli")

# 3) no HTTP LLM client anywhere in app/ run/ scripts/
http_llm = re.compile(r"(api\.x\.ai|api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis"
                      r"|/chat/completions|/v1/messages|\bimport (openai|anthropic|litellm|xai_sdk)\b"
                      r"|\bfrom (openai|anthropic|litellm|xai_sdk)\b|aiplatform\.googleapis)")
_XAI_HOME = {"app/config.py", "app/services/llm.py"}   # the one LLM client (+ its config)
http_sites = sorted({f"{rel(p)}:{i}" for p in PY_FILES for i, l in _code_lines(p)
                     if http_llm.search(l)
                     and not (rel(p) in _XAI_HOME and "api.x.ai" in l and "import" not in l)})
ok(not http_sites, f"no HTTP LLM client call site (found: {http_sites})")
# the xAI API (Enterprise) backend lives only in llm.py; its base URL only in config.py
xai_sites = sorted({rel(p) for p in PY_FILES for _, l in _code_lines(p)
                    if re.search(r"api\.x\.ai|/responses\b|xai_api_key", l)})
ok(set(xai_sites) <= {"app/config.py", "app/services/llm.py"},
   f"xAI API base URL / key / endpoint only in config.py + llm.py: {xai_sites}")
asrc = inspect.getsource(llm._complete_api)
ok(asrc.index("check_budget(") < asrc.index('.post("/responses"'),
   "api backend checks the budget before POST")

# 4) MPT LLM endpoints only via the guarded client methods
mpt_eps = sorted({rel(p) for p in PY_FILES for _, l in _code_lines(p)
                  if re.search(r"""\.post\(\s*f?["'](/videos|/social-metadata)""", l)})
ok(mpt_eps == ["app/services/mpt_client.py"], f"MPT LLM endpoints only in mpt_client.py: {mpt_eps}")
for m in (mpt_client.MPTClient.submit, mpt_client.MPTClient.social_metadata):
    ms = inspect.getsource(m)
    ok(ms.index("llm_guard.require(") < ms.index(".post("), f"MPTClient.{m.__name__} guarded before POST")

# 5) only the known wrappers call llm.complete
complete_users = sorted({rel(p) for p in PY_FILES for _, l in _code_lines(p)
                         if re.search(r"from app\.services\.llm import [^\n]*\bcomplete\b|llm\.complete\(", l)})
ok(complete_users == ["app/services/engines/worker.py", "app/services/metadata.py",
                      "app/services/video_gen.py"],
   f"llm.complete imported only by worker/metadata/video_gen: {complete_users}")

# 6) every headless grok agent in run/*.sh runs the preflight before grok
for sh in sorted((ROOT / "run").glob("*.sh")):
    text = sh.read_text()
    lines = text.splitlines()
    grok_lines = [i for i, l in enumerate(lines)
                  if not l.strip().startswith("#")
                  and re.search(r"(^|\s|watchdog\s)grok\s+(-p|--prompt|--single|--prompt-file)", l)]
    if not grok_lines:
        continue
    pre = [i for i, l in enumerate(lines) if re.match(r"\s*if ! llm_guard_preflight \w+", l)]
    ok(pre and min(pre) < min(grok_lines), f"{sh.name}: llm_guard_preflight before the grok run")
    ok(all("agent-exec" in lines[i] for i in grok_lines),
       f"{sh.name}: grok runs through the LLM client (llm agent-exec)")

print(f"\nALL OK ({_checks} checks)")
