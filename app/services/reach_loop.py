"""YouTube Reporting API reach sync: thumbnail impressions + impressions CTR.

The Analytics API v2 targeted queries the analytics loop uses cannot return
impressions / impressionClickThroughRate (400 "Unknown identifier", audit
2026-07-09), so VideoMetric.impressions / .ctr sat at 0. The bulk Reporting API
can: report type ``channel_reach_basic_a1`` (dimensions date, channel_id,
video_id; metrics video_thumbnail_impressions, video_thumbnail_impressions_ctr).

Per connected channel, each tick:
  1. ensure ONE reporting job for that report type exists (list → create only
     if missing; idempotent);
  2. list the job's available reports and skip every report id already in the
     ReachReport ledger;
  3. download each new report (oldest createTime first), parse the CSV and
     aggregate per (day, video): impressions summed, clicks = Σ impressions×CTR;
  4. REPLACE that day's VideoReachDaily rows for the channel (a backfill report
     — same day, new id, newer createTime — supersedes the old one; an older
     report never overwrites a newer one), record the report in the ledger;
  5. write the per-video totals (Σ impressions, CTR = Σ clicks / Σ impressions,
     0..1) into the latest measured VideoMetric row — the row every leaderboard /
     by-topic consumer reads. The analytics loop carries the same totals into
     each new daily snapshot (reach_totals), so they don't fall back to 0.

Totals cover the days the job has reports for: YouTube backfills ~30 days
before the job was created, then one report per day from then on (first ones
~24–48h after job creation). Older lifetime impressions are not available.

Auth: uses the channel's existing token (yt-analytics.readonly, already in
CONSENT_SCOPES). A 401/403 (scope missing, API disabled in the Cloud project)
or an unloadable token is logged clearly and the channel is skipped for this
tick — the loop never starts a re-auth, never flips channel status, and never
raises into the scheduler. The Reporting API has its own quota (not the Data
API's 10k/day), so nothing here is billed against the publish budget.
"""

import csv
import io
import logging
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from app.config import settings
from app.db import app_settings, session_scope
from app.models import (Channel, OAuthStatus, ReachReport, Video, VideoMetric,
                        VideoReachDaily)
from app.services import quota, youtube

logger = logging.getLogger("manager.reach")

KIND = "reach"                                     # JobRun.kind for this loop
_COL_DATE = "date"
_COL_VIDEO = "video_id"
_COL_IMPR = "video_thumbnail_impressions"
_COL_CTR = "video_thumbnail_impressions_ctr"
_REQUIRED = (_COL_DATE, _COL_VIDEO, _COL_IMPR, _COL_CTR)
_PACIFIC = "America/Los_Angeles"                   # Reporting API days are Pacific days

_AUTH_HINT = {
    "scope": "the token lacks a Reporting API scope (yt-analytics.readonly); "
             "a manual reconnect with 'Select all' would be needed",
    "api_disabled": "enable the 'YouTube Reporting API' in the channel's Google "
                    "Cloud project",
    "forbidden": "the Reporting API refused this channel's token",
}


# ---- parsing ---------------------------------------------------------------

def _iso_day(raw: str) -> str:
    """Report date → YYYY-MM-DD (the CSV uses YYYYMMDD; accept ISO too)."""
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()


def _num(raw, cast=float):
    try:
        return cast(float(raw)) if raw not in (None, "") else cast(0)
    except (TypeError, ValueError):
        return cast(0)


def parse_reach_csv(data, ctr_unit: str | None = None) -> dict:
    """Parse one channel_reach_basic_a1 CSV.

    Returns ``{"days": {(day, yt_video_id): {"impressions": int, "clicks": float}},
    "rows": n, "ctr_unit": "fraction"|"percent"}``. Rows with an empty video_id
    (channel-level rows) are skipped. Several rows for the same (day, video) are
    summed. The CTR unit is ambiguous in Google's docs ("percentage … calculated
    as clicks divided by impressions"): with ``auto`` a report whose CTR column
    has any value > 1 is read as percent, otherwise as a fraction; the result is
    always a 0..1 fraction (clamped). Raises ValueError on a missing column."""
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    unit = (ctr_unit or settings.reach_ctr_unit or "auto").lower()
    reader = csv.DictReader(io.StringIO(data))
    missing = [c for c in _REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"reach report is missing column(s) {missing} "
                         f"(got {reader.fieldnames})")
    parsed = []
    for row in reader:
        vid = (row.get(_COL_VIDEO) or "").strip()
        if not vid:
            continue
        parsed.append((_iso_day(row[_COL_DATE]), vid,
                       _num(row.get(_COL_IMPR), int), _num(row.get(_COL_CTR), float)))
    if unit not in ("fraction", "percent"):
        unit = "percent" if any(ctr > 1.0 for *_x, ctr in parsed) else "fraction"
    scale = 100.0 if unit == "percent" else 1.0
    days: dict[tuple[str, str], dict] = {}
    for day, vid, impressions, ctr in parsed:
        frac = min(1.0, max(0.0, ctr / scale))
        agg = days.setdefault((day, vid), {"impressions": 0, "clicks": 0.0})
        agg["impressions"] += impressions
        agg["clicks"] += impressions * frac
    return {"days": days, "rows": len(parsed), "ctr_unit": unit}


# ---- storage ---------------------------------------------------------------

def _ts(raw: str | None) -> datetime:
    """RFC 3339 → aware datetime (fractional digits vary; 'Z' suffix)."""
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = raw.strip().replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _report_day(report: dict) -> str:
    """The Pacific day a report covers, from its startTime."""
    start = _ts(report.get("startTime"))
    try:
        from zoneinfo import ZoneInfo
        return start.astimezone(ZoneInfo(_PACIFIC)).date().isoformat()
    except Exception:
        return start.date().isoformat()


def _apply_report(session: Session, channel: Channel, job_id: str, report: dict,
                  parsed: dict) -> set[int]:
    """Store one parsed report and record it in the ledger. Returns the manager
    video ids whose daily rows changed. Never accumulates: the day's rows are
    replaced by the newest report for that day; an older report is ledgered
    and ignored."""
    create_time = report.get("createTime") or ""
    # The CSV's own date column is authoritative; startTime only names the day of
    # an empty (header-only) report, so a tz edge can never wipe a neighbour day.
    days = {d for d, _v in parsed["days"]} or {_report_day(report)}
    existing = session.exec(select(VideoReachDaily).where(
        VideoReachDaily.channel_id == channel.id, VideoReachDaily.day.in_(days))).all()
    touched = {r.video_id for r in existing if r.video_id is not None}
    newest = max((_ts(r.report_create_time) for r in existing), default=None)
    stale = newest is not None and newest > _ts(create_time)
    if stale:
        logger.info("reach: %s report %s (%s) is older than the stored data for "
                    "that day — ledgered, not applied", channel.slug,
                    report.get("id"), create_time)
        touched = set()
    else:
        session.exec(delete(VideoReachDaily).where(
            VideoReachDaily.channel_id == channel.id, VideoReachDaily.day.in_(days)))
        yt_ids = {v for _d, v in parsed["days"]}
        known = {}
        if yt_ids:
            known = {v.yt_video_id: v.id for v in session.exec(select(Video).where(
                Video.channel_id == channel.id, Video.yt_video_id.in_(yt_ids))).all()}
        for (day, yt_id), agg in parsed["days"].items():
            impressions = agg["impressions"]
            clicks = agg["clicks"]
            vid = known.get(yt_id)
            if vid is not None:
                touched.add(vid)
            session.add(VideoReachDaily(
                channel_id=channel.id, video_id=vid, yt_video_id=yt_id, day=day,
                impressions=impressions, clicks=clicks,
                ctr=(clicks / impressions) if impressions else 0.0,
                report_id=report["id"], report_create_time=create_time))
    session.add(ReachReport(
        channel_id=channel.id, report_id=report["id"], job_id=job_id,
        report_date=_report_day(report), create_time=create_time,
        rows=parsed["rows"], videos=len({v for _d, v in parsed["days"]}),
        ctr_unit=parsed.get("ctr_unit")))
    return touched


def reach_totals(session: Session, video_id: int) -> tuple[int, float] | None:
    """(Σ impressions, CTR 0..1) over every stored day for a video, or None if
    the Reporting API has no rows for it. CTR is impression-weighted
    (Σ clicks / Σ impressions), never a mean of daily ratios."""
    rows = session.exec(select(VideoReachDaily).where(
        VideoReachDaily.video_id == video_id)).all()
    if not rows:
        return None
    impressions = sum(r.impressions or 0 for r in rows)
    clicks = sum(r.clicks or 0.0 for r in rows)
    ctr = round(clicks / impressions, 6) if impressions else 0.0
    return impressions, ctr


def write_video_metrics(session: Session, video_ids) -> int:
    """Write reach totals into VideoMetric.impressions / VideoMetric.ctr of each
    video's latest *measured* snapshot (views not NULL — the row
    youtube_admin._latest_metrics serves). Videos without a measured snapshot
    are left to the analytics loop's carry-forward. ``video_ids`` are videos
    that have (or just lost) reach rows. Returns rows updated."""
    updated = 0
    for vid in sorted(v for v in video_ids if v is not None):
        # None here = a backfill removed the video's only rows: no impressions.
        totals = reach_totals(session, vid) or (0, 0.0)
        m = session.exec(
            select(VideoMetric).where(VideoMetric.video_id == vid,
                                      VideoMetric.views.is_not(None))
            .order_by(VideoMetric.captured_at.desc(), VideoMetric.id.desc())
        ).first()
        if m is None:
            continue
        m.impressions, m.ctr = totals
        session.add(m)
        updated += 1
    return updated


# ---- the loop --------------------------------------------------------------

def _skip(session: Session, channel: Channel, reason: str, detail: str) -> dict:
    """Log + JobRun a skipped channel. Never touches channel status/oauth."""
    logger.warning("reach: skipping %s — %s: %s (no re-auth triggered; retrying "
                   "next tick)", channel.slug, reason, detail)
    quota.log(session, kind=KIND, status="error", channel_id=channel.id,
              detail=f"{reason}: {detail}", quota_cost=0)
    session.commit()
    return {"skipped": reason}


def sync_channel(session: Session, channel: Channel) -> dict:
    """One reach pass for a channel. Never raises for auth/scope problems."""
    try:
        reporting = youtube.get_reporting_service(channel.slug)
    except Exception as e:
        return _skip(session, channel, "token unavailable", str(e))
    try:
        job_id, created = youtube.ensure_reach_job(reporting)
        if created:
            logger.info("reach: created Reporting API job %s (%s) for %s — first "
                        "reports in ~24-48h, plus ~30 days of history",
                        job_id, youtube.REACH_REPORT_TYPE, channel.slug)
            quota.log(session, kind=KIND, status="success", channel_id=channel.id,
                      detail=f"created reporting job {job_id} "
                             f"({youtube.REACH_REPORT_TYPE})", quota_cost=0)
            session.commit()
        reports = youtube.list_reports(reporting, job_id)
    except youtube.ReportingNotAuthorized as e:
        session.rollback()
        return _skip(session, channel, f"Reporting API refused ({e.kind}) — "
                     f"{_AUTH_HINT.get(e.kind, '')}", str(e))

    done = set(session.exec(select(ReachReport.report_id).where(
        ReachReport.channel_id == channel.id)).all())
    fresh = sorted((r for r in reports if r.get("id") and r["id"] not in done),
                   key=lambda r: _ts(r.get("createTime")))
    applied = 0
    failed = 0
    touched: set[int] = set()
    for report in fresh:
        try:
            raw = youtube.download_report(reporting, report["downloadUrl"])
            parsed = parse_reach_csv(raw)
            touched |= _apply_report(session, channel, job_id, report, parsed)
            session.commit()
            applied += 1
        except youtube.ReportingNotAuthorized as e:
            session.rollback()
            return _skip(session, channel, f"Reporting API refused ({e.kind}) — "
                         f"{_AUTH_HINT.get(e.kind, '')}", str(e))
        except Exception as e:
            # Not ledgered → retried next tick. One bad report never ends the pass.
            session.rollback()
            failed += 1
            logger.warning("reach: report %s for %s failed (%s) — will retry",
                           report.get("id"), channel.slug, e)
    written = write_video_metrics(session, touched)
    if applied or failed:
        quota.log(session, kind=KIND, status="success" if not failed else "error",
                  channel_id=channel.id, quota_cost=0,
                  detail=f"job {job_id}: {applied} report(s) applied, {failed} failed, "
                         f"{written} VideoMetric row(s) updated")
    session.commit()
    logger.info("reach: %s job %s — %d new report(s), %d applied, %d failed, "
                "%d metric row(s) updated", channel.slug, job_id, len(fresh),
                applied, failed, written)
    return {"job_id": job_id, "created": created, "reports": len(fresh),
            "applied": applied, "failed": failed, "written": written}


def tick() -> None:
    with session_scope() as session:
        if app_settings(session).scheduler_paused:
            return
        channels = session.exec(
            select(Channel).where(Channel.oauth_status == OAuthStatus.CONNECTED)
        ).all()
        for channel in channels:
            if not channel.yt_channel_id:
                continue
            try:
                sync_channel(session, channel)
            except Exception:
                # One channel's unexpected failure must not starve the others.
                session.rollback()
                logger.exception("reach pass failed for %s — continuing", channel.slug)
