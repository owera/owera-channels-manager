"""Gate B preflight before a render slot is consumed (CoS 2026-11-01).

Runs the same hook / first-cut / card-sync checks Gate B applies after a full
HyperFrames render, but against compose + edge-tts word timings only. Topics
flagged ``gate_b_preflight`` (see topic_flags) skip submit when those checks
would FAIL, with a stored reason — no silent skip, no daily_render_budget burn.

Does not touch publish, vm_pass, or operator-held rows (#1458).
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from app.services import craft, topic_flags

logger = logging.getLogger("manager.gate_b_preflight")

PREFIX = "gate B preflight:"
CC_KEY = "gate_b_preflight"


def applies(topic_id, *, content_format: str | None) -> bool:
    if (content_format or "short") == "long":
        return False
    return topic_flags.has(topic_flags.GATE_B_PREFLIGHT, topic_id)


def _first_cut_hit(board: list[dict]) -> str | None:
    if len(board) < 2 or (board[0].get("type") or "") != "hook":
        return None
    cut = float(board[1].get("start") or 0.0)
    if cut > craft.HOOK_FIRST_CUT_BY_S + 1e-6:
        return (f"first cut at {cut:.2f}s "
                f"(max {craft.HOOK_FIRST_CUT_BY_S:.1f}s)")
    return None


def _os_hook_timing_hits(board, words, script, sources, duration) -> list[str]:
    """Hook-class timing hits on OS boards (stale hook, claim spoken / word count)."""
    if not board or (board[0].get("type") or "") != "hook":
        return []
    hits: list[str] = []
    for h in craft.card_timing_hits(
            board, words, script, sources, duration, fail_closed=True):
        if h.get("i") == 0 and h.get("check") in (
                "stale_card", "quote_before_speech", "unspoken_card"):
            hits.append(h.get("detail") or str(h))
    claim = board[0].get("text") or ""
    n = craft.claim_word_count(claim)
    if n > craft.HOOK_CLAIM_MAX_WORDS:
        hits.append(f"claim {n} words (max {craft.HOOK_CLAIM_MAX_WORDS})")
    end = craft.claim_spoken_end(claim, words)
    if isinstance(end, (int, float)) and float(end) > craft.HOOK_CLAIM_SPOKEN_BY_S + 1e-6:
        hits.append(
            f"claim spoken by {float(end):.2f}s "
            f"(max {craft.HOOK_CLAIM_SPOKEN_BY_S:.1f}s)")
    return hits


def preflight_hits(
    board: list[dict],
    *,
    brand: str | None,
    topic_id,
    hook_pace: dict | None,
    card_sync: dict | None,
    words,
    script: str,
    duration: float,
    sources,
) -> list[dict[str, str]]:
    """Structured hits in scope for preflight (hook / cut / card_sync only)."""
    out: list[dict[str, str]] = []
    for detail in craft.hook_pace_hits(board, hook_pace):
        area = "cut" if detail.startswith("first cut") else "hook"
        out.append({"area": area, "detail": detail})
    if not hook_pace and topic_flags.has(topic_flags.LITERAL_CARDS, topic_id):
        cut = _first_cut_hit(board)
        if cut:
            out.append({"area": "cut", "detail": cut})
    for detail in craft.card_sync_hits(card_sync):
        out.append({"area": "card_sync", "detail": detail})
    if (brand or "") == "os":
        for detail in _os_hook_timing_hits(board, words, script, sources, duration):
            out.append({"area": "hook", "detail": detail})
    return out


def format_reason(hits: list[dict[str, str]]) -> str:
    parts = []
    for h in hits:
        parts.append(f"{h['area']}: {h['detail']}")
    body = "; ".join(parts) if parts else "checks failed"
    return f"{PREFIX} {body}"


def _spoken_words_and_duration(script: str, params: dict) -> tuple[list[dict] | None, float | None]:
    from app.services.engines import worker

    voice = worker._voice(params)
    brand = params.get("brand")
    fmt = params.get("content_format") or "short"
    with tempfile.TemporaryDirectory() as d:
        mp3 = Path(d) / "narration.mp3"
        try:
            words = worker._tts(script, voice, mp3)
        except Exception as e:
            logger.info("gate B preflight: TTS skipped (%s)", e)
            return None, None
        if craft.needs_hook_join(script, words, brand, fmt):
            joined = worker._tts(craft.hook_join_text(script), voice, mp3)
            if joined:
                words = joined
        narr = worker._probe_duration(mp3) or 12.0
        duration = craft.video_duration(narr, words)
    return words, duration


def _compose_board(script: str, subject: str, params: dict, words, duration: float):
    from app.services.engines import worker, storyboard
    from app.services.video_gen import language_from_voice

    aspect = params.get("video_aspect") or "9:16"
    resolution, width, height = worker._ASPECTS.get(aspect, worker._ASPECTS["9:16"])
    brand = params.get("brand")
    html = storyboard.compose(
        subject=subject, script=script, words=words, duration=duration,
        resolution=resolution, width=width, height=height,
        topic_id=params.get("topic_id"),
        content_format=params.get("content_format") or "short",
        language=language_from_voice(params.get("voice_name")),
        llm=worker._llm_compose, brand=brand,
        topic_name=params.get("topic_name"),
        on_screen_allow=params.get("on_screen_allow"),
        on_screen=params.get("on_screen"),
        title=params.get("title"),
    )
    if not html or not worker._looks_valid(html):
        return None
    return craft.beats_from_html(html)


def evaluate(
    *,
    script: str,
    subject: str,
    params: dict,
) -> list[dict[str, str]] | None:
    """Run preflight. None = PASS or skipped (fail-open); else list of hits."""
    topic_id = params.get("topic_id")
    fmt = params.get("content_format") or "short"
    if not applies(topic_id, content_format=fmt):
        return None
    vo = (script or "").strip()
    if not vo:
        return None
    words, duration = _spoken_words_and_duration(vo, params)
    if not words or duration is None:
        return None
    beats = _compose_board(vo, subject, params, words, duration)
    if not beats:
        logger.info("gate B preflight: compose skipped for topic %s", topic_id)
        return None
    brand = params.get("brand")
    sources = params.get("on_screen_allow")
    hook_pace = craft.hook_pace_marker(beats, words, brand, fmt)
    card_sync = craft.card_sync_marker(beats, words, fmt, script=vo, brand=brand)
    hits = preflight_hits(
        beats, brand=brand, topic_id=topic_id, hook_pace=hook_pace,
        card_sync=card_sync, words=words, script=vo, duration=duration,
        sources=sources,
    )
    return hits or None


def stamp_creation_config(raw, *, hits: list[dict[str, str]] | None) -> str:
    cc: dict[str, Any]
    if isinstance(raw, dict):
        cc = dict(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            cc = json.loads(raw)
        except json.JSONDecodeError:
            cc = {}
        if not isinstance(cc, dict):
            cc = {}
    else:
        cc = {}
    cc[CC_KEY] = {"result": "FAIL" if hits else "PASS",
                  "hits": hits or []}
    return json.dumps(cc)
