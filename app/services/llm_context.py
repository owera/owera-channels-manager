"""S8: the sanctioned way to put a channel's YouTube data into an LLM prompt.

L2 (workspace scope): rows are read only for the workspace bound by
``llm_guard.scope``; a channel of another workspace raises ``LLMCrossWorkspace``
before anything is read, and every read is noted so ``complete()`` re-checks it.
L3 (minimization): only the L6 fields (video titles, descriptions, tags and the
channel's raw statistics) come out, through ``llm_guard.minimize``.
L4: statistics are the latest raw snapshot (subscriber/view/video counts), never a
computed rate, average or delta.
L7: excerpts are capped (``llm_guard.MAX_*``); no raw API payload, no token, no
comment/commenter data, no e-mail.

Prompt builders should call ``youtube_context`` and then ``render_block`` instead
of reading Channel/Video/ChannelMetric rows themselves.
"""
from __future__ import annotations

import json

from sqlmodel import Session, select

from app.models import Channel, ChannelMetric, Video
from app.services import llm_guard


def youtube_context(session: Session, channel_id: int, *, max_videos: int = 20,
                    include_stats: bool = True) -> dict:
    """Allowlisted YouTube fields of ``channel_id`` for the bound workspace."""
    ch = session.get(Channel, int(channel_id))
    if ch is None:
        raise ValueError(f"channel {channel_id} not found")
    llm_guard.assert_data_workspace(ch.workspace_id)       # L2: before any data read
    # S2 (PO CM 14/11): a disconnected channel (or one on the #214 clock) gives no
    # Google data at once, even while the workspace's other channels keep consent.
    from app.services import llm_derived
    if not llm_derived.channel_data_allowed(ch):
        llm_derived.note_excluded(ch, "youtube_context")
        return {}
    n = max(0, min(int(max_videos), llm_guard.MAX_VIDEOS))
    vids = session.exec(
        select(Video).where(Video.channel_id == ch.id).order_by(Video.id.desc()).limit(n)
    ).all() if n else []
    titles = [v.title or v.subject for v in vids if (v.title or v.subject)]
    descs = [v.description for v in vids if v.description]
    tags: list[str] = []
    for v in vids:
        try:
            t = json.loads(v.tags_json or "[]")
        except (TypeError, ValueError):
            t = []
        tags.extend(x for x in t if isinstance(x, str) and x not in tags)
    raw: dict = {"video_title": titles, "video_description": descs, "video_tags": tags}
    if include_stats:
        m = session.exec(select(ChannelMetric).where(ChannelMetric.channel_id == ch.id)
                         .order_by(ChannelMetric.captured_at.desc())).first()
        if m is not None:
            raw["channel_stats"] = {"subscriber_count": m.subscriber_count,
                                    "view_count": m.view_count,
                                    "video_count": m.video_count}
    clean, _ = llm_guard.minimize(raw)
    return clean


def render_block(ctx: dict) -> str:
    """Plain-text block for a prompt (never JSON of an API response)."""
    lines: list[str] = []
    for t in ctx.get("video_title") or []:
        lines.append(f"- title: {t}")
    for d in ctx.get("video_description") or []:
        lines.append(f"- description: {d}")
    if ctx.get("video_tags"):
        lines.append("- tags: " + ", ".join(ctx["video_tags"]))
    st = ctx.get("channel_stats") or {}
    if st:
        lines.append("- channel stats: " + ", ".join(f"{k.replace('_', ' ')} {v}"
                                                     for k, v in sorted(st.items())))
    return "\n".join(lines)


def fields_of(ctx: dict) -> tuple[str, ...]:
    """Field names to pass to ``llm_guard.declare`` / ``complete(fields=...)``."""
    return tuple(k for k in ("video_title", "video_description", "video_tags",
                             "channel_stats") if ctx.get(k))
