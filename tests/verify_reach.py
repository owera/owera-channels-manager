"""YouTube Reporting API reach sync — thumbnail impressions + CTR (P1 2026-10-06).

Run: PYTHONPATH=. .venv/bin/python tests/verify_reach.py

Before: VideoMetric.impressions / .ctr were always 0 — the Analytics API v2
targeted queries reject impressions / impressionClickThroughRate (400 "Unknown
identifier"), so nothing ever measured them. reach_loop now reads the bulk
Reporting API report ``channel_reach_basic_a1`` and writes them there.

Pins:
- youtube helpers over a mocked HTTP layer (HttpMockSequence, real discovery
  doc): ensure_reach_job is idempotent (list → create only when missing, with
  reportTypeId=channel_reach_basic_a1); 403 scope / API-disabled map to
  ReportingNotAuthorized(kind); 5xx retried; reports paginated; gzip download
- parse_reach_csv: YYYYMMDD dates, per (day, video) aggregation, channel-level
  rows skipped, CTR unit (fraction / percent / auto) → 0..1, missing column
- sync: job created once per channel (OS + RR), reports applied, totals written
  to the latest MEASURED VideoMetric row's impressions + ctr (older rows and
  NULL "not reported yet" rows untouched); CTR = Σclicks/Σimpressions (0..1),
  not a mean of daily ratios; videos unknown to the manager stored, not fatal
- re-run idempotent (ledger: no re-download, no double count); backfill report
  replaces the day; an older report never overwrites a newer one; a failed
  download is retried next tick, the rest still applied
- scope insufficient / API disabled / token unavailable: clear WARNING, JobRun
  error, channel skipped — no re-auth, no status flip, no notify, no raise;
  tick isolates channels and honors scheduler_paused
- analytics_loop carries the reach totals into each new daily snapshot
- the two new tables are created by init_db (create_all), idempotently
- leaderboard / by-topic endpoints now serve the values

In-memory SQLite, mocked APIs, no network, never the real manager.db or any
credential. Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import contextlib
import gzip
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as app_db
from app.config import settings
from app.models import (Channel, JobRun, OAuthStatus, ReachReport, Settings, Topic,
                        Video, VideoMetric, VideoReachDaily, VideoStatus)
from app.routers import youtube_admin
from app.services import analytics_loop, notify, reach_loop, youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ok(Path(reach_loop.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "reach_loop loaded from this tree")
youtube._sleep = lambda s: None                    # no real backoff waits

# ---------------------------------------------------------------------------
print("youtube Reporting helpers (mocked HTTP, real discovery doc)")


def svc(responses):
    http = HttpMockSequence(responses)
    return build("youtubereporting", "v1", http=http, static_discovery=True), http


def j(obj) -> str:
    return json.dumps(obj)


r, http = svc([({"status": "200"}, j({"jobs": [
    {"id": "other", "reportTypeId": "channel_basic_a3"},
    {"id": "job-os", "reportTypeId": "channel_reach_basic_a1"}]}))])
ok(youtube.ensure_reach_job(r) == ("job-os", False) and len(http.request_sequence) == 1
   and http.request_sequence[0][1] == "GET",
   "existing reach job found → reused, a single GET, no create")

r, http = svc([({"status": "200"}, j({"jobs": [{"id": "x", "reportTypeId": "channel_basic_a3"}],
                                       "nextPageToken": "p2"})),
               ({"status": "200"}, j({})),
               ({"status": "200"}, j({"id": "job-new", "reportTypeId": "channel_reach_basic_a1"}))])
got = youtube.ensure_reach_job(r)
methods = [m for _u, m, _b, _h in http.request_sequence]
body = json.loads(http.request_sequence[2][2])
ok(got == ("job-new", True) and methods == ["GET", "GET", "POST"],
   "no reach job on any page → list (paginated) then exactly one create")
ok(body["reportTypeId"] == "channel_reach_basic_a1" and body.get("name"),
   "create body asks for channel_reach_basic_a1 with a name")
ok("pageToken=p2" in http.request_sequence[1][0], "jobs.list follows nextPageToken")

SCOPE_403 = j({"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                         "status": "PERMISSION_DENIED", "errors": [
                             {"reason": "insufficientPermissions", "domain": "global"}]}})
DISABLED_403 = j({"error": {"code": 403, "message": "YouTube Reporting API has not been used in "
                            "project 1 before or it is disabled.", "errors": [
                                {"reason": "accessNotConfigured"}]}})
for payload, kind in ((SCOPE_403, "scope"), (DISABLED_403, "api_disabled"),
                      (j({"error": {"code": 403, "message": "Forbidden"}}), "forbidden")):
    r, _h = svc([({"status": "403"}, payload)])
    try:
        youtube.ensure_reach_job(r)
        ok(False, f"403 {kind} must raise")
    except youtube.ReportingNotAuthorized as e:
        ok(e.kind == kind and "403" in str(e), f"403 → ReportingNotAuthorized(kind={kind!r})")

r, http = svc([({"status": "503"}, j({"error": {"code": 503, "message": "backend"}})),
               ({"status": "200"}, j({"jobs": [{"id": "j1", "reportTypeId": "channel_reach_basic_a1"}]}))])
ok(youtube.ensure_reach_job(r) == ("j1", False) and len(http.request_sequence) == 2,
   "5xx retried with backoff, then succeeds")

r, http = svc([({"status": "200"}, j({"reports": [{"id": "a"}], "nextPageToken": "n"})),
               ({"status": "200"}, j({"reports": [{"id": "b"}]}))])
ok([x["id"] for x in youtube.list_reports(r, "j1")] == ["a", "b"]
   and "/jobs/j1/reports" in http.request_sequence[0][0],
   "list_reports paginates jobs/{id}/reports")

CSV_PLAIN = b"date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
gz = gzip.compress(CSV_PLAIN)
r, http = svc([({"status": "200", "content-length": str(len(gz))}, gz)])
ok(youtube.download_report(r, "https://youtubereporting.googleapis.com/v1/media/CHANNEL/x?alt=media")
   == CSV_PLAIN and http.request_sequence[0][0].startswith(
       "https://youtubereporting.googleapis.com/v1/media/CHANNEL/x"),
   "download_report fetches the downloadUrl and gunzips")
r, _h = svc([({"status": "200", "content-length": str(len(CSV_PLAIN))}, CSV_PLAIN)])
ok(youtube.download_report(r, "https://youtubereporting.googleapis.com/v1/media/y") == CSV_PLAIN,
   "an uncompressed report passes through unchanged")
r, _h = svc([({"status": "403"}, SCOPE_403)])
try:
    youtube.download_report(r, "https://youtubereporting.googleapis.com/v1/media/z")
    ok(False, "403 download must raise")
except youtube.ReportingNotAuthorized as e:
    ok(e.kind == "scope", "403 on download → ReportingNotAuthorized(scope)")

# ---------------------------------------------------------------------------
print("parse_reach_csv")
HDR = "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
p = reach_loop.parse_reach_csv(
    HDR + "20261001,UC1,vA,1000,0.05\n20261001,UC1,vA,1000,0.15\n"
          "20261001,UC1,,500,0.5\n20261001,UC1,vB,0,0\n", "auto")
ok(set(p["days"]) == {("2026-10-01", "vA"), ("2026-10-01", "vB")},
   "YYYYMMDD → ISO day; channel-level rows (empty video_id) skipped")
a = p["days"][("2026-10-01", "vA")]
ok(a["impressions"] == 2000 and abs(a["clicks"] - 200.0) < 1e-9,
   "duplicate (day, video) rows summed; clicks = Σ impressions × CTR (50 + 150)")
ok(p["ctr_unit"] == "fraction" and p["rows"] == 3, "auto: all CTR ≤ 1 → fraction; 3 video rows")
p = reach_loop.parse_reach_csv(HDR + "2026-10-01,UC1,vA,1000,5.0\n2026-10-01,UC1,vB,200,0.5\n", "auto")
ok(p["ctr_unit"] == "percent" and abs(p["days"][("2026-10-01", "vA")]["clicks"] - 50) < 1e-9
   and abs(p["days"][("2026-10-01", "vB")]["clicks"] - 1.0) < 1e-9,
   "auto: any CTR > 1 → whole report read as percent (5.0 → 5%, 0.5 → 0.5%)")
p = reach_loop.parse_reach_csv(HDR + "20261001,UC1,vA,1000,0.8\n", "percent")
ok(abs(p["days"][("2026-10-01", "vA")]["clicks"] - 8.0) < 1e-9, "pinned percent: 0.8 → 0.8%")
p = reach_loop.parse_reach_csv(HDR + "20261001,UC1,vA,1000,0.8\n", "fraction")
ok(abs(p["days"][("2026-10-01", "vA")]["clicks"] - 800.0) < 1e-9, "pinned fraction: 0.8 → 80%")
p = reach_loop.parse_reach_csv(("\ufeff" + HDR).encode() + b"20261001,UC1,vA,10,1.7\n", "fraction")
ok(p["days"][("2026-10-01", "vA")]["clicks"] == 10.0, "BOM tolerated; CTR clamped to ≤ 1")
ok(reach_loop.parse_reach_csv(HDR, "auto")["days"] == {}, "header-only report (no data day) → empty")
try:
    reach_loop.parse_reach_csv("date,video_id,views\n20261001,vA,3\n", "auto")
    ok(False, "missing column must raise")
except ValueError as e:
    ok("video_thumbnail_impressions" in str(e), "missing reach column → ValueError naming it")

# ---------------------------------------------------------------------------
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
s = Session(engine)
s.add(Settings(id=1))
os_ch = Channel(slug="owera-software", name="Owera Software", oauth_status=OAuthStatus.CONNECTED,
                yt_channel_id="UCSH53DmossqLTUZdh2PE1Hg")
rr_ch = Channel(slug="rodrigo-recio", name="Rodrigo Recio", oauth_status=OAuthStatus.CONNECTED,
                yt_channel_id="UCGHkzi8k9Oma6IiMjQuDnbA")
s.add(os_ch)
s.add(rr_ch)
s.commit()
ok((os_ch.id, rr_ch.id) == (1, 2), "OS is channel 1, RR is channel 2 (as in the manager)")
s.add(Topic(channel_id=1, name="t1"))
s.add(Topic(channel_id=2, name="t2"))
s.commit()
now = datetime.now(timezone.utc)


def video(ch_id, yt, topic):
    v = Video(channel_id=ch_id, topic_id=topic, subject=yt, status=VideoStatus.PUBLISHED,
              yt_video_id=yt, published_at=now - timedelta(days=10))
    s.add(v)
    s.commit()
    return v


vA, vB = video(1, "vidA", 1), video(1, "vidB", 1)
vR = video(2, "vidR", 2)


def metric(v, views, minutes_ago, **kw):
    m = VideoMetric(video_id=v.id, channel_id=v.channel_id, views=views,
                    captured_at=now - timedelta(minutes=minutes_ago), **kw)
    s.add(m)
    s.commit()
    return m


mA_old = metric(vA, 5, 2000)
mA_new = metric(vA, 9, 60)
mA_null = VideoMetric(video_id=vA.id, channel_id=1, views=None, impressions=None, ctr=None,
                      avg_view_pct=None, watch_time_minutes=None, average_view_duration=None,
                      likes=None, comments=None, subscribers_gained=None, captured_at=now)
s.add(mA_null)
s.commit()
mB = metric(vB, 3, 60)
mR = metric(vR, 4, 60)
ok(mA_new.impressions == 0 and mA_new.ctr == 0.0, "precondition: impressions/ctr are 0 today")


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeReporting:
    """jobs().list/create, jobs().reports().list — what reach_loop calls."""

    def __init__(self, name):
        self.name = name
        self.jobs_store: list[dict] = []
        self.reports_store: list[dict] = []
        self.creates = 0
        self.fail_with: Exception | None = None

    def jobs(self):
        return self

    def reports(self):
        outer = self

        class _R:
            def list(self, jobId, pageToken=None):
                return _Req(lambda: outer._guard({"reports": list(outer.reports_store)}))
        return _R()

    def list(self, pageToken=None):
        return _Req(lambda: self._guard({"jobs": list(self.jobs_store)}))

    def create(self, body):
        def go():
            self._guard(None)
            self.creates += 1
            job = {"id": f"job-{self.name}", "reportTypeId": body["reportTypeId"]}
            self.jobs_store.append(job)
            return job
        return _Req(go)

    def _guard(self, val):
        if self.fail_with is not None:
            raise self.fail_with
        return val


def http_error(status, payload):
    resp = httplib2.Response({"status": status})
    resp.reason = "err"
    return HttpError(resp, payload.encode())


FAKES = {"owera-software": FakeReporting("os"), "rodrigo-recio": FakeReporting("rr")}
CSVS: dict[str, bytes] = {}
downloads: list[str] = []
download_fail: set[str] = set()


def fake_download(reporting, url):
    downloads.append(url)
    if url in download_fail:
        raise http_error(500, j({"error": {"code": 500, "message": "boom"}}))
    return CSVS[url]


def report(rid, day, created, csv_body):
    url = f"https://youtubereporting.googleapis.com/v1/media/{rid}"
    CSVS[url] = (HDR + csv_body).encode()
    # Pacific midnight of `day` in UTC (PDT, -7h) — the report's startTime.
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=7)
    return {"id": rid, "jobId": "job", "startTime": start.isoformat().replace("+00:00", "Z"),
            "endTime": (start + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "createTime": created, "downloadUrl": url}


def must_not_call(name):
    def _f(*a, **k):
        raise AssertionError(f"{name} must never be called by the reach loop")
    return _f


@contextlib.contextmanager
def patched():
    with patch.object(youtube, "get_reporting_service", lambda slug: FAKES[slug]), \
            patch.object(youtube, "download_report", fake_download), \
            patch.object(youtube, "build_flow", must_not_call("build_flow")), \
            patch.object(youtube, "connect_interactive", must_not_call("connect_interactive"),
                         create=True), \
            patch.object(youtube, "get_service", must_not_call("get_service")), \
            patch.object(notify, "mark_dead_committed", must_not_call("mark_dead_committed")):
        yield


def rows(ch_id=None):
    q = select(VideoReachDaily)
    if ch_id:
        q = q.where(VideoReachDaily.channel_id == ch_id)
    return s.exec(q).all()


def fresh(m):
    s.refresh(m)
    return m


print("sync: reporting job per channel, idempotent")
with patched():
    out_os = reach_loop.sync_channel(s, os_ch)
    out_rr = reach_loop.sync_channel(s, rr_ch)
ok(out_os["created"] and out_os["job_id"] == "job-os" and FAKES["owera-software"].creates == 1,
   "OS: no job yet → channel_reach_basic_a1 job created")
ok(out_rr["created"] and out_rr["job_id"] == "job-rr" and FAKES["rodrigo-recio"].creates == 1,
   "RR: its own job created (one job per channel)")
ok(FAKES["owera-software"].jobs_store[0]["reportTypeId"] == "channel_reach_basic_a1",
   "job report type is channel_reach_basic_a1")
created_runs = s.exec(select(JobRun).where(JobRun.kind == "reach",
                                           JobRun.detail.like("created reporting job%"))).all()
ok(len(created_runs) == 2, "job creation recorded as a JobRun (kind=reach) per channel")
with patched():
    again = reach_loop.sync_channel(s, os_ch)
ok(again["created"] is False and FAKES["owera-software"].creates == 1,
   "second pass reuses the job — create is idempotent")
ok(out_os["applied"] == 0 and fresh(mA_new).impressions == 0,
   "no reports yet (24–48h latency) → nothing written, no error")

print("sync: reports parsed, aggregated, written to VideoMetric.impressions / .ctr")
fo = FAKES["owera-software"]
fo.reports_store = [
    report("r2", "2026-10-02", "2026-10-04T10:00:00.123456Z",
           "20261002,UC1,vidA,3000,0.01\n20261002,UC1,vidB,400,0.025\n"),
    report("r1", "2026-10-01", "2026-10-03T10:00:00Z",
           "20261001,UC1,vidA,1000,0.05\n20261001,UC1,vidGhost,70,0.1\n"
           "20261001,UC1,,99,0.5\n"),
]
downloads.clear()
with patched():
    out = reach_loop.sync_channel(s, os_ch)
ok(out["reports"] == 2 and out["applied"] == 2 and out["failed"] == 0 and len(downloads) == 2,
   "two new reports downloaded and applied")
led = s.exec(select(ReachReport).order_by(ReachReport.id)).all()
ok([x.report_id for x in led] == ["r1", "r2"], "applied oldest createTime first, ledgered")
ok([x.report_date for x in led] == ["2026-10-01", "2026-10-02"] and led[0].rows == 2
   and led[0].videos == 2 and led[0].ctr_unit == "fraction",
   "ledger keeps Pacific day, row/video counts and the detected CTR unit")
ok(len(rows(1)) == 4, "4 daily rows (A×2, B, ghost); channel-level row skipped")
ghost = s.exec(select(VideoReachDaily).where(VideoReachDaily.yt_video_id == "vidGhost")).one()
ok(ghost.video_id is None and ghost.impressions == 70,
   "a YouTube video the manager doesn't know is stored (video_id NULL), not fatal")
A = fresh(mA_new)
ok(A.impressions == 4000, "vidA impressions = 1000 + 3000 written to VideoMetric.impressions")
ok(abs(A.ctr - 0.02) < 1e-9,
   f"vidA ctr = (50 + 30) / 4000 = 0.02 (0..1, impression-weighted, not mean 0.03) got {A.ctr}")
ok(0.0 <= A.ctr <= 1.0, "ctr stored as a 0..1 fraction, matching VideoMetric.ctr's unit")
B = fresh(mB)
ok(B.impressions == 400 and abs(B.ctr - 0.025) < 1e-9, "vidB: 400 impressions, ctr 0.025")
ok(fresh(mA_old).impressions == 0 and fresh(mA_null).impressions is None
   and fresh(mA_null).views is None,
   "only the latest MEASURED snapshot is written; older rows + the NULL row untouched")
ok(fresh(mR).impressions == 0, "RR data untouched by the OS pass")
ok(out["written"] == 2, "2 VideoMetric rows updated (A, B)")

print("re-run idempotent")
downloads.clear()
with patched():
    out = reach_loop.sync_channel(s, os_ch)
ok(out["reports"] == 0 and out["applied"] == 0 and downloads == [],
   "same reports again → ledger skips them, nothing re-downloaded")
ok(len(rows(1)) == 4 and fresh(mA_new).impressions == 4000 and abs(fresh(mA_new).ctr - 0.02) < 1e-9,
   "no double count: rows and totals unchanged")

print("backfill replaces a day; an older report never overwrites a newer one")
fo.reports_store.append(report("r1b", "2026-10-01", "2026-10-05T09:00:00Z",
                               "20261001,UC1,vidA,2000,0.05\n"))
with patched():
    out = reach_loop.sync_channel(s, os_ch)
day1 = s.exec(select(VideoReachDaily).where(VideoReachDaily.channel_id == 1,
                                            VideoReachDaily.day == "2026-10-01")).all()
ok(out["applied"] == 1 and len(day1) == 1 and day1[0].impressions == 2000
   and day1[0].report_id == "r1b",
   "backfill r1b (same day, newer createTime) replaced day 1 wholesale (ghost row gone)")
A = fresh(mA_new)
ok(A.impressions == 5000 and abs(A.ctr - (100 + 30) / 5000) < 1e-9,
   "totals recomputed from the replaced day: 2000 + 3000, ctr 130/5000")
fo.reports_store.append(report("r1old", "2026-10-01", "2026-10-03T08:00:00Z",
                               "20261001,UC1,vidA,999999,0.9\n"))
with patched():
    out = reach_loop.sync_channel(s, os_ch)
ok(fresh(mA_new).impressions == 5000 and s.exec(select(ReachReport).where(
    ReachReport.report_id == "r1old")).one() is not None,
   "a report older than the stored day is ledgered but not applied")

print("a failed download is retried next tick; the rest still apply")
fo.reports_store.append(report("r3", "2026-10-03", "2026-10-05T10:00:00Z",
                               "20261003,UC1,vidB,600,0.05\n"))
fo.reports_store.append(report("r4", "2026-10-04", "2026-10-06T10:00:00Z",
                               "20261004,UC1,vidB,1000,0.0\n"))
download_fail.add(fo.reports_store[-2]["downloadUrl"])
with patched():
    out = reach_loop.sync_channel(s, os_ch)
ok(out["applied"] == 1 and out["failed"] == 1 and fresh(mB).impressions == 1400,
   "r3 failed (not ledgered), r4 applied: vidB = 400 + 1000")
err = s.exec(select(JobRun).where(JobRun.kind == "reach", JobRun.status == "error")
             .order_by(JobRun.id.desc())).first()
ok(err is not None and "1 failed" in err.detail, "partial failure recorded as a reach error JobRun")
download_fail.clear()
with patched():
    out = reach_loop.sync_channel(s, os_ch)
ok(out["applied"] == 1 and fresh(mB).impressions == 2000
   and abs(fresh(mB).ctr - (10 + 30) / 2000) < 1e-9,
   "next tick retried r3: vidB = 400 + 600 + 1000, ctr (10 + 30 + 0) / 2000")
r4b = report("r4b", "2026-10-04", "2026-10-07T10:00:00Z", "")
fo.reports_store.append(r4b)
with patched():
    out = reach_loop.sync_channel(s, os_ch)
ok(out["applied"] == 1 and fresh(mB).impressions == 1000
   and abs(fresh(mB).ctr - 40 / 1000) < 1e-9
   and not s.exec(select(VideoReachDaily).where(VideoReachDaily.day == "2026-10-04")).all(),
   "an empty (header-only) backfill for a day clears that day: vidB = 400 + 600")

print("scope insufficient / API disabled / token unavailable → clean skip")


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


cap = _Logs()
logging.getLogger("manager.reach").addHandler(cap)
logging.getLogger("manager.reach").setLevel(logging.INFO)
fr = FAKES["rodrigo-recio"]
fr.fail_with = http_error(403, SCOPE_403)
before_status = (rr_ch.oauth_status, rr_ch.oauth_error)
raised = None
with patched():
    try:
        out = reach_loop.sync_channel(s, rr_ch)
    except Exception as e:                       # noqa: BLE001
        raised = e
ok(raised is None and out == {"skipped": out["skipped"]} and "scope" in out["skipped"],
   "403 insufficient scope → channel skipped, no exception")
s.refresh(rr_ch)
ok((rr_ch.oauth_status, rr_ch.oauth_error) == before_status,
   "channel oauth_status / oauth_error untouched (no NeedsConnect flip)")
warn = [x for x in cap.records if x.levelno == logging.WARNING]
ok(warn and "rodrigo-recio" in warn[-1].getMessage()
   and "no re-auth triggered" in warn[-1].getMessage()
   and "yt-analytics.readonly" in warn[-1].getMessage(),
   "a clear WARNING names the channel, the scope and that no re-auth was triggered")
err = s.exec(select(JobRun).where(JobRun.kind == "reach", JobRun.channel_id == 2,
                                  JobRun.status == "error")).first()
ok(err is not None and "insufficient authentication scopes" in err.detail and err.quota_cost == 0,
   "JobRun error (kind=reach, cost 0) carries Google's message")
ok(fr.creates == 1, "no job created while refused")
fr.fail_with = http_error(403, DISABLED_403)
with patched():
    out = reach_loop.sync_channel(s, rr_ch)
ok("api_disabled" in out["skipped"] and s.get(Channel, 2).oauth_status == OAuthStatus.CONNECTED,
   "403 API disabled → skipped, channel still CONNECTED")
ok(any("enable the 'YouTube Reporting API'" in x.getMessage() for x in cap.records),
   "the API-disabled warning says to enable the YouTube Reporting API")
fr.fail_with = None


def no_token(slug):
    raise youtube.NeedsConnect(f"token missing/expired for channel '{slug}' — reconnect required")


with patched(), patch.object(youtube, "get_reporting_service", no_token):
    out = reach_loop.sync_channel(s, rr_ch)
s.refresh(rr_ch)
ok(out["skipped"] == "token unavailable" and rr_ch.oauth_status == OAuthStatus.CONNECTED,
   "unloadable token → skipped; status NOT flipped (analytics loop owns dead-token handling)")
fr.reports_store = [report("rr1", "2026-10-02", "2026-10-04T11:00:00Z",
                           "20261002,UC2,vidR,800,0.04\n")]
fo.fail_with = http_error(403, SCOPE_403)


@contextlib.contextmanager
def _scope():
    ss = Session(engine)
    try:
        yield ss
        ss.commit()
    finally:
        ss.close()


with patched(), patch.object(reach_loop, "session_scope", _scope):
    reach_loop.tick()
s.expire_all()
ok(fresh(mR).impressions == 800 and abs(fresh(mR).ctr - 0.04) < 1e-9,
   "tick: OS refused (scope) but RR still processed — vidR 800 impressions, ctr 0.04")
fo.fail_with = None


def boom(session, channel):
    if channel.slug == "owera-software":
        raise RuntimeError("unexpected")
    calls.append(channel.slug)
    return {}


calls: list[str] = []
with patch.object(reach_loop, "session_scope", _scope), patch.object(reach_loop, "sync_channel", boom):
    reach_loop.tick()
ok(calls == ["rodrigo-recio"], "tick: an unexpected crash on one channel never starves the other")
s.get(Settings, 1).scheduler_paused = True
s.commit()
calls.clear()
with patch.object(reach_loop, "session_scope", _scope), patch.object(
        reach_loop, "sync_channel", lambda ss, c: calls.append(c.slug)):
    reach_loop.tick()
ok(calls == [], "tick honors scheduler_paused")
s.get(Settings, 1).scheduler_paused = False
s.commit()

print("analytics loop carries the reach totals into new snapshots")
FETCHED = {"empty": False, "views": 12, "impressions": 0, "ctr": 0.0, "avg_view_pct": 40.0,
           "average_view_duration": 10.0, "watch_time_minutes": 2, "likes": 1,
           "comments": 0, "subscribers_gained": 0}
with patch.object(youtube, "fetch_video_analytics", lambda *a: dict(FETCHED)), \
        patch.object(youtube, "fetch_traffic_sources", lambda *a: {"sources": {}}):
    newA = analytics_loop.record_video_snapshot(s, "ana", os_ch, vA, now + timedelta(hours=1))
    vC = video(1, "vidC", 1)
    newC = analytics_loop.record_video_snapshot(s, "ana", os_ch, vC, now + timedelta(hours=1))
s.commit()
ok(newA.impressions == 5000 and abs(newA.ctr - 130 / 5000) < 1e-9,
   "new daily snapshot for vidA carries impressions 5000 / ctr 0.026 (not the v2 zero)")
ok(newC.impressions == 0 and newC.ctr == 0.0, "a video without reach data keeps the v2 value (0)")

print("leaderboard + by-topic serve the values")
lb = youtube_admin.video_analytics(1, "impressions", s)
top = lb["items"][0]
ok(top["yt_video_id"] == "vidA" and top["impressions"] == 5000 and abs(top["ctr"] - 0.026) < 1e-9,
   "GET video-analytics?sort=impressions → vidA first with impressions/ctr")
agg = youtube_admin.video_analytics_by_topic(1, s)
ok(agg["by_topic"][0]["impressions"] == 6000 and agg["by_topic"][0]["avg_ctr"] > 0,
   "by-topic impressions summed (5000 + 1000) and avg_ctr > 0")

print("schema: the new tables are created by init_db (create_all), idempotently")
fresh_engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                             poolclass=StaticPool)
with patch.object(app_db, "engine", fresh_engine):
    app_db.init_db()
    app_db.init_db()
names = set(inspect(fresh_engine).get_table_names())
ok({"reachreport", "videoreachdaily"} <= names, "reachreport + videoreachdaily exist after init_db")
cols = {c["name"] for c in inspect(fresh_engine).get_columns("videometric")}
ok({"impressions", "ctr"} <= cols and "video_thumbnail_impressions" not in cols,
   "no new VideoMetric column — existing impressions/ctr fields reused")
uniq = [ix for ix in inspect(fresh_engine).get_indexes("reachreport")
        if ix["column_names"] == ["report_id"]]
ok(uniq and uniq[0]["unique"], "ReachReport.report_id is unique (a report applies once)")
ok(settings.reach_tick_hours > 0 and settings.reach_ctr_unit == "auto",
   "settings: reach_tick_hours / reach_ctr_unit defaults")

print(f"all {_checks} checks passed")
