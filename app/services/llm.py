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
import tempfile
from pathlib import Path

from app.config import settings

logger = logging.getLogger("manager.llm")

# Child env must not fall through to HTTP API keys. Grok CLI on claw0 is OIDC-only.
_STRIP_ENV = ("XAI_API_KEY", "GROK_API_KEY", "ANTHROPIC_API_KEY")

_OIDC_HINT = (
    "Refresh the Grok CLI OIDC session (`grok login`), then retry. "
    "No Anthropic/LiteLLM/XAI_API_KEY fallback."
)


class LLMError(RuntimeError):
    """An LLM call failed (grok CLI or xAI API)."""


GrokCLIError = LLMError  # backward-compatible alias


INSUFFICIENT_CREDIT = "insufficient_credit"


class LLMInsufficientCredit(LLMError):
    """The LLM provider refused for credit / spending-limit reasons. Not transient.

    Raised for xAI API HTTP 402, for HTTP 403/429 whose body carries billing text
    or a billing error code (team prepaid accounts out of credit or at the monthly
    spending limit), and for grok CLI stderr with the same billing text. Do not
    retry and do not fall back to another backend. Render failures record this as
    reason ``insufficient_credit``.
    """
    code = INSUFFICIENT_CREDIT


# Case-insensitive billing phrases (body text / CLI stderr / stored error text).
# A plain "Rate limit exceeded" contains none of these and stays a rate limit.
_CREDIT_TEXT = (
    "used all available credits",
    "monthly spending limit",
    "spending limit",
    "insufficient credit",          # also "insufficient credits"
    "insufficient_credit",          # also "insufficient_credits"
    "spending_limit",
    "run out of credits",           # xAI 402 text seen on 1478–1481
    "out of credits",
)
# JSON error codes (exact, lower-cased) that mean credit / spending limit.
_CREDIT_CODES = frozenset({
    "insufficient_credits", "insufficient_credit", "spending_limit",
    "spending_limit_reached", "monthly_spending_limit", "credit_limit_exceeded",
})


def _has_credit_text(text: str | None) -> bool:
    t = (text or "").lower()
    return any(p in t for p in _CREDIT_TEXT)


def _json_error_codes(payload) -> set[str]:
    """Error codes/types found in an xAI-style error body, lower-cased.

    Shapes seen: {"error": {"code": ..., "type": ...}}, {"code": ..., "error": "..."}."""
    out: set[str] = set()
    if not isinstance(payload, dict):
        return out
    for k in ("code", "type"):
        if isinstance(payload.get(k), str):
            out.add(payload[k].strip().lower())
    err = payload.get("error")
    if isinstance(err, dict):
        for k in ("code", "type"):
            if isinstance(err.get(k), str):
                out.add(err[k].strip().lower())
    return out


def is_credit_response(status: int, body_text: str | None = None, payload=None) -> bool:
    """HTTP response means insufficient credit / spending limit?

    402 always. 403 / 429 only with billing text or a billing JSON code, so a plain
    429 "Rate limit exceeded" is NOT credit (it keeps its rate-limit behavior)."""
    if status == 402:
        return True
    if status not in (403, 429):
        return False
    if _json_error_codes(payload) & _CREDIT_CODES:
        return True
    msg = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        parts = [err if isinstance(err, str) else "",
                 (err.get("message") or "") if isinstance(err, dict) else "",
                 payload.get("message") if isinstance(payload.get("message"), str) else ""]
        msg = " ".join(p for p in parts if isinstance(p, str))
    return _has_credit_text(msg) or _has_credit_text(body_text)


# Label kept for plain LLMError failures in stored error text. Before the rename
# every grok/xAI failure was stored as "GrokCLIError: ..."; ops greps, agent
# reports and the OIDC test match that prefix. Subclasses keep their own names
# (LLMBudgetExceeded, LLMConfigError, ...) exactly as before.
_LEGACY_LABEL = "GrokCLIError"


def format_failure(exc: BaseException) -> str:
    """Reason stored on a failed video / engine status. No prompt or body content."""
    if isinstance(exc, LLMInsufficientCredit) or getattr(exc, "code", None) == INSUFFICIENT_CREDIT:
        return INSUFFICIENT_CREDIT
    name = _LEGACY_LABEL if type(exc) is LLMError else type(exc).__name__
    text = f"{name}: {exc}"
    if is_insufficient_credit(text):
        return INSUFFICIENT_CREDIT
    return text


def is_insufficient_credit(err: str | None) -> bool:
    """True for the new reason, the historical 1478–1481 HTTP 402 text, and any
    stored error / CLI stderr with credit or spending-limit text (no "HTTP 402"
    needed). A plain rate-limit message is not credit."""
    e = err or ""
    if INSUFFICIENT_CREDIT in e:
        return True
    # Videos 1478–1481 (2026-11-13) were marked:
    # "GrokCLIError: xai api HTTP 402: You have run out of credits or need a Grok subscription..."
    if "http 402" in e.lower():
        return True
    return _has_credit_text(e)


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
        if is_insufficient_credit(tail):
            # Credit / spending limit in CLI stderr (with or without "HTTP 402").
            # Not transient; no body in the log or the error.
            logger.warning("insufficient_credit backend=grok-cli exit=%s", proc.returncode)
            raise LLMInsufficientCredit(INSUFFICIENT_CREDIT)
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


class LLMRetentionNotAcknowledged(LLMError):
    """LLM_BACKEND=api without a valid XAI_DATA_RETENTION_ACK. Not transient."""


# Allowed values of XAI_DATA_RETENTION_ACK (an explicit statement by the operator of
# what the xAI account behind XAI_API_KEY retains — never inferred):
#   zdr              the org has Zero Data Retention enabled;
#   30d_no_training  standard API retention (30 days), prompts not used for training.
RETENTION_ACK_VALUES = ("zdr", "30d_no_training")

RETENTION_MESSAGE = ("LLM_BACKEND=api requires XAI_DATA_RETENTION_ACK=zdr|30d_no_training "
                     "in the manager .env: state what the xAI account behind XAI_API_KEY "
                     "retains (zdr only if ZDR is on for the org), or set LLM_BACKEND=cli.")


def data_retention() -> str:
    """The acknowledged retention value ("" when unset/invalid)."""
    v = (getattr(settings, "xai_data_retention_ack", "") or "").strip().lower()
    return v if v in RETENTION_ACK_VALUES else ""


def require_retention_ack() -> str:
    """Fail closed in api mode: empty or unknown XAI_DATA_RETENTION_ACK raises."""
    v = data_retention()
    if backend() == "api" and not v:
        raise LLMRetentionNotAcknowledged(RETENTION_MESSAGE)
    return v


def boot_check() -> None:
    """Called from the app lifespan: refuse to start in api mode without a valid
    retention acknowledgement; log the acknowledged value."""
    if backend() == "api":
        v = require_retention_ack()
        logger.info("llm backend=xai-api data_retention=%s base=%s (agent runs stay on "
                    "grok-cli)", v, settings.llm_api_base_url)


# CLI effort vocabulary (grok 1.0.77: none|minimal|low|medium|high|xhigh|max) → xAI
# Responses API ``reasoning.effort`` (grok-4.5/4.6/4.7: low|medium|high|xhigh;
# reasoning cannot be disabled). Unknown values are dropped (API default "high").
_API_EFFORT = {"none": "low", "minimal": "low", "low": "low", "medium": "medium",
               "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def api_effort(reasoning_effort: str | None) -> str | None:
    if not reasoning_effort:
        return None
    v = _API_EFFORT.get(reasoning_effort.strip().lower())
    if v is None:
        logger.warning("llm backend=xai-api unknown reasoning_effort=%r: not sent "
                       "(API default)", reasoning_effort)
    return v


# Stateless requests only: these keys are the whole allowed request body.
# No previous_response_id / conversation / background / store=true, ever.
_ALLOWED_BODY_KEYS = frozenset({"model", "input", "instructions", "reasoning", "store"})


def _assert_stateless(body: dict) -> None:
    extra = set(body) - _ALLOWED_BODY_KEYS
    if extra or body.get("store") is not False or not isinstance(body.get("input"), str):
        raise LLMConfigError(f"xai api request is not stateless (keys={sorted(body)})")


class LLMBudgetExceeded(LLMError):
    """api backend refused: monthly cap reached or not configured. Not transient."""


class LLMConfigError(LLMError):
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

    retention = require_retention_ack()
    mdl = api_model(model)
    if mdl not in price_table():
        raise LLMConfigError(f"no price for model {mdl!r}: add it to MANAGER_LLM_PRICE_TABLE_JSON")
    key = _api_key()
    check_budget(call_type, workspace_id)
    body: dict = {"model": mdl, "input": prompt if prompt is not None else "",
                  "store": False}
    if system and system.strip():
        body["instructions"] = system.strip()
    effort = api_effort(reasoning_effort)
    if effort:
        body["reasoning"] = {"effort": effort}
    _assert_stateless(body)   # store=false explicit, no previous_response_id/conversation
    logger.info("llm backend=xai-api model=%s effort=%s prompt_chars=%d call_type=%s "
                "workspace_id=%s data_retention=%s", mdl, effort or "default",
                len(build_prompt(prompt, system)), call_type, workspace_id, retention)
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "backend": "api", "model": mdl, "call_type": call_type,
           "workspace_id": workspace_id, "effort": effort,
           "effort_requested": reasoning_effort or None, "data_retention": retention}
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
        try:
            payload = r.json()
        except ValueError:
            payload = None
        try:
            body_text = r.text
        except Exception:  # noqa: BLE001 - undecodable body: classify on status/JSON only
            body_text = ""
        if is_credit_response(r.status_code, body_text, payload):
            # 402, or 403/429 with billing text / code (team prepaid out of credit
            # or at the monthly spending limit). No body/content in the log.
            # Never retried (not transient).
            logger.warning("insufficient_credit workspace_id=%s route=%s http=%s",
                           workspace_id, call_type, r.status_code)
            raise LLMInsufficientCredit(INSUFFICIENT_CREDIT)
        detail = ""
        err = (payload or {}).get("error") if isinstance(payload, dict) else None
        detail = (err.get("code") or err.get("type") or "") if isinstance(err, dict) else str(err or "")
        # Same shape as "grok -p exited N: <tail>"; status code kept so 503/529 stay
        # transient in render_loop exactly as a CLI stderr with them would.
        raise LLMError(f"xai api HTTP {r.status_code}: {str(detail)[:200] or '(no detail)'}")
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
    logger.info("llm_usage backend=xai-api model=%s response_model=%s call_type=%s "
                "workspace_id=%s input_tokens=%d cached_input_tokens=%d output_tokens=%d "
                "reasoning_tokens=%d cost_usd=%.6f", mdl, data.get("model") or mdl, call_type,
                workspace_id, in_t, cached, out_t, reasoning,
                reported if reported is not None else est)
    record_usage({**row, "status": data.get("status") or "completed",
                  "response_model": data.get("model") or mdl,
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
# Headless grok agents (run/growth-agent.sh, code-agent.sh, run-check.sh) exec
# through this module:
#   python -m app.services.llm agent-exec <call_type> -- grok --prompt-file ...
# Guard preflight, then exec on the Grok CLI (consumer ``grok login`` session)
# in BOTH backends: LLM_BACKEND=api moves only ``complete()`` to the xAI API
# (CTO, 2026-11-13). API keys are stripped from the child env so the CLI can
# never silently bill XAI_API_KEY outside the cap/ledger.
#
# Agent runs are scoped to MANAGER_AGENT_RUN_WORKSPACE_IDS (default "1"): the
# preflight no longer blocks the global run because another workspace exists,
# and a workspace-filtered preamble is prepended to --prompt-file. The LLM
# allowlist stays a defense (unallowlisted ids are dropped / fail closed).

def _with_scope_prompt(cmd: list[str], scope_text: str, dest: Path) -> list[str]:
    """Prepend the workspace-scope preamble to --prompt-file (or add one)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = list(cmd)
    original = ""
    if "--prompt-file" in out:
        i = out.index("--prompt-file")
        if i + 1 < len(out):
            src = Path(out[i + 1])
            if src.is_file():
                original = src.read_text(encoding="utf-8")
            body = scope_text + (("\n\n---\n\n" + original) if original else "")
            dest.write_text(body, encoding="utf-8")
            out[i + 1] = str(dest)
            return out
    dest.write_text(scope_text, encoding="utf-8")
    out[1:1] = ["--prompt-file", str(dest)]
    return out


def agent_exec_plan(call_type: str, cmd: list[str], session=None
                    ) -> tuple[list[str], dict[str, str]]:
    """Return (argv, env) for an agent run (always the CLI), or raise (guard).

    The argv's ``--prompt-file`` is rewritten to a combined file that starts
    with the workspace-scope preamble (ws2 / other tenants excluded). The child
    env also carries ``MANAGER_AGENT_RUN_WORKSPACE_IDS`` (effective ids only).
    """
    from app.services import llm_guard
    if not cmd:
        raise LLMConfigError("agent-exec: no command")
    if not llm_guard.agent_preflight(call_type, session=session):
        raise llm_guard.LLMBlocked("external", call_type)
    ctx = llm_guard.agent_run_context(session=session)
    if ctx is None:
        raise llm_guard.LLMBlocked("unknown", call_type)
    ids = ",".join(str(i) for i in ctx.get("workspace_ids") or [])
    dest = Path(tempfile.gettempdir()) / f"cm-agent-run-scope-{call_type}-{os.getpid()}.md"
    argv = _with_scope_prompt(cmd, llm_guard.agent_run_scope_prompt(ctx), dest)
    env = dict(_child_env())
    env["MANAGER_AGENT_RUN_WORKSPACE_IDS"] = ids
    env["MANAGER_AGENT_RUN_CONTEXT"] = str(dest)
    if backend() == "api":
        logger.info("llm agent-exec call_type=%s backend=grok-cli (LLM_BACKEND=api applies "
                    "to complete() only) agent_run_workspace_ids=%s", call_type, ids or "none")
    else:
        logger.info("llm agent-exec call_type=%s agent_run_workspace_ids=%s",
                    call_type, ids or "none")
    return argv, env


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
