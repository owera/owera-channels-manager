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
from datetime import timedelta

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
