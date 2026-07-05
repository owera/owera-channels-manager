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
from app.models import Channel, JobRun, OAuthStatus, Video, VideoStatus, utcnow
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
publish_loop._publish_one(s, ch, v)
ok(ch.oauth_status == OAuthStatus.EXPIRED, "revoked token flips the channel to EXPIRED")
ok(v.status == VideoStatus.APPROVED, "video returns to approved, not stranded in publishing")

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

print(f"\nALL {_checks} CHECKS PASSED")
