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
import re
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

    def __init__(self, workspace_id, call_type: str, reason: str | None = None):
        self.workspace_id = workspace_id
        self.call_type = call_type
        self.reason = reason
        if reason is None:
            msg = (f"{BLOCK_EVENT}: LLM call refused for workspace_id={workspace_id} "
                   f"call_type={call_type} (only Owera-internal workspaces may use the LLM "
                   "until the xAI Enterprise key; see docs/LLM_GUARD.md)")
        else:
            msg = (f"{BLOCK_EVENT}: LLM call refused for workspace_id={workspace_id} "
                   f"call_type={call_type} reason={reason} (see docs/LLM_GUARD.md)")
        super().__init__(msg)


class LLMCrossWorkspace(LLMBlocked):
    """L2: data from one workspace was about to enter another workspace's prompt."""


class LLMFieldRejected(LLMBlocked):
    """L3: a field outside the L6 allowlist was declared for a prompt."""


class LLMRawDump(LLMBlocked):
    """L7: a raw YouTube API payload was about to enter a prompt."""


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
    dtok = _data_ws.set(frozenset())
    try:
        yield
    finally:
        _data_ws.reset(dtok)
        _scope.reset(token)


def _log_block(workspace_id, call_type: str, reason: str | None = None) -> None:
    if reason is None:  # exact pre-S8 line (allowlist block), kept for log greps
        logger.warning("%s workspace_id=%s call_type=%s", BLOCK_EVENT, workspace_id, call_type)
    else:
        logger.warning("%s workspace_id=%s call_type=%s reason=%s", BLOCK_EVENT,
                       workspace_id, call_type, reason)


def _resolve(call_type: str | None, workspace_id) -> tuple[int | None, str]:
    sc = _scope.get()
    if workspace_id is _UNSET:
        ws = sc[0] if sc else None
    else:
        ws = _coerce_ws(workspace_id)
    ct = call_type or (sc[1] if sc else None) or "unknown"
    if sc and call_type and sc[1] and sc[1] != call_type:
        ct = f"{sc[1]}/{call_type}"
    return ws, ct


def block_reason(ws: int | None, ct: str, fields=_UNSET) -> str | None | bool:
    """Why ``ws`` may not make an LLM call of type ``ct`` (no side effects, no log).

    Returns ``False`` when allowed, ``None`` for the pre-S8 allowlist block (exact
    old log line / message), or a short reason string."""
    if ws is None:
        return None
    if ws in internal_workspace_ids():
        if kill_switch_on(ws, internal=True):
            return "kill_switch"
        bad = foreign_data_workspaces(ws)
        if bad:
            return "cross_workspace_data"
        return False
    if not external_release_enabled():
        return None   # flag OFF: identical to the #217 allowlist guard
    r = release_block_reason(ws, ct, fields)
    if r:
        return r
    if foreign_data_workspaces(ws):
        return "cross_workspace_data"
    return False


def would_allow(workspace_id, call_type: str) -> bool:
    """``require`` without raising or logging (render pre-check)."""
    return block_reason(_coerce_ws(workspace_id), call_type) is False


def require(call_type: str | None = None, *, workspace_id=_UNSET, fields=_UNSET) -> int:
    """Gate one LLM call. Returns the allowed workspace id or raises ``LLMBlocked``.

    ``workspace_id`` overrides the context scope when given explicitly.
    ``call_type`` names the call for the log (defaults to the scope's).
    ``fields``: the prompt's declared field names (``complete()`` passes them;
    ``None`` = undeclared). Omitted (pre-checks) → no field check. Declared
    fields outside the L6 allowlist are always rejected (``LLMFieldRejected``)."""
    ws, ct = _resolve(call_type, workspace_id)
    if fields is not _UNSET and fields is not None:
        bad = rejected_fields(fields)
        if bad:
            _log_block(ws, ct, "field_not_allowed:" + ",".join(sorted(bad)))
            raise LLMFieldRejected(ws, ct, "field_not_allowed:" + ",".join(sorted(bad)))
    r = block_reason(ws, ct, fields)
    if r is False:
        return ws  # type: ignore[return-value]
    _log_block(ws, ct, r)
    if r == "cross_workspace_data":
        raise LLMCrossWorkspace(ws, ct, r)
    raise LLMBlocked(ws, ct, r)


# ------------------------------------------------------------------ S8 (L2/L3/L7)
#
# L3 / L6 field allowlist. LegalOps (LEGAL-DECISION-LLM-YT-DATA.md L3) and the L6
# consent line (CM-CONNECT-AI-LINE.md, sha 845c22005853) name exactly these
# Google-sourced fields: video titles, descriptions, tags and channel stats.
# Adding a YouTube field here changes the public L6 line too (back to CMO +
# LegalOps), so tests/verify_llm_guard_s8.py pins this set.
YT_FIELDS: frozenset[str] = frozenset({"video_title", "video_description", "video_tags",
                                       "channel_stats"})
# Raw statistics only (YouTube channels.list ``statistics``). No derived metric
# (L4, YT III.E.4.h): no averages, rates, deltas, percentages or scores.
CHANNEL_STATS_KEYS: frozenset[str] = frozenset({"subscriber_count", "view_count",
                                                "video_count"})
# Workspace-authored, non-Google inputs (typed by the customer/operator in CM or
# generated by CM itself): allowed in a prompt, not covered by the L6 line.
CM_FIELDS: frozenset[str] = frozenset({"topic", "theme_prompt", "niche", "steer_notes",
                                       "language", "script", "storyboard", "craft_rules"})
ALLOWED_FIELDS: frozenset[str] = YT_FIELDS | CM_FIELDS

# L7 caps: a prompt carries short excerpts, never a dump of the channel.
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 1000
MAX_TAGS = 30
MAX_TAG_CHARS = 60
MAX_VIDEOS = 60

_RAW_DUMP_RE = re.compile(
    r'"kind"\s*:\s*"youtube(?:Analytics)?#|"etag"\s*:\s*"|youtube#(?:video|channel|playlistItem|search)ListResponse'
    r'|"columnHeaders"\s*:\s*\[|"(?:access|refresh)_token"\s*:', re.I)

_fields: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "llm_guard_fields", default=None)
_data_ws: contextvars.ContextVar[frozenset[int]] = contextvars.ContextVar(
    "llm_guard_data_ws", default=frozenset())


def rejected_fields(fields) -> set[str]:
    return {str(f) for f in (fields or ()) if str(f) not in ALLOWED_FIELDS}


@contextlib.contextmanager
def declare(*fields: str) -> Iterator[None]:
    """Declare the field names the next ``complete()`` prompt is built from (L3).
    Unknown names raise ``LLMFieldRejected`` right here (fail fast)."""
    bad = rejected_fields(fields)
    if bad:
        sc = _scope.get()
        ws, ct = (sc if sc else (None, "unknown"))
        _log_block(ws, ct, "field_not_allowed:" + ",".join(sorted(bad)))
        raise LLMFieldRejected(ws, ct, "field_not_allowed:" + ",".join(sorted(bad)))
    token = _fields.set(tuple(dict.fromkeys(fields)))
    try:
        yield
    finally:
        _fields.reset(token)


def declared_fields() -> tuple[str, ...] | None:
    return _fields.get()


def note_data(workspace_id) -> None:
    """L2: record that data owned by ``workspace_id`` is going into the current
    scope's prompt. ``require()`` refuses the call when any noted workspace differs
    from the bound one. Call it wherever channel/video rows are read for a prompt."""
    ws = _coerce_ws(workspace_id)
    _data_ws.set(_data_ws.get() | {ws if ws is not None else -1})


def foreign_data_workspaces(ws: int | None) -> set[int]:
    return {w for w in _data_ws.get() if w != ws}


def assert_data_workspace(workspace_id) -> None:
    """L2 hard check at the read site: data of ``workspace_id`` may only be read for
    a prompt of the same (bound) workspace. Raises ``LLMCrossWorkspace``."""
    note_data(workspace_id)
    sc = _scope.get()
    bound = sc[0] if sc else None
    ct = sc[1] if sc else "unknown"
    if bound is None or foreign_data_workspaces(bound):
        _log_block(bound, ct, "cross_workspace_data")
        raise LLMCrossWorkspace(bound, ct, "cross_workspace_data")


def minimize(payload: dict) -> tuple[dict, list[str]]:
    """L3/L4/L7: keep only allowlisted fields, raw channel stats and capped excerpts.

    Returns ``(clean, stripped)`` where ``stripped`` lists dropped field names
    (``channel_stats.<key>`` for dropped stat keys). Values are never logged."""
    clean: dict = {}
    stripped: list[str] = []
    for k, v in (payload or {}).items():
        k = str(k)
        if k not in ALLOWED_FIELDS:
            stripped.append(k)
            continue
        if k == "channel_stats":
            if not isinstance(v, dict):
                stripped.append(k)
                continue
            stats = {}
            for sk, sv in v.items():
                if sk in CHANNEL_STATS_KEYS and isinstance(sv, int) and not isinstance(sv, bool):
                    stats[sk] = sv
                else:
                    stripped.append(f"channel_stats.{sk}")
            if stats:
                clean[k] = stats
            continue
        if k == "video_tags":
            if not isinstance(v, (list, tuple)):
                stripped.append(k)
                continue
            clean[k] = [str(t)[:MAX_TAG_CHARS] for t in v if isinstance(t, str)][:MAX_TAGS]
            continue
        if k in ("video_title", "video_description") and isinstance(v, (list, tuple)):
            cap = MAX_TITLE_CHARS if k == "video_title" else MAX_DESCRIPTION_CHARS
            clean[k] = [str(x)[:cap] for x in v if isinstance(x, str)][:MAX_VIDEOS]
            continue
        if isinstance(v, (dict, list, tuple, set)):
            stripped.append(k)          # L7: no nested / raw structures
            continue
        if v is None:
            continue
        text = str(v)
        if k == "video_title":
            text = text[:MAX_TITLE_CHARS]
        elif k == "video_description":
            text = text[:MAX_DESCRIPTION_CHARS]
        clean[k] = text
    if stripped:
        sc = _scope.get()
        logger.info("llm_fields_stripped workspace_id=%s call_type=%s fields=%s",
                    sc[0] if sc else None, sc[1] if sc else "unknown",
                    ",".join(sorted(stripped)))
    return clean, stripped


def check_prompt(prompt: str | None, system: str | None = None, call_type: str | None = None) -> None:
    """L7: refuse a prompt that carries a raw YouTube / Analytics API payload or an
    OAuth token field. Logs ids only."""
    text = f"{system or ''}\n{prompt or ''}"
    if _RAW_DUMP_RE.search(text):
        ws, ct = _resolve(call_type, _UNSET)
        _log_block(ws, ct, "raw_youtube_payload")
        raise LLMRawDump(ws, ct, "raw_youtube_payload")


def log_call(workspace_id, call_type: str, fields, *, backend: str,
             input_tokens: int | None, output_tokens: int | None, estimated: bool) -> None:
    """L3 per-call log: workspace, route (call type), field NAMES and token counts.
    Never the prompt or the response."""
    logger.info("llm_call workspace_id=%s call_type=%s fields=%s backend=%s "
                "input_tokens=%s output_tokens=%s tokens=%s ws_read=%s", workspace_id, call_type,
                ",".join(fields) if fields else "undeclared", backend,
                "na" if input_tokens is None else input_tokens,
                "na" if output_tokens is None else output_tokens,
                "estimated" if estimated else "reported", ws_read_state())


def ws_read_state() -> str:
    """``ok`` / ``fail`` / ``na`` (no row read in this context)."""
    try:
        return _WS_READ.get()
    except Exception:  # noqa: BLE001
        return "na"


# ------------------------------------------------------------------ kill switch + release

import contextvars as _cv

# S8 (CTO 2026-11-14): last workspace-row read result in this context, surfaced as
# ``ws_read=ok|fail|na`` on the llm_call line so unreadable-row passes are countable.
_WS_READ: _cv.ContextVar[str] = _cv.ContextVar("llm_guard_ws_read", default="na")

# Short SQLite busy_timeout for the guard's row read (CTO: ~200 ms) so a writer
# lock doesn't stall an LLM call; on timeout internal passes with a warning,
# external is blocked.
WS_READ_BUSY_TIMEOUT_MS = 200

def external_release_enabled() -> bool:
    """``MANAGER_LLM_EXTERNAL_RELEASE`` (default OFF). OFF = #217 allowlist only."""
    return bool(getattr(settings, "llm_external_release", False))


# Purposes LegalOps accepts for an external workspace (ideas, script, metadata,
# Gate B, submit). Anything else (rubric harness, agents…) stays internal-only.
RELEASE_CALL_TYPES: frozenset[str] = frozenset({
    "ideas", "script_compose", "metadata", "thumbnail", "gate_b_preflight", "render",
    "render_submit", "mpt_render", "mpt_social_metadata"})


def _workspace_row(ws: int):
    """``(row, error)``; row None + error None = no such workspace."""
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.models import Workspace

        with Session(engine) as s:
            prev = None
            try:
                if engine.dialect.name == "sqlite":
                    conn = s.connection()
                    prev = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
                    conn.exec_driver_sql(f"PRAGMA busy_timeout={int(WS_READ_BUSY_TIMEOUT_MS)}")
                row = s.get(Workspace, int(ws))
                if row is not None:
                    s.expunge(row)
            finally:
                if prev is not None:
                    try:
                        s.connection().exec_driver_sql(f"PRAGMA busy_timeout={int(prev)}")
                    except Exception:  # noqa: BLE001
                        pass
            _WS_READ.set("ok")
            return row, None
    except Exception as e:  # noqa: BLE001
        _WS_READ.set("fail")
        return None, type(e).__name__


def kill_switch_on(ws: int, *, internal: bool) -> bool:
    """``Workspace.llm_disabled``. Unreadable row: an internal workspace keeps
    today's behavior (allowed, warning logged); an external one is blocked."""
    row, err = _workspace_row(ws)
    if err is not None:
        if internal:
            logger.warning("llm_guard_kill_switch_unreadable workspace_id=%s error=%s", ws, err)
            return False
        return True
    return bool(row is not None and getattr(row, "llm_disabled", False))


def _is_paid(row) -> bool:
    # S1 adds Workspace.paid_at (acd4794); until it merges, Stripe's "active".
    return (getattr(row, "paid_at", None) is not None
            or (getattr(row, "subscription_status", "") or "") == "active")


def release_block_reason(ws: int, ct: str, fields=_UNSET) -> str | None:
    """Release policy for a NON-internal workspace (flag ON only). ``None`` = allowed.

    PROPOSED policy (PO S8 cut; final decision CTO/LegalOps/CoS/PO, due Wed 18):
    paid or is_test AND terms + privacy accepted, not kill-switched, purpose in
    RELEASE_CALL_TYPES, prompt fields declared, and the API backend with the
    no-training retention ack (the CLI consumer session may train: never external)."""
    try:
        from app.services import llm as _llm
        if _llm.backend() != "api" or not _llm.data_retention():
            return "backend_may_train"
    except Exception:  # noqa: BLE001
        return "backend_may_train"
    parts = [p for p in str(ct).split("/") if p]
    if not parts or any(p not in RELEASE_CALL_TYPES for p in parts):
        return "purpose_not_released"
    if fields is None:
        return "fields_undeclared"
    row, err = _workspace_row(ws)
    if err is not None:
        return "workspace_unreadable"
    if row is None:
        return "workspace_unknown"
    if getattr(row, "llm_disabled", False):
        return "kill_switch"
    if not (_is_paid(row) or getattr(row, "is_test", False)):
        return "not_paid_or_test"
    if getattr(row, "terms_accepted_at", None) is None or getattr(row, "privacy_accepted_at", None) is None:
        return "terms_privacy_not_accepted"
    return None


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
