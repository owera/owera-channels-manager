"""Dependency-free regression checks for the publish path (publish_loop + quota).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_publish.py

Covers the failure modes that actually hit production, so they can't silently
regress:
  - stuck-'publishing' recovery + retry cap (the mislabeled ch2 "stalls")
  - a revoked OAuth token flips the channel to EXPIRED and returns the video to
    'approved' — never stranded in 'publishing' (the 362691a fix)
  - upload-stall retry-then-fail, quota-exceeded cooldown, and drip spacing

Uses an in-memory SQLite DB and stubs the YouTube calls — no network, no creds.
Exits non-zero on the first failed assertion.
"""
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

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

print(f"\nALL {_checks} CHECKS PASSED")
