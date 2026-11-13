"""LLM backend switch (cli | xAI API) — mocked HTTP transport, no real calls, no key.

Run: PYTHONPATH=. .venv/bin/python tests/verify_llm_backend.py

Covers: cli is the default (and anything but "api" stays cli); api backend request
shape (POST /responses, Bearer header, model mapping, effort, store=false); output
text; usage JSONL row (tokens/model/call type/workspace/ts/cost, NO content, NO key);
cost from the price table and from cost_in_usd_ticks; monthly cap reached → blocked,
no cap → blocked, unreadable ledger → blocked; missing key → clear error; timeout →
"grok.Timeout" (transient, like the CLI); HTTP 4xx/5xx → GrokCLIError, one attempt
(no in-client retry, like the CLI); the (B) guard still applies in api mode; agent
exec plan (cli unchanged / api isolated GROK_HOME + budget row).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from app.config import Settings, settings
from app.services import llm, llm_guard

_checks = 0
FAKE_KEY = "xai-TEST-not-a-real-key-000"
PROMPT = "SECRET-PROMPT channel analytics 98765 views"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, r):
        self.lines.append(r.getMessage())


cap = _Cap()
for n in ("manager.llm", "manager.llm_guard"):
    logging.getLogger(n).addHandler(cap)
    logging.getLogger(n).setLevel(logging.INFO)

tmp = Path(tempfile.mkdtemp())
USAGE = tmp / "llm-usage.jsonl"

# ------------------------------------------------------------------ defaults
print("defaults: cli unless LLM_BACKEND=api")
ok(settings.llm_backend == "cli" and llm.backend() == "cli", "default backend is cli")
for v in ("", "CLI", "nope", "apii"):
    with patch.object(settings, "llm_backend", v):
        ok(llm.backend() == "cli", f"llm_backend={v!r} → cli")
with patch.dict(os.environ, {"LLM_BACKEND": "api", "LLM_MONTHLY_USD_CAP": "25",
                             "XAI_API_KEY": FAKE_KEY}):
    s2 = Settings()
    ok(s2.llm_backend == "api" and s2.llm_monthly_usd_cap == 25.0
       and s2.xai_api_key.get_secret_value() == FAKE_KEY,
       "env names LLM_BACKEND / LLM_MONTHLY_USD_CAP / XAI_API_KEY are read")
    ok(FAKE_KEY not in repr(s2) and FAKE_KEY not in str(s2.model_dump()),
       "the key never shows in repr(settings) / model_dump")
with patch.dict(os.environ, {"MANAGER_LLM_BACKEND": "api"}):
    ok(Settings().llm_backend == "api", "MANAGER_LLM_BACKEND alias also works")
ok(settings.llm_monthly_usd_cap == 0.0, "no default cap (api mode fails closed without one)")

# cli path unchanged by default: subprocess grok, no HTTP
calls = []
with llm_guard.scope(1, "ideas"), \
        patch.object(subprocess, "run",
                     side_effect=lambda cmd, **k: calls.append(cmd) or SimpleNamespace(
                         returncode=0, stdout=" cli out ", stderr="")), \
        patch.object(llm, "_http_client", side_effect=AssertionError("no HTTP in cli mode")):
    ok(llm.complete("x") == "cli out" and calls and calls[0][0] == settings.grok_bin,
       "cli backend: grok -p spawned, no HTTP")

# ------------------------------------------------------------------ api helpers
seen = []


def _resp_ok(req: httpx.Request) -> httpx.Response:
    seen.append(req)
    return httpx.Response(200, json={
        "id": "r1", "status": "completed", "model": "grok-4.7",
        "output": [{"type": "reasoning", "summary": []},
                   {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "  hello api  "}]}],
        "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200},
                  "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 400},
                  "total_tokens": 1500}})


def _client_with(handler):
    def factory(timeout):
        return httpx.Client(base_url=settings.llm_api_base_url, timeout=timeout,
                            transport=httpx.MockTransport(handler))
    return factory


def api_env(**over):
    vals = {"llm_backend": "api", "xai_api_key": SecretStr(FAKE_KEY),
            "llm_monthly_usd_cap": 10.0, "llm_usage_path": str(USAGE),
            "grok_model": "grok-4.7", "llm_price_table_json": "",
            "xai_zdr_confirmed": True, "llm_agent_api_stateless_verified": True}
    vals.update(over)
    return [patch.object(settings, k, v) for k, v in vals.items()]


class _Multi:
    def __init__(self, ps):
        self.ps = ps

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()


def rows():
    if not USAGE.exists():
        return []
    return [json.loads(l) for l in USAGE.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------------ api ok
print("api backend: request, output, usage row")
cap.lines.clear()
with _Multi(api_env()), patch.object(llm, "_http_client", side_effect=_client_with(_resp_ok)), \
        patch.object(subprocess, "run", side_effect=AssertionError("no grok in api mode")):
    with llm_guard.scope(1, "storyboard_compose"):
        out = llm.complete(PROMPT, system="SYS-INSTR", reasoning_effort="medium", timeout=123)
ok(out == "hello api", "output_text extracted and stripped")
req = seen[-1]
body = json.loads(req.content)
ok(req.method == "POST" and str(req.url) == "https://api.x.ai/v1/responses",
   "POST https://api.x.ai/v1/responses")
ok(req.headers.get("authorization") == f"Bearer {FAKE_KEY}", "Bearer key in the header only")
ok(body["model"] == "grok-4.7" and body["input"] == PROMPT and body["instructions"] == "SYS-INSTR"
   and body["reasoning"] == {"effort": "medium"} and body["store"] is False,
   "body: model grok-4.7, input, instructions=system, reasoning.effort, store=false")
r = rows()[-1]
ok(r["call_type"] == "storyboard_compose" and r["workspace_id"] == 1 and r["model"] == "grok-4.7"
   and r["input_tokens"] == 1000 and r["cached_input_tokens"] == 200
   and r["output_tokens"] == 100 and r["reasoning_tokens"] == 400 and r["ts"].endswith("+00:00"),
   "usage row: tokens in/cached/out/reasoning, model, call type, workspace, UTC ts")
exp = (800 * 2.0 + 200 * 0.5 + 500 * 6.0) / 1e6
ok(abs(r["cost_usd_estimated"] - exp) < 1e-9 and r["cost_usd"] == r["cost_usd_estimated"],
   f"cost estimated from the price table (${exp:.6f})")
raw = USAGE.read_text()
ok(PROMPT not in raw and "SYS-INSTR" not in raw and "hello api" not in raw and FAKE_KEY not in raw,
   "usage JSONL has no prompt, system, output or key")
ok(all(FAKE_KEY not in l and "SECRET-PROMPT" not in l for l in cap.lines),
   "logs carry no key and no content")

ok(set(body) <= {"model", "input", "instructions", "reasoning", "store"}
   and "previous_response_id" not in body and "conversation" not in body
   and "background" not in body and body["store"] is False and isinstance(body["input"], str),
   "ZDR: stateless payload — store=false explicit, no previous_response_id/conversation/background")
for bad in ({"model": "m", "input": "x", "store": True},
            {"model": "m", "input": "x", "store": False, "previous_response_id": "r0"},
            {"model": "m", "input": "x"},
            {"model": "m", "input": [{"role": "user", "content": "x"}], "store": False}):
    try:
        llm._assert_stateless(bad)
        ok(False, f"stateful body must be refused: {sorted(bad)}")
    except llm.LLMConfigError:
        ok(True, f"stateful/implicit body refused: {sorted(bad)}")
use = [l for l in cap.lines if l.startswith("llm_usage ")]
ok(use and "model=grok-4.7" in use[-1] and "input_tokens=1000" in use[-1]
   and "output_tokens=100" in use[-1] and "call_type=storyboard_compose" in use[-1]
   and "workspace_id=1" in use[-1], "per-call log line: model + usage (no content)")

print("ZDR boot check")
with _Multi(api_env(xai_zdr_confirmed=False)), \
        patch.object(llm, "_http_client", side_effect=AssertionError("no HTTP without ZDR")):
    try:
        llm.boot_check()
        ok(False, "boot must refuse api without ZDR")
    except llm.LLMZDRNotConfirmed as e:
        ok("XAI_ZDR_CONFIRMED=1" in str(e), "boot_check: api without XAI_ZDR_CONFIRMED=1 → clear error")
    try:
        with llm_guard.scope(1, "ideas"):
            llm.complete("x")
        ok(False, "call must refuse without ZDR")
    except llm.LLMZDRNotConfirmed:
        ok(True, "per-call: api without ZDR refused too (no HTTP)")
    with patch.object(llm_guard, "agent_preflight", return_value=True):
        try:
            llm.agent_exec_plan("growth_agent", ["grok"])
            ok(False, "agent must refuse without ZDR")
        except llm.LLMZDRNotConfirmed:
            ok(True, "agent-exec: api without ZDR refused")
with _Multi(api_env()):
    llm.boot_check()
    ok(True, "boot_check passes with api + XAI_ZDR_CONFIRMED=1")
with patch.object(settings, "xai_zdr_confirmed", False):
    llm.boot_check()
    ok(True, "boot_check is a no-op in cli mode (default)")
with patch.dict(os.environ, {"XAI_ZDR_CONFIRMED": "1"}):
    ok(Settings().xai_zdr_confirmed is True, "env XAI_ZDR_CONFIRMED=1 is read")
ok(Settings().xai_zdr_confirmed is False or os.environ.get("XAI_ZDR_CONFIRMED"),
   "default: ZDR not confirmed")
import inspect  # noqa: E402
import app.main as _main  # noqa: E402
_ls = inspect.getsource(_main.lifespan)
ok(_ls.index("boot_check()") < _ls.index("ensure_dirs()"), "app lifespan runs the ZDR boot check first")
with _Multi(api_env(llm_agent_api_stateless_verified=False)), \
        patch.object(llm_guard, "agent_preflight", return_value=True):
    try:
        llm.agent_exec_plan("growth_agent", ["grok"])
        ok(False, "unverified agent statelessness must refuse")
    except llm.LLMConfigError as e:
        ok("STATELESS_VERIFIED" in str(e), "api agents refused until CLI statelessness is verified")

# model mapping: CLI default (empty) → llm_api_default_model
with _Multi(api_env(grok_model="")), patch.object(llm, "_http_client",
                                                  side_effect=_client_with(_resp_ok)):
    with llm_guard.scope(1, "ideas"):
        llm.complete("x")
ok(json.loads(seen[-1].content)["model"] == "grok-4.7" and "reasoning" not in json.loads(seen[-1].content),
   "empty grok_model → grok-4.7 (api default); no effort → no reasoning field")


# reported cost wins
def _resp_ticks(req):
    seen.append(req)
    return httpx.Response(200, json={"status": "completed", "output_text": "t",
                                     "usage": {"input_tokens": 10, "output_tokens": 5,
                                               "cost_in_usd_ticks": 250_000_000}})


with _Multi(api_env()), patch.object(llm, "_http_client", side_effect=_client_with(_resp_ticks)):
    with llm_guard.scope(1, "metadata"):
        llm.complete("x")
ok(rows()[-1]["cost_usd_reported"] == 0.025 and rows()[-1]["cost_usd"] == 0.025,
   "cost_in_usd_ticks (1e10 = $1) recorded and used for the budget")

# long-context pricing
ok(abs(llm.estimate_cost("grok-4.7", 200_000, 0, 0) - 0.8) < 1e-9,
   "≥200k prompt tokens → long-context input rate ($4/1M)")
with patch.object(settings, "llm_price_table_json", json.dumps({"grok-4.7": {"input": 1, "output": 1}})):
    ok(abs(llm.estimate_cost("grok-4.7", 1_000_000, 0, 0) - 1.0) < 1e-9,
       "price table override via MANAGER_LLM_PRICE_TABLE_JSON")
with patch.object(settings, "llm_price_table_json", "{bad"):
    try:
        llm.price_table()
        ok(False, "bad price JSON must raise")
    except llm.LLMConfigError:
        ok(True, "malformed price table → LLMConfigError (fail closed)")

# ------------------------------------------------------------------ budget
print("budget guard (fail closed)")
n_before = len(seen)
for label, over in (("cap not set", {"llm_monthly_usd_cap": 0.0}),
                    ("cap reached", {"llm_monthly_usd_cap": 0.000001})):
    with _Multi(api_env(**over)), patch.object(llm, "_http_client", side_effect=_client_with(_resp_ok)):
        try:
            with llm_guard.scope(1, "ideas"):
                llm.complete("x")
            ok(False, f"{label} must block")
        except llm.LLMBudgetExceeded as e:
            ok("llm_budget_blocked" in str(e), f"{label} → LLMBudgetExceeded")
ok(len(seen) == n_before, "no HTTP when the budget blocks")
ok(any("llm_budget_blocked reason=cap_reached" in l for l in cap.lines)
   and any("llm_budget_blocked reason=cap_not_set" in l for l in cap.lines), "budget blocks logged")
from datetime import datetime, timezone  # noqa: E402
old = tmp / "old.jsonl"
old.write_text(json.dumps({"ts": "2020-01-15T00:00:00+00:00", "cost_usd": 999}) + "\n"
               + "not json\n"
               + json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "cost_usd": 1.5}) + "\n")
with patch.object(settings, "llm_usage_path", str(old)):
    ok(abs(llm.month_to_date_usd() - 1.5) < 1e-9, "month-to-date sums only this UTC month (bad lines skipped)")
with _Multi(api_env(llm_usage_path=str(tmp))):   # a directory → unreadable ledger
    try:
        with llm_guard.scope(1, "ideas"):
            llm.complete("x")
        ok(False, "unreadable ledger must block")
    except llm.LLMBudgetExceeded:
        ok(True, "unreadable ledger → blocked")
ok(not any(sig in str(llm.LLMBudgetExceeded("llm_budget_blocked: x"))
           for sig in __import__("app.services.render_loop", fromlist=["_TRANSIENT"])._TRANSIENT),
   "budget block is not transient (no render retry loop)")

# ------------------------------------------------------------------ errors
print("errors: no key, timeout, HTTP, one attempt")
with _Multi(api_env(xai_api_key=SecretStr(""))):
    try:
        with llm_guard.scope(1, "ideas"):
            llm.complete("x")
        ok(False, "missing key must raise")
    except llm.LLMConfigError as e:
        ok("XAI_API_KEY is not set" in str(e), "missing key → clear LLMConfigError, no CLI fallback")
attempts = []


def _timeout(req):
    attempts.append(req)
    raise httpx.ReadTimeout("slow", request=req)


with _Multi(api_env()), patch.object(llm, "_http_client", side_effect=_client_with(_timeout)):
    try:
        with llm_guard.scope(1, "storyboard_compose"):
            llm.complete("x", timeout=7)
        ok(False, "timeout must raise")
    except llm.GrokCLIError as e:
        ok(str(e).startswith("grok.Timeout") and "7s" in str(e),
           "timeout → 'grok.Timeout: …' (same transient token as the CLI)")
ok(len(attempts) == 1, "one attempt (no in-client retry, like the CLI)")
ok(rows()[-1]["status"] == "timeout", "timeout recorded in usage (cost 0)")
with patch.object(llm, "_http_client", wraps=llm._http_client) as hc, _Multi(api_env()), \
        patch("httpx.Client.post", side_effect=httpx.ReadTimeout("x")):
    try:
        with llm_guard.scope(1, "ideas"):
            llm.complete("x", timeout=33)
    except llm.GrokCLIError:
        pass
    ok(hc.call_args.args[0] == 33, "the caller's timeout (CLI semantics) is the HTTP timeout")
for status, transient in ((429, False), (503, True), (401, False)):
    attempts.clear()

    def _err(req, status=status):
        attempts.append(req)
        return httpx.Response(status, json={"error": {"code": f"e{status}", "message": PROMPT}})

    with _Multi(api_env()), patch.object(llm, "_http_client", side_effect=_client_with(_err)):
        try:
            with llm_guard.scope(1, "ideas"):
                llm.complete("x")
            ok(False, f"HTTP {status} must raise")
        except llm.GrokCLIError as e:
            from app.services.render_loop import _TRANSIENT
            is_t = any(sig in str(e) for sig in _TRANSIENT)
            ok(is_t == transient and len(attempts) == 1 and PROMPT not in str(e),
               f"HTTP {status} → GrokCLIError (transient={transient}), 1 attempt, no echo of content")

# ------------------------------------------------------------------ guard in api mode
print("(B) guard still applies in api mode")
n_before = len(seen)
with _Multi(api_env()), patch.object(llm, "_http_client", side_effect=_client_with(_resp_ok)):
    for ws in (2, None):
        try:
            with llm_guard.scope(ws, "ideas"):
                llm.complete("x")
            ok(False, "external must be blocked in api mode")
        except llm_guard.LLMBlocked:
            ok(True, f"workspace {ws!r} blocked in api mode")
    try:
        llm.complete("x")
        ok(False, "no scope must block")
    except llm_guard.LLMBlocked:
        ok(True, "no scope blocked in api mode")
ok(len(seen) == n_before, "no HTTP for blocked workspaces")

# ------------------------------------------------------------------ agents
print("headless agents through the same client (agent-exec)")
cmd = ["grok", "--prompt-file", "p.md"]
with patch.object(llm_guard, "agent_preflight", return_value=True):
    argv, env = llm.agent_exec_plan("growth_agent", cmd)
    ok(argv == cmd and env.get("GROK_HOME") == os.environ.get("GROK_HOME"),
       "cli: command unchanged, consumer session")
    with _Multi(api_env(storage_dir=str(tmp))):
        n = len(rows())
        argv, env = llm.agent_exec_plan("growth_agent", cmd)
        home = Path(env["GROK_HOME"])
        conf = (home / "config.toml").read_text()
        ok(argv == cmd and env["XAI_API_KEY"] == FAKE_KEY and home == tmp / "grok-api-home",
           "api: isolated GROK_HOME + XAI_API_KEY in the child env")
        ok('base_url = "https://api.x.ai/v1"' in conf and 'env_key = "XAI_API_KEY"' in conf
           and FAKE_KEY not in conf and 'model = "grok-4.7"' in conf,
           "api: generated config binds grok-4.7 to api.x.ai via env_key (no key on disk)")
        r = rows()[-1]
        ok(len(rows()) == n + 1 and r["call_type"] == "growth_agent"
           and r["cost_usd"] == settings.llm_agent_run_usd_estimate,
           "api: one usage row per agent run, charged the per-run estimate")
    with _Multi(api_env(storage_dir=str(tmp), llm_monthly_usd_cap=0.0)):
        try:
            llm.agent_exec_plan("growth_agent", cmd)
            ok(False, "agent without cap must block")
        except llm.LLMBudgetExceeded:
            ok(True, "api: agent run blocked without a cap")
with patch.object(llm_guard, "agent_preflight", return_value=False):
    try:
        llm.agent_exec_plan("code_agent", cmd)
        ok(False, "agent preflight block must raise")
    except llm_guard.LLMBlocked:
        ok(True, "agent-exec re-checks the (B) guard")
ok(llm.main(["agent-exec", "x"]) == 2, "agent-exec usage error → 2")
with patch.object(llm_guard, "agent_preflight", return_value=False):
    ok(llm.main(["agent-exec", "run_check", "--", "grok", "-p", "x"]) == 3,
       "agent-exec blocked → exit 3, nothing exec'd")

print(f"\nALL OK ({_checks} checks)")
