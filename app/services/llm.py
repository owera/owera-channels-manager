"""Manager LLM client: Grok Build CLI in headless mode (`grok -p`).

Every manager completion (topic autogen, script/metadata, HyperFrames motion
steps, thumbnail hooks) goes through ``complete``. Live claw0: grok 1.0.41 at
``~/.local/bin/grok`` → ``~/.grok/bin/grok``. Auth is the CLI OIDC session
(``grok login``), not api.x.ai / LiteLLM / Anthropic / XAI_API_KEY.

If grok fails (expired OIDC, missing binary, nonzero), this raises. There is
no HTTP-API fallback — fail clearly so the operator can ``grok login`` and retry.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from app.config import settings

logger = logging.getLogger("manager.llm")

# Child env must not fall through to HTTP API keys. Grok CLI on claw0 is OIDC-only.
_STRIP_ENV = ("XAI_API_KEY", "GROK_API_KEY", "ANTHROPIC_API_KEY")

_OIDC_HINT = (
    "Refresh the Grok CLI OIDC session (`grok login`), then retry. "
    "No Anthropic/LiteLLM/XAI_API_KEY fallback."
)


class GrokCLIError(RuntimeError):
    """grok -p failed (nonzero, missing binary, or timeout)."""


def scratch_dir() -> Path:
    """Isolated cwd so a coding-agent CLI does not treat the manager repo as a project."""
    p = Path(settings.storage_dir) / "grok-scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_prompt(prompt: str, system: str | None = None) -> str:
    """Flatten a system+user pair into the single string ``grok -p`` / ``--single`` accepts."""
    user = prompt if prompt is not None else ""
    if system and system.strip():
        return f"{system.strip()}\n\n{user}"
    return user


def build_cmd(
    prompt: str,
    system: str | None = None,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Headless: ``grok -p <prompt>`` (``-p`` is ``--single``).

    ``model`` / ``reasoning_effort`` add ``-m`` / ``--reasoning-effort`` (grok
    >= 1.0.41) only when non-empty; with neither the argv is exactly
    ``[grok, -p, prompt]`` and the CLI uses its server-side defaults. The prompt
    stays the LAST element (``complete`` logs ``len(cmd[-1])``)."""
    cmd = [settings.grok_bin]
    if model:
        cmd += ["-m", model]
    if reasoning_effort:
        cmd += ["--reasoning-effort", reasoning_effort]
    return cmd + ["-p", build_prompt(prompt, system)]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for k in _STRIP_ENV:
        env.pop(k, None)
    return env


def complete(
    prompt: str,
    system: str | None = None,
    max_tokens: int | None = None,
    *,
    timeout: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    call_type: str | None = None,
) -> str:
    """Run ``grok -p`` and return stdout text.

    Guarded: the caller must have bound an Owera-internal workspace with
    ``llm_guard.scope(workspace_id, call_type)``; otherwise ``LLMBlocked``.

    ``max_tokens`` is accepted so callers of the old ``_llm(prompt, system, max_tokens)``
    seam keep their signature; grok 1.0.5 ``-p`` has no equivalent flag and it is ignored.

    ``timeout`` overrides ``settings.grok_timeout_seconds`` when set (use
    ``settings.grok_timeout_seconds_light`` for idea/metadata callers).

    ``model`` defaults to ``settings.grok_model`` (empty = CLI default);
    ``reasoning_effort`` defaults to none (CLI default). Callers pin it:
    storyboard compose (``worker._llm_compose``) uses
    ``settings.grok_compose_reasoning_effort`` (default "medium"); light
    callers (``video_gen.generate_ideas``, ``metadata._llm_fallback``) use
    ``settings.grok_effort_ideas`` (default "low", env MANAGER_GROK_EFFORT_IDEAS).
    """
    # Hard guard (choke point): refuse before building/spawning anything when the
    # bound workspace is not Owera-internal. Raises LLMBlocked (a GrokCLIError).
    # Applies to BOTH backends (cli and api).
    from app.services import llm_guard
    ws = llm_guard.require(call_type)
    model = model if model is not None else (settings.grok_model or None)
    timeout = int(timeout if timeout is not None else settings.grok_timeout_seconds)
    if backend() == "api":
        sc = llm_guard.current()
        return _complete_api(prompt, system, model=model, reasoning_effort=reasoning_effort,
                             timeout=timeout, workspace_id=ws,
                             call_type=call_type or (sc[1] if sc else None) or "unknown")
    return _complete_cli(prompt, system, model=model, reasoning_effort=reasoning_effort,
                         timeout=timeout)


def backend() -> str:
    """``settings.llm_backend``: "api" only when explicitly set; anything else = cli."""
    return "api" if (getattr(settings, "llm_backend", "") or "").strip().lower() == "api" else "cli"


# --------------------------------------------------------------------------- cli backend

def _complete_cli(prompt: str, system: str | None, *, model: str | None,
                  reasoning_effort: str | None, timeout: int) -> str:
    """``grok -p`` (consumer ``grok login`` session). Unchanged behavior."""
    cmd = build_cmd(prompt, system, model=model, reasoning_effort=reasoning_effort)
    cwd = scratch_dir()
    env = _child_env()
    logger.info("llm backend=grok-cli bin=%s model=%s effort=%s prompt_chars=%d",
                settings.grok_bin, model or "default", reasoning_effort or "default",
                len(cmd[-1]))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as e:
        raise GrokCLIError(
            f"grok CLI not found ({settings.grok_bin!r}). launchd PATH already "
            "includes ~/.local/bin (live: ~/.local/bin/grok). Set MANAGER_GROK_BIN "
            f"if needed. {_OIDC_HINT}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise GrokCLIError(
            f"grok.Timeout: grok -p timed out after {timeout}s. {_OIDC_HINT}"
        ) from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise GrokCLIError(
            f"grok -p exited {proc.returncode}: {tail or '(no stderr)'}. {_OIDC_HINT}"
        )
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------- api backend
#
# xAI API (Enterprise ToS), POST {base}/responses — https://docs.x.ai/docs/api-reference
# (fetched 2026-11-13). Same contract as the CLI path for callers:
#   * one attempt per call, no in-client retry (the CLI does none; render_loop owns
#     retries via _TRANSIENT);
#   * timeout → GrokCLIError "grok.Timeout: ..." (same transient token);
#   * any other failure → GrokCLIError (non-zero exit equivalent);
#   * returns the stripped output text.
# The key is sent only in the Authorization header; it is never logged, never put in
# an error message, never written to the usage JSONL.

# USD per 1M tokens. Source: https://docs.x.ai/docs/models "Text API Pricing",
# fetched 2026-11-13. Requests whose prompt reaches long_threshold tokens are billed
# at the long_* rate for ALL tokens. Override: MANAGER_LLM_PRICE_TABLE_JSON.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "grok-4.7": {"input": 2.00, "cached_input": 0.50, "output": 6.00,
                 "long_threshold": 200_000, "long_input": 4.00,
                 "long_cached_input": 1.00, "long_output": 12.00},
    "grok-4.6": {"input": 2.00, "cached_input": 0.50, "output": 6.00,
                 "long_threshold": 200_000, "long_input": 4.00,
                 "long_cached_input": 1.00, "long_output": 12.00},
    "grok-4.5": {"input": 2.00, "cached_input": 0.30, "output": 6.00,
                 "long_threshold": 200_000, "long_input": 4.00,
                 "long_cached_input": 0.60, "long_output": 12.00},
}

BUDGET_EVENT = "llm_budget_blocked"


class LLMBudgetExceeded(GrokCLIError):
    """api backend refused: monthly cap reached or not configured. Not transient."""


class LLMConfigError(GrokCLIError):
    """api backend misconfigured (no key, unknown model/price). Not transient."""


def price_table() -> dict[str, dict[str, float]]:
    import json
    raw = (getattr(settings, "llm_price_table_json", "") or "").strip()
    if not raw:
        return dict(DEFAULT_PRICES)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return {str(k): {kk: float(vv) for kk, vv in v.items()} for k, v in data.items()}
    except Exception as e:  # noqa: BLE001
        raise LLMConfigError(f"MANAGER_LLM_PRICE_TABLE_JSON is not valid JSON price table ({e})")


def api_model(model: str | None) -> str:
    """CLI model id → API model id. ``-m grok-4.7`` (live claw0 MANAGER_GROK_MODEL)
    is the same id on the API; empty (CLI default) → settings.llm_api_default_model."""
    return (model or "").strip() or (settings.llm_api_default_model or "grok-4.7")


def estimate_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int,
                  reasoning_tokens: int = 0) -> float:
    p = price_table().get(model)
    if p is None:
        raise LLMConfigError(f"no price for model {model!r} in the LLM price table")
    long = input_tokens >= p.get("long_threshold", float("inf"))
    pin = p.get("long_input", p["input"]) if long else p["input"]
    pcache = (p.get("long_cached_input", p.get("cached_input", pin)) if long
              else p.get("cached_input", pin))
    pout = p.get("long_output", p["output"]) if long else p["output"]
    uncached = max(0, input_tokens - cached_tokens)
    # Reasoning tokens are generated tokens: billed at the output rate.
    return (uncached * pin + cached_tokens * pcache
            + (output_tokens + reasoning_tokens) * pout) / 1_000_000


def usage_path():
    from pathlib import Path
    p = (getattr(settings, "llm_usage_path", "") or "").strip()
    return Path(p) if p else Path(settings.storage_dir) / "llm-usage.jsonl"


def record_usage(row: dict) -> None:
    """Append one usage row (no content). A write failure is logged, never raised."""
    import json
    path = usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    except OSError as e:
        logger.warning("llm usage JSONL write failed (%s): %s", path, type(e).__name__)


def month_to_date_usd(now=None) -> float:
    """Sum of cost_usd over the current UTC month. Unreadable rows are skipped;
    an unreadable FILE raises (caller fails closed)."""
    import json
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    prefix = now.strftime("%Y-%m")
    path = usage_path()
    if not path.exists():
        return 0.0
    total = 0.0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("ts", "")).startswith(prefix):
                try:
                    total += float(r.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    continue
    return total


def check_budget(call_type: str, workspace_id) -> float:
    """Fail closed: no cap → blocked; month-to-date ≥ cap → blocked; ledger
    unreadable → blocked. Returns the remaining USD."""
    cap = float(getattr(settings, "llm_monthly_usd_cap", 0.0) or 0.0)
    if cap <= 0:
        logger.warning("%s reason=cap_not_set workspace_id=%s call_type=%s",
                       BUDGET_EVENT, workspace_id, call_type)
        raise LLMBudgetExceeded(
            f"{BUDGET_EVENT}: LLM_MONTHLY_USD_CAP is not set; the api backend refuses "
            "every call until it is (fail closed)")
    try:
        spent = month_to_date_usd()
    except OSError as e:
        logger.warning("%s reason=ledger_unreadable workspace_id=%s call_type=%s",
                       BUDGET_EVENT, workspace_id, call_type)
        raise LLMBudgetExceeded(f"{BUDGET_EVENT}: usage ledger unreadable ({type(e).__name__})")
    if spent >= cap:
        logger.warning("%s reason=cap_reached spent_usd=%.4f cap_usd=%.2f workspace_id=%s "
                       "call_type=%s", BUDGET_EVENT, spent, cap, workspace_id, call_type)
        raise LLMBudgetExceeded(
            f"{BUDGET_EVENT}: monthly cap reached (spent ${spent:.2f} of ${cap:.2f})")
    return cap - spent


def _api_key() -> str:
    key = settings.xai_api_key.get_secret_value() if settings.xai_api_key else ""
    if not (key or "").strip():
        raise LLMConfigError(
            "LLM_BACKEND=api but XAI_API_KEY is not set in the manager .env "
            "(no fallback to the grok CLI)")
    return key.strip()


def _http_client(timeout: int):
    """Seam for tests (mock transport)."""
    import httpx
    return httpx.Client(base_url=settings.llm_api_base_url.rstrip("/"), timeout=timeout)


def _output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text":
                parts.append(c.get("text") or "")
    return "".join(parts)


def _complete_api(prompt: str, system: str | None, *, model: str | None,
                  reasoning_effort: str | None, timeout: int, workspace_id,
                  call_type: str) -> str:
    import time
    from datetime import datetime, timezone

    import httpx

    mdl = api_model(model)
    if mdl not in price_table():
        raise LLMConfigError(f"no price for model {mdl!r}: add it to MANAGER_LLM_PRICE_TABLE_JSON")
    key = _api_key()
    check_budget(call_type, workspace_id)
    body: dict = {"model": mdl, "input": prompt if prompt is not None else "",
                  "store": False}
    if system and system.strip():
        body["instructions"] = system.strip()
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    logger.info("llm backend=xai-api model=%s effort=%s prompt_chars=%d call_type=%s "
                "workspace_id=%s", mdl, reasoning_effort or "default",
                len(build_prompt(prompt, system)), call_type, workspace_id)
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "backend": "api", "model": mdl, "call_type": call_type,
           "workspace_id": workspace_id, "effort": reasoning_effort or None}
    t0 = time.monotonic()
    try:
        with _http_client(timeout) as c:
            r = c.post("/responses", json=body,
                       headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException as e:
        record_usage({**row, "status": "timeout", "cost_usd": 0.0,
                      "duration_s": round(time.monotonic() - t0, 2)})
        raise GrokCLIError(f"grok.Timeout: xai api timed out after {timeout}s") from e
    except httpx.HTTPError as e:
        record_usage({**row, "status": "transport_error", "cost_usd": 0.0,
                      "duration_s": round(time.monotonic() - t0, 2)})
        raise GrokCLIError(f"xai api transport error: {type(e).__name__}") from e
    dur = round(time.monotonic() - t0, 2)
    if r.status_code >= 400:
        record_usage({**row, "status": f"http_{r.status_code}", "cost_usd": 0.0,
                      "duration_s": dur})
        detail = ""
        try:
            err = (r.json() or {}).get("error")
            detail = (err.get("code") or err.get("type") or "") if isinstance(err, dict) else str(err or "")
        except ValueError:
            pass
        # Same shape as "grok -p exited N: <tail>"; status code kept so 503/529 stay
        # transient in render_loop exactly as a CLI stderr with them would.
        raise GrokCLIError(f"xai api HTTP {r.status_code}: {str(detail)[:200] or '(no detail)'}")
    try:
        data = r.json()
    except ValueError as e:
        record_usage({**row, "status": "bad_json", "cost_usd": 0.0, "duration_s": dur})
        raise GrokCLIError("xai api returned non-JSON") from e
    u = data.get("usage") or {}
    in_t = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
    out_t = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
    cached = int(((u.get("input_tokens_details") or u.get("prompt_tokens_details") or {})
                  .get("cached_tokens")) or 0)
    reasoning = int(((u.get("output_tokens_details") or u.get("completion_tokens_details") or {})
                     .get("reasoning_tokens")) or 0)
    est = estimate_cost(mdl, in_t, cached, out_t, reasoning)
    reported = None
    if u.get("cost_in_usd_ticks") is not None:
        reported = int(u["cost_in_usd_ticks"]) / 10_000_000_000   # 1e10 ticks = $1
    record_usage({**row, "status": data.get("status") or "completed",
                  "input_tokens": in_t, "cached_input_tokens": cached,
                  "output_tokens": out_t, "reasoning_tokens": reasoning,
                  "cost_usd_estimated": round(est, 6),
                  "cost_usd_reported": None if reported is None else round(reported, 6),
                  "cost_usd": round(reported if reported is not None else est, 6),
                  "duration_s": dur})
    if data.get("status") not in (None, "completed"):
        raise GrokCLIError(f"xai api response status={data.get('status')}")
    return (_output_text(data) or "").strip()


# --------------------------------------------------------------------------- agents
#
# Headless grok agents (run/growth-agent.sh, code-agent.sh, run-check.sh) are the
# growth agent's analytics path. They exec through this module too:
#   python -m app.services.llm agent-exec <call_type> -- grok --prompt-file ... 
# cli: guard preflight, then exec the command unchanged (consumer session).
# api: guard preflight + key + budget; one usage row per run charged at
#      settings.llm_agent_run_usd_estimate; exec grok with an isolated GROK_HOME
#      whose config binds the model to the xAI API (env_key XAI_API_KEY), so the
#      session token in ~/.grok/auth.json is never used.

def agent_grok_home():
    from pathlib import Path
    return Path(settings.storage_dir) / "grok-api-home"


def _write_agent_grok_home(mdl: str):
    home = agent_grok_home()
    home.mkdir(parents=True, exist_ok=True)
    base = settings.llm_api_base_url.rstrip("/")
    (home / "config.toml").write_text(
        "# Generated by app/services/llm.py (LLM_BACKEND=api). No secret here:\n"
        "# the key is read from the XAI_API_KEY env var at request time.\n"
        f'[model.cm-xai-api]\nmodel = "{mdl}"\nbase_url = "{base}"\n'
        'env_key = "XAI_API_KEY"\napi_backend = "responses"\n\n'
        '[models]\ndefault = "cm-xai-api"\n', encoding="utf-8")
    return home


def agent_exec_plan(call_type: str, cmd: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return (argv, env) for an agent run, or raise (guard / budget / config)."""
    from datetime import datetime, timezone

    from app.services import llm_guard
    if not cmd:
        raise LLMConfigError("agent-exec: no command")
    if not llm_guard.agent_preflight(call_type):
        raise llm_guard.LLMBlocked("external", call_type)
    env = os.environ.copy()
    if backend() != "api":
        return cmd, env
    mdl = api_model(settings.grok_model or None)
    if mdl not in price_table():
        raise LLMConfigError(f"no price for model {mdl!r}")
    key = _api_key()
    check_budget(call_type, "agents")
    est = float(getattr(settings, "llm_agent_run_usd_estimate", 1.0) or 1.0)
    record_usage({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "backend": "api", "model": mdl, "call_type": call_type,
                  "workspace_id": "agents", "status": "agent_run_started",
                  "cost_usd_estimated": est, "cost_usd_reported": None, "cost_usd": est})
    home = _write_agent_grok_home(mdl)
    env["GROK_HOME"] = str(home)
    env["XAI_API_KEY"] = key
    env.pop("GROK_API_KEY", None)
    return cmd, env


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3 or argv[0] != "agent-exec" or "--" not in argv:
        print("usage: python -m app.services.llm agent-exec <call_type> -- <cmd...>",
              file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    call_type = argv[1]
    cmd = argv[argv.index("--") + 1:]
    try:
        argv2, env = agent_exec_plan(call_type, cmd)
    except GrokCLIError as e:
        print(str(e), file=sys.stderr)
        return 3
    os.execvpe(argv2[0], argv2, env)   # watchdog keeps the grok PID
    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
