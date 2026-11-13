"""Per-channel auto-produce draft policy (RR 2026-11-13).

RR auto-produce spent 5 render slots on Sept/Oct drafts (#1316, #1431,
#1433, #1434, #1435). All failed Gate B "RR hook pace": the hook claim is the
draft's title head (craft.hook_claim_source), and those heads were written
before the draft-time hook check (#70, 2026-10-06) and the RR sentence-pace
rules (#202 / 8ea3a21, 2026-11-11) — 9–11 words, or a number the voice reads
out long ("num_ctx 2048"). Nothing in compose rewrites a draft title, so such a
draft can never pass; rendering it only burns the slot.

Policy (``settings.auto_produce_draft_policy``, keyed like
``settings.sentence_pace``: "channel:<id>", channel slug, brand): before
``_auto_produce`` promotes a DRAFT on a configured channel it checks

* ``max_age_days`` — drafts older than this are stale;
* ``not_before`` — ISO UTC instant; drafts created earlier are stale;
* ``hook_preflight`` — shorts must pass the existing RR draft-time hook rule
  (``video_gen.rr_hook_reason``: title head ≤ 8 words and, when
  ``measure_spoken``, spoken by 2.8s with the channel voice via edge-tts;
  a TTS outage is fail-open, same as at draft time).

A draft that misses goes to REJECTED with ``rejected_reason`` starting
``SKIP_PREFIX`` plus the reason, and one ``produce``/``skipped`` JobRun — no
render slot used, and it no longer counts as pending, so the normal autofill
refill (which already enforces hook pace at birth) tops the topic up with fresh
ideas. Operator-provided scripts are exempt (their own pre-render check runs at
submit). A passing measurement is cached on ``creation_config`` so the 15s tick
does not re-measure. Channels without an entry are unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("manager.autoproduce_policy")

SKIP_PREFIX = "auto-produce skipped (stale draft):"
CC_KEY = "autoproduce_preflight"


def policy_for(channel) -> dict | None:
    """The channel's policy dict, or None (auto-produce unchanged)."""
    try:
        from app.config import settings
        cfg = getattr(settings, "auto_produce_draft_policy", None) or {}
    except Exception:  # pragma: no cover
        cfg = {}
    if not isinstance(cfg, dict) or channel is None:
        return None
    from app.services.engines import theme
    cid = getattr(channel, "id", None)
    slug = getattr(channel, "slug", None)
    keys = []
    if cid is not None:
        keys.append(f"channel:{cid}")
    if slug:
        keys.append(str(slug))
    brand = theme.infer_brand(cid, slug, getattr(channel, "name", None))
    if brand:
        keys.append(str(brand))
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, dict):
            return {**v, "key": k}
    return None


def _aware(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cc(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return v if isinstance(v, dict) else {}
    return {}


def claim_of(video) -> str:
    from app.services import craft
    return craft.title_head(craft.hook_claim_source(video.title, video.subject))


def stale_reason(video, topic, policy: dict, *, now=None, voice=None,
                 measure=None) -> str | None:
    """Why ``video`` (a DRAFT) must not take a render slot, or None."""
    from app.services import craft
    if craft.is_provided_script(video.creation_config) and (video.script or "").strip():
        return None
    now = now or datetime.now(timezone.utc)
    created = _aware(video.created_at)
    days = policy.get("max_age_days")
    if created is not None and isinstance(days, (int, float)) and days > 0:
        age = now - created
        if age > timedelta(days=float(days)):
            return (f"draft is {age.days}d old (max {float(days):g}d) — "
                    f"written before the current hook rules")
    nb = _aware(policy.get("not_before"))
    if created is not None and nb is not None and created < nb:
        return (f"draft created {created:%Y-%m-%d} before {nb:%Y-%m-%d} "
                f"(current hook rules)")
    if policy.get("hook_preflight") and (getattr(topic, "content_format", None) or "short") != "long":
        cc = _cc(video.creation_config)
        claim = claim_of(video)
        prior = cc.get(CC_KEY)
        if isinstance(prior, dict) and prior.get("result") == "PASS" and prior.get("claim") == claim:
            return None
        from app.services import video_gen
        r = video_gen.rr_hook_reason(
            claim, voice if policy.get("measure_spoken", True) else None, measure)
        if r:
            return f"hook preflight: claim {claim!r} {r}"
        cc[CC_KEY] = {"result": "PASS", "claim": claim,
                      "measured": bool(voice and policy.get("measure_spoken", True))}
        video.creation_config = json.dumps(cc)
    return None


def skip(session, video, ch, reason: str) -> None:
    """Park ``video`` out of the draft bench with a clear reason (no slot used)."""
    from app.models import VideoStatus
    from app.services import quota
    video.status = VideoStatus.REJECTED
    video.rejected_reason = f"{SKIP_PREFIX} {reason}"[:500]
    session.add(video)
    quota.log(session, kind="produce", status="skipped", video_id=video.id,
              channel_id=ch.id, detail=video.rejected_reason)
    logger.info("auto-produce skipped draft %s on channel %s: %s", video.id, ch.slug, reason)


def filter_rows(session, ch, rows, *, now=None, measure=None, apply=True):
    """Apply the channel policy to ``(Video, Topic)`` draft rows.

    Returns ``(kept_rows, skipped)`` where ``skipped`` is a list of
    ``(video, reason)``. ``apply=False`` is the dry-run: nothing is written."""
    policy = policy_for(ch)
    if not policy:
        return rows, []
    voice = None
    if policy.get("hook_preflight") and policy.get("measure_spoken", True):
        from app.services import video_gen
        voice = video_gen.channel_voice(session, ch.id)
    kept, skipped = [], []
    for v, t in rows:
        before = v.creation_config
        try:
            r = stale_reason(v, t, policy, now=now, voice=voice, measure=measure)
        except Exception as e:  # never block the loop on a policy bug
            logger.info("auto-produce policy skipped for video %s: %s", v.id, e)
            r = None
        if r:
            skipped.append((v, r))
            if apply:
                skip(session, v, ch, r)
            continue
        if not apply:
            v.creation_config = before
        elif v.creation_config != before:
            session.add(v)
        kept.append((v, t))
    return kept, skipped
