"""Duplicate episode number check (P1, council 06/10).

An episode number lives in the title (else subject) suffix ``· <Series> NN``
— the same representation video_gen.renumber_series continues from. VM Gate B
06/10 failed #1400 "duplicate Shipping 1 (#1313)" and #1402 "duplicate
Shipping 3 (#1311)"; renumber_series (P0 10/03) fixes NEW ideas, this blocks
the ones that still collide.

duplicate_episode_reason(session, video) is None or why the row may not
leave review toward publish:
  * the title repeats its episode number (two ``· Series N`` suffixes, the
    ``Series N`` again in the head, or an ``Ep N`` / ``Episode N`` / ``#N``
    marker in the head next to the suffix);
  * another video on the SAME channel already holds that series + number:
    a row that aired or is committed to air (approved / publishing /
    published). Two rows still in review with the same number can both sit
    there; the first one approved takes the number and the other is blocked.
    Rejected / failed / queued rows never hold a number here (the audience
    never saw them); renumber_series still treats them as burnt for new ideas.
Called next to craft.publish_craft_block_reason at approve, retry-republish,
render finalize (skip-gate auto-approve), the publish loop and the
review_ready digest.
"""
from __future__ import annotations

import re

from sqlmodel import Session, select

from app.models import Video, VideoStatus

# Every "· <Series> NN" group in a title ("·" never appears inside a label).
_SUFFIX_RE = re.compile(r"·\s*([^·\d][^·]*?)\s+(\d{1,4})(?=\s*(?:·|$))")
_EP_MARK_RE = re.compile(r"(?:\b(?:ep|eps|episode|epis[oó]dio)\.?\s*#?\s*|#)(\d{1,4})\b", re.IGNORECASE)
HOLDER_STATUSES = frozenset({VideoStatus.APPROVED, VideoStatus.PUBLISHING, VideoStatus.PUBLISHED})
REASON_PREFIX = "duplicate episode number"


def episode_of(title: str | None) -> tuple[str, str, int] | None:
    """(series casefolded, series as written, number) of the LAST suffix group."""
    groups = _SUFFIX_RE.findall((title or "").strip())
    if not groups:
        return None
    label, n = groups[-1]
    label = " ".join(label.split())
    return label.casefold(), label, int(n)


def title_repeat_reason(title: str | None) -> str | None:
    """Why the title itself repeats/doubles its episode number, else None."""
    t = (title or "").strip()
    groups = _SUFFIX_RE.findall(t)
    if not groups:
        return None
    label, n = " ".join(groups[-1][0].split()), int(groups[-1][1])
    if len(groups) > 1:
        shown = ", ".join(f"{' '.join(g[0].split())} {int(g[1])}" for g in groups)
        return f"{REASON_PREFIX}: title carries {len(groups)} series numbers ({shown})"
    head = t.split("·", 1)[0]
    if re.search(r"(?<!\w)" + re.escape(label) + r"\s+0*" + str(n) + r"\b", head, re.IGNORECASE):
        return f"{REASON_PREFIX}: '{label} {n}' is repeated in the title head"
    m = _EP_MARK_RE.search(head)
    if m:
        return (f"{REASON_PREFIX}: title head carries an episode marker {m.group(0).strip()!r} "
                f"next to the series number '{label} {n}'")
    return None


def _label_of(v) -> str:
    return (getattr(v, "title", None) or getattr(v, "subject", None) or "")


def episode_holder(session: Session, video: Video) -> Video | None:
    """The other video on this channel that holds video's series + number."""
    ep = episode_of(_label_of(video))
    if not ep or not video.channel_id:
        return None
    rows = session.exec(select(Video).where(Video.channel_id == video.channel_id,
                                            Video.status.in_(sorted(HOLDER_STATUSES)))).all()
    holders = []
    for o in rows:
        if o.id == video.id or o.status not in HOLDER_STATUSES:
            continue
        oe = episode_of(_label_of(o))
        if oe and (oe[0], oe[2]) == (ep[0], ep[2]):
            holders.append(o)
    if not holders:
        return None
    # Prefer the one that aired, then the oldest.
    holders.sort(key=lambda o: (o.status != VideoStatus.PUBLISHED, o.id or 0))
    return holders[0]


def duplicate_episode_reason(session: Session, video: Video) -> str | None:
    """None = no duplicate episode number. Else an operator-readable reason."""
    title = _label_of(video)
    rep = title_repeat_reason(title)
    if rep:
        return rep
    holder = episode_holder(session, video)
    if holder is None:
        return None
    ep = episode_of(title)
    return (f"{REASON_PREFIX}: '{ep[1]} {ep[2]}' is already used on this channel by "
            f"#{holder.id} ({holder.status}) — renumber (next free via renumber_series) "
            "or reject one")
