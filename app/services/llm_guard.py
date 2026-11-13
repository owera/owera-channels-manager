"""LLM hard guard: no Google-sourced data from a non-Owera workspace in any LLM prompt.

Why (CTO/CoS 2026-11-13): every CM LLM call goes through a *consumer* ``grok login``
session (``app/services/llm.py``) whose policy allows training. Google's API
Services User Data Policy (Limited Use) forbids feeding Google user data
(YouTube Data / Analytics, channel and comment content via OAuth, the growth
agent's analytics) to a model that may train on it. Until CM moves to an xAI
Enterprise API key, LLM calls are only allowed for Owera's own workspaces.

Single choke point
------------------
``require(call_type)`` is the ONLY gate. Every LLM-bearing call calls it first:

* ``llm.complete`` (the only ``grok -p`` spawner in ``app/``) — ideas, script,
  storyboard compose, legacy composition, metadata fallback, thumbnail hook,
  Gate B preflight compose, rubric harness;
* ``mpt_client.MPTClient.submit`` / ``social_metadata`` (MoneyPrinterTurbo runs
  its own LLM on the subject/script we send);
* the headless ``grok`` agents in ``run/*.sh`` (growth agent, code agent, run
  check) via ``python -m app.services.llm_guard agent-preflight <call_type>``.

``tests/verify_llm_guard.py`` greps the tree and fails if a new ``grok`` /
LLM-HTTP call site appears outside these.

Workspace resolution
--------------------
The caller sets the workspace with ``with llm_guard.scope(workspace_id, call_type):``
at the entry point (render job, idea generation, metadata, thumbnail, compose…).
A call with no scope, a ``None`` workspace, or a workspace id not in the
allowlist is refused (fail closed): ``LLMBlocked`` is raised and one log line
``llm_blocked_external_google_data workspace_id=<id> call_type=<type>`` is
written. No prompt content is ever logged.

Allowlist
---------
``settings.llm_internal_workspace_ids`` (env ``MANAGER_LLM_INTERNAL_WORKSPACE_IDS``,
comma-separated ints). Default ``"1"``: workspace id 1, slug ``owera``
("Owera (legacy default workspace)") — the only Owera-internal workspace in the
claw0 DB read 2026-11-13 (id 2 ``tenant-b`` is the O3/O4 external-tenant test
and is NOT internal). Empty / unparseable value → no workspace is allowed
(everything blocked). Stripe-provisioned customer workspaces are never in it.

No exemption for "pure provided script" calls: a provided script still travels
with title/subject/topic text that can come from a YouTube import, and the
render prompt carries channel context. Proving a call Google-free per call is
not reliable today, so the safe default applies: every LLM call needs an
allowlisted workspace (see docs/LLM_GUARD.md).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
from typing import Iterator

from app.config import settings

logger = logging.getLogger("manager.llm_guard")

BLOCK_EVENT = "llm_blocked_external_google_data"

_UNSET = object()
_scope: contextvars.ContextVar[tuple[int | None, str] | None] = contextvars.ContextVar(
    "llm_guard_scope", default=None)


try:  # subclass GrokCLIError so every existing grok-failure path treats it as a hard failure
    from app.services.llm import GrokCLIError as _Base  # noqa: WPS433
except Exception:  # pragma: no cover - import cycle safety
    _Base = RuntimeError


class LLMBlocked(_Base):
    """The LLM call was refused by the external-Google-data guard. Not transient."""

    def __init__(self, workspace_id, call_type: str):
        self.workspace_id = workspace_id
        self.call_type = call_type
        super().__init__(
            f"{BLOCK_EVENT}: LLM call refused for workspace_id={workspace_id} "
            f"call_type={call_type} (only Owera-internal workspaces may use the LLM "
            "until the xAI Enterprise key; see docs/LLM_GUARD.md)")


def internal_workspace_ids() -> frozenset[int]:
    raw = getattr(settings, "llm_internal_workspace_ids", "") or ""
    out: set[int] = set()
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            # A malformed allowlist must not widen access: drop everything.
            return frozenset()
    return frozenset(out)


def _coerce_ws(workspace_id) -> int | None:
    if workspace_id is None or isinstance(workspace_id, bool):
        return None
    try:
        return int(workspace_id)
    except (TypeError, ValueError):
        return None


def is_allowed(workspace_id) -> bool:
    ws = _coerce_ws(workspace_id)
    return ws is not None and ws in internal_workspace_ids()


def current() -> tuple[int | None, str] | None:
    return _scope.get()


def bind(workspace_id, call_type: str):
    """Non-``with`` form of ``scope`` (returns a token for ``unbind``)."""
    return _scope.set((_coerce_ws(workspace_id), call_type))


def unbind(token) -> None:
    _scope.reset(token)


@contextlib.contextmanager
def scope(workspace_id, call_type: str) -> Iterator[None]:
    """Bind the workspace (and a default call type) for LLM calls in this context.

    Contextvars are per-thread for new threads: set the scope INSIDE the thread
    target (e.g. ``worker.run_job``), not around ``Thread.start``."""
    token = _scope.set((_coerce_ws(workspace_id), call_type))
    try:
        yield
    finally:
        _scope.reset(token)


def _log_block(workspace_id, call_type: str) -> None:
    logger.warning("%s workspace_id=%s call_type=%s", BLOCK_EVENT, workspace_id, call_type)


def require(call_type: str | None = None, *, workspace_id=_UNSET) -> int:
    """Gate one LLM call. Returns the allowed workspace id or raises ``LLMBlocked``.

    ``workspace_id`` overrides the context scope when given explicitly.
    ``call_type`` names the call for the log (defaults to the scope's)."""
    sc = _scope.get()
    if workspace_id is _UNSET:
        ws = sc[0] if sc else None
    else:
        ws = _coerce_ws(workspace_id)
    ct = call_type or (sc[1] if sc else None) or "unknown"
    if sc and call_type and sc[1] and sc[1] != call_type:
        ct = f"{sc[1]}/{call_type}"
    if not is_allowed(ws):
        _log_block(ws, ct)
        raise LLMBlocked(ws, ct)
    return ws  # type: ignore[return-value]


def workspace_of_channel(channel_id, session=None) -> int | None:
    """``Channel.workspace_id`` for scripts that only know a channel id. ``None``
    (→ blocked) when the channel or DB can't be read."""
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.models import Channel

        def _q(s):
            ch = s.get(Channel, int(channel_id))
            return _coerce_ws(ch.workspace_id) if ch else None

        if session is not None:
            return _q(session)
        with Session(engine) as s:
            return _q(s)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------- agents

def external_workspaces_with_data(session=None) -> list[int] | None:
    """Workspace ids outside the allowlist that own at least one channel (and so
    can hold Google OAuth / YouTube data). ``None`` when the DB can't be read."""
    try:
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Channel

        allowed = internal_workspace_ids()

        def _q(s):
            rows = s.exec(select(Channel.workspace_id)).all()
            return sorted({int(w) for w in rows if w is not None and int(w) not in allowed}
                          | ({-1} if any(w is None for w in rows) else set()))

        if session is not None:
            return _q(session)
        with Session(engine) as s:
            return _q(s)
    except Exception:  # noqa: BLE001 - unreadable DB = unknown = blocked
        return None


def agent_preflight(call_type: str, session=None) -> bool:
    """Headless ``grok`` agents (growth agent, code agent, run check) read the API
    and the repo across ALL workspaces with the shared Basic credential, so their
    effective workspace is "every workspace". Allowed only when no non-allowlisted
    workspace owns a channel. Fail closed on any doubt (unreadable DB)."""
    ext = external_workspaces_with_data(session)
    if ext is None:
        _log_block("unknown", call_type)
        return False
    if ext:
        for ws in ext:
            _log_block(ws if ws != -1 else None, call_type)
        return False
    if not internal_workspace_ids():
        _log_block("none-allowlisted", call_type)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """``python -m app.services.llm_guard agent-preflight <call_type>`` → exit 0 allowed,
    exit 3 blocked. Prints only the event line (ids, no content)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] != "agent-preflight":
        print("usage: python -m app.services.llm_guard agent-preflight <call_type>",
              file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    call_type = argv[1]
    if agent_preflight(call_type):
        print(f"llm_guard allowed call_type={call_type}")
        return 0
    ext = external_workspaces_with_data()
    print(f"{BLOCK_EVENT} call_type={call_type} external_workspace_ids="
          f"{','.join(str(w) for w in (ext or [])) or 'unknown'}")
    return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
