"""S2 (PO CM / LegalOps 14/11): Google-derived text and per-channel exclusion.

``script`` and ``storyboard`` are CM-authored but derived from Google data, so:

1. **Same draft, same workspace only.** A render / compose / metadata step binds
   its draft with ``draft_scope(video_id)``. ``check_prompt`` (called by
   ``llm.complete`` after ``llm_guard.check_prompt``) refuses an EXTERNAL
   workspace's prompt that declares ``script`` / ``storyboard`` and carries the
   head of ANOTHER draft's script (``LLMCrossDraft``, reason
   ``foreign_draft_data``). Internal workspaces (OS/RR) are only logged: their
   prompts and quality gates stay exactly as they are.
2. **#214 scope.** ``youtube_purge.purge_channel`` also clears scripts and
   storyboards (``Video.script``, ``Video.creation_config``, the render job's
   text files) when the loss reason is a deletion request or a workspace
   closure (7 days). A plain disconnect keeps the draft.
3. **No channel numbers.** External workspaces' script / storyboard prompts get
   ``NO_CHANNEL_NUMBERS_RULE`` appended to the system prompt; ``flag_stats_echo``
   logs a warning (never blocks) when a raw channel statistic appears literally
   in the generated script / storyboard text.

Per-channel exclusion: ``channel_data_allowed`` — a channel that is not
connected (or whose access was lost, #214 clock running) contributes no Google
data to any prompt, even while the workspace's other channels keep consent.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from typing import Iterator

logger = logging.getLogger("manager.llm_derived")

DERIVED_FIELDS: frozenset[str] = frozenset({"script", "storyboard"})
FOREIGN_DRAFT_EVENT = "llm_foreign_draft_data"
STATS_ECHO_EVENT = "llm_stats_echo"
CHANNEL_EXCLUDED_EVENT = "llm_channel_excluded"

# Head of a script used as its fingerprint (normalized, whitespace-collapsed).
FINGERPRINT_CHARS = 60
# Most recent scripts compared per prompt (bounded cost; a prompt is seconds).
MAX_SCRIPTS_SCANNED = 2000
# Raw statistics below this are too common in prose ("3 tips") to flag.
MIN_STAT_TO_FLAG = 100

NO_CHANNEL_NUMBERS_RULE = (
    "Do NOT copy this channel's numbers (views, subscribers, watch time, retention, "
    "video counts or any other channel statistic) into the script or the storyboard.")

_draft: contextvars.ContextVar[int | None] = contextvars.ContextVar("cm_llm_draft", default=None)


def current_draft() -> int | None:
    return _draft.get()


def bind_draft(video_id):
    return _draft.set(int(video_id) if video_id is not None else None)


def unbind_draft(token) -> None:
    _draft.reset(token)


@contextlib.contextmanager
def draft_scope(video_id) -> Iterator[None]:
    """Bind the draft whose script / storyboard may appear in this context's prompts."""
    tok = bind_draft(video_id)
    try:
        yield
    finally:
        _draft.reset(tok)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _is_external(ws) -> bool:
    from app.services import llm_guard
    return ws is not None and int(ws) not in llm_guard.internal_workspace_ids()


def foreign_drafts_in(prompt: str, *, bound_video_id: int | None) -> list[int]:
    """Ids of OTHER drafts whose script head appears in ``prompt``."""
    from sqlmodel import Session, func, select

    from app.db import engine
    from app.models import Video

    text = _norm(prompt)
    if not text:
        return []
    hits: list[int] = []
    with Session(engine) as s:
        rows = s.exec(select(Video.id, Video.script).where(
            Video.script.is_not(None), func.length(Video.script) >= FINGERPRINT_CHARS)
            .order_by(Video.id.desc()).limit(MAX_SCRIPTS_SCANNED)).all()
    for vid, script in rows:
        if bound_video_id is not None and vid == bound_video_id:
            continue
        head = _norm(script)[:FINGERPRINT_CHARS]
        if len(head) >= FINGERPRINT_CHARS and head in text:
            hits.append(int(vid))
    return hits


def check_prompt(prompt: str | None, system: str | None, fields, workspace_id) -> None:
    """Refuse (external) / log (internal) another draft's script in this prompt."""
    if not fields or not (set(fields) & DERIVED_FIELDS):
        return
    from app.services import llm_guard
    try:
        hits = foreign_drafts_in(f"{system or ''}\n{prompt or ''}", bound_video_id=current_draft())
    except Exception as e:  # noqa: BLE001
        if _is_external(workspace_id):   # can't verify → fail closed for external
            logger.warning("%s workspace_id=%s draft=%s check_failed=%s", FOREIGN_DRAFT_EVENT,
                           workspace_id, current_draft(), type(e).__name__)
            raise llm_guard.LLMCrossDraft(workspace_id, "derived", "foreign_draft_unverifiable")
        return
    if not hits:
        return
    logger.warning("%s workspace_id=%s draft=%s foreign_drafts=%s", FOREIGN_DRAFT_EVENT,
                   workspace_id, current_draft(), ",".join(map(str, hits[:10])))
    if _is_external(workspace_id):
        raise llm_guard.LLMCrossDraft(workspace_id, "derived", "foreign_draft_data")


def external_system(system: str | None) -> str | None:
    """System prompt for a script / storyboard call: the no-channel-numbers rule is
    appended ONLY for an external workspace (bound guard scope). Internal (OS/RR)
    prompts are returned unchanged, byte for byte."""
    from app.services import llm_guard
    sc = llm_guard.current()
    ws = sc[0] if sc else None
    if not _is_external(ws):
        return system
    return f"{system}\n\n{NO_CHANNEL_NUMBERS_RULE}" if system else NO_CHANNEL_NUMBERS_RULE


def channel_stats(channel_id) -> dict[str, int]:
    """Latest raw statistics of the channel (what ``llm_context`` may send)."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import ChannelMetric

    if channel_id is None:
        return {}
    with Session(engine) as s:
        m = s.exec(select(ChannelMetric).where(ChannelMetric.channel_id == int(channel_id))
                   .order_by(ChannelMetric.captured_at.desc())).first()
    if m is None:
        return {}
    return {k: int(getattr(m, k)) for k in ("subscriber_count", "view_count", "video_count")
            if isinstance(getattr(m, k, None), int)}


def _variants(n: int) -> set[str]:
    plain = str(n)
    out = {plain}
    grouped = f"{n:,}"
    out |= {grouped, grouped.replace(",", "."), grouped.replace(",", " ")}
    return out


def stats_echo(text: str, stats: dict[str, int]) -> list[str]:
    """Stat keys whose value appears literally (as a whole number) in ``text``."""
    hits: list[str] = []
    t = text or ""
    for k, v in sorted((stats or {}).items()):
        if not isinstance(v, int) or v < MIN_STAT_TO_FLAG:
            continue
        for form in _variants(v):
            if re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d]|[.,]\d)", t):
                hits.append(k)
                break
    return hits


def flag_stats_echo(text: str, *, kind: str, channel_id=None, video_id=None,
                    workspace_id=None, stats: dict[str, int] | None = None) -> list[str]:
    """Post-generation check: log ``llm_stats_echo`` (warning) when a raw channel
    statistic appears literally in the generated ``kind`` text. Never raises,
    never blocks."""
    try:
        st = stats if stats is not None else channel_stats(channel_id)
        hits = stats_echo(text, st)
        if hits:
            logger.warning("%s workspace_id=%s channel_id=%s video_id=%s kind=%s stats=%s",
                           STATS_ECHO_EVENT, workspace_id, channel_id, video_id, kind,
                           ",".join(hits))
        return hits
    except Exception as e:  # noqa: BLE001
        logger.warning("%s check_failed=%s", STATS_ECHO_EVENT, type(e).__name__)
        return []


def html_text(html: str) -> str:
    """Visible text of a storyboard composition (tags/scripts stripped)."""
    h = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    return re.sub(r"<[^>]+>", " ", h)


def channel_data_allowed(channel) -> bool:
    """Google data of this channel may enter a prompt: connected and no #214 clock."""
    from app.models import OAuthStatus
    return (channel is not None and getattr(channel, "oauth_status", None) == OAuthStatus.CONNECTED
            and getattr(channel, "access_lost_at", None) is None)


def note_excluded(channel, where: str) -> None:
    logger.info("%s channel_id=%s workspace_id=%s where=%s", CHANNEL_EXCLUDED_EVENT,
                getattr(channel, "id", None), getattr(channel, "workspace_id", None), where)


def idea_existing_subjects(session, topic_id: int, channel) -> list[str]:
    """Subjects the ideas prompt lists as "don't repeat". For an EXTERNAL
    workspace's channel that is not connected (disconnect / #214 clock), rows
    that went to or came from YouTube (``yt_video_id`` set) are left out at once:
    their titles are that channel's YouTube data. Internal channels: unchanged."""
    from sqlmodel import select

    from app.models import Video
    q = select(Video.subject).where(Video.topic_id == topic_id)
    if channel is not None and _is_external(channel.workspace_id) and not channel_data_allowed(channel):
        note_excluded(channel, "ideas")
        q = q.where(Video.yt_video_id.is_(None))
    return list(session.exec(q).all())
