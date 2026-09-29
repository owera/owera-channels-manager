"""Operational issue detection — the growth agent's triage signal.

The background loops already self-heal *transient* states (orphaned renders, stuck
publishing, transient render retries, blank-render fallback). This module surfaces the
**terminal / persistent / judgment-needed** class that nothing else handles: videos
stranded in failed/rejected, channels needing OAuth reconnect, quota walls, recurring
error signatures, gate backlogs, and idea-board overflow.

`detect(session)` is read-only. The agent reads it (via GET /api/agent/issues, and folded
into GET /api/agent/state) and remediates by composing the existing REST endpoints —
requeue / retry / reject / delete / PATCH. Each entry carries `suggested_action` and an
`auto` flag (auto-fixable vs. needs-operator escalation).
"""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from pathlib import Path

from app.config import settings
from app.db import app_settings
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus)
from app.services import quota

# Same signatures render_loop treats as transient (retryable) render failures. Kept here
# as the single source the agent reasons over; mirror render_loop._TRANSIENT if changed
# (pinned by verify_render). 2026-08-15: litellm 600s / Anthropic disconnect. 2026-08-31:
# grok -p subprocess timeout (distinct from npx TimeoutExpired). Do not
# add a bare "timed out" — that would retry CLI TimeoutExpired (30 min npx).
# 2026-08-31: edge-tts NoAudioReceived (v1213) — no artifact, retryable.
# 2026-09-03: BlockingIOError Errno 35 on 14 concurrent midnight renders.
TRANSIENT_SIGNATURES = (
    "overloaded_error", "rate_limit_error", "RateLimitError",
    "overloaded", "529", "503",
    "litellm.Timeout", "Connection timed out",
    "InternalServerError", "Server disconnected",
    "grok.Timeout",
    "NoAudioReceived",
    "BlockingIOError",
)

# Tunable thresholds (could move to the Settings table later).
REVIEW_STALE_HOURS = 48          # a video sitting in Review longer than this is a gate backlog
DEAD_VIDEO_AGE_DAYS = 7          # failed/rejected older than this are delete candidates
QUOTA_NEAR_CAP_FRACTION = 0.9    # flag a channel once it has spent this share of the daily cap
MAX_RETRIES = 2                  # render_loop gives up after this many retries
# Runway target = daily_publish_budget + settings.runway_buffer (5 + 1 = 6). The
# buffer lives on app.config.settings so it can be tuned without touching budgets.


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalize to tz-aware UTC (as the loops do)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _age_hours(dt: datetime | None, now: datetime) -> float | None:
    dt = _aware(dt)
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 3600, 1)


def review_age_ref(v: Video) -> datetime | None:
    """When a REVIEW video became reviewable: its last render attempt.

    `updated_at` is NOT bumped by a finished render (SQLModel has no onupdate and
    _finalize doesn't set it), so on 2026-09-27 RR #1357/#1358 read ~32h old —
    their idea-creation time — and missed the 48h stuck_review cut. last_attempt_at
    is stamped at render submit (the last attempt, i.e. within the render timeout
    of the finish); fall back to updated_at/created_at for rows never rendered by
    this loop (legacy / hand-moved)."""
    return v.last_attempt_at or v.updated_at or v.created_at


def _is_transient(error: str | None) -> bool:
    err = error or ""
    return any(sig in err for sig in TRANSIENT_SIGNATURES)


def _signature(detail: str | None) -> str:
    """Collapse a JobRun detail to a recurring-error signature: lowercased, digits
    stripped (so ids/counts don't fragment the group), whitespace-collapsed, truncated."""
    s = (detail or "").lower()
    s = "".join(" " if c.isdigit() else c for c in s)
    s = " ".join(s.split())
    return s[:80]


def _failed_action(v: Video, age_hours: float | None) -> tuple[str, bool]:
    """(suggested_action, auto) for a FAILED video. With full autonomy the agent fixes
    all of these; the action tells it *how*."""
    if v.video_path:
        if v.retry_count >= settings.publish_max_retries:
            # Upload stalled past the timeout on every attempt (see publish_loop recovery
            # cap). Auto-retrying just re-enters the same stall and blocks the channel's
            # drip for another timeout window — escalate for a human decision instead.
            return "retry", False       # exhausted publish retries → needs operator, not auto-retry
        return "retry", True            # render succeeded, failed at publish → re-approve
    if _is_transient(v.error) and v.retry_count < MAX_RETRIES:
        return "requeue", True          # transient render error → re-render
    if age_hours is not None and age_hours > DEAD_VIDEO_AGE_DAYS * 24:
        return "delete", True           # dead: non-transient/exhausted and old → clear board
    return "requeue", True              # recent non-transient, no file → one more render


def _fmt_of(session: Session, v: Video) -> str:
    topic = session.get(Topic, v.topic_id)
    return "long" if topic and topic.content_format == "long" else "short"


def craft_gate_state(creation_config) -> str:
    """The render-time Video Maker gate recorded in creation_config.craft_gate:
    "PASS" / "FAIL", or "absent" (legacy / pre-gate snapshot)."""
    from app.services import craft as _craft
    gate = _craft._as_dict(creation_config).get("craft_gate")
    if isinstance(gate, dict) and gate.get("result") in ("PASS", "FAIL"):
        return gate["result"]
    return "absent"


def review_ready_reason(session: Session, v: Video) -> str | None:
    """None when a REVIEW video is ready for a human/agent decision: it has a
    rendered artifact and clears the craft gate. Else why it is not.

    Same semantics the publish loop enforces (craft.publish_craft_block_reason:
    nonsense/currency title, title lock, Gate A/B/C, script, VO beats) minus the
    ffprobe mute probe (the digest must stay cheap; the publish gate still
    probes). A render-time craft_gate FAIL or a durable craft_review=fail also
    disqualifies."""
    from app.services import craft as _craft
    if v.status != VideoStatus.REVIEW:
        return "not in review"
    if not v.video_path:
        return "no rendered artifact"
    if v.craft_review == _craft.CRAFT_REVIEW_FAIL:
        return v.error or "craft_review=fail"
    if craft_gate_state(v.creation_config) == "FAIL":
        return "creation_config.craft_gate=FAIL"
    return _craft.publish_craft_block_reason(
        title=v.title, script=v.script, creation_config=v.creation_config,
        content_format=_fmt_of(session, v), video_path=v.video_path,
        check_audio=False,
    )


def _ops_tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(settings.ops_tz)
    except Exception:
        return timezone(timedelta(hours=-3))    # Fortaleza has no DST


def review_decide_by(now: datetime) -> datetime:
    """Today's review deadline (settings.review_decide_by, default 10:45) in the
    operator tz, as tz-aware UTC. Items that become ready after it are overdue."""
    try:
        hh, mm = (int(x) for x in settings.review_decide_by.split(":", 1))
    except Exception:
        hh, mm = 10, 45
    local = now.astimezone(_ops_tz())
    return local.replace(hour=hh, minute=mm, second=0, microsecond=0).astimezone(timezone.utc)


def held_drafts(session: Session, channel_id: int | None = None) -> list[tuple[Video, str]]:
    """Drafts on producible topics (active, weight>0) whose subject fails the
    pre-produce subject guard — _auto_produce keeps these as drafts."""
    from app.services.subject_guard import subject_guard_reason
    q = (select(Video).join(Topic, Topic.id == Video.topic_id)
         .where(Video.status == VideoStatus.DRAFT, Topic.active == True,  # noqa: E712
                Topic.weight > 0))
    if channel_id is not None:
        q = q.where(Video.channel_id == channel_id)
    out = []
    for v in session.exec(q.order_by(Video.channel_id, Video.position, Video.id)).all():
        reason = subject_guard_reason(v.subject)
        if reason:
            out.append((v, reason))
    return out


def publish_signals(session: Session, ch: Channel, now: datetime | None = None,
                    review_ready_n: int | None = None) -> dict:
    """Per-channel publish runway signals (shared by the digest and /api/dashboard).

    - under_publish: inside the channel's publish window (its publish_windows /
      publish_tz; none configured = the loop publishes any time), approved +
      published_today < daily_publish_budget while craft-ready REVIEW items wait
      (review_ready > 0). A review row that cannot be approved (no artifact,
      craft fail, currency title) does not page — the suggested action is
      "decide review_ready", and paging needs_operator when that list is empty
      sends the growth agent at items the publish gate will refuse. The
      2026-09-27 RR shape is 4/5 published, approved 0, two craft-PASS reviews
      idle from 13:04 to 22:00.
    - runway_low: approved + published_today (≈ the approved stock the publish
      day started with) < daily_publish_budget + settings.runway_buffer.
    Paused channels, publish budget <= 0, and never-published channels signal nothing.
    """
    from app.services.publish_loop import _window_ok
    now = now or datetime.now(timezone.utc)

    def _count(st):
        return int(session.exec(select(func.count(Video.id)).where(
            Video.channel_id == ch.id, Video.status == st)).one() or 0)

    # Held approved rows never publish — they are not runway (P0 2026-09-29).
    approved = int(session.exec(select(func.count(Video.id)).where(
        Video.channel_id == ch.id, Video.status == VideoStatus.APPROVED,
        Video.held.is_not(True))).one() or 0)
    held = _count(VideoStatus.APPROVED) - approved
    review = _count(VideoStatus.REVIEW)
    published_ever = _count(VideoStatus.PUBLISHED)
    published_today = quota.published_today(session, ch.id)
    budget = ch.daily_publish_budget
    target = budget + max(0, int(settings.runway_buffer))
    runway = approved + published_today
    operating = (not ch.paused) and budget > 0 and published_ever > 0
    in_window = _window_ok(ch, now)
    cu = _aware(ch.cooldown_until)
    cooling = bool(cu and cu > now)
    if review_ready_n is None:
        review_ready_n = sum(
            1 for v in session.exec(select(Video).where(
                Video.channel_id == ch.id, Video.status == VideoStatus.REVIEW)).all()
            if review_ready_reason(session, v) is None)
    return {
        "approved": approved, "held": held, "published_today": published_today,
        "daily_publish_budget": budget, "review_waiting": review,
        "review_ready": review_ready_n, "in_publish_window": in_window,
        "runway": runway, "runway_target": target,
        "under_publish": bool(operating and in_window and not cooling
                              and runway < budget and review_ready_n > 0),
        "runway_low": bool(operating and runway < target),
    }


def detect(session: Session, now: datetime | None = None) -> dict:
    """Classify the system's current operational state into an issues digest.

    `now` is injectable for tests (window / deadline checks); defaults to wall clock."""
    now = now or datetime.now(timezone.utc)
    cfg = app_settings(session)
    channels = session.exec(select(Channel).order_by(Channel.id)).all()
    names = {c.id: c.name for c in channels}

    failed, rejected = [], []
    stuck_rendering, stuck_publishing, stuck_review = [], [], []

    for v in session.exec(select(Video).where(Video.status == VideoStatus.FAILED)).all():
        age = _age_hours(v.updated_at, now)
        action, auto = _failed_action(v, age)
        failed.append({
            "id": v.id, "channel_id": v.channel_id, "topic_id": v.topic_id,
            "subject": v.subject, "error": v.error, "retry_count": v.retry_count,
            "has_file": bool(v.video_path), "transient": _is_transient(v.error),
            "age_hours": age, "suggested_action": action, "auto": auto,
        })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.REJECTED)).all():
        age = _age_hours(v.updated_at, now)
        old = age is not None and age > DEAD_VIDEO_AGE_DAYS * 24
        rejected.append({
            "id": v.id, "channel_id": v.channel_id, "subject": v.subject,
            "reason": v.rejected_reason, "age_hours": age,
            "suggested_action": "delete" if old else "leave", "auto": True,
        })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all():
        started = _aware(v.last_attempt_at or v.updated_at)
        if started and (now - started).total_seconds() > settings.render_timeout_seconds:
            stuck_rendering.append({
                "id": v.id, "channel_id": v.channel_id, "subject": v.subject,
                "age_hours": _age_hours(started, now), "render_progress": v.render_progress,
                "suggested_action": "requeue", "auto": True,
            })

    for v in session.exec(select(Video).where(Video.status == VideoStatus.PUBLISHING)).all():
        started = _aware(v.last_attempt_at or v.updated_at)
        if started and (now - started).total_seconds() > settings.publish_timeout_seconds:
            stuck_publishing.append({
                "id": v.id, "channel_id": v.channel_id, "subject": v.subject,
                "age_hours": _age_hours(started, now),
                "suggested_action": "retry", "auto": True,
            })

    # Review age = time since the last render attempt (review_age_ref), not
    # updated_at — see review_age_ref for the 2026-09-27 RR miss.
    # review_ready has NO age filter: every rendered, craft-clear REVIEW item is
    # listed for the morning pass (growth agent, 09:00 local) with a decide_by
    # deadline of settings.review_decide_by (10:45) so approved stock is in place
    # before the 11:00 publish window. The app never auto-approves here
    # (skip_gate stays the only auto-approve path); this is the decision list.
    review_ready = []
    ready_by_channel: dict[int, int] = {}
    decide_by = review_decide_by(now)
    for v in session.exec(select(Video).where(Video.status == VideoStatus.REVIEW)
                          .order_by(Video.channel_id, Video.id)).all():
        age = _age_hours(review_age_ref(v), now)
        if age is not None and age > REVIEW_STALE_HOURS:
            stuck_review.append({
                "id": v.id, "channel_id": v.channel_id, "topic_id": v.topic_id,
                "subject": v.subject, "age_hours": age,
                "suggested_action": "approve or reject", "auto": True,
            })
        if review_ready_reason(session, v) is None:
            ready_by_channel[v.channel_id] = ready_by_channel.get(v.channel_id, 0) + 1
            review_ready.append({
                "id": v.id, "channel_id": v.channel_id, "topic_id": v.topic_id,
                "subject": v.subject, "title": v.title, "age_hours": age,
                "craft_gate": craft_gate_state(v.creation_config),
                "craft_review": v.craft_review,
                "decide_by": decide_by.isoformat(),
                "overdue": now >= decide_by,
                "suggested_action": ("approve (craft PASS — spot-check title/VO) or reject; "
                                     f"decide by {settings.review_decide_by} "
                                     f"{settings.ops_tz}, no age filter"),
                "auto": True,
            })

    # Channel health — OAuth (escalate) and quota cooldown / spend (monitor).
    oauth, cooldown, quota_walls = [], [], []
    for ch in channels:
        if ch.oauth_status != OAuthStatus.CONNECTED:
            oauth.append({
                "channel_id": ch.id, "name": ch.name, "status": ch.oauth_status,
                "error": ch.oauth_error,
                "suggested_action": "operator must reconnect OAuth", "auto": False,
            })
        cu = _aware(ch.cooldown_until)
        if cu and cu > now:
            cooldown.append({
                "channel_id": ch.id, "name": ch.name, "until": cu.isoformat(),
                "suggested_action": "self-resets at reset; monitor", "auto": False,
            })
        hit = quota.daily_limit_hit(session, ch.id)
        spent = quota.quota_spent_today(session, ch.id)
        if hit or spent >= settings.youtube_daily_quota_cap * QUOTA_NEAR_CAP_FRACTION:
            quota_walls.append({
                "channel_id": ch.id, "name": ch.name, "daily_limit_hit": hit,
                "quota_spent_today": spent, "quota_cap": settings.youtube_daily_quota_cap,
                "suggested_action": "lower publish budget / widen drip, or monitor",
                "auto": False,
            })

    # Recurring error signatures in the last 24h → root-cause code-fix candidates.
    since = now - timedelta(hours=24)
    groups: dict[tuple[str, str], dict] = {}
    for r in session.exec(
        select(JobRun).where(JobRun.status == "error", JobRun.created_at >= since)
        .order_by(JobRun.created_at.desc())
    ).all():
        key = (r.kind, _signature(r.detail))
        g = groups.setdefault(key, {"kind": r.kind, "signature": key[1],
                                    "count": 0, "last_detail": r.detail})
        g["count"] += 1
    error_runs_24h = sorted(groups.values(), key=lambda g: g["count"], reverse=True)

    # Idea-board overflow — topics over the autogen ceiling (ties to the autofill cap).
    # Parked (weight<=0) topics are skipped, same gate as autofill/auto-produce: a
    # `t.weight or 1` ceiling would treat 0 as 1 and page the growth agent to
    # "produce or trim drafts" on a topic it just parked.
    ceiling_base = max(cfg.topic_autogen_min_pending, cfg.topic_autogen_target)
    board_overflow = []
    for t in session.exec(select(Topic).where(Topic.active == True)).all():  # noqa: E712
        weight = t.weight if t.weight is not None else 1
        if weight <= 0:
            continue
        pending = session.exec(
            select(func.count(Video.id)).where(
                Video.topic_id == t.id,
                Video.status.in_([VideoStatus.DRAFT, VideoStatus.QUEUED]))
        ).one()
        ceiling = ceiling_base * min(weight, 4)
        if pending > ceiling:
            board_overflow.append({
                "topic_id": t.id, "channel_id": t.channel_id, "name": t.name,
                "pending": pending, "ceiling": ceiling,
                "suggested_action": "produce or trim drafts", "auto": True,
            })

    # BGM pool health — flag when the music pool is below the minimum threshold.
    # The scheduled replenish job fires daily; if the agent sees this it can trigger
    # an on-demand top-up via POST /api/music/generate.
    bgm_pool_issues = []
    try:
        from app.services import music_gen as _mg
        bgm_dir = Path(settings.bgm_dir)
        pool_count = _mg.pool_count(bgm_dir)
        if pool_count < settings.bgm_pool_min:
            need = settings.bgm_pool_target - pool_count
            bgm_pool_issues.append({
                "count": pool_count,
                "min": settings.bgm_pool_min,
                "target": settings.bgm_pool_target,
                "need": need,
                "suggested_action": f"POST /api/music/generate {{\"count\": {need}}}",
                "auto": True,
            })
    except Exception:
        pass

    # Board inventory — days of DRAFT+QUEUED work per channel vs the horizon cap.
    # Informational: not counted in issue totals, but visible to the growth agent.
    # The draft/queued split matters: `pending` (and therefore `at_capacity`) counts
    # BOTH, so a channel whose bench is all unproduced DRAFTs reads "at capacity" while
    # the render loop — which only ever consumes QUEUED — has nothing to do.
    # Format split (long/short) on draft/queued/approved lets the agent enforce the
    # standing 1 long + 4 shorts daily mix without a DB dive (growth agent 2026-08-10).
    def _fmt_counts(channel_id: int, status: VideoStatus) -> dict:
        longs = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == channel_id, Video.status == status,
                   Video.held.is_not(True),
                   Topic.content_format == "long")
        ).one()
        shorts = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == channel_id, Video.status == status,
                   Video.held.is_not(True),
                   Topic.content_format != "long")
        ).one()
        return {"long": int(longs or 0), "short": int(shorts or 0)}

    board_inventory = []
    for ch in channels:
        daily_cap = ch.daily_render_budget
        if daily_cap > 0:
            drafts = session.exec(
                select(func.count(Video.id)).where(
                    Video.channel_id == ch.id, Video.status == VideoStatus.DRAFT)
            ).one()
            queued = session.exec(
                select(func.count(Video.id)).where(
                    Video.channel_id == ch.id, Video.status == VideoStatus.QUEUED)
            ).one()
            pending = drafts + queued
            days = round(pending / daily_cap, 1)
            board_inventory.append({
                "channel_id": ch.id, "name": ch.name,
                "pending": pending, "drafts": drafts, "queued": queued,
                "daily_render_budget": daily_cap,
                "days_of_inventory": days,
                "board_horizon_days": cfg.board_horizon_days,
                "at_capacity": days >= cfg.board_horizon_days,
                "by_format": {
                    "draft": _fmt_counts(ch.id, VideoStatus.DRAFT),
                    "queued": _fmt_counts(ch.id, VideoStatus.QUEUED),
                    "approved": _fmt_counts(ch.id, VideoStatus.APPROVED),
                },
            })

    # Pipeline starvation — the failure this digest was blind to for 5 days (07-18→07-23).
    # Back then nothing promoted DRAFT → QUEUED, so when the agent stopped producing the
    # render loop starved behind a full idea bench. render_loop._auto_produce now owns
    # that transition (every tick it queues drafts from active weight>0 topics into free
    # render capacity), so such drafts are self-healing stock, not starvation — a
    # mid-cycle digest read must not call the pipeline starved when the next auto-produce
    # pass will refill it (07-27→30: publish_starved fired every noon on that artifact).
    # What still genuinely needs the agent:
    #   render_starved  — render capacity free + drafts waiting, but auto-produce can't
    #                     touch any of them (all on weight-0/inactive/missing topics)
    #   publish_starved — even counting in-flight work and one render-budget-day of
    #                     producible drafts, the APPROVED buffer misses a publish day
    pipeline_starved = []
    for ch in channels:
        if ch.paused:
            continue
        counts = {}
        for st in (VideoStatus.DRAFT, VideoStatus.QUEUED, VideoStatus.RENDERING,
                   VideoStatus.APPROVED, VideoStatus.PUBLISHED):
            q = select(func.count(Video.id)).where(
                Video.channel_id == ch.id, Video.status == st)
            if st == VideoStatus.APPROVED:
                q = q.where(Video.held.is_not(True))   # held never publishes
            counts[st] = session.exec(q).one()
        drafts = counts[VideoStatus.DRAFT]
        active = counts[VideoStatus.QUEUED] + counts[VideoStatus.RENDERING]
        ready = counts[VideoStatus.APPROVED]
        rendered_today = quota.rendered_today(session, ch.id)
        render_headroom = ch.daily_render_budget - rendered_today
        # Drafts _auto_produce is allowed to queue — the same filter it applies
        # (inner join: a draft whose topic row is missing is equally untouchable).
        producible = session.exec(
            select(func.count(Video.id)).join(Topic, Topic.id == Video.topic_id)
            .where(Video.channel_id == ch.id, Video.status == VideoStatus.DRAFT,
                   Topic.active == True, Topic.weight > 0)  # noqa: E712
        ).one()
        # ...minus drafts the subject guard holds (auto-produce skips those too).
        producible = max(0, producible - len(held_drafts(session, ch.id)))

        if render_headroom > 0 and drafts > 0 and active == 0 and producible == 0:
            pipeline_starved.append({
                "channel_id": ch.id, "name": ch.name, "kind": "render_starved",
                "drafts": drafts, "producible": producible,
                "queued": counts[VideoStatus.QUEUED],
                "rendering": counts[VideoStatus.RENDERING],
                "rendered_today": rendered_today,
                "daily_render_budget": ch.daily_render_budget,
                "detail": (f"{drafts} draft(s) waiting and {render_headroom} render "
                           f"slot(s) free, but none is on an active weight>0 topic "
                           f"with a guard-valid subject — "
                           f"auto-produce will never queue them, so production has stalled"),
                "suggested_action": ("unpark a topic (PATCH weight) or generate ideas on "
                                     "an active one, then produce"),
                "auto": True,
            })

        # Only an *operating* channel can starve. A channel that has never published is
        # not running dry, it simply hasn't started — flagging it would fire forever on
        # every newly-added channel. Project the buffer forward: in-flight work plus at
        # most one render-budget-day of producible drafts becomes APPROVED stock before
        # the next publish day.
        projected = ready + active + min(producible, ch.daily_render_budget)
        if (ch.daily_publish_budget > 0 and projected < ch.daily_publish_budget
                and counts[VideoStatus.PUBLISHED] > 0):
            pipeline_starved.append({
                "channel_id": ch.id, "name": ch.name, "kind": "publish_starved",
                "approved": ready, "in_flight": active, "producible": producible,
                "projected": projected,
                "daily_publish_budget": ch.daily_publish_budget,
                "days_of_publish_inventory": round(ready / ch.daily_publish_budget, 1),
                "detail": (f"{ready} approved + {active} in flight + "
                           f"{min(producible, ch.daily_render_budget)} producible draft(s) "
                           f"still misses the {ch.daily_publish_budget}/day publish budget — "
                           f"this channel will miss publishes even after auto-produce runs"),
                "suggested_action": "generate ideas / unpark topics so the bench can refill",
                "auto": True,
            })

    # Publish runway (2026-09-28 RR refill): escalate an under-publishing channel
    # while craft-ready review items wait, and flag a thin approved runway.
    # Unready review rows (no artifact / craft fail) do not page — there is
    # nothing to approve, and the page is needs_operator.
    under_publish, runway_low = [], []
    for ch in channels:
        sig = publish_signals(session, ch, now, ready_by_channel.get(ch.id, 0))
        base = {"channel_id": ch.id, "name": ch.name, **sig}
        if sig["under_publish"]:
            under_publish.append({
                **base,
                "detail": (f"{sig['approved']} approved + {sig['published_today']} published "
                           f"today < {sig['daily_publish_budget']}/day while "
                           f"{sig['review_waiting']} review item(s) wait "
                           f"({sig['review_ready']} craft-ready) inside the publish window"),
                "suggested_action": ("decide review_ready now (approve craft-PASS / reject) "
                                     "so the drip has stock before the window closes"),
                "auto": False,
            })
        if sig["runway_low"]:
            runway_low.append({
                **base,
                "detail": (f"approved runway {sig['runway']} (approved + published today) "
                           f"< target {sig['runway_target']} "
                           f"(daily_publish_budget + {settings.runway_buffer} buffer)"),
                "suggested_action": ("decide review_ready by the deadline; keep drafts "
                                     "subject-valid so tonight's auto-produce refills"),
                "auto": True,
            })

    # Drafts the pre-produce subject guard holds (stripped leading number / bare
    # unit / currency in subject) — _auto_produce skips them; fix the subject
    # (PATCH) or reject so the next valid draft keeps getting the slots.
    subject_held = [{
        "id": v.id, "channel_id": v.channel_id, "topic_id": v.topic_id,
        "subject": v.subject, "detail": reason,
        "suggested_action": "PATCH subject (restore number/stake, no currency) or reject",
        "auto": True,
    } for v, reason in held_drafts(session)]

    buckets = {
        "failed": failed, "rejected": rejected,
        "stuck_rendering": stuck_rendering, "stuck_publishing": stuck_publishing,
        "stuck_review": stuck_review, "review_ready": review_ready,
        "oauth": oauth, "cooldown": cooldown,
        "quota": quota_walls, "error_runs_24h": error_runs_24h,
        "board_overflow": board_overflow, "bgm_pool_low": bgm_pool_issues,
        "pipeline_starved": pipeline_starved,
        "under_publish": under_publish, "runway_low": runway_low,
        "subject_held": subject_held,
    }
    # board_inventory is informational — excluded from issue counts.
    extra = {"board_inventory": board_inventory}

    from app.services import craft as _craft
    title_pattern_blocked = []
    for v in session.exec(select(Video).where(
            Video.status.in_([VideoStatus.APPROVED, VideoStatus.REVIEW]))).all():
        topic = session.get(Topic, v.topic_id)
        fmt = "long" if topic and topic.content_format == "long" else "short"
        reason = _craft.title_gate_reason(v.title, fmt)
        currency = None if reason else _craft.nonsense_title_reason(v.title)
        if currency == _craft.CURRENCY_TITLE_REASON:
            # 2026-09-28: the publish gate now rejects these at upload time —
            # surface them while there is still time to retitle.
            title_pattern_blocked.append({
                "id": v.id, "channel_id": v.channel_id, "title": v.title,
                "status": v.status,
                "suggested_action": ("retitle without the currency value (PATCH title) "
                                     "or reject — the publish gate will reject it as is"),
                "auto": False,
                "detail": currency,
            })
        elif reason:
            title_pattern_blocked.append({
                "id": v.id, "channel_id": v.channel_id, "title": v.title,
                "status": v.status,
                "suggested_action": "reject (pré-pattern leftover — do not mass-retitle)",
                "auto": False,
                "detail": reason,
            })
    extra["title_pattern_blocked"] = title_pattern_blocked
    craft_gate_blocked = []
    for v in session.exec(select(Video).where(
            Video.status.in_([VideoStatus.APPROVED, VideoStatus.REVIEW]))).all():
        topic = session.get(Topic, v.topic_id)
        fmt = "long" if topic and topic.content_format == "long" else "short"
        reason = _craft.video_maker_gate_reason(v.creation_config, fmt)
        if reason:
            craft_gate_blocked.append({
                "id": v.id, "channel_id": v.channel_id, "title": v.title,
                "status": v.status,
                "suggested_action": "requeue (Video Maker craft gate — fix object 0–3s / beat ≤ mid-hold / statement≤1)",
                "auto": False,
                "detail": reason,
            })
    extra["craft_gate_blocked"] = craft_gate_blocked
    # needs_operator = anything explicitly non-auto (OAuth, quota walls, cooldown).
    needs_operator = sum(
        1 for b in buckets.values() for item in b
        if isinstance(item, dict) and item.get("auto") is False
    )
    total = sum(len(b) for b in buckets.values())
    return {
        "now": now.isoformat(),
        **buckets,
        **extra,
        "summary": {"total_issues": total, "needs_operator": needs_operator,
                    "clean": total == 0},
        "channel_names": names,
    }
