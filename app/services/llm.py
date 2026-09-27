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
) -> str:
    """Run ``grok -p`` and return stdout text.

    ``max_tokens`` is accepted so callers of the old ``_llm(prompt, system, max_tokens)``
    seam keep their signature; grok 1.0.5 ``-p`` has no equivalent flag and it is ignored.

    ``timeout`` overrides ``settings.grok_timeout_seconds`` when set (use
    ``settings.grok_timeout_seconds_light`` for idea/metadata callers).

    ``model`` defaults to ``settings.grok_model`` (empty = CLI default);
    ``reasoning_effort`` defaults to none (CLI default). The storyboard compose
    seam (``worker._llm_compose``) passes ``settings.grok_compose_reasoning_effort``.
    """
    model = model if model is not None else (settings.grok_model or None)
    cmd = build_cmd(prompt, system, model=model, reasoning_effort=reasoning_effort)
    cwd = scratch_dir()
    timeout = int(timeout if timeout is not None else settings.grok_timeout_seconds)
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
