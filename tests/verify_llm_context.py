"""Dependency-free regression checks for app/services/llm_context.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_llm_context.py

``youtube_context`` / ``render_block`` / ``fields_of`` are the S8 sanctioned
seam that puts a channel's YouTube data into an LLM prompt. Prompt builders
must go through this module instead of reading Channel/Video/ChannelMetric
rows themselves. Previously only incidental pins lived inside
``verify_llm_guard_s8`` (cross-workspace + declare wiring) and
``verify_s2_derived_data`` (disconnect → empty dict). The L2-before-allowed
order, max_videos clamp, newest-first window, title-or-subject fallback,
tags_json non-list swallow, include_stats, latest-metric pick, render_block
shape, and fields_of allowlist order had no dedicated suite.

Covers, dependency-free (in-memory SQLite, no network, no LLM):
  - missing channel → ValueError naming the id
  - L2: assert_data_workspace before the allowed/empty short-circuit (a
    disconnected foreign channel still raises LLMCrossWorkspace, not {})
  - no scope → LLMCrossWorkspace
  - disconnected / access_lost_at (#214 clock) → {} + llm_channel_excluded
  - titles: title-or-subject, skip empty both, newest id first
  - max_videos: 0 skips the query; negative → 0; clamp to llm_guard.MAX_VIDEOS
  - descriptions skip None/""; tags unique strings, skip non-str
  - tags_json corrupt / null / number / object / bool never raises
  - include_stats False omits channel_stats even when a metric exists
  - no metric omits the key; two snapshots pick captured_at desc
  - sibling channel videos/metrics stay out
  - minimize keeps only L6 keys (no derived rates)
  - render_block / fields_of shape and empty ctx

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import io
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, ChannelMetric, OAuthStatus, Topic, Video, Workspace
from app.services import llm_context, llm_derived, llm_guard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _seed():
    with Session(engine) as s:
        s.add(Workspace(id=1, slug="owera", name="Owera"))
        s.add(Workspace(id=2, slug="buyer-b", name="Buyer B"))
        s.commit()
        ch = Channel(workspace_id=1, slug="os", name="Owera Software",
                     oauth_status=OAuthStatus.CONNECTED)
        other = Channel(workspace_id=1, slug="rr", name="Rodrigo Recio",
                        oauth_status=OAuthStatus.CONNECTED)
        foreign = Channel(workspace_id=2, slug="buyer", name="Buyer",
                          oauth_status=OAuthStatus.CONNECTED)
        s.add_all([ch, other, foreign])
        s.commit()
        for c in (ch, other, foreign):
            s.refresh(c)
        t = Topic(channel_id=ch.id, name="tools", weight=1)
        t_other = Topic(channel_id=other.id, name="other", weight=1)
        t_foreign = Topic(channel_id=foreign.id, name="foreign", weight=1)
        s.add_all([t, t_other, t_foreign])
        s.commit()
        s.refresh(t)
        s.refresh(t_other)
        s.refresh(t_foreign)
        return {
            "ch": ch.id, "other": other.id, "foreign": foreign.id,
            "topic": t.id, "topic_other": t_other.id, "topic_foreign": t_foreign.id,
        }


IDS = _seed()


def sess():
    return Session(engine)


def scoped(ws=1, ct="ideas"):
    return llm_guard.scope(ws, ct)


def add_video(s, *, channel_id, topic_id, subject, title=None, description=None,
              tags_json=None, **kw):
    v = Video(channel_id=channel_id, topic_id=topic_id, subject=subject,
              title=title, description=description, tags_json=tags_json, **kw)
    s.add(v)
    s.commit()
    s.refresh(v)
    return v


def ctx(s, channel_id, **kw):
    with scoped():
        return llm_context.youtube_context(s, channel_id, **kw)


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------
print("module contracts")
ok(callable(llm_context.youtube_context) and callable(llm_context.render_block)
   and callable(llm_context.fields_of), "three public helpers")
ok(llm_guard.MAX_VIDEOS == 60, "L7 video cap the clamp uses")
ok(llm_derived.CHANNEL_EXCLUDED_EVENT == "llm_channel_excluded",
   "excluded-channel log event")
_src = Path(llm_context.__file__).read_text()
ok("min(int(max_videos), llm_guard.MAX_VIDEOS)" in _src,
   "max_videos clamp is at the query (minimize's [:MAX_VIDEOS] cannot discriminate a dropped min())")
ok("llm_guard.assert_data_workspace(ch.workspace_id)" in _src.split(
    "channel_data_allowed")[0],
   "assert_data_workspace is source-before channel_data_allowed")


# ---------------------------------------------------------------------------
# Missing channel / L2 workspace
# ---------------------------------------------------------------------------
print("missing channel + L2 workspace")
with sess() as s:
    try:
        ctx(s, 99999)
        ok(False, "missing channel raises")
    except ValueError as e:
        ok("99999" in str(e), f"ValueError names the id ({e})")

with sess() as s:
    try:
        llm_context.youtube_context(s, IDS["ch"])   # no scope
        ok(False, "no scope raises")
    except llm_guard.LLMCrossWorkspace:
        ok(True, "no scope → LLMCrossWorkspace")

with sess() as s:
    # Discriminator: allowed-check-before-assert would return {} for a
    # disconnected foreign channel instead of raising.
    foreign = s.get(Channel, IDS["foreign"])
    foreign.oauth_status = OAuthStatus.DISCONNECTED
    s.add(foreign)
    s.commit()
    try:
        with scoped(1, "ideas"):
            llm_context.youtube_context(s, IDS["foreign"])
        ok(False, "disconnected foreign channel raises L2")
    except llm_guard.LLMCrossWorkspace:
        ok(True, "disconnected foreign channel → LLMCrossWorkspace (assert before allowed)")
    foreign = s.get(Channel, IDS["foreign"])
    foreign.oauth_status = OAuthStatus.CONNECTED
    s.add(foreign)
    s.commit()

with sess() as s, scoped(2, "ideas"):
    try:
        llm_context.youtube_context(s, IDS["ch"])
        ok(False, "ws2 scope cannot read ws1 channel")
    except llm_guard.LLMCrossWorkspace:
        ok(True, "bound ws2 + ws1 channel → LLMCrossWorkspace")


# ---------------------------------------------------------------------------
# Per-channel exclusion (disconnect / #214 clock)
# ---------------------------------------------------------------------------
print("per-channel exclusion")
log = io.StringIO()
handler = logging.StreamHandler(log)
lg = logging.getLogger("manager.llm_derived")
lg.addHandler(handler)
lg.setLevel(logging.INFO)

with sess() as s:
    ch = s.get(Channel, IDS["ch"])
    ch.oauth_status = OAuthStatus.DISCONNECTED
    s.add(ch)
    s.commit()
    out = ctx(s, IDS["ch"])
    ok(out == {}, "disconnected → {}")
    ok("llm_channel_excluded" in log.getvalue() and "youtube_context" in log.getvalue(),
       "disconnect logs llm_channel_excluded where=youtube_context")
    ch = s.get(Channel, IDS["ch"])
    ch.oauth_status = OAuthStatus.CONNECTED
    s.add(ch)
    s.commit()

log.seek(0)
log.truncate(0)
with sess() as s:
    ch = s.get(Channel, IDS["ch"])
    ch.access_lost_at = NOW
    ch.oauth_status = OAuthStatus.CONNECTED   # clock running, status not yet flipped
    s.add(ch)
    s.commit()
    out = ctx(s, IDS["ch"])
    ok(out == {}, "#214 clock (access_lost_at set) → {} even while CONNECTED")
    ok("youtube_context" in log.getvalue(), "#214 clock logs where=youtube_context")
    ch = s.get(Channel, IDS["ch"])
    ch.access_lost_at = None
    s.add(ch)
    s.commit()

lg.removeHandler(handler)


# ---------------------------------------------------------------------------
# Titles / newest-first / max_videos
# ---------------------------------------------------------------------------
print("titles, newest-first, max_videos")
with sess() as s:
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="older subject", title="older title")
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="newer subject", title="newer title")
    add_video(s, channel_id=IDS["other"], topic_id=IDS["topic_other"],
              subject="sibling subject", title="sibling title")
    out = ctx(s, IDS["ch"])
    titles = out.get("video_title") or []
    ok(titles[0] == "newer title" and "older title" in titles,
       f"newest id first ({titles})")
    ok("sibling title" not in titles, "sibling channel titles stay out")

    out1 = ctx(s, IDS["ch"], max_videos=1)
    ok(out1.get("video_title") == ["newer title"],
       "max_videos=1 is the newest only")

    out0 = ctx(s, IDS["ch"], max_videos=0)
    ok("video_title" not in out0 or out0.get("video_title") in (None, []),
       "max_videos=0 returns no titles")

    out_neg = ctx(s, IDS["ch"], max_videos=-3)
    ok("video_title" not in out_neg or out_neg.get("video_title") in (None, []),
       "negative max_videos clamps to 0")

with sess() as s:
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="subject-only", title=None)
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="subject-when-blank-title", title="")
    out = ctx(s, IDS["ch"], max_videos=20)
    titles = out.get("video_title") or []
    ok("subject-only" in titles, "None title falls back to subject")
    ok("subject-when-blank-title" in titles, "empty title falls back to subject")

with sess() as s, patch.object(llm_guard, "MAX_VIDEOS", 2):
    # 3 videos already on ch from above; clamp must bite even when the caller
    # asks for 20. A dropped `min(..., MAX_VIDEOS)` returns 3.
    out = ctx(s, IDS["ch"], max_videos=20)
    n = len(out.get("video_title") or [])
    ok(n == 2, f"max_videos request is clamped to MAX_VIDEOS (got {n})")


# ---------------------------------------------------------------------------
# Descriptions + tags (including non-list tags_json)
# ---------------------------------------------------------------------------
print("descriptions + tags_json")
with sess() as s:
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="with desc", title="with desc",
              description="a caption")
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="empty desc", title="empty desc",
              description="")
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="null desc", title="null desc",
              description=None)
    out = ctx(s, IDS["ch"])
    descs = out.get("video_description") or []
    ok("a caption" in descs, "non-empty description is kept")
    ok("" not in descs, "empty description is skipped")

with sess() as s:
    add_video(s, channel_id=IDS["ch"], topic_id=IDS["topic"],
              subject="tags-good", title="tags-good",
              tags_json=json.dumps(["alpha", "beta", "alpha", 1, None, "beta"]))
    out = ctx(s, IDS["ch"])
    tags = out.get("video_tags") or []
    ok(tags.count("alpha") == 1 and tags.count("beta") == 1,
       f"duplicate tags collapsed ({tags})")
    ok(1 not in tags and None not in tags, "non-str tags never land")

# TDD red on main: tags_json JSON-null / number / object / bool TypeError
# because json.loads succeeds and the iterate sits outside the try.
print("tags_json non-list swallow")
for raw, label in (("null", "JSON null"), ("0", "JSON number"),
                   ("true", "JSON bool"), ("{}", "JSON object"),
                   ("not-json", "corrupt"), (None, "SQL NULL")):
    with sess() as s:
        add_video(s, channel_id=IDS["other"], topic_id=IDS["topic_other"],
                  subject=f"tags-{label}", title=f"tags-{label}",
                  tags_json=raw)
        try:
            out = ctx(s, IDS["other"])
        except Exception as e:
            ok(False, f"tags_json={raw!r} ({label}) must not raise ({type(e).__name__}: {e})")
            out = {}
        ok("video_tags" not in out or out.get("video_tags") in (None, []),
           f"tags_json={raw!r} ({label}) yields no tags")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
print("channel_stats")
with sess() as s:
    old = ChannelMetric(channel_id=IDS["ch"], subscriber_count=1, view_count=10,
                        video_count=1, captured_at=NOW - timedelta(days=1))
    new = ChannelMetric(channel_id=IDS["ch"], subscriber_count=7, view_count=99,
                        video_count=3, captured_at=NOW)
    sibling = ChannelMetric(channel_id=IDS["other"], subscriber_count=50,
                            view_count=500, video_count=9, captured_at=NOW)
    s.add_all([old, new, sibling])
    s.commit()
    out = ctx(s, IDS["ch"])
    st = out.get("channel_stats") or {}
    ok(st == {"subscriber_count": 7, "view_count": 99, "video_count": 3},
       f"latest captured_at snapshot, raw counts only ({st})")
    ok(set(st) <= llm_guard.CHANNEL_STATS_KEYS, "stats keys ⊆ CHANNEL_STATS_KEYS")
    ok("avg" not in str(st).lower() and "rate" not in str(st).lower(),
       "no derived rate/average in stats")

    out_off = ctx(s, IDS["ch"], include_stats=False)
    ok("channel_stats" not in out_off,
       "include_stats=False omits channel_stats even when a metric exists")

with sess() as s, scoped(2, "ideas"):
    out = llm_context.youtube_context(s, IDS["foreign"], include_stats=True)
    ok("channel_stats" not in out, "no metric row → no channel_stats key")


# ---------------------------------------------------------------------------
# Output key set (minimize)
# ---------------------------------------------------------------------------
print("minimize / L6 keys")
with sess() as s:
    out = ctx(s, IDS["ch"])
    ok(set(out) <= {"video_title", "video_description", "video_tags", "channel_stats"},
       f"only L6 keys leave youtube_context ({sorted(out)})")


# ---------------------------------------------------------------------------
# render_block + fields_of
# ---------------------------------------------------------------------------
print("render_block + fields_of")
empty = llm_context.render_block({})
ok(empty == "", "empty ctx renders as empty string")
ok(llm_context.fields_of({}) == (), "empty ctx declares no fields")
ok(llm_context.fields_of({"video_title": []}) == (),
   "falsy empty list is omitted from fields_of")

block = llm_context.render_block({
    "video_title": ["T1", "T2"],
    "video_description": ["D1"],
    "video_tags": ["a", "b"],
    "channel_stats": {"view_count": 9, "subscriber_count": 2, "video_count": 1},
})
ok("- title: T1" in block and "- title: T2" in block, "titles each get a line")
ok("- description: D1" in block, "description line")
ok("- tags: a, b" in block, "tags joined on one line")
ok("- channel stats: subscriber count 2, video count 1, view count 9" in block,
   "stats keys sorted, underscores → spaces")
ok("{" not in block and "etag" not in block, "render_block is not a JSON dump")

fields = llm_context.fields_of({
    "video_title": ["T"],
    "video_description": ["D"],
    "video_tags": ["a"],
    "channel_stats": {"view_count": 1},
    "secret": "nope",
})
ok(fields == ("video_title", "video_description", "video_tags", "channel_stats"),
   f"fields_of is the L6 allowlist order, extras ignored ({fields})")
ok(llm_context.fields_of({"video_title": ["T"], "channel_stats": {"view_count": 1}})
   == ("video_title", "channel_stats"),
   "fields_of skips missing middle keys, keeps order")

print(f"\nALL {_checks} CHECKS PASSED")
