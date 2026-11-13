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

Headless agents (growth, code, run-check) are *additionally* scoped by
``settings.agent_run_workspace_ids`` (env ``MANAGER_AGENT_RUN_WORKSPACE_IDS``,
default ``"1"``). Their context and actions only include those workspaces, so a
non-allowlisted tenant with channels no longer blocks the global run. The LLM
allowlist stays a hard defense: an id in the agent-run env that is not on the
LLM allowlist is dropped (logged) and never enters the context; an empty
effective set refuses the run (fail closed).

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


def _parse_id_list(raw) -> frozenset[int]:
    """Comma-separated ints. Empty or any malformed token → empty (never widens)."""
    out: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            # A malformed list must not widen access: drop everything.
            return frozenset()
    return frozenset(out)


def internal_workspace_ids() -> frozenset[int]:
    return _parse_id_list(getattr(settings, "llm_internal_workspace_ids", "") or "")


def agent_run_workspace_ids() -> frozenset[int]:
    """Configured agent-run workspace ids (not yet intersected with the LLM allowlist)."""
    return _parse_id_list(getattr(settings, "agent_run_workspace_ids", "") or "")


def effective_agent_run_workspace_ids(
        *, log_drops: bool = False, call_type: str = "agent_run") -> frozenset[int]:
    """Agent-run ids that are also on the LLM allowlist.

    Ids in ``MANAGER_AGENT_RUN_WORKSPACE_IDS`` that are missing from
    ``MANAGER_LLM_INTERNAL_WORKSPACE_IDS`` are dropped (the guard blocks that
    workspace). When ``log_drops`` is set, each drop is logged with
    ``llm_blocked_external_google_data``. An empty result means the run must
    be refused (fail closed).
    """
    configured = agent_run_workspace_ids()
    allowed = internal_workspace_ids()
    dropped = configured - allowed
    if log_drops and dropped:
        for ws in sorted(dropped):
            logger.warning(
                "%s workspace_id=%s call_type=%s reason=dropped_from_agent_run",
                BLOCK_EVENT, ws, call_type)
    return configured & allowed


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
    """Workspace ids outside the LLM allowlist that own at least one channel
    (and so can hold Google OAuth / YouTube data). ``None`` when the DB can't
    be read. Diagnostic only — agent preflight no longer blocks the global run
    just because such a workspace exists; see ``agent_preflight``."""
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


def agent_run_context(session=None) -> dict | None:
    """Workspaces and channels the headless agents may observe or act on.

    Restricted to ``effective_agent_run_workspace_ids()``. Safe fields only
    (id / slug / name) — no OAuth tokens, emails, or analytics. ``None`` when
    the DB can't be read (fail closed).
    """
    try:
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Channel, Workspace

        allowed = effective_agent_run_workspace_ids()

        def _q(s):
            if not allowed:
                return {"workspace_ids": [], "workspaces": [], "channels": []}
            wss = list(s.exec(select(Workspace).where(Workspace.id.in_(sorted(allowed)))).all())
            chs = list(s.exec(select(Channel).where(Channel.workspace_id.in_(sorted(allowed)))).all())
            workspaces = [
                {"id": int(w.id), "slug": w.slug, "name": w.name}
                for w in wss if w.id is not None and int(w.id) in allowed
            ]
            channels = [
                {"id": int(c.id), "workspace_id": int(c.workspace_id),
                 "slug": c.slug, "name": c.name}
                for c in chs
                if c.id is not None and c.workspace_id is not None
                and int(c.workspace_id) in allowed
            ]
            return {
                "workspace_ids": sorted(allowed),
                "workspaces": sorted(workspaces, key=lambda r: r["id"]),
                "channels": sorted(channels, key=lambda r: r["id"]),
            }

        if session is not None:
            return _q(session)
        with Session(engine) as s:
            return _q(s)
    except Exception:  # noqa: BLE001
        return None


def agent_run_scope_prompt(ctx: dict) -> str:
    """Markdown preamble injected into the agent ``--prompt-file``. No secrets."""
    lines = [
        "# Agent run workspace scope (injected by llm agent-exec; do not ignore)",
        "",
        "This run is limited to the workspaces listed below "
        "(`MANAGER_AGENT_RUN_WORKSPACE_IDS`). You may only observe and act on "
        "these workspaces and their channels.",
        "",
        "- Do not read `manager.db`, the API, or the filesystem for any workspace "
        "or channel that is not listed below.",
        "- For HTTP Basic Auth, use only these workspace slugs as the username "
        "(or `{slug}+channels` / `{slug}+vm`). Legacy bare usernames "
        "`channels`, `vm`, and `admin` map to workspace `owera` (id 1) — use "
        "them only if id 1 is in the list below.",
        "- If a tool returns data for a workspace or channel not listed here, "
        "discard it and do not send it onward in a prompt or report.",
        "",
        "## Allowed workspaces",
    ]
    wss = ctx.get("workspaces") or []
    chs = ctx.get("channels") or []
    if not wss:
        lines.append("- (none — the run should have been refused)")
    else:
        for w in wss:
            lines.append(f"- id={w['id']} slug={w['slug']} name={w['name']}")
    lines += ["", "## Allowed channels"]
    if not chs:
        lines.append("- (none)")
    else:
        for c in chs:
            lines.append(
                f"- id={c['id']} workspace_id={c['workspace_id']} "
                f"slug={c['slug']} name={c['name']}")
    return "\n".join(lines) + "\n"


def agent_preflight(call_type: str, session=None) -> bool:
    """Headless ``grok`` agents (growth, code, run-check) are scoped to
    ``MANAGER_AGENT_RUN_WORKSPACE_IDS`` (default ``1``). A non-allowlisted
    workspace that owns a channel no longer blocks the run, because that
    workspace is excluded from the injected context.

    Fail closed when the DB is unreadable, the LLM allowlist is empty, or the
    effective agent-run set is empty (including when every configured agent-run
    id was dropped for not being on the LLM allowlist). The allowlist remains a
    defense: a configured id that is not allowlisted is logged and dropped.
    """
    # Touch the DB first: an unreadable DB must block even if the id lists
    # look fine (we cannot prove what the agent would see).
    try:
        from sqlmodel import Session, select

        from app.db import engine
        from app.models import Channel

        def _probe(s):
            s.exec(select(Channel.workspace_id).limit(1)).all()

        if session is not None:
            _probe(session)
        else:
            with Session(engine) as s:
                _probe(s)
    except Exception:  # noqa: BLE001
        _log_block("unknown", call_type)
        return False

    if not internal_workspace_ids():
        _log_block("none-allowlisted", call_type)
        return False
    effective = effective_agent_run_workspace_ids(log_drops=True, call_type=call_type)
    if not effective:
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
        ctx = agent_run_context() or {}
        ids = ",".join(str(i) for i in ctx.get("workspace_ids") or [])
        print(f"llm_guard allowed call_type={call_type} agent_run_workspace_ids={ids}")
        return 0
    print(f"{BLOCK_EVENT} call_type={call_type} agent_run_workspace_ids="
          f"{','.join(str(i) for i in sorted(effective_agent_run_workspace_ids())) or 'none'}")
    return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
