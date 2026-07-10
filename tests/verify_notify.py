"""Dependency-free regression checks for the OAuth-expiry alert (notify + wiring).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_notify.py

The 362691a fix made a revoked token flip the channel to EXPIRED, but the signal
stayed passive (issues digest only) — ch2 died silently for days. These checks
pin the push side: a CONNECTED -> EXPIRED flip emits exactly one alert with a
working reconnect recipe, repeated checks against a dead token stay silent, a
healthy check alerts nothing, and webhook delivery is inert-by-default and
best-effort (its failure can never break the caller).

Uses an in-memory SQLite DB, a capturing log handler, and stubbed YouTube /
httpx calls — no network, no creds. Exits non-zero on the first failure.
"""
import logging
import sys

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.models import Channel, OAuthStatus, Video, VideoStatus
from app.routers import channels as channels_router
from app.services import notify, publish_loop, youtube

# Safety: the operator's .env may carry a REAL webhook URL. Kill it before any
# alert can fire, so running this suite never pages anyone with fake alerts.
settings.alert_webhook_url = ""

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh_session() -> Session:
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


class CaptureAlerts(logging.Handler):
    """Collects ERROR records off manager.notify — one record == one alert."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []
        notify.logger.addHandler(self)

    def emit(self, record):
        self.records.append(record.getMessage())

    def close(self):
        notify.logger.removeHandler(self)
        super().close()


# --- oauth_expired: transition guard (exactly-once semantics) -----------------
print("oauth_expired: fires only on the CONNECTED -> EXPIRED transition")
s = fresh_session()
ch = make_channel(s)

cap = CaptureAlerts()
ok(notify.oauth_expired(ch, "invalid_grant", OAuthStatus.CONNECTED) is True,
   "prev=connected fires the alert")
ok(len(cap.records) == 1, "the fired alert logs exactly one ERROR record")
ok(notify.oauth_expired(ch, "invalid_grant", OAuthStatus.EXPIRED) is False,
   "prev=expired stays silent (already alerted)")
ok(notify.oauth_expired(ch, "invalid_grant", OAuthStatus.DISCONNECTED) is False,
   "prev=disconnected stays silent (operator action, not an expiry)")
ok(notify.oauth_expired(ch, "invalid_grant", None) is False,
   "prev=None stays silent")
ok(len(cap.records) == 1, "non-transition calls added no records")
cap.close()

# --- alert content: reconnect recipe honors MANAGER_PUBLIC_BASE_URL -----------
print("alert content: reconnect recipe + base-url derivation")
_orig_base = settings.public_base_url

settings.public_base_url = ""
recipe = notify.reconnect_recipe(ch.id)
ok(f"http://localhost:{settings.port}/api/channels/{ch.id}/oauth/start" in recipe,
   "unset base url -> reconnect recipe points at localhost:port")

settings.public_base_url = "http://localhost:7070/"
recipe = notify.reconnect_recipe(ch.id)
ok(f"http://localhost:7070/api/channels/{ch.id}/oauth/start" in recipe,
   "set base url -> reconnect recipe uses it (trailing slash normalized)")

cap = CaptureAlerts()
notify.oauth_expired(ch, "invalid_grant", OAuthStatus.CONNECTED)
msg = cap.records[0]
ok(ch.name in msg and "invalid_grant" in msg, "alert names the channel and the error")
ok("/oauth/start" in msg, "alert carries the reconnect recipe")
cap.close()
settings.public_base_url = _orig_base

# --- webhook: inert until configured, best-effort when it is ------------------
print("webhook: inert by default, posts JSON when configured, never raises")
_orig_post = notify.httpx.post
_posts = []


class _OkResponse:
    def raise_for_status(self):
        return self


def _capture_post(url, json=None, timeout=None):
    _posts.append((url, json))
    return _OkResponse()


notify.httpx.post = _capture_post

settings.alert_webhook_url = ""
notify.oauth_expired(ch, "e", OAuthStatus.CONNECTED)
ok(_posts == [], "no webhook url -> nothing posted (log-only)")

settings.alert_webhook_url = "http://hook.test/alert"
notify.oauth_expired(ch, "invalid_grant", OAuthStatus.CONNECTED)
ok(len(_posts) == 1 and _posts[0][0] == "http://hook.test/alert",
   "configured url -> exactly one POST to it")
payload = _posts[0][1]
ok(payload["event"] == "oauth_expired" and payload["channel_id"] == ch.id,
   "payload identifies the event and channel")
ok(payload.get("text") == payload.get("content") and "/oauth/start" in payload["reconnect"],
   "payload has Slack 'text' + Discord 'content' keys and the reconnect recipe")


def _boom(url, json=None, timeout=None):
    raise OSError("connection refused")


notify.httpx.post = _boom
fired = notify.oauth_expired(ch, "e", OAuthStatus.CONNECTED)
ok(fired is True, "a webhook delivery failure is swallowed — the alert still fires")

notify.httpx.post = _orig_post
settings.alert_webhook_url = ""

# --- never-raise: alerting must not break the publish path --------------------
print("never-raise: a broken alert can't propagate into the caller")


class _Exploding:
    """Channel stand-in whose attribute access blows up mid-alert."""
    @property
    def id(self):
        raise RuntimeError("detached instance")
    name = "Boom"


try:
    fired = notify.oauth_expired(_Exploding(), "e", OAuthStatus.CONNECTED)
    raised = False
except Exception:
    raised = True
ok(raised is False, "an exception inside the alert body is swallowed, not propagated")

# --- wiring: publish loop alerts once when the token dies mid-drip ------------
print("publish_loop wiring: NeedsConnect alerts exactly once")
_orig_get = youtube.get_service


def _raise_needs_connect(slug):
    raise youtube.NeedsConnect("token missing/expired — reconnect required")


youtube.get_service = _raise_needs_connect
s = fresh_session()
ch = make_channel(s, oauth_status=OAuthStatus.CONNECTED)
v = make_video(s, ch, status=VideoStatus.APPROVED, video_path="/tmp/x.mp4", title="T")

cap = CaptureAlerts()
publish_loop._publish_one(s, ch, v)
ok(ch.oauth_status == OAuthStatus.EXPIRED, "revoked token still flips the channel to EXPIRED")
ok(len(cap.records) == 1, "the flip emits exactly one alert")
publish_loop._publish_one(s, ch, v)   # channel already expired (e.g. manual retry)
ok(len(cap.records) == 1, "a repeat NeedsConnect on the dead channel adds no alert")
cap.close()
youtube.get_service = _orig_get

# --- wiring: oauth-status route alerts on discovery, silent when healthy ------
print("oauth-status route wiring: alert on discovery, silence on health/repeat")
_orig_get, _orig_has = youtube.get_service, youtube.has_token

s = fresh_session()
ch = make_channel(s, oauth_status=OAuthStatus.CONNECTED)

youtube.get_service = _raise_needs_connect
youtube.has_token = lambda slug: True
cap = CaptureAlerts()
out = channels_router.oauth_status(ch.id, session=s)
ok(out["oauth_status"] == OAuthStatus.EXPIRED, "dashboard check flips a dead token to EXPIRED")
ok(len(cap.records) == 1, "the dashboard-discovered flip emits exactly one alert")
s.refresh(ch)
ok(ch.oauth_status == OAuthStatus.EXPIRED,
   "the flip is committed before the alert fires (alert can't gate persistence)")
channels_router.oauth_status(ch.id, session=s)
ok(len(cap.records) == 1, "re-checking the already-dead channel stays silent")

youtube.get_service = lambda slug: object()
ch2 = make_channel(s, slug="ch-ok", name="Healthy")
channels_router.oauth_status(ch2.id, session=s)
ok(len(cap.records) == 1, "a healthy check alerts nothing")
cap.close()
youtube.get_service, youtube.has_token = _orig_get, _orig_has

print(f"\nALL {_checks} CHECKS PASSED")
