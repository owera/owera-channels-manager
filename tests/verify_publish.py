"""Dependency-free regression checks for the publish path (publish_loop + quota).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_publish.py

Covers the failure modes that actually hit production, so they can't silently
regress:
  - stuck-'publishing' recovery + retry cap (the mislabeled ch2 "stalls")
  - a revoked OAuth token flips the channel to EXPIRED and returns the video to
    'approved' — never stranded in 'publishing' (the 362691a fix)
  - upload-stall retry-then-fail, quota-exceeded cooldown, and drip spacing
  - custom thumbnail: content_format from Topic (not overrides_json), and a
    generation/upload failure never fails the publish
  - first-comment engagement seed (language + playlist series pointer) is
    best-effort and never fails a publish
  - tick() skip gates: scheduler_paused, paused/dead channels, cooldown,
    daily budget, quota-cap headroom, in-flight, drip — each proven at the
    tick() call, not only at the helper
  - daily mix: empty/"LONG" topic formats count as short after the day's
    long is out (same != "long" gate as render/issues), not == "short"
  - daily_publish_budget<=0: tick never publishes, so publish-plan is empty
    and the dashboard next_publish_eta is None (the max(1,) clamp lied)
  - dashboard publish_hold: budget<=0 with approved work is labeled "budget"
    (the UI used to chip only paused, so budget-0 showed neither ETA nor why)

Uses an in-memory SQLite DB and stubs the YouTube calls — no network, no creds.
Exits non-zero on the first failed assertion.
"""
import sys
from datetime import datetime, timedelta, timezone

from pathlib import Path
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

# Importing app.models defines every table=True model, registering them all on
# SQLModel.metadata so create_all() below builds the full schema.
from app.config import settings
from app.models import (Channel, JobRun, OAuthStatus, Playlist, Topic, Video,
                        VideoStatus, utcnow)
from app.services import publish_loop, quota, youtube

CAP = settings.publish_max_retries
TIMEOUT = settings.publish_timeout_seconds
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh_session() -> Session:
    """A private in-memory DB per test, so cases can't leak into each other."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-test"), name=kw.pop("name", "Test"),
                 oauth_status=kw.pop("oauth_status", OAuthStatus.CONNECTED), **kw)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def make_video(session, channel, **kw):
    v = Video(channel_id=channel.id, topic_id=kw.pop("topic_id", 1),
              subject=kw.pop("subject", "Test subject"), **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


# --- recover_stuck_publishing: the stall→cap incident ------------------------
print("recover_stuck_publishing (stall recovery + retry cap)")

# past the timeout, already at the cap boundary -> give up and fail
s = fresh_session()
ch = make_channel(s)
v = make_video(s, ch, status=VideoStatus.PUBLISHING, retry_count=CAP - 1,
               last_attempt_at=utcnow() - timedelta(seconds=TIMEOUT + 60),
               video_path="/tmp/x.mp4")
publish_loop._recover_stuck_publishing(s)
s.refresh(v)
ok(v.status == VideoStatus.FAILED, "stuck upload at the retry cap is marked failed")
ok(v.retry_count == CAP, "retry_count incremented to the cap")
ok("stalled" in (v.error or ""), "failed video records a stall reason")

# past the timeout but under the cap -> re-queue to approved
s = fresh_session()
ch = make_channel(s)
v = make_video(s, ch, status=VideoStatus.PUBLISHING, retry_count=0,
               last_attempt_at=utcnow() - timedelta(seconds=TIMEOUT + 60),
               video_path="/tmp/x.mp4")
publish_loop._recover_stuck_publishing(s)
s.refresh(v)
ok(v.status == VideoStatus.APPROVED, "stuck upload under the cap is re-queued to approved")
ok(v.retry_count == 1, "retry_count incremented on re-queue")
ok(v.error is None, "re-queued video clears its error")

# still inside the timeout window -> a genuine in-flight upload is left alone
s = fresh_session()
ch = make_channel(s)
v = make_video(s, ch, status=VideoStatus.PUBLISHING, retry_count=0,
               last_attempt_at=utcnow() - timedelta(seconds=5),
               video_path="/tmp/x.mp4")
publish_loop._recover_stuck_publishing(s)
s.refresh(v)
ok(v.status == VideoStatus.PUBLISHING, "an in-flight upload inside the timeout is left alone")
ok(v.retry_count == 0, "in-flight upload's retry_count is not bumped")

# Keep every _publish_one / tick() hermetic. make_thumbnail_png otherwise
# calls the live LLM (grok -p) unless stubbed and the suite hangs on the first
# un-stubbed publish.
_REAL_MAKE_THUMB = publish_loop.thumbnail.make_thumbnail_png
publish_loop.thumbnail.make_thumbnail_png = lambda *a, **k: None

# --- publish_one: revoked OAuth token (the 362691a fix) ----------------------
print("publish_one: revoked OAuth token")
_ORIG_GET, _ORIG_UPLOAD = youtube.get_service, youtube.upload_video

s = fresh_session()
ch = make_channel(s, oauth_status=OAuthStatus.CONNECTED)
v = make_video(s, ch, status=VideoStatus.APPROVED, video_path="/tmp/x.mp4", title="T")


def _raise_needs_connect(slug):
    raise youtube.NeedsConnect("token missing/expired — reconnect required")


youtube.get_service = _raise_needs_connect
_ORIG_HAS = youtube.has_token
youtube.has_token = lambda slug: True   # revoked = the token *file* still exists
publish_loop._publish_one(s, ch, v)
ok(ch.oauth_status == OAuthStatus.EXPIRED, "revoked token flips the channel to EXPIRED")
ok(v.status == VideoStatus.APPROVED, "video returns to approved, not stranded in publishing")
youtube.has_token = _ORIG_HAS

# --- publish_one: upload stall retry-then-fail -------------------------------
print("publish_one: upload stall retry-then-fail")


def _dummy_service(slug):
    return object()


def _raise_stalled(*a, **k):
    raise youtube.UploadStalled("socket read timed out")


youtube.get_service = _dummy_service
youtube.upload_video = _raise_stalled

s = fresh_session()
ch = make_channel(s)
v = make_video(s, ch, status=VideoStatus.APPROVED, retry_count=0,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.APPROVED, "a stalled upload under the cap goes back to approved")
ok(v.retry_count == 1, "stall bumps retry_count")

s = fresh_session()
ch = make_channel(s)
v = make_video(s, ch, status=VideoStatus.APPROVED, retry_count=CAP - 1,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.FAILED, "a stalled upload at the cap is marked failed")
ok("gave up" in (v.error or ""), "failed stall records that it gave up")

# --- publish_one: quota exceeded -> cooldown ---------------------------------
print("publish_one: quota exceeded -> cooldown")


def _raise_quota(*a, **k):
    raise youtube.QuotaExceeded("quota exceeded: daily", reason="quotaExceeded")


youtube.get_service = _dummy_service
youtube.upload_video = _raise_quota

s = fresh_session()
ch = make_channel(s)
v = make_video(s, ch, status=VideoStatus.APPROVED, video_path="/tmp/x.mp4", title="T")
try:
    publish_loop._publish_one(s, ch, v)
    raised = False
except youtube.QuotaExceeded:
    raised = True
ok(raised, "quota exceeded propagates so the tick can stop the channel")
ok(v.status == VideoStatus.APPROVED, "quota-blocked video stays approved for retry")
ok(ch.cooldown_until is not None, "channel gets a cooldown after hitting the quota cap")

youtube.get_service, youtube.upload_video = _ORIG_GET, _ORIG_UPLOAD  # restore

# --- drip spacing + daily cap guard ------------------------------------------
print("drip spacing + daily cap guard")

s = fresh_session()
ch = make_channel(s)
ok(publish_loop._drip_ok(s, ch, 30) is True, "no prior publish -> the first one is allowed")
quota.log(s, kind="publish", status="success", channel_id=ch.id)
s.commit()
ok(publish_loop._drip_ok(s, ch, 30) is False, "a recent publish blocks the next within the window")

s = fresh_session()
ch = make_channel(s)
s.add(JobRun(kind="publish", status="success", channel_id=ch.id,
             created_at=utcnow() - timedelta(minutes=40)))
s.commit()
ok(publish_loop._drip_ok(s, ch, 30) is True, "a publish older than the window allows the next")

s = fresh_session()
ch = make_channel(s)
ok(quota.daily_limit_hit(s, ch.id) is False, "no quota errors -> daily limit not hit")
quota.log(s, kind="publish", status="error", channel_id=ch.id,
          detail="quota exceeded: [quotaExceeded] cooldown until ...")
s.commit()
ok(quota.daily_limit_hit(s, ch.id) is True, "a 'quota exceeded:' error trips the daily-limit guard")

# --- audience-peak publish windows (BACKLOG 12) -------------------------------
print("parse_windows: spec parsing is all-or-nothing")

ok(publish_loop.parse_windows(None) == [], "None spec parses to no restriction")
ok(publish_loop.parse_windows("  ") == [], "blank spec parses to no restriction")
ok(publish_loop.parse_windows("12:00-13:30") == [(720, 810)], "single range parses")
ok(publish_loop.parse_windows("12:00-13:30, 19:00-20:30") == [(720, 810), (1140, 1230)],
   "comma+space separated ranges parse")
ok(publish_loop.parse_windows("9:00-10:00") == [(540, 600)], "single-digit hour parses")
ok(publish_loop.parse_windows("22:00-01:00") == [(1320, 60)], "past-midnight range parses")
ok(publish_loop.parse_windows("9am-10am") is None, "non-HH:MM tokens are invalid")
ok(publish_loop.parse_windows("25:00-26:00") is None, "out-of-range hours are invalid")
ok(publish_loop.parse_windows("12:00-12:00") is None, "zero-length range is invalid")
ok(publish_loop.parse_windows("12:00-13:00,19:0O-20:00") is None,
   "one bad range invalidates the whole spec (no silent narrowing)")

print("_window_ok: channel-local windows, wrap-around, fail-open")
NOON_UTC = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)  # 09:00 in São Paulo

s = fresh_session()
ch = make_channel(s, publish_windows="09:00-10:00", publish_tz="America/Sao_Paulo")
ok(publish_loop._window_ok(ch, NOON_UTC) is True,
   "12:00 UTC is inside a 09:00-10:00 São Paulo window (local time is what counts)")
ch.publish_windows = "12:00-13:00"
ok(publish_loop._window_ok(ch, NOON_UTC) is False,
   "12:00 UTC is outside a 12:00-13:00 São Paulo window")
ch.publish_tz = None
ok(publish_loop._window_ok(ch, NOON_UTC) is True, "unset publish_tz reads windows as UTC")
ok(publish_loop._window_ok(ch, datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)) is False,
   "window end is exclusive")
ch.publish_windows = "22:00-01:00"
ok(publish_loop._window_ok(ch, datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)) is True,
   "a past-midnight window admits 23:30")
ok(publish_loop._window_ok(ch, datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc)) is True,
   "a past-midnight window admits 00:30")
ok(publish_loop._window_ok(ch, datetime(2026, 7, 1, 1, 30, tzinfo=timezone.utc)) is False,
   "a past-midnight window excludes 01:30")
ch.publish_windows = "12:00-00:00"
ok(publish_loop._window_ok(ch, datetime(2026, 7, 1, 23, 59, tzinfo=timezone.utc)) is True,
   "an until-midnight window admits 23:59")
ok(publish_loop._window_ok(ch, datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)) is False,
   "an until-midnight window excludes midnight itself")
ch.publish_windows = None
ok(publish_loop._window_ok(ch, NOON_UTC) is True, "no windows -> publish anytime (unchanged)")
ch.publish_windows = "not-a-window"
ok(publish_loop._window_ok(ch, NOON_UTC) is True,
   "an invalid stored spec FAILS OPEN — a typo must never silently stall a channel")
ch.publish_windows = "13:00-14:00"
ch.publish_tz = "Mars/Olympus"
ok(publish_loop._window_ok(ch, NOON_UTC) is False,
   "an unknown tz still enforces the windows (does not fail open)")
ch.publish_windows = "12:00-13:00"
ok(publish_loop._window_ok(ch, NOON_UTC) is True,
   "an unknown tz reads the windows as UTC")

print("tick: the window gate blocks a real publish outside, admits it inside")
from contextlib import contextmanager

from app.db import app_settings


def _minute_spec(offset_start: int, offset_end: int) -> str:
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute

    def fmt(x):
        return f"{(x // 60) % 24:02d}:{x % 60:02d}"

    return f"{fmt((m + offset_start) % 1440)}-{fmt((m + offset_end) % 1440)}"


s = fresh_session()
app_settings(s)                       # seed the Settings row tick() reads
ch = make_channel(s, publish_windows=_minute_spec(120, 180), publish_tz="UTC")
topic = Topic(channel_id=ch.id, name="Windows", theme_prompt="x")
s.add(topic); s.commit(); s.refresh(topic)   # _next_approved inner-joins Topic
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=topic.id,
               video_path="/tmp/x.mp4", title="T")

_uploads = []
_ORIG_SCOPE = publish_loop.session_scope
_ORIG_GET2, _ORIG_UPLOAD2 = youtube.get_service, youtube.upload_video
_ORIG_COMMENT2 = youtube.insert_comment
_ORIG_CREATE2, _ORIG_ADD2 = youtube.create_playlist, youtube.add_to_playlist


@contextmanager
def _test_scope():
    yield s
    s.commit()


publish_loop.session_scope = _test_scope
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: (_uploads.append(a), "vidW")[1]
youtube.insert_comment = lambda *a, **k: "c1"
youtube.create_playlist = lambda service, title, description="", privacy="public": {
    "yt_playlist_id": "PL" + "W" * 32, "title": title,
    "description": description, "privacy": privacy}
youtube.add_to_playlist = lambda *a, **k: "item1"

publish_loop.tick()
s.refresh(v)
ok(_uploads == [], "outside the window, tick uploads nothing")
ok(v.status == VideoStatus.APPROVED, "outside the window, the video stays approved")

ch.publish_windows = _minute_spec(-60, 60)
s.add(ch); s.commit()
publish_loop.tick()
s.refresh(v)
ok(len(_uploads) == 1, "inside the window, tick publishes")
ok(v.status == VideoStatus.PUBLISHED, "inside the window, the video reaches published")

publish_loop.session_scope = _ORIG_SCOPE
youtube.get_service, youtube.upload_video = _ORIG_GET2, _ORIG_UPLOAD2
youtube.insert_comment = _ORIG_COMMENT2
youtube.create_playlist, youtube.add_to_playlist = _ORIG_CREATE2, _ORIG_ADD2

print("next_window_open: ETA cursor advances to the next window opening")
s = fresh_session()
ch = make_channel(s, publish_windows="10:00-11:00", publish_tz="UTC")
T = datetime(2026, 7, 1, 8, 0, 30, tzinfo=timezone.utc)
ok(publish_loop.next_window_open(ch, T) == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
   "before today's window -> advances to its start")
ok(publish_loop.next_window_open(ch, datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
   == datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
   "after today's window -> advances to tomorrow's start")
IN_WIN = datetime(2026, 7, 1, 10, 30, 45, tzinfo=timezone.utc)
ok(publish_loop.next_window_open(ch, IN_WIN) == IN_WIN, "inside the window -> unchanged")
ch.publish_windows = "06:00-07:00,10:00-11:00"
ok(publish_loop.next_window_open(ch, T) == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
   "multiple windows -> the nearest upcoming start wins")
ch.publish_windows = "22:00-01:00"
LATE = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
ok(publish_loop.next_window_open(ch, LATE) == LATE, "inside a past-midnight window -> unchanged")
ch.publish_windows = "19:00-19:30"
ch.publish_tz = "America/Sao_Paulo"
ok(publish_loop.next_window_open(ch, NOON_UTC) == datetime(2026, 7, 1, 22, 0, tzinfo=timezone.utc),
   "advance lands on the window start in the channel's tz (19:00 SP = 22:00 UTC)")
ch.publish_windows, ch.publish_tz = None, None
ok(publish_loop.next_window_open(ch, T) == T, "no windows -> unchanged")
ch.publish_windows = "garbage"
ok(publish_loop.next_window_open(ch, T) == T, "invalid spec -> unchanged (fail open)")

print("publish-plan: board ETAs honor the windows (mirrors the tick gate)")
from app.routers.videos import publish_plan

s = fresh_session()
app_settings(s)
ch = make_channel(s, publish_windows="10:00-11:00", publish_tz="UTC")
vids = [make_video(s, ch, status=VideoStatus.APPROVED, subject=f"w{i}",
                   video_path="/tmp/x.mp4", title="T", approved_at=utcnow())
        for i in range(4)]
plan = publish_plan(ch.id, s)
etas = [datetime.fromisoformat(plan[str(v.id)]) for v in vids]
ok(len(plan) == 4, "every approved video gets an ETA")
ok(all(publish_loop._window_ok(ch, e) for e in etas),
   "every ETA falls inside a publish window")
ok(all(a < b for a, b in zip(etas, etas[1:])), "ETAs stay strictly ascending")
ok(len({e.date() for e in etas}) >= 2,
   "a 1h window with 30min drip spills the plan across days")
ch.publish_windows = None
s.add(ch); s.commit()
ok(len(publish_plan(ch.id, s)) == 4, "without windows the plan still covers every video")

print("publish-plan: ETAs follow _next_approved mix (long-first, then weight-desc shorts)")
# The 08-21 noon lie: FIFO-by-approved_at scheduled leftover t10 / t27 ahead of
# the long and the w4 shorts. Plan order must match the live picker.
s = fresh_session()
app_settings(s)
ch = make_channel(s, daily_publish_budget=5, slug="plan-mix")
t_long = Topic(channel_id=ch.id, name="Longs", theme_prompt="x",
               content_format="long", weight=2)
t_hi = Topic(channel_id=ch.id, name="Winner", theme_prompt="x",
             content_format="short", weight=4)
t_lo = Topic(channel_id=ch.id, name="Leftover", theme_prompt="x",
             content_format="short", weight=1)
s.add(t_long); s.add(t_hi); s.add(t_lo); s.commit()
s.refresh(t_long); s.refresh(t_hi); s.refresh(t_lo)
now = utcnow()
# Oldest approved is the leftover short — FIFO would put it first.
v_lo = make_video(s, ch, topic_id=t_lo.id, subject="old leftover",
                  status=VideoStatus.APPROVED, video_path="/tmp/lo.mp4",
                  approved_at=now - timedelta(days=20))
v_hi1 = make_video(s, ch, topic_id=t_hi.id, subject="winner 1",
                   status=VideoStatus.APPROVED, video_path="/tmp/hi1.mp4",
                   approved_at=now - timedelta(hours=2))
v_hi2 = make_video(s, ch, topic_id=t_hi.id, subject="winner 2",
                   status=VideoStatus.APPROVED, video_path="/tmp/hi2.mp4",
                   approved_at=now - timedelta(hours=1))
v_long = make_video(s, ch, topic_id=t_long.id, subject="the long",
                    status=VideoStatus.APPROVED, video_path="/tmp/lg.mp4",
                    approved_at=now - timedelta(hours=3))
plan = publish_plan(ch.id, s)
order = sorted(plan, key=lambda vid: plan[vid])
ok(order[0] == str(v_long.id),
   "first ETA is the long (not the oldest leftover short)")
ok(order[1] == str(v_hi1.id) and order[2] == str(v_hi2.id),
   "slots 2-3 are the w4 shorts (weight-desc, then FIFO among them)")
ok(order[3] == str(v_lo.id),
   "leftover w1 short is last, even though it was approved first")

print("publish-plan: after a long is already out today, remaining ETAs prefer shorts")
s = fresh_session()
app_settings(s)
ch = make_channel(s, daily_publish_budget=5, slug="plan-after-long")
t_long = Topic(channel_id=ch.id, name="Longs", theme_prompt="x",
               content_format="long", weight=2)
t_short = Topic(channel_id=ch.id, name="Shorts", theme_prompt="x",
                content_format="short", weight=1)
s.add(t_long); s.add(t_short); s.commit()
s.refresh(t_long); s.refresh(t_short)
now = utcnow()
# One long already published this quota day — the picker must not reserve again.
pub_long = make_video(s, ch, topic_id=t_long.id, subject="already out",
                      status=VideoStatus.PUBLISHED, video_path="/tmp/pl.mp4",
                      published_at=now)
from app.models import JobRun
s.add(JobRun(kind="publish", status="success", channel_id=ch.id,
             video_id=pub_long.id, quota_cost=0, created_at=now))
s.commit()
v_long2 = make_video(s, ch, topic_id=t_long.id, subject="banked long",
                     status=VideoStatus.APPROVED, video_path="/tmp/l2.mp4",
                     approved_at=now - timedelta(hours=1))
v_s1 = make_video(s, ch, topic_id=t_short.id, subject="short 1",
                  status=VideoStatus.APPROVED, video_path="/tmp/s1.mp4",
                  approved_at=now - timedelta(hours=2))
plan = publish_plan(ch.id, s)
order = sorted(plan, key=lambda vid: plan[vid])
ok(order[0] == str(v_s1.id),
   "with a long already out today, first remaining ETA is the short")
ok(order[1] == str(v_long2.id),
   "banked long drains after shorts (no second long reserved today)")

# --- publish_one: a stored playlist id is trusted regardless of its shape -----
# YouTube returns more than one playlist-id format (13-char "PL…" ids are live and
# accept inserts — verified on the real channels 2026-07-24). Pre-judging ids by
# length caused a recreate loop: one duplicate playlist per publish. The only
# truthful invalidity signal is the add_to_playlist 404, tested further below.
print("publish_one: a 13-char stored playlist id is kept and used (no recreate loop)")
_SHORT_PL = "PLLdeDcM9G5vY"  # a real, live 13-char playlist id format
_added_to = []
_created = []


def _dummy_upload(*a, **k):
    return "vid123"


def _record_create(service, title, description="", privacy="public"):
    _created.append(title)
    return {"yt_playlist_id": "PL" + "Z" * 32, "title": title,
            "description": description, "privacy": privacy}


def _record_add(service, playlist_id, video_id):
    _added_to.append(playlist_id)
    return "item1"


_ORIG_CREATE, _ORIG_ADD, _ORIG_COMMENT = (
    youtube.create_playlist, youtube.add_to_playlist, youtube.insert_comment)
youtube.get_service = _dummy_service
youtube.upload_video = _dummy_upload
youtube.create_playlist = _record_create
youtube.add_to_playlist = _record_add
youtube.insert_comment = lambda *a, **k: "c1"

s = fresh_session()
ch = make_channel(s)
topic = Topic(channel_id=ch.id, name="OpenCode", theme_prompt="x")
s.add(topic); s.commit(); s.refresh(topic)
short_pl = Playlist(channel_id=ch.id, yt_playlist_id=_SHORT_PL, title="OpenCode")
s.add(short_pl); s.commit(); s.refresh(short_pl)
topic.playlist_id = short_pl.id
s.add(topic); s.commit()
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=topic.id,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
s.refresh(topic); s.refresh(v)
ok(v.status == VideoStatus.PUBLISHED, "video publishes")
ok(topic.playlist_id == short_pl.id, "the 13-char playlist mapping is KEPT, not dropped")
ok(_created == [], "no duplicate playlist was created")
ok(_added_to == [_SHORT_PL], "add_to_playlist used the stored 13-char id")
ok(v.added_to_playlist is True, "video recorded as added to the playlist")

# --- publish_one: a genuinely dead playlist heals via the add 404 -------------
print("publish_one: add_to_playlist 404 -> mapping dropped for recreate next publish")
import httplib2
from googleapiclient.errors import HttpError as _HttpError

_dead_resp = httplib2.Response({"status": 404})
_dead_resp.reason = "Not Found"
_DEAD_404 = _HttpError(
    _dead_resp, b'{"error": {"errors": [{"reason": "playlistNotFound"}]}}')


def _add_raises_404(service, playlist_id, video_id):
    raise _DEAD_404


youtube.add_to_playlist = _add_raises_404
s = fresh_session()
ch = make_channel(s)
topic = Topic(channel_id=ch.id, name="Dead", theme_prompt="x")
s.add(topic); s.commit(); s.refresh(topic)
dead_pl = Playlist(channel_id=ch.id, yt_playlist_id=_SHORT_PL, title="Dead")
s.add(dead_pl); s.commit(); s.refresh(dead_pl)
topic.playlist_id = dead_pl.id
s.add(topic); s.commit()
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=topic.id,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
s.commit()  # tick()'s session_scope commits after _publish_one returns
s.refresh(topic); s.refresh(v)
ok(v.status == VideoStatus.PUBLISHED, "the publish itself still succeeds on a dead playlist")
ok(v.added_to_playlist is False, "video not marked added when the add 404s")
ok(topic.playlist_id is None, "dead mapping dropped so next publish recreates the playlist")

youtube.create_playlist, youtube.add_to_playlist = _ORIG_CREATE, _ORIG_ADD
youtube.insert_comment = _ORIG_COMMENT
youtube.get_service, youtube.upload_video = _ORIG_GET, _ORIG_UPLOAD

# --- daily mix: 1 long + rest shorts (the 08-03/05 ch1 5L/0S bug) -------------
print("\n_next_approved: daily mix caps longs at 1/day when shorts remain")
s = fresh_session()
ch = make_channel(s)
t_long = Topic(channel_id=ch.id, name="Longs", theme_prompt="x", content_format="long", weight=2)
t_short = Topic(channel_id=ch.id, name="Shorts", theme_prompt="x", content_format="short", weight=0)
s.add(t_long); s.add(t_short); s.commit(); s.refresh(t_long); s.refresh(t_short)
now = utcnow()
for i in range(3):
    make_video(s, ch, topic_id=t_long.id, subject=f"L{i}", status=VideoStatus.APPROVED,
               approved_at=now - timedelta(hours=10 - i), video_path=f"/tmp/l{i}.mp4")
for i in range(2):
    make_video(s, ch, topic_id=t_short.id, subject=f"S{i}", status=VideoStatus.APPROVED,
               approved_at=now - timedelta(hours=5 - i), video_path=f"/tmp/s{i}.mp4")

v1 = publish_loop._next_approved(s, ch.id)
ok(v1 is not None and v1.topic_id == t_long.id,
   "slot 1 reserves a long when none published yet (even with shorts waiting)")
v1.status = VideoStatus.PUBLISHED
v1.published_at = utcnow()
s.add(v1); s.commit()

v2 = publish_loop._next_approved(s, ch.id)
ok(v2 is not None and v2.topic_id == t_short.id,
   "after a long is out, prefer w0 shorts over remaining w2 longs (no 5L/0S sweep)")
v2.status = VideoStatus.PUBLISHED
v2.published_at = utcnow()
s.add(v2); s.commit()
v3 = publish_loop._next_approved(s, ch.id)
ok(v3 is not None and v3.topic_id == t_short.id, "second post-long slot is also a short")
v3.status = VideoStatus.PUBLISHED
v3.published_at = utcnow()
s.add(v3); s.commit()
v4 = publish_loop._next_approved(s, ch.id)
ok(v4 is not None and v4.topic_id == t_long.id,
   "once shorts are exhausted, remaining longs still drain (no starve)")

# Defect: after the day's long is out, _pick("short") used Topic.content_format
# == "short". Render, chapters, thumbnails, and issues.detect all treat empty
# and "LONG" as short via != "long" / == "long". A PATCH leftover of those
# values (update_topic does not clamp) would not fill remaining slots, so
# higher-weight longs could sweep the rest of the day — same class as the
# 08-03/05 5L/0S bug. Discriminator: leftover shorts MUST be lower-weight
# than remaining longs; a _pick(None) fallthrough would then pick the long
# (the first cut of this fixture had leftover weight 4 and passed on the
# unfixed code). Slot 1 still requires the canonical "long".
print("\n_next_approved: empty/'LONG' count as short after the daily long (BACKLOG 23)")
s = fresh_session()
app_settings(s)
ch = make_channel(s, slug="slot1")
t_long = Topic(channel_id=ch.id, name="Longs", theme_prompt="x",
               content_format="long", weight=1)
t_empty = Topic(channel_id=ch.id, name="Empty", theme_prompt="x",
                content_format="", weight=4)
t_case = Topic(channel_id=ch.id, name="LONG", theme_prompt="x",
               content_format="LONG", weight=4)
s.add(t_long); s.add(t_empty); s.add(t_case)
s.commit(); s.refresh(t_long); s.refresh(t_empty); s.refresh(t_case)
now = utcnow()
v_long = make_video(s, ch, topic_id=t_long.id, subject="L-slot1",
                    status=VideoStatus.APPROVED,
                    approved_at=now - timedelta(hours=4),
                    video_path="/tmp/l-slot1.mp4")
make_video(s, ch, topic_id=t_empty.id, subject="E-slot1",
           status=VideoStatus.APPROVED,
           approved_at=now - timedelta(hours=2),
           video_path="/tmp/e-slot1.mp4")
make_video(s, ch, topic_id=t_case.id, subject="C-slot1",
           status=VideoStatus.APPROVED,
           approved_at=now - timedelta(hours=1),
           video_path="/tmp/c-slot1.mp4")
v1 = publish_loop._next_approved(s, ch.id)
ok(v1 is not None and v1.id == v_long.id,
   "slot 1 still reserves canonical long (empty/'LONG' at weight 4 do not steal it)")
plan = publish_plan(ch.id, s)
order = sorted(plan, key=lambda vid: plan[vid])
ok(order[0] == str(v_long.id),
   "publish-plan slot 1 is the canonical long (leftover formats do not steal the ETA)")

s = fresh_session()
app_settings(s)
ch = make_channel(s, slug="post-long")
t_long = Topic(channel_id=ch.id, name="Longs", theme_prompt="x",
               content_format="long", weight=4)
t_empty = Topic(channel_id=ch.id, name="Empty", theme_prompt="x",
                content_format="", weight=0)
t_case = Topic(channel_id=ch.id, name="LONG", theme_prompt="x",
               content_format="LONG", weight=0)
t_other = Topic(channel_id=ch.id, name="Medium", theme_prompt="x",
                content_format="medium", weight=0)
s.add(t_long); s.add(t_empty); s.add(t_case); s.add(t_other)
s.commit(); s.refresh(t_long); s.refresh(t_empty); s.refresh(t_case); s.refresh(t_other)
now = utcnow()
v_long_a = make_video(s, ch, topic_id=t_long.id, subject="L-a",
                      status=VideoStatus.APPROVED,
                      approved_at=now - timedelta(hours=4),
                      video_path="/tmp/la.mp4")
v_long_b = make_video(s, ch, topic_id=t_long.id, subject="L-b",
                      status=VideoStatus.APPROVED,
                      approved_at=now - timedelta(hours=3),
                      video_path="/tmp/lb.mp4")
v_empty = make_video(s, ch, topic_id=t_empty.id, subject="E",
                     status=VideoStatus.APPROVED,
                     approved_at=now - timedelta(hours=2),
                     video_path="/tmp/e.mp4")
v_case = make_video(s, ch, topic_id=t_case.id, subject="C",
                    status=VideoStatus.APPROVED,
                    approved_at=now - timedelta(hours=1),
                    video_path="/tmp/c.mp4")
v_other = make_video(s, ch, topic_id=t_other.id, subject="M",
                     status=VideoStatus.APPROVED,
                     approved_at=now - timedelta(minutes=30),
                     video_path="/tmp/m.mp4")

v1 = publish_loop._next_approved(s, ch.id)
ok(v1 is not None and v1.id == v_long_a.id,
   "post-long fixture: slot 1 is still the canonical long")
v1.status = VideoStatus.PUBLISHED
v1.published_at = utcnow()
s.add(v1); s.commit()

plan = publish_plan(ch.id, s)
order = sorted(plan, key=lambda vid: plan[vid])
ok(order == [str(v_empty.id), str(v_case.id), str(v_other.id), str(v_long_b.id)],
   "publish-plan remaining ETAs match leftover-short then remaining-long order")

v2 = publish_loop._next_approved(s, ch.id)
ok(v2 is not None and v2.id == v_empty.id,
   "after a long is out, w0 empty-format is picked before remaining w4 longs")
v2.status = VideoStatus.PUBLISHED
v2.published_at = utcnow()
s.add(v2); s.commit()

v3 = publish_loop._next_approved(s, ch.id)
ok(v3 is not None and v3.id == v_case.id,
   "after a long is out, w0 non-canonical 'LONG' is picked before remaining w4 longs")
v3.status = VideoStatus.PUBLISHED
v3.published_at = utcnow()
s.add(v3); s.commit()

v4 = publish_loop._next_approved(s, ch.id)
ok(v4 is not None and v4.id == v_other.id,
   "after a long is out, any non-long leftover (not an empty/'LONG' allowlist) is short")
v4.status = VideoStatus.PUBLISHED
v4.published_at = utcnow()
s.add(v4); s.commit()

v5 = publish_loop._next_approved(s, ch.id)
ok(v5 is not None and v5.id == v_long_b.id,
   "once leftover shorts are exhausted, remaining longs still drain")

# --- custom thumbnail: format from Topic, never fail a publish (BACKLOG 20) ---
# Defect: _set_custom_thumbnail read content_format from video.overrides_json,
# which is the operator per-video override blob — almost always empty, and
# render_loop never writes the topic format back into it. Long-form videos
# therefore always got the "short-form vertical video" hook prompt. Source of
# truth is Topic.content_format (same gate chapters and render_loop use).
print("\n_set_custom_thumbnail: content_format from topic, never fails publish")

_thumb_calls = []
_set_thumb_calls = []
_ORIG_MAKE_THUMB = publish_loop.thumbnail.make_thumbnail_png
_ORIG_SET_THUMB = youtube.set_thumbnail


def _record_make_thumb(subject, title, out_png, **kw):
    _thumb_calls.append({"subject": subject, "title": title,
                         "out_png": str(out_png), **kw})
    return Path(out_png)


def _none_make_thumb(subject, title, out_png, **kw):
    _thumb_calls.append({"subject": subject, "title": title,
                         "out_png": str(out_png), **kw})
    return None


def _record_set_thumb(service, video_id, png_path):
    _set_thumb_calls.append((video_id, png_path))


def _raise_set_thumb(service, video_id, png_path):
    raise RuntimeError("403 channel not verified")


def _setup_thumb_case(fmt="short", overrides=None, topic_exists=True,
                      video_path="/tmp/x.mp4"):
    s = fresh_session()
    ch = make_channel(s)
    if topic_exists:
        t = Topic(channel_id=ch.id, name="Fmt", theme_prompt="x",
                  content_format=fmt)
        s.add(t)
        s.commit()
        s.refresh(t)
        tid = t.id
    else:
        tid = 999
    kw = dict(status=VideoStatus.APPROVED, topic_id=tid, video_path=video_path,
              title="The Title", subject="the subject")
    if overrides is not None:
        kw["overrides_json"] = overrides
    v = make_video(s, ch, **kw)
    return s, ch, v


publish_loop.thumbnail.make_thumbnail_png = _record_make_thumb
youtube.set_thumbnail = _record_set_thumb

_thumb_calls.clear()
_set_thumb_calls.clear()
s, ch, v = _setup_thumb_case("long")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytLONG")
ok(len(_thumb_calls) == 1, "long topic generates a thumbnail")
ok(_thumb_calls[0].get("content_format") == "long",
   "long topic forwards content_format=long (not the short default)")
ok(_thumb_calls[0].get("topic_id") == v.topic_id,
   "bound video's real topic_id is forwarded (not coerced to 0)")
ok(_thumb_calls[0]["subject"] == "the subject"
   and _thumb_calls[0]["title"] == "The Title",
   "hook inputs are the video's subject and title")
ok(_set_thumb_calls == [("ytLONG", "/tmp/thumb_custom.png")],
   "successful generation uploads the PNG as the YouTube thumbnail")
ok(v.thumb_path == "/tmp/thumb_custom.png",
   "video.thumb_path records the custom PNG")
thumb_rows = list(s.exec(select(JobRun).where(JobRun.kind == "thumbnail")))
ok(len(thumb_rows) == 1 and thumb_rows[0].status == "success",
   "success JobRun logged")
ok(thumb_rows[0].video_id == v.id and thumb_rows[0].channel_id == ch.id,
   "JobRun attributed to this video+channel")
ok(thumb_rows[0].quota_cost == youtube.QUOTA_THUMBNAIL_SET,
   "success logs the thumbnail-set quota cost")

_thumb_calls.clear()
_set_thumb_calls.clear()
s, ch, v = _setup_thumb_case("short")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytS")
ok(_thumb_calls[0].get("content_format") == "short",
   "short topic forwards content_format=short")

# THE BUG: overrides_json is the operator blob; render_loop never writes the
# topic format into it. A long topic with a leftover/empty/poisoned override
# must still get a long-form hook prompt.
_thumb_calls.clear()
s, ch, v = _setup_thumb_case("long", overrides='{"content_format": "short"}')
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytP")
ok(_thumb_calls[0].get("content_format") == "long",
   "overrides_json content_format does NOT win — topic is the source of truth")

_thumb_calls.clear()
s, ch, v = _setup_thumb_case("short", overrides='{"content_format": "long"}')
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytR")
ok(_thumb_calls[0].get("content_format") == "short",
   "a short topic stays short even if overrides_json claims long")

_thumb_calls.clear()
s, ch, v = _setup_thumb_case("long", overrides="not-json")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytJ")
ok(len(_thumb_calls) == 1 and _thumb_calls[0].get("content_format") == "long",
   "garbage overrides_json is ignored (must not skip generation)")

_thumb_calls.clear()
s, ch, v = _setup_thumb_case("long", topic_exists=False)
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytM")
ok(_thumb_calls[0].get("content_format") == "short",
   "missing topic falls back to short (same gate as render_loop)")

# The == "long" gate (not a raw forward of topic.content_format): PATCH can
# store "LONG"/"" because update_topic does not clamp. A raw-forward helper
# would send those strings into make_thumbnail_png; render/chapters would
# still treat the topic as short.
_thumb_calls.clear()
s, ch, v = _setup_thumb_case("LONG")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytCASE")
ok(_thumb_calls[0].get("content_format") == "short",
   "non-canonical 'LONG' is short (same == 'long' gate as render_loop)")
_thumb_calls.clear()
s, ch, v = _setup_thumb_case("")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytEMPTY")
ok(_thumb_calls[0].get("content_format") == "short",
   "empty topic content_format is short, not forwarded raw")

_thumb_calls.clear()
_set_thumb_calls.clear()
s, ch, v = _setup_thumb_case("long")
v.video_path = None
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytN")
ok(_thumb_calls == [] and _set_thumb_calls == [],
   "no video_path skips generation entirely")

publish_loop.thumbnail.make_thumbnail_png = _none_make_thumb
_thumb_calls.clear()
_set_thumb_calls.clear()
s, ch, v = _setup_thumb_case("long")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "ytF")
ok(_set_thumb_calls == [], "generation failure does not call set_thumbnail")
ok(v.thumb_path is None, "generation failure leaves thumb_path unset")
ok(v.status == VideoStatus.APPROVED,
   "generation failure does not flip the video status")
fail_rows = list(s.exec(select(JobRun).where(JobRun.kind == "thumbnail")))
ok(len(fail_rows) == 1 and fail_rows[0].status == "error",
   "generation failure logs a thumbnail error")
ok("generation failed" in (fail_rows[0].detail or ""),
   "error detail names generation failed")

publish_loop.thumbnail.make_thumbnail_png = _record_make_thumb
youtube.set_thumbnail = _raise_set_thumb
_thumb_calls.clear()
s, ch, v = _setup_thumb_case("short")
publish_loop._set_custom_thumbnail(s, object(), ch, v, "yt403")
ok(v.status == VideoStatus.APPROVED,
   "set_thumbnail 403 does not change video status")
ok(v.thumb_path is None, "failed upload leaves thumb_path unset")
err_rows = list(s.exec(select(JobRun).where(JobRun.kind == "thumbnail")))
ok(len(err_rows) == 1 and err_rows[0].status == "error",
   "set_thumbnail failure logs a thumbnail error")
ok("403" in (err_rows[0].detail or ""),
   "error detail carries the upload exception")

# Wiring: _publish_one actually calls the helper with the long-form topic.
# A helper that is never invoked would pass every unit check above.
publish_loop.thumbnail.make_thumbnail_png = _record_make_thumb
youtube.set_thumbnail = _record_set_thumb
youtube.get_service = _dummy_service
youtube.upload_video = _dummy_upload
youtube.insert_comment = lambda *a, **k: "c1"
youtube.create_playlist = _record_create
youtube.add_to_playlist = lambda *a, **k: "item1"
_thumb_calls.clear()
_set_thumb_calls.clear()
s = fresh_session()
ch = make_channel(s)
t = Topic(channel_id=ch.id, name="Longs", theme_prompt="x", content_format="long")
s.add(t)
s.commit()
s.refresh(t)
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=t.id,
               video_path="/tmp/x.mp4", title="T", subject="long subj")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.PUBLISHED,
   "thumbnail work never fails the publish itself")
ok(len(_thumb_calls) == 1,
   "_publish_one actually calls make_thumbnail_png (wiring)")
ok(_thumb_calls[0].get("content_format") == "long",
   "_publish_one long-form video gets a long-form thumbnail hook prompt")
ok(_set_thumb_calls and _set_thumb_calls[0][0] == "vid123",
   "uploaded thumbnail is attached to the new YouTube id")

# Failure through _publish_one (helper-level 403/gen-fail pins start APPROVED
# and never observe a PUBLISHED video). A mutant that FAILs the video when
# thumb_path is unset would survive those.
publish_loop.thumbnail.make_thumbnail_png = _none_make_thumb
youtube.set_thumbnail = _record_set_thumb
_thumb_calls.clear()
_set_thumb_calls.clear()
s = fresh_session()
ch = make_channel(s)
t = Topic(channel_id=ch.id, name="Longs", theme_prompt="x", content_format="long")
s.add(t)
s.commit()
s.refresh(t)
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=t.id,
               video_path="/tmp/x.mp4", title="T", subject="long subj")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.PUBLISHED,
   "_publish_one still PUBLISHED when thumbnail generation returns None")
ok(_set_thumb_calls == [],
   "failed generation does not call set_thumbnail on the publish path")
ok(v.yt_video_id == "vid123", "upload id is kept after a thumbnail miss")

publish_loop.thumbnail.make_thumbnail_png = _record_make_thumb
youtube.set_thumbnail = _raise_set_thumb
_thumb_calls.clear()
s = fresh_session()
ch = make_channel(s)
t = Topic(channel_id=ch.id, name="Longs", theme_prompt="x", content_format="long")
s.add(t)
s.commit()
s.refresh(t)
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=t.id,
               video_path="/tmp/x.mp4", title="T", subject="long subj")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.PUBLISHED,
   "_publish_one still PUBLISHED when set_thumbnail 403s")
ok(v.yt_video_id == "vid123", "upload id is kept after a thumbnail 403")

publish_loop.thumbnail.make_thumbnail_png = _ORIG_MAKE_THUMB
youtube.set_thumbnail = _ORIG_SET_THUMB
youtube.get_service, youtube.upload_video = _ORIG_GET, _ORIG_UPLOAD
youtube.insert_comment = _ORIG_COMMENT
youtube.create_playlist, youtube.add_to_playlist = _ORIG_CREATE, _ORIG_ADD

# --- first comment: language + playlist series pointer, never fails publish ---
# The author first-comment is the engagement seed (_publish_one posts it after
# a successful upload). Helpers and tick() skips had coverage; this path had
# none — a dropped insert_comment, a hardcoded EN phrase, or a comment 403
# flipping the video FAILED would all have shipped.
print("\n_first_comment_text: language prefix + playlist series pointer")
EN = "What would you change here? I read every comment."
PT = "O que você mudaria nesse setup? Leio todos os comentários."
EN_SERIES = "▶ Full series:"
PT_SERIES = "▶ Série completa:"
PL_ID = "PLLdeDcM9G5vY"

ok(publish_loop._first_comment_text(None, None) == EN,
   "None language -> English (the fallback)")
ok(publish_loop._first_comment_text("", None) == EN,
   "empty language -> English")
ok(publish_loop._first_comment_text("en", None) == EN, "en -> English")
ok(publish_loop._first_comment_text("en-US", None) == EN,
   "en-US uses the BCP-47 prefix, not the whole tag")
ok(publish_loop._first_comment_text("pt", None) == PT, "pt -> Portuguese")
ok(publish_loop._first_comment_text("pt-BR", None) == PT,
   "pt-BR uses the prefix (the live ch2 voice tag)")
ok(publish_loop._first_comment_text("PT-BR", None) == PT,
   "language match is case-insensitive")
ok(publish_loop._first_comment_text("es", None) == EN,
   "unknown language (es) falls back to English, not empty/raise")

text_pl = publish_loop._first_comment_text("en", PL_ID)
ok(text_pl.startswith(EN + "\n"), "playlist pointer is appended after the ask")
ok(EN_SERIES in text_pl, "English series cue is used for en")
ok(f"https://www.youtube.com/playlist?list={PL_ID}" in text_pl,
   "series pointer is the playlist URL (list=), not a watch URL")
ok(PT_SERIES not in text_pl, "English comment does not carry the PT series cue")

text_pt_pl = publish_loop._first_comment_text("pt-BR", PL_ID)
ok(text_pt_pl.startswith(PT + "\n"), "pt-BR + playlist keeps the PT ask")
ok(PT_SERIES in text_pt_pl, "pt-BR uses the PT series cue, not the EN one")
ok(f"list={PL_ID}" in text_pt_pl, "pt-BR series pointer still carries the yt id")

ok(publish_loop._first_comment_text("en", None) == EN,
   "no playlist id -> no series line")
ok(publish_loop._first_comment_text("en", "") == EN,
   "empty playlist id is falsy -> no series line")

print("_publish_one: first comment is posted, best-effort, never fails the publish")
_comments = []
_ORIG_LANG = publish_loop.video_gen.channel_language_code
_ORIG_MAKE_THUMB_C = publish_loop.thumbnail.make_thumbnail_png


def _record_comment(service, video_id, text):
    _comments.append((video_id, text))
    return "cmt1"


def _raise_comment(service, video_id, text):
    _comments.append((video_id, text))
    raise RuntimeError("commentsDisabled")


publish_loop.thumbnail.make_thumbnail_png = lambda *a, **k: None
youtube.get_service = _dummy_service
youtube.upload_video = _dummy_upload
# Raise so ensure_topic_playlist cannot mint a playlist — this case pins
# the no-series comment. (A succeeding create would append the series URL
# and the EN-only pin would be vacuously false.)
def _no_playlist(*a, **k):
    raise RuntimeError("no playlist")


youtube.create_playlist = _no_playlist
youtube.add_to_playlist = lambda *a, **k: "item1"
youtube.insert_comment = _record_comment
publish_loop.video_gen.channel_language_code = lambda *a, **k: None

_comments.clear()
s = fresh_session()
ch = make_channel(s)
t = Topic(channel_id=ch.id, name="Shorts", theme_prompt="x")
s.add(t); s.commit(); s.refresh(t)
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=t.id,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.PUBLISHED, "comment path still publishes")
ok(len(_comments) == 1, "_publish_one actually calls insert_comment (wiring)")
ok(_comments[0][0] == "vid123", "comment is posted on the new YouTube id")
ok(_comments[0][1] == EN, "no language profile + no playlist -> English ask, no series line")
cmt_rows = list(s.exec(select(JobRun).where(JobRun.kind == "comment")))
ok(len(cmt_rows) == 1 and cmt_rows[0].status == "success",
   "success JobRun logged for the comment")
ok(cmt_rows[0].video_id == v.id and cmt_rows[0].channel_id == ch.id,
   "comment JobRun attributed to this video+channel")
ok(cmt_rows[0].quota_cost == youtube.QUOTA_COMMENT_INSERT,
   "success logs the comment-insert quota cost")

# pt-BR + stored playlist: the series pointer must ride the same comment.
publish_loop.video_gen.channel_language_code = lambda *a, **k: "pt-BR"
_comments.clear()
s = fresh_session()
ch = make_channel(s)
t = Topic(channel_id=ch.id, name="Série", theme_prompt="x")
s.add(t); s.commit(); s.refresh(t)
pl = Playlist(channel_id=ch.id, yt_playlist_id=PL_ID, title="Série")
s.add(pl); s.commit(); s.refresh(pl)
t.playlist_id = pl.id
s.add(t); s.commit()
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=t.id,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.PUBLISHED, "pt-BR + playlist still publishes")
ok(len(_comments) == 1, "one comment posted")
ok(_comments[0][1] == publish_loop._first_comment_text("pt-BR", PL_ID),
   "comment text is _first_comment_text(pt-BR, stored playlist id)")
ok(PT in _comments[0][1] and PT_SERIES in _comments[0][1],
   "live ch2 shape: PT ask + PT series cue")
ok(f"list={PL_ID}" in _comments[0][1],
   "stored 13-char playlist id is the series pointer (not a newly minted one)")

# A comment 403 (commentsDisabled, quota, …) must not FAIL the video — same
# best-effort contract as thumbnails.
youtube.insert_comment = _raise_comment
publish_loop.video_gen.channel_language_code = lambda *a, **k: "en"
_comments.clear()
s = fresh_session()
ch = make_channel(s)
t = Topic(channel_id=ch.id, name="Shorts", theme_prompt="x")
s.add(t); s.commit(); s.refresh(t)
v = make_video(s, ch, status=VideoStatus.APPROVED, topic_id=t.id,
               video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch, v)
ok(v.status == VideoStatus.PUBLISHED,
   "_publish_one still PUBLISHED when insert_comment raises")
ok(v.yt_video_id == "vid123", "upload id is kept after a comment failure")
err_cmt = list(s.exec(select(JobRun).where(JobRun.kind == "comment")))
ok(len(err_cmt) == 1 and err_cmt[0].status == "error",
   "comment failure logs a comment error JobRun")
ok("commentsDisabled" in (err_cmt[0].detail or ""),
   "error detail carries the insert exception")

youtube.insert_comment = _ORIG_COMMENT
publish_loop.video_gen.channel_language_code = _ORIG_LANG
publish_loop.thumbnail.make_thumbnail_png = _ORIG_MAKE_THUMB_C
youtube.get_service, youtube.upload_video = _ORIG_GET, _ORIG_UPLOAD
youtube.create_playlist, youtube.add_to_playlist = _ORIG_CREATE, _ORIG_ADD

# --- tick() remaining skip gates (helpers were tested; tick wiring was not) ---
# The 07-26 hung-upload pile-up, a paused scheduler that still recovered
# stalled publishes, and a paused channel that still dripped were all
# tick()-level bugs that helper-only checks cannot catch.
print("\ntick: remaining skip gates (paused / oauth / cooldown / budget / cap / in-flight / drip)")

_tick_services = []
_tick_uploads = []
_ORIG_SCOPE_T = publish_loop.session_scope
_ORIG_GET_T, _ORIG_UPLOAD_T = youtube.get_service, youtube.upload_video
_ORIG_COMMENT_T = youtube.insert_comment
_ORIG_CREATE_T, _ORIG_ADD_T = youtube.create_playlist, youtube.add_to_playlist
_ORIG_MAKE_THUMB_T = publish_loop.thumbnail.make_thumbnail_png
_CAP = settings.youtube_daily_quota_cap
_COST = (youtube.QUOTA_UPLOAD + youtube.QUOTA_THUMBNAIL_SET
         + youtube.QUOTA_COMMENT_INSERT)


def _arm_tick():
    _tick_services.clear()
    _tick_uploads.clear()
    youtube.get_service = lambda slug: (_tick_services.append(slug) or object())
    youtube.upload_video = lambda *a, **k: (_tick_uploads.append(a[2] if len(a) > 2 else k.get("title")) or "vidT")
    youtube.insert_comment = lambda *a, **k: "c1"
    youtube.create_playlist = lambda *a, **k: {
        "yt_playlist_id": "PL" + "T" * 32, "title": "t",
        "description": "", "privacy": "public"}
    youtube.add_to_playlist = lambda *a, **k: "item1"
    publish_loop.thumbnail.make_thumbnail_png = lambda *a, **k: None


def _disarm_tick():
    publish_loop.session_scope = _ORIG_SCOPE_T
    youtube.get_service, youtube.upload_video = _ORIG_GET_T, _ORIG_UPLOAD_T
    youtube.insert_comment = _ORIG_COMMENT_T
    youtube.create_playlist, youtube.add_to_playlist = _ORIG_CREATE_T, _ORIG_ADD_T
    publish_loop.thumbnail.make_thumbnail_png = _ORIG_MAKE_THUMB_T


def _ready(session, title="ready", **ch_kw):
    app_settings(session)
    ch = make_channel(session, oauth_status=ch_kw.pop("oauth_status", OAuthStatus.CONNECTED),
                      **ch_kw)
    topic = Topic(channel_id=ch.id, name="Tick", theme_prompt="x")
    session.add(topic)
    session.commit()
    session.refresh(topic)
    v = make_video(session, ch, status=VideoStatus.APPROVED, topic_id=topic.id,
                   video_path="/tmp/x.mp4", title=title)
    return ch, v


try:
    _arm_tick()

    # scheduler_paused returns BEFORE recovery — a stuck upload must stay put.
    s = fresh_session()
    cfg = app_settings(s)
    cfg.scheduler_paused = True
    s.add(cfg)
    s.commit()
    ch, v = _ready(s, title="paused-sched")
    stuck = make_video(s, ch, status=VideoStatus.PUBLISHING, topic_id=v.topic_id,
                       video_path="/tmp/x.mp4", title="stuck",
                       retry_count=0,
                       last_attempt_at=utcnow() - timedelta(seconds=TIMEOUT + 60))

    @contextmanager
    def _scope_paused():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_paused
    publish_loop.tick()
    s.refresh(v)
    s.refresh(stuck)
    # Recovery pin FIRST: a `pass` instead of `return` recovers the stuck
    # row AND then publishes (get_service fires too). Checking services
    # first would hide the early-return claim behind the skip-publish pin.
    ok(stuck.status == VideoStatus.PUBLISHING,
       "scheduler_paused: returns before recover_stuck_publishing")
    ok(_tick_services == [] and _tick_uploads == [],
       "scheduler_paused: tick opens no YouTube service")
    ok(v.status == VideoStatus.APPROVED,
       "scheduler_paused: approved work is not published")

    cfg.scheduler_paused = False
    s.add(cfg)
    s.commit()
    _tick_services.clear()
    _tick_uploads.clear()
    publish_loop.tick()
    s.refresh(v)
    s.refresh(stuck)
    ok(stuck.status == VideoStatus.APPROVED,
       "unpaused: the stuck upload is recovered (proves the pause pin was the early return)")
    ok("paused-sched" in _tick_uploads or "stuck" in _tick_uploads,
       "unpaused: tick publishes (recovery + approved pool)")

    # Channel.paused: skip this channel, sibling still publishes.
    _tick_services.clear()
    _tick_uploads.clear()
    s = fresh_session()
    app_settings(s)
    ch_p, v_p = _ready(s, title="is-paused", slug="paused-ch", paused=True)
    ch_l, v_l = _ready(s, title="is-live", slug="live-ch")

    @contextmanager
    def _scope_chpause():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_chpause
    publish_loop.tick()
    s.refresh(v_p)
    s.refresh(v_l)
    ok(v_p.status == VideoStatus.APPROVED, "paused channel is not published")
    ok(v_l.status == VideoStatus.PUBLISHED, "unpaused sibling still publishes")
    ok("paused-ch" not in _tick_services,
       "paused channel never even opens get_service")
    ok("live-ch" in _tick_services,
       "unpaused sibling does open get_service (sibling isolation)")

    # Dead oauth_status values skip; CONNECTED publishes. Per-status so a
    # mutant that only special-cases EXPIRED still dies.
    for dead in (OAuthStatus.EXPIRED, OAuthStatus.DISCONNECTED, OAuthStatus.ERROR):
        _tick_services.clear()
        _tick_uploads.clear()
        s = fresh_session()
        app_settings(s)
        ch_d, v_d = _ready(s, title="dead", slug=f"dead-{dead}", oauth_status=dead)
        ch_ok, v_ok = _ready(s, title="ok", slug=f"ok-{dead}")

        @contextmanager
        def _scope_oauth(_s=s):
            yield _s
            _s.commit()

        publish_loop.session_scope = _scope_oauth
        publish_loop.tick()
        s.refresh(v_d)
        s.refresh(v_ok)
        ok(v_d.status == VideoStatus.APPROVED,
           f"{dead} channel is not published")
        ok(v_ok.status == VideoStatus.PUBLISHED,
           f"CONNECTED sibling still publishes next to {dead}")
        ok(f"dead-{dead}" not in _tick_services,
           f"{dead} never opens get_service")
        ok(f"ok-{dead}" in _tick_services,
           f"CONNECTED sibling next to {dead} does open get_service")

    # Cooldown: future until skips; SQLite stores naive so the tzinfo-is-None
    # branch actually runs (the 08-02 aware-leg lesson).
    _tick_services.clear()
    _tick_uploads.clear()
    s = fresh_session()
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    ch, v = _ready(s, title="cooling", slug="cool-ch", cooldown_until=future)
    s.refresh(ch)
    ok(ch.cooldown_until is not None and ch.cooldown_until.tzinfo is None,
       "sqlite stored cooldown_until naive (the tick() naive branch is the one that runs)")

    @contextmanager
    def _scope_cd():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_cd
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.APPROVED, "future cooldown: tick does not publish")
    ok(_tick_services == [], "future cooldown: get_service not opened")

    ch.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    s.add(ch)
    s.commit()
    _tick_services.clear()
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.PUBLISHED, "expired cooldown: tick publishes")

    # daily_limit_hit is checked at tick, not only via the helper.
    _tick_services.clear()
    s = fresh_session()
    ch, v = _ready(s, title="capped", slug="cap-ch")
    quota.log(s, kind="publish", status="error", channel_id=ch.id,
              detail="quota exceeded: [quotaExceeded] cooldown until ...")
    s.commit()

    @contextmanager
    def _scope_lim():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_lim
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.APPROVED,
       "daily_limit_hit at tick skips the publish (helper-only pin would miss a dropped call)")
    ok(_tick_services == [], "daily_limit_hit: get_service not opened")

    # Daily budget: published_today >= budget. quota_cost=0 so the quota-cap
    # gate cannot be the one that fires.
    _tick_services.clear()
    s = fresh_session()
    ch, v = _ready(s, title="budgeted", slug="bud-ch", daily_publish_budget=1)
    # Older than the drip window so a dropped budget check cannot hide behind drip.
    s.add(JobRun(kind="publish", status="success", channel_id=ch.id, quota_cost=0,
                 created_at=utcnow() - timedelta(minutes=40)))
    s.commit()
    ok(quota.published_today(s, ch.id) >= 1, "precondition: budget is spent")
    ok(publish_loop._drip_ok(s, ch, app_settings(s).publish_drip_minutes) is True,
       "precondition: drip is open (budget must be the gate that fires)")

    @contextmanager
    def _scope_bud():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_bud
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.APPROVED, "spent daily_publish_budget: tick skips")
    ok(_tick_services == [], "spent budget: get_service not opened")

    # Quota-cap headroom uses `>` not `>=`: spent + per-publish cost == cap
    # must still publish; one unit over must skip.
    _tick_services.clear()
    s = fresh_session()
    ch, v = _ready(s, title="at-cap", slug="eq-ch")
    s.add(JobRun(kind="publish", status="success", channel_id=ch.id,
                 quota_cost=_CAP - _COST,
                 created_at=utcnow() - timedelta(minutes=40)))
    s.commit()
    ok(quota.quota_spent_today(s, ch.id) + _COST == _CAP,
       "precondition: spent + this publish == cap (the `>` boundary)")
    ok(publish_loop._drip_ok(s, ch, app_settings(s).publish_drip_minutes) is True,
       "precondition: drip is open (cap equality must be the observed gate)")

    @contextmanager
    def _scope_eq():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_eq
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.PUBLISHED,
       "spent + cost == cap still publishes (`>` not `>=`)")

    _tick_services.clear()
    s = fresh_session()
    ch, v = _ready(s, title="over-cap", slug="gt-ch")
    s.add(JobRun(kind="publish", status="success", channel_id=ch.id,
                 quota_cost=_CAP - _COST + 1,
                 created_at=utcnow() - timedelta(minutes=40)))
    s.commit()
    ok(publish_loop._drip_ok(s, ch, app_settings(s).publish_drip_minutes) is True,
       "precondition: drip is open (quota-cap must be the gate that fires)")

    @contextmanager
    def _scope_gt():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_gt
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.APPROVED,
       "spent + cost > cap: tick skips")
    ok(_tick_services == [], "over-cap: get_service not opened")

    # In-flight guard: a PUBLISHING video inside the timeout blocks the
    # channel (the hung-upload pile-up). Must be recent so recovery doesn't
    # flip it first. Sibling channel still publishes.
    _tick_services.clear()
    _tick_uploads.clear()
    s = fresh_session()
    app_settings(s)
    ch_a, v_a = _ready(s, title="waiting", slug="inflight-a")
    inflight = make_video(s, ch_a, status=VideoStatus.PUBLISHING,
                          topic_id=v_a.topic_id, video_path="/tmp/x.mp4",
                          title="flying", retry_count=0,
                          last_attempt_at=utcnow() - timedelta(seconds=5))
    ch_b, v_b = _ready(s, title="other", slug="inflight-b")

    @contextmanager
    def _scope_if():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_if
    publish_loop.tick()
    s.refresh(v_a)
    s.refresh(inflight)
    s.refresh(v_b)
    ok(inflight.status == VideoStatus.PUBLISHING,
       "in-flight inside timeout is not recovered")
    ok(v_a.status == VideoStatus.APPROVED,
       "in-flight channel does not start a second upload")
    ok("inflight-a" not in _tick_services,
       "in-flight channel never opens get_service")
    ok("inflight-b" in _tick_services,
       "in-flight skip is per-channel (global count would also skip the sibling)")
    ok(v_b.status == VideoStatus.PUBLISHED,
       "sibling channel is not blocked by the other channel's in-flight")

    # Drip at tick(), not only _drip_ok: a recent publish JobRun blocks.
    _tick_services.clear()
    s = fresh_session()
    ch, v = _ready(s, title="dripped", slug="drip-ch")
    s.add(JobRun(kind="publish", status="success", channel_id=ch.id, quota_cost=0,
                 created_at=utcnow()))
    # Budget default is 6, so published_today=1 must not be the budget gate.
    ch.daily_publish_budget = 6
    s.add(ch)
    s.commit()
    ok(publish_loop._drip_ok(s, ch, app_settings(s).publish_drip_minutes) is False,
       "precondition: drip window is closed")

    @contextmanager
    def _scope_drip():
        yield s
        s.commit()

    publish_loop.session_scope = _scope_drip
    publish_loop.tick()
    s.refresh(v)
    ok(v.status == VideoStatus.APPROVED,
       "closed drip window at tick skips (a dropped _drip_ok call would publish)")
    ok(_tick_services == [], "drip skip: get_service not opened")

finally:
    _disarm_tick()

# Defect: publish_plan and dashboard _next_publish_eta clamped daily_limit
# with max(1, min(budget, cap//upload)). tick() uses
# published_today >= daily_publish_budget, so budget 0 (and negative) skip
# every tick — issues.py already treats 0 as "the channel isn't trying to
# publish". The clamp still handed the board / growth agent an ETA. Without
# the early return the plan still drains one video per future quota day
# (not a hang) and the dashboard emits tomorrow's window.
print("publish-plan / dashboard ETA: daily_publish_budget<=0 means no ETAs")
from app.routers.queue import _next_publish_eta  # noqa: E402
from app.routers import queue as queue_router, videos as videos_router  # noqa: E402

ok(Path(videos_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "videos module loaded from this tree")
ok(Path(queue_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "queue module loaded from this tree")

s = fresh_session()
cfg = app_settings(s)
parked = make_channel(s, slug="parked-pub", daily_publish_budget=0)
live = make_channel(s, slug="live-pub", daily_publish_budget=1)
neg = make_channel(s, slug="neg-pub", daily_publish_budget=-1)
v_parked = make_video(s, parked, subject="parked approved",
                      status=VideoStatus.APPROVED, video_path="/tmp/p.mp4",
                      title="P", approved_at=utcnow())
v_live = make_video(s, live, subject="live approved",
                    status=VideoStatus.APPROVED, video_path="/tmp/l.mp4",
                    title="L", approved_at=utcnow())
make_video(s, neg, subject="neg approved",
           status=VideoStatus.APPROVED, video_path="/tmp/n.mp4",
           title="N", approved_at=utcnow())

ok(publish_plan(parked.id, s) == {},
   "budget=0 publish-plan is empty (max(1,) would still ETA; tick never publishes)")
ok(_next_publish_eta(s, parked, cfg) is None,
   "budget=0 dashboard ETA is None (max(1,) would claim a slot)")
ok(publish_plan(neg.id, s) == {},
   "budget=-1 publish-plan is empty (`if not budget` would miss negatives)")
ok(_next_publish_eta(s, neg, cfg) is None,
   "budget=-1 dashboard ETA is None")
plan_live = publish_plan(live.id, s)
ok(plan_live == {str(v_live.id): plan_live.get(str(v_live.id))}
   and plan_live.get(str(v_live.id)),
   "sibling budget=1 still gets an ETA (a global empty-plan short-circuit dies here)")
ok(_next_publish_eta(s, live, cfg) is not None,
   "sibling budget=1 dashboard ETA is set")
ok(str(v_parked.id) not in plan_live,
   "parked channel's video is absent from the sibling's plan")

# Defect (found shipping #27, not bundled): dashboard chipped a missing ETA
# only when Channel.paused. budget<=0 (and oauth-dead) also yield no ETA
# with approved work sitting there, so the card showed 0/0 and silence.
# _publish_hold is the reason the UI (and the growth agent) should read;
# dashboard() must actually put it on the payload (a helper nobody calls
# would leave the frontend guessing from paused again).
print("dashboard publish_hold: budget<=0 with approved work is labeled")
from app.routers.queue import _publish_hold, dashboard as dashboard_endpoint  # noqa: E402

ok(_publish_hold(parked, 1) == "budget",
   "budget=0 + approved → hold=budget (pre-fix UI only chipped paused)")
ok(_publish_hold(neg, 1) == "budget",
   "budget=-1 + approved → hold=budget (`== 0` would miss negatives)")
ok(_publish_hold(live, 1) is None,
   "sibling budget=1 + approved → no hold (ETA is the signal)")
ok(_publish_hold(parked, 0) is None,
   "budget=0 with nothing approved → no hold (nothing to explain)")

paused_hold = make_channel(s, slug="paused-hold", daily_publish_budget=0, paused=True)
ok(_publish_hold(paused_hold, 1) == "paused",
   "paused wins over budget=0 (operator pause is the reason)")
paused_live = make_channel(s, slug="paused-live", daily_publish_budget=5, paused=True)
ok(_publish_hold(paused_live, 1) == "paused",
   "paused + live budget → hold=paused (existing chip)")
oauth_dead = make_channel(s, slug="oauth-hold", daily_publish_budget=5,
                          oauth_status=OAuthStatus.EXPIRED)
ok(_publish_hold(oauth_dead, 1) == "oauth",
   "expired token + approved → hold=oauth (same missing-ETA hole)")
ok(_publish_hold(oauth_dead, 0) is None,
   "expired token with nothing approved → no hold")
oauth_disc = make_channel(s, slug="oauth-disc", daily_publish_budget=5,
                          oauth_status=OAuthStatus.DISCONNECTED)
ok(_publish_hold(oauth_disc, 1) == "oauth",
   "disconnected + approved → hold=oauth (not only EXPIRED)")
make_video(s, paused_hold, subject="paused-budget approved",
           status=VideoStatus.APPROVED, video_path="/tmp/pb.mp4",
           title="PB", approved_at=utcnow())

rows = dashboard_endpoint(s)
by_id = {r["channel"].id: r for r in rows}
ok(by_id[parked.id]["publish_hold"] == "budget",
   "dashboard payload: budget=0 channel carries publish_hold=budget")
ok(by_id[parked.id]["next_publish_eta"] is None,
   "dashboard payload: budget=0 next_publish_eta stays None")
ok(by_id[neg.id]["publish_hold"] == "budget",
   "dashboard payload: budget=-1 carries publish_hold=budget")
ok(by_id[live.id]["publish_hold"] is None
   and by_id[live.id]["next_publish_eta"] is not None,
   "dashboard payload: budget=1 sibling has no hold and still has an ETA")
ok(by_id[paused_hold.id]["publish_hold"] == "paused",
   "dashboard payload: paused+budget=0 carries paused (not budget)")
ok(by_id[oauth_dead.id]["publish_hold"] is None,
   "dashboard payload: expired with no approved work has no hold")
make_video(s, oauth_dead, subject="oauth approved",
           status=VideoStatus.APPROVED, video_path="/tmp/o.mp4",
           title="O", approved_at=utcnow())
rows = dashboard_endpoint(s)
by_id = {r["channel"].id: r for r in rows}
ok(by_id[oauth_dead.id]["publish_hold"] == "oauth",
   "dashboard payload: expired + approved carries publish_hold=oauth")
ok("publish_hold" in by_id[live.id],
   "dashboard payload always includes publish_hold (frontend must not re-derive)")

print(f"\nALL {_checks} CHECKS PASSED")
