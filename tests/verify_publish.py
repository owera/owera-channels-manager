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

print(f"\nALL {_checks} CHECKS PASSED")
