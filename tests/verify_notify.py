"""Dependency-free regression checks for the dead-channel alert choke point.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_notify.py

The 362691a fix made a revoked token flip the channel to EXPIRED, but the signal
stayed passive (issues digest only) — ch2 died silently for days. These checks
pin the push side: notify.mark_dead()/mark_dead_committed() are the single
choke point for CONNECTED -> dead transitions, emitting exactly one alert per
incident (with a working reconnect recipe) from every discovery site — publish
loop, metrics/analytics loops, admin 409s, playlist endpoints, the oauth-status
probe, and failed consents — while repeated checks against an already-dead
channel, operator disconnects, transient failures, scope-only analytics gaps,
and stale/replayed OAuth callbacks all stay silent. The alert never precedes
the commit, and webhook delivery is inert-by-default and best-effort (its
failure can never break the caller).

Uses an in-memory SQLite DB, a capturing log handler, and stubbed YouTube /
httpx calls — no network, no creds. Exits non-zero on the first failure.
"""
import logging
import sys
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.models import Channel, OAuthStatus, Video, VideoStatus
from app.routers import channels as channels_router
from app.routers import playlists as playlists_router
from app.routers import youtube_admin
from app.services import analytics_loop, metrics_loop, notify, publish_loop, youtube

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


def _raise_needs_connect(slug):
    raise youtube.NeedsConnect("token missing/expired — reconnect required")


# --- mark_dead: the one choke point (assign + guard + alert together) ---------
print("mark_dead: assigns state, alerts once per incident, every dead status")
s = fresh_session()
ch = make_channel(s, slug="ch-choke")

cap = CaptureAlerts()
ok(notify.mark_dead(ch, "x" * 500, status=OAuthStatus.EXPIRED) is True,
   "CONNECTED -> EXPIRED reports a genuine flip")
ok(ch.oauth_status == OAuthStatus.EXPIRED, "mark_dead assigns the new status")
ok(ch.oauth_error == "x" * 300, "mark_dead truncates the stored error to 300 chars")
ok(len(cap.records) == 1, "the flip alerts exactly once")
ok(notify.mark_dead(ch, "x", status=OAuthStatus.EXPIRED) is False,
   "EXPIRED -> EXPIRED is not a flip (already alerted)")
ok(len(cap.records) == 1, "and adds no repeat alert")

ch.oauth_status = OAuthStatus.CONNECTED
ok(notify.mark_dead(ch, "consent denied", status=OAuthStatus.ERROR) is True,
   "CONNECTED -> ERROR flips too (a failed consent halts publishing as well)")
ch.oauth_status = OAuthStatus.CONNECTED
ok(notify.mark_dead(ch, "token file gone", status=OAuthStatus.DISCONNECTED) is True,
   "CONNECTED -> DISCONNECTED flips too (token-file loss)")
ok(len(cap.records) == 3, "every dead status alerts")

ch.oauth_status = OAuthStatus.CONNECTED
ok(notify.mark_dead(ch, "e", status=OAuthStatus.EXPIRED, alert=False) is True
   and len(cap.records) == 3,
   "alert=False reports the flip but defers the alert to the caller")
notify.alert_dead(ch, "e")
ok(len(cap.records) == 4, "alert_dead then fires the deferred alert")
cap.close()

# --- mark_dead_committed: the alert never precedes durability -----------------
print("mark_dead_committed: flip commits first; a failed commit alerts nothing")
s = fresh_session()
ch = make_channel(s, slug="ch-durable")

cap = CaptureAlerts()
ok(notify.mark_dead_committed(s, ch, "revoked", status=OAuthStatus.EXPIRED) is True,
   "CONNECTED -> EXPIRED via the committed path reports the flip")
s.refresh(ch)
ok(ch.oauth_status == OAuthStatus.EXPIRED, "the flip is durable (committed)")
ok(len(cap.records) == 1, "and alerted exactly once")


class _FailingSession:
    def add(self, obj):
        pass

    def commit(self):
        raise RuntimeError("database is locked")


ch.oauth_status = OAuthStatus.CONNECTED
try:
    notify.mark_dead_committed(_FailingSession(), ch, "e", status=OAuthStatus.EXPIRED)
    raised = False
except RuntimeError:
    raised = True
ok(raised is True, "a failed commit propagates to the caller (rollback semantics)")
ok(len(cap.records) == 1, "and sends NO alert — the guard stays armed for the retry")
cap.close()

# --- dead_status_for: classification of *how* dead -----------------------------
print("dead_status_for: EXPIRED with a token file, DISCONNECTED without")
_orig_has = youtube.has_token
youtube.has_token = lambda slug: True
ok(notify.dead_status_for("any") == OAuthStatus.EXPIRED,
   "token file present -> EXPIRED (revoked/unrefreshable)")
youtube.has_token = lambda slug: False
ok(notify.dead_status_for("any") == OAuthStatus.DISCONNECTED,
   "token file gone -> DISCONNECTED")
youtube.has_token = _orig_has

# --- alert content: reconnect recipe honors MANAGER_PUBLIC_BASE_URL -----------
print("alert content: reconnect recipe + base-url derivation")
s = fresh_session()
ch = make_channel(s, slug="ch-content")
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
notify.mark_dead(ch, "invalid_grant", status=OAuthStatus.EXPIRED)
msg = cap.records[0]
ok(ch.name in msg and "invalid_grant" in msg, "alert names the channel and the error")
ok("/oauth/start" in msg, "alert carries the reconnect recipe")
ok("revoked/expired" in msg, "alert phrase matches the EXPIRED classification")
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
ch.oauth_status = OAuthStatus.CONNECTED
notify.mark_dead(ch, "e", status=OAuthStatus.EXPIRED)
ok(_posts == [], "no webhook url -> nothing posted (log-only)")

settings.alert_webhook_url = "http://hook.test/alert"
ch.oauth_status = OAuthStatus.CONNECTED
notify.mark_dead(ch, "invalid_grant", status=OAuthStatus.EXPIRED)
ok(len(_posts) == 1 and _posts[0][0] == "http://hook.test/alert",
   "configured url -> exactly one POST to it")
payload = _posts[0][1]
ok(payload["event"] == "oauth_expired" and payload["channel_id"] == ch.id,
   "payload identifies the event and channel")
ok(payload.get("text") == payload.get("content") and "/oauth/start" in payload["reconnect"],
   "payload has Slack 'text' + Discord 'content' keys and the reconnect recipe")

ch.oauth_status = OAuthStatus.CONNECTED
notify.mark_dead(ch, "gone", status=OAuthStatus.DISCONNECTED)
ok(_posts[-1][1]["event"] == "oauth_disconnected",
   "the webhook event names the dead status it reports")


def _boom(url, json=None, timeout=None):
    raise OSError("connection refused")


notify.httpx.post = _boom
ch.oauth_status = OAuthStatus.CONNECTED
fired = notify.mark_dead(ch, "e", status=OAuthStatus.EXPIRED)
ok(fired is True, "a webhook delivery failure is swallowed — the alert still fires")

notify.httpx.post = _orig_post
settings.alert_webhook_url = ""

# --- never-raise: alerting must not break the publish path --------------------
print("never-raise: a broken alert can't propagate into the caller")


class _Exploding:
    """Channel stand-in whose attribute access blows up mid-alert."""
    oauth_status = OAuthStatus.EXPIRED

    @property
    def id(self):
        raise RuntimeError("detached instance")
    name = "Boom"


try:
    notify.alert_dead(_Exploding(), "e")
    raised = False
except Exception:
    raised = True
ok(raised is False, "an exception inside the alert body is swallowed, not propagated")

# --- wiring: publish loop alerts once when the token dies mid-drip ------------
print("publish_loop wiring: NeedsConnect alerts exactly once")
_orig_get, _orig_has = youtube.get_service, youtube.has_token

youtube.get_service = _raise_needs_connect
youtube.has_token = lambda slug: True
s = fresh_session()
ch = make_channel(s, oauth_status=OAuthStatus.CONNECTED)
v = make_video(s, ch, status=VideoStatus.APPROVED, video_path="/tmp/x.mp4", title="T")

cap = CaptureAlerts()
publish_loop._publish_one(s, ch, v)
ok(ch.oauth_status == OAuthStatus.EXPIRED, "revoked token still flips the channel to EXPIRED")
ok(len(cap.records) == 1, "the flip emits exactly one alert")
publish_loop._publish_one(s, ch, v)   # channel already expired (e.g. manual retry)
ok(len(cap.records) == 1, "a repeat NeedsConnect on the dead channel adds no alert")

youtube.has_token = lambda slug: False   # the token *file* itself vanished
ch3 = make_channel(s, slug="ch-gone")
v3 = make_video(s, ch3, status=VideoStatus.APPROVED, video_path="/tmp/x.mp4", title="T")
publish_loop._publish_one(s, ch3, v3)
ok(ch3.oauth_status == OAuthStatus.DISCONNECTED,
   "token-file loss classifies as DISCONNECTED (not the old EXPIRED mislabel)")
ok(len(cap.records) == 2, "and that flip alerts too")
cap.close()
youtube.get_service, youtube.has_token = _orig_get, _orig_has

# --- wiring: metrics loop alerts a dead token during a publishing lull --------
print("metrics_loop wiring: the daily probe alerts a dead token; transient skips don't")
_orig_get, _orig_has = youtube.get_service, youtube.has_token
youtube.get_service = _raise_needs_connect
youtube.has_token = lambda slug: True
s = fresh_session()
ch = make_channel(s, slug="ch-metrics")

cap = CaptureAlerts()
ok(metrics_loop.record_snapshot(s, ch) is None, "a dead token yields no snapshot")
s.refresh(ch)
ok(ch.oauth_status == OAuthStatus.EXPIRED,
   "the metrics probe flips the dead channel durably (committed)")
ok(len(cap.records) == 1, "the metrics-discovered flip alerts exactly once")
ok(metrics_loop.record_snapshot(s, ch) is None and len(cap.records) == 1,
   "a repeat probe on the dead channel stays silent")


def _raise_transient(slug):
    raise RuntimeError("network down")


youtube.get_service = _raise_transient
ch2 = make_channel(s, slug="ch-metrics-ok")
ok(metrics_loop.record_snapshot(s, ch2) is None, "a transient failure yields no snapshot")
ok(ch2.oauth_status == OAuthStatus.CONNECTED and len(cap.records) == 1,
   "a transient failure neither flips nor alerts")
cap.close()
youtube.get_service, youtube.has_token = _orig_get, _orig_has

# --- wiring: analytics loop flips a dead token, skips a missing scope ---------
print("analytics_loop wiring: dead token flips; missing analytics scope only skips")
_now = datetime.now(timezone.utc)
_orig_get, _orig_ana, _orig_has = (youtube.get_service, youtube.get_analytics_service,
                                   youtube.has_token)
youtube.get_analytics_service = _raise_needs_connect
youtube.has_token = lambda slug: True
s = fresh_session()

youtube.get_service = _raise_needs_connect       # narrow scope dead too -> dead channel
ch = make_channel(s, slug="ch-ana")
cap = CaptureAlerts()
ok(analytics_loop._snapshot_channel(s, ch, _now) == 0, "a dead token records nothing")
s.refresh(ch)
ok(ch.oauth_status == OAuthStatus.EXPIRED, "a genuinely dead token flips the channel")
ok(len(cap.records) == 1, "the analytics-discovered flip alerts exactly once")

youtube.get_service = lambda slug: object()      # narrow scope healthy -> scope-only
ch2 = make_channel(s, slug="ch-ana-scope")
ok(analytics_loop._snapshot_channel(s, ch2, _now) == 0, "a missing scope records nothing")
ok(ch2.oauth_status == OAuthStatus.CONNECTED and len(cap.records) == 1,
   "a missing analytics scope skips without flipping or alerting (publishing is fine)")
cap.close()
youtube.get_service, youtube.get_analytics_service, youtube.has_token = (
    _orig_get, _orig_ana, _orig_has)

# --- wiring: admin endpoints flip + alert behind their 409 --------------------
print("youtube_admin wiring: the 409 also flips and alerts the channel")
_orig_get, _orig_has = youtube.get_service, youtube.has_token
youtube.get_service = _raise_needs_connect
youtube.has_token = lambda slug: True
s = fresh_session()
ch = make_channel(s, slug="ch-admin")

cap = CaptureAlerts()
try:
    youtube_admin._connected(s, ch.id)
    code = None
except HTTPException as e:
    code = e.status_code
ok(code == 409, "a dead channel still answers 409")
s.refresh(ch)
ok(ch.oauth_status == OAuthStatus.EXPIRED,
   "the 409 path flips the channel durably (committed before the alert)")
ok(len(cap.records) == 1, "the admin-discovered flip alerts exactly once")
try:
    youtube_admin._connected(s, ch.id)
except HTTPException:
    pass
ok(len(cap.records) == 1, "a repeat 409 on the dead channel stays silent")

# playlists endpoints share the same gap class: 400 without a flip.
ch_pl = make_channel(s, slug="ch-playlists")
try:
    playlists_router.sync(ch_pl.id, session=s)
    code = None
except HTTPException as e:
    code = e.status_code
ok(code == 400, "playlist sync on a dead channel still answers 400")
s.refresh(ch_pl)
ok(ch_pl.oauth_status == OAuthStatus.EXPIRED and len(cap.records) == 2,
   "and the flip alerts exactly once through the same choke point")
cap.close()
youtube.get_service, youtube.has_token = _orig_get, _orig_has

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

# --- wiring: a failed reconnect consent alerts a previously-live channel ------
print("oauth_callback wiring: failed consent alerts once; stale callbacks don't kill")
s = fresh_session()
ch = make_channel(s, slug="ch-consent")

cap = CaptureAlerts()
channels_router.oauth_callback(ch.id, None, code=None, error="access_denied", session=s)
s.refresh(ch)
ok(ch.oauth_status == OAuthStatus.ERROR, "a failed consent flips a CONNECTED channel to ERROR")
ok(len(cap.records) == 1, "and alerts exactly once")

ch_dead = make_channel(s, slug="ch-consent-dead", oauth_status=OAuthStatus.EXPIRED)
channels_router.oauth_callback(ch_dead.id, None, code=None, error="access_denied", session=s)
s.refresh(ch_dead)
ok(ch_dead.oauth_status == OAuthStatus.ERROR and len(cap.records) == 1,
   "a failed reconnect of an already-dead channel records ERROR but stays silent")

# A replayed/stale callback (no pending flow, no error param — e.g. the operator
# refreshes the success page) must not kill a healthy channel.
ch_ok = make_channel(s, slug="ch-consent-ok")
channels_router.oauth_callback(ch_ok.id, None, code="abc123", error=None, session=s)
s.refresh(ch_ok)
ok(ch_ok.oauth_status == OAuthStatus.CONNECTED,
   "a stale/replayed callback leaves a CONNECTED channel untouched")
ok(len(cap.records) == 1, "and alerts nothing")
cap.close()

print(f"\nALL {_checks} CHECKS PASSED")
