"""Manager LLM client: Grok Build CLI in headless mode (`grok -p`).

Every manager completion (topic autogen, script/metadata, HyperFrames motion
steps, thumbnail hooks) goes through ``complete``. The CLI uses the machine's
existing ``grok login`` OAuth cache (~/.grok) — not api.x.ai, not LiteLLM, not
an Anthropic/XAI API key.

launchd does not inherit a login PATH. The agent plist must put the grok binary
on PATH (typically ``$HOME/.grok/bin``) or set ``MANAGER_GROK_BIN`` to an
absolute path. See README / ``run/com.owera.channels-manager.plist``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from app.config import settings

logger = logging.getLogger("manager.llm")

# Child env must not fall through to HTTP API keys. Grok CLI on the live host
# is OAuth-only; Rodrigo rejected the XAI_API_KEY plan.
_STRIP_ENV = ("XAI_API_KEY", "GROK_API_KEY", "ANTHROPIC_API_KEY")


class GrokCLIError(RuntimeError):
    """grok -p failed (nonzero, missing binary, or timeout)."""


def scratch_dir() -> Path:
    """Isolated cwd so the coding-agent CLI does not treat the manager repo as a project."""
    p = Path(settings.storage_dir) / "grok-scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_prompt(prompt: str, system: str | None = None) -> str:
    """Flatten a system+user pair into the single string ``grok -p`` accepts."""
    user = prompt if prompt is not None else ""
    if system and system.strip():
        return f"{system.strip()}\n\n{user}"
    return user


def build_cmd(prompt: str, system: str | None = None) -> list[str]:
    """``grok -p <prompt>`` plus the boring headless flags.

    ``--permission-mode bypassPermissions`` matches the live growth-agent
    invocation so the subprocess never hangs on a tool-approval TUI.
    ``--no-auto-update`` is the documented flag for scripts/launchd.
    """
    return [
        settings.grok_bin,
        "--no-auto-update",
        "--permission-mode", "bypassPermissions",
        "-p", build_prompt(prompt, system),
    ]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for k in _STRIP_ENV:
        env.pop(k, None)
    return env


def complete(prompt: str, system: str | None = None, max_tokens: int | None = None) -> str:
    """Run ``grok -p`` and return stdout text.

    ``max_tokens`` is accepted so callers of the old ``_llm(prompt, system, max_tokens)``
    seam keep their signature; the CLI has no equivalent flag and it is ignored.
    """
    cmd = build_cmd(prompt, system)
    cwd = scratch_dir()
    timeout = int(settings.grok_timeout_seconds)
    env = _child_env()
    logger.info("llm backend=grok-cli bin=%s prompt_chars=%d", settings.grok_bin,
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
            f"grok CLI not found ({settings.grok_bin!r}). Put it on PATH "
            "(launchd: add $HOME/.grok/bin to EnvironmentVariables PATH) or set "
            "MANAGER_GROK_BIN to the absolute binary. Auth is `grok login` OAuth "
            "— no XAI_API_KEY."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise GrokCLIError(
            f"grok.Timeout: grok -p timed out after {timeout}s"
        ) from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise GrokCLIError(
            f"grok -p exited {proc.returncode}: {tail or '(no stderr)'}"
        )
    return (proc.stdout or "").strip()
