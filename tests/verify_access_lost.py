"""P3 + CTO nit + P7: access-lost lifecycle.

- _refresh_and_persist logs the RefreshError class and OAuth code (invalid_grant),
  never the token, and the code reaches the NeedsConnect message;
- invalid_grant / operator disconnect / deletion request / workspace close
  stamp Channel.access_lost_at; reconnect clears;
- youtube_purge deletes only YouTube API data once the deadline is reached,
  idempotently, keeping the workspace and non-YouTube rows:
  7 calendar days for disconnect in the app, a deletion request or a
  workspace close (LegalOps cláusula 7 v2, YT III.D.2.3.a and III.E.4.g);
  30 days for a Google revoke or RefreshError / invalid_grant (III.D.2.3.b).
  Fake clock: at (deadline - 1h) the data is still there; at the deadline it
  has been purged;
- one access-lost email to contact@owera.com per channel+status+transition
  (persisted dedupe), only for real channels (not paused, not is_test), via
  Resend (mocked), never with a token, never to rodrigo@.

Run: PYTHONPATH=. uv run python tests/verify_access_lost.py
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("MANAGER_APP_PASSWORD", "")

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as db_mod
from app.config import settings
from app.db import get_session
from app.models import (Channel, ChannelAlert, ChannelMetric, CredentialSource, OAuthCredential,
                        OAuthStatus, Playlist, Topic, Video, VideoMetric, Workspace, utcnow)
from app.routers import channels as channels_router
from app.routers import settings as settings_router
from app.services import notify, token_store, youtube, youtube_purge
from app.workspace import current_workspace

_checks = 0
REFRESH = "1//refresh-SECRET-token"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


settings.token_enc_key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
settings.google_client_id = "web-client-id"
settings.google_client_secret = "web-client-secret"
settings.credentials_dir = tempfile.mkdtemp()
settings.alert_webhook_url = ""

d = tempfile.mkdtemp()
eng = create_engine(f"sqlite:///{d}/t.db", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(eng)
db_patch = patch.object(db_mod, "engine", eng)
db_patch.start()

with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="buyer", name="Buyer Co"))
    s.commit()

log_buf = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(log_buf))
logging.getLogger().setLevel(logging.INFO)

SENT: list[dict] = []


def fake_resend(url, **kw):
    SENT.append({"url": url, **(kw.get("json") or {})})
    return httpx.Response(200, request=httpx.Request("POST", url))


def mk_channel(cid, *, ws=2, status=OAuthStatus.CONNECTED, paused=False, is_test=False,
               platform=True, name=None):
    with Session(eng) as s:
        ch = Channel(id=cid, workspace_id=ws, slug=f"ch{cid}", name=name or f"Chan {cid}",
                     credential_source=CredentialSource.PLATFORM_OAUTH if platform else CredentialSource.LEGACY_FILE,
                     oauth_status=status, paused=paused, is_test=is_test,
                     yt_channel_id=f"UC{cid}", yt_channel_title=name or f"Chan {cid}")
        s.add(ch)
        s.commit()
        s.refresh(ch)
        if platform:
            c = Credentials(token="a", refresh_token=REFRESH, token_uri="https://oauth2.googleapis.com/token",
                            client_id="web-client-id", client_secret="web-client-secret")
            token_store.save_credentials(s, ch, c)
            s.commit()
        return ch


def get(cid):
    with Session(eng) as s:
        return s.get(Channel, cid)


# ======================= nit: RefreshError logged ==========================
print("nit: RefreshError is logged with class + code, never the token")
e1 = RefreshError("invalid_grant: Token has been expired or revoked.",
                  {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
ok(youtube.refresh_error_code(e1) == "invalid_grant", "code from response dict")
ok(youtube.refresh_error_code(RefreshError("invalid_client: Unauthorized")) == "invalid_client",
   "code from message prefix")
ok(youtube.refresh_error_code(RefreshError("weird")) == "unknown", "unknown code fallback")

mk_channel(1)
expired = Credentials(token="old", refresh_token=REFRESH, token_uri="https://oauth2.googleapis.com/token",
                      client_id="web-client-id", client_secret="web-client-secret")
expired.expiry = datetime.utcnow() - timedelta(hours=1)
log_buf.truncate(0); log_buf.seek(0)
with patch.object(Credentials, "refresh", side_effect=e1), \
        patch("app.services.token_store.load_credentials", return_value=expired):
    try:
        with Session(eng) as s:
            youtube.get_service("ch1", channel=s.get(Channel, 1), session=s)
        ok(False, "should raise NeedsConnect")
    except youtube.NeedsConnect as e:
        msg = str(e)
logs = log_buf.getvalue()
ok("RefreshError" in logs and "code=invalid_grant" in logs, "refresh failure logs class and code")
ok(REFRESH not in logs and "SECRET" not in logs,
   "refresh failure log has no token")
ok(msg.endswith("reconnect required (refresh failed: invalid_grant)"),
   "NeedsConnect message carries the code")
ok("ch1" not in youtube._LAST_REFRESH_ERROR, "code consumed once (no stale carry-over)")

# ======================= P3: access_lost_at stamping =======================
print("P3: access_lost_at stamping")
settings.resend_api_key = ""
with Session(eng) as s:
    ch = s.get(Channel, 1)
    notify.mark_dead_committed(s, ch, msg, status=OAuthStatus.EXPIRED)
c1 = get(1)
ok(c1.oauth_status == OAuthStatus.EXPIRED and c1.access_lost_at is not None
   and c1.access_lost_reason == "invalid_grant", "invalid_grant dead flip stamps access_lost_at")
ok(c1.oauth_status_changed_at is not None, "status change time recorded")

mk_channel(2)
with Session(eng) as s:
    notify.mark_dead_committed(s, s.get(Channel, 2), "token missing/expired — reconnect required",
                               status=OAuthStatus.EXPIRED)
ok(get(2).access_lost_at is not None and get(2).access_lost_reason == "expired",
   "P3b: generic EXPIRED flip (no invalid_grant) also starts the clock")

first_stamp = get(1).access_lost_at
with Session(eng) as s:
    notify.mark_dead_committed(s, s.get(Channel, 1), msg, status=OAuthStatus.EXPIRED)
ok(get(1).access_lost_at == first_stamp, "first stamp wins on repeated dead checks")

with Session(eng) as s:
    notify.mark_connected(s, s.get(Channel, 1), {"id": "UC1", "title": "Chan 1"})
c1 = get(1)
ok(c1.access_lost_at is None and c1.access_lost_reason is None and c1.youtube_data_purged_at is None,
   "reconnect clears the clock")

def _has_metrics(cid: int) -> bool:
    with Session(eng) as s:
        return s.exec(select(ChannelMetric).where(ChannelMetric.channel_id == cid)).first() is not None


def _set_lost(cid: int, when: datetime, reason: str, status=OAuthStatus.EXPIRED):
    with Session(eng) as s:
        ch = s.get(Channel, cid)
        ch.oauth_status = status
        ch.access_lost_at = when
        ch.access_lost_reason = reason
        s.add(ch)
        s.commit()


def _assert_deadline(cid: int, t0: datetime, days: int, label: str):
    """Fake clock (LegalOps cláusula 7 v2): data still there at deadline-1h,
    purged when the purge job runs at the deadline."""
    almost = t0 + timedelta(days=days) - timedelta(hours=1)
    youtube_purge.tick(now=almost)
    ok(_has_metrics(cid), f"{label}: at {days}d-1h ({days-1}d23h) YouTube data still exists")
    n = youtube_purge.tick(now=t0 + timedelta(days=days))
    ok(n >= 1 and not _has_metrics(cid),
       f"{label}: purge job at {days}d deletes the YouTube data")
    with Session(eng) as s:
        ok(s.get(Channel, cid).youtube_data_purged_at is not None, f"{label}: purge stamped")


# ======================= P3: purge job (30-day, fake clock) ================
print("P3: purge job — 30 days for Google revoke / RefreshError")
mk_channel(10, name="Buyer YT")
mk_channel(11, name="Other")
T30 = datetime(2020, 1, 15, 12, 0, 0)
with Session(eng) as s:
    tpc = Topic(channel_id=10, name="T")
    s.add(tpc); s.commit(); s.refresh(tpc)
    v = Video(channel_id=10, topic_id=tpc.id, subject="s", yt_video_id="vid10", title="My video")
    s.add(v); s.add(Playlist(channel_id=10, yt_playlist_id="PL10", title="P"))
    s.commit(); s.refresh(v)
    for cid in (10, 11):
        s.add(ChannelMetric(channel_id=cid, subscriber_count=5))
    s.add(VideoMetric(video_id=v.id, channel_id=10, views=3, traffic_json='{"search_terms":{"x":1}}'))
    s.commit()
    vid = v.id
    ch = s.get(Channel, 10)
    ch.oauth_status = OAuthStatus.EXPIRED
    ch.access_lost_at = T30
    ch.access_lost_reason = "invalid_grant"
    s.add(ch)
    c11 = s.get(Channel, 11)
    c11.access_lost_at = T30 - timedelta(days=60)   # stale stamp but CONNECTED → never purged
    s.add(c11)
    s.commit()

_assert_deadline(10, T30, 30, "invalid_grant / RefreshError")
with Session(eng) as s:
    c10 = s.get(Channel, 10)
    ok(not s.exec(select(VideoMetric).where(VideoMetric.channel_id == 10)).all(),
       "VideoMetric (analytics + traffic/search terms) deleted")
    ok(s.get(OAuthCredential, 10) is None, "dead credential deleted")
    ok(c10.yt_channel_title is None and c10.name == "ch10", "cached YouTube title/name removed")
    ok(s.get(Workspace, 2) is not None, "workspace kept")
    ok(s.get(Video, vid) is not None and s.get(Video, vid).title == "My video", "Video row (our record) kept")
    ok(s.exec(select(Topic).where(Topic.channel_id == 10)).first() is not None, "topics kept")
    ok(s.exec(select(Playlist).where(Playlist.channel_id == 10)).first() is not None, "CM playlists kept")
    ok(c10.yt_channel_id == "UC10", "identity binding kept")
    ok(s.exec(select(ChannelMetric).where(ChannelMetric.channel_id == 11)).first() is not None,
       "connected channel's data untouched")
    ok(s.get(OAuthCredential, 11) is not None, "connected channel credential untouched")
ok(youtube_purge.tick(now=T30 + timedelta(days=40)) == 0, "re-run is a no-op (idempotent)")

# reconnect then lose again → new clock → purged again later
with Session(eng) as s:
    notify.mark_connected(s, s.get(Channel, 10), {"id": "UC10", "title": "Buyer YT"})
    s.add(ChannelMetric(channel_id=10, subscriber_count=9)); s.commit()
youtube_purge.tick(now=T30 + timedelta(days=90))
with Session(eng) as s:
    ok(s.exec(select(ChannelMetric).where(ChannelMetric.channel_id == 10)).first() is not None,
       "reconnected channel is not purged")

# ======================= P3: 7-day fake clock (3 triggers) =================
print("P3: 7-day purge — disconnect, deletion request, workspace close")
# Isolated TestClient so the three HTTP triggers can run before the P7 email section.
app_p3 = FastAPI(); app_p3.include_router(channels_router.router); app_p3.include_router(settings_router.router)


def _sess_p3():
    with Session(eng) as session:
        yield session


def _ws_for(ws_id):
    def _ws():
        with Session(eng) as session:
            return session.get(Workspace, ws_id)
    return _ws


app_p3.dependency_overrides[get_session] = _sess_p3
app_p3.dependency_overrides[current_workspace] = _ws_for(2)
client_p3 = TestClient(app_p3)

# --- disconnect in the app (YT III.D.2.3.a) ---
T7A = datetime(2020, 3, 1, 12, 0, 0)
mk_channel(30, name="Disc 7d")
with Session(eng) as s:
    s.add(ChannelMetric(channel_id=30, subscriber_count=5)); s.commit()
r = client_p3.post("/api/channels/30/disconnect")
ok(r.status_code == 200 and r.json()["oauth_status"] == OAuthStatus.DISCONNECTED,
   "disconnect 200 marks the channel disconnected")
c30 = get(30)
ok(c30.access_lost_at is not None and c30.access_lost_reason == "disconnected",
   "disconnect in the app stamps access_lost_at (reason=disconnected)")
_set_lost(30, T7A, "disconnected", status=OAuthStatus.DISCONNECTED)
_assert_deadline(30, T7A, 7, "disconnect in the app")

# --- deletion request (YT III.E.4.g) ---
T7B = datetime(2020, 4, 1, 12, 0, 0)
mk_channel(31, name="Del 7d")
with Session(eng) as s:
    s.add(ChannelMetric(channel_id=31, subscriber_count=5)); s.commit()
r = client_p3.post("/api/channels/31/request-deletion")
ok(r.status_code == 200 and r.json()["oauth_status"] == OAuthStatus.DISCONNECTED,
   "request-deletion 200 marks the channel disconnected")
c31 = get(31)
ok(c31.access_lost_at is not None and c31.access_lost_reason == "deletion_requested",
   "deletion request stamps access_lost_at (reason=deletion_requested)")
ok(c31.yt_channel_id is None, "deletion request drops the identity")
_set_lost(31, T7B, "deletion_requested", status=OAuthStatus.DISCONNECTED)
_assert_deadline(31, T7B, 7, "deletion request")

# --- workspace close (YT III.E.4.g) ---
T7C = datetime(2020, 5, 1, 12, 0, 0)
with Session(eng) as s:
    s.add(Workspace(id=3, slug="closing", name="Closing Co")); s.commit()
mk_channel(32, ws=3, name="Close 7d")
with Session(eng) as s:
    s.add(ChannelMetric(channel_id=32, subscriber_count=5)); s.commit()
app_p3.dependency_overrides[current_workspace] = _ws_for(3)
r = client_p3.post("/api/workspace/close")
ok(r.status_code == 200 and r.json().get("ok") and r.json().get("channels") >= 1,
   "workspace close 200 stamps the workspace channels")
c32 = get(32)
ok(c32.access_lost_at is not None and c32.access_lost_reason == "workspace_closed",
   "workspace close stamps access_lost_at (reason=workspace_closed)")
with Session(eng) as s:
    ok(s.get(Workspace, 3).closed_at is not None, "workspace.closed_at is set")
_set_lost(32, T7C, "workspace_closed", status=OAuthStatus.DISCONNECTED)
_assert_deadline(32, T7C, 7, "workspace close")
app_p3.dependency_overrides[current_workspace] = _ws_for(2)

# ======================= P7: access-lost email =============================
print("P7: access-lost email")
settings.resend_api_key = "re_test_key"
mk_channel(20, name="Real Channel")
SENT.clear()
log_buf.truncate(0); log_buf.seek(0)
with patch("httpx.post", side_effect=fake_resend):
    with Session(eng) as s:
        notify.mark_dead_committed(s, s.get(Channel, 20),
                                   "token missing/expired for channel 'ch20' — reconnect required (refresh failed: invalid_grant)",
                                   status=OAuthStatus.EXPIRED)
ok(len(SENT) == 1, "real channel CONNECTED→EXPIRED: one email")
m = SENT[0]
ok(m["to"] == ["contact@owera.com"], "sent to contact@owera.com")
ok(m["from"] == "channels@mail.owera.com", "from channels@mail.owera.com")
ok("rodrigo" not in str(m).lower(), "never rodrigo@")
ok("Real Channel" in m["subject"] and "expired" in m["subject"], "subject names channel + status")
ok("Buyer Co" in m["html"] and "buyer" in m["html"], "body names the workspace")
ok("America/Belem" in m["html"] and "UTC" in m["html"], "body has the time (Belém + UTC)")
ok("invalid_grant" in m["html"], "body has the error code")
ok(REFRESH not in str(m) and "SECRET" not in str(m), "no token in the email")
with Session(eng) as s:
    rows = s.exec(select(ChannelAlert).where(ChannelAlert.channel_id == 20)).all()
    ok(len(rows) == 1 and rows[0].delivered, "dedupe row persisted, delivered")

# same transition again (retry / second worker) → no resend
with patch("httpx.post", side_effect=fake_resend):
    notify.email_access_lost(get(20), cause="retry")
    with Session(eng) as s:
        notify.mark_dead_committed(s, s.get(Channel, 20), "again", status=OAuthStatus.EXPIRED)
ok(len(SENT) == 1, "same transition never emails twice (persisted dedupe)")

# reconnect, then a new loss → a new transition → one more email
with patch("httpx.post", side_effect=fake_resend):
    with Session(eng) as s:
        notify.mark_connected(s, s.get(Channel, 20), {"id": "UC20", "title": "Real Channel"})
    import time; time.sleep(0.01)
    with Session(eng) as s:
        notify.mark_dead_committed(s, s.get(Channel, 20), "x", status=OAuthStatus.EXPIRED)
ok(len(SENT) == 2, "a new transition after reconnect emails again")

# paused / is_test / ERROR → no email
mk_channel(21, paused=True); mk_channel(22, is_test=True); mk_channel(23)
SENT.clear()
with patch("httpx.post", side_effect=fake_resend):
    with Session(eng) as s:
        notify.mark_dead_committed(s, s.get(Channel, 21), "x", status=OAuthStatus.EXPIRED)
        notify.mark_dead_committed(s, s.get(Channel, 22), "x", status=OAuthStatus.EXPIRED)
        notify.mark_dead_committed(s, s.get(Channel, 23), "x", status=OAuthStatus.ERROR)
ok(len(SENT) == 0, "paused, is_test and ERROR channels send no email")

# disconnect route → DISCONNECTED email + clock
app = FastAPI(); app.include_router(channels_router.router)


def _sess():
    with Session(eng) as session:
        yield session


def _ws():
    with Session(eng) as session:
        return session.get(Workspace, 2)


app.dependency_overrides[get_session] = _sess
app.dependency_overrides[current_workspace] = _ws
client = TestClient(app)
mk_channel(24, name="Disc Channel")
SENT.clear()
with patch("httpx.post", side_effect=fake_resend):
    r = client.post("/api/channels/24/disconnect")
ok(r.status_code == 200 and r.json()["oauth_status"] == OAuthStatus.DISCONNECTED, "disconnect 200")
resend_mails = [x for x in SENT if "resend" in x["url"]]
ok(len(resend_mails) == 1 and "disconnected" in resend_mails[0]["subject"], "disconnect: one email")
c24 = get(24)
ok(c24.access_lost_at is not None and c24.access_lost_reason == "disconnected", "disconnect starts the purge clock")
ok(c24.yt_channel_id is None, "disconnect still clears identity")
with patch("httpx.post", side_effect=fake_resend):
    client.post("/api/channels/24/disconnect")
ok(len([x for x in SENT if "resend" in x["url"]]) == 1, "second disconnect of a disconnected channel: no email")

# no key → warning, nothing sent, no dedupe row
settings.resend_api_key = ""
mk_channel(25)
SENT.clear(); log_buf.truncate(0); log_buf.seek(0)
with patch("httpx.post", side_effect=fake_resend):
    with Session(eng) as s:
        notify.mark_dead_committed(s, s.get(Channel, 25), "x", status=OAuthStatus.EXPIRED)
ok(len(SENT) == 0 and "MANAGER_RESEND_API_KEY is unset" in log_buf.getvalue(), "no key: warning only, not sent")

# Resend failure never breaks the caller
settings.resend_api_key = "re_test_key"
mk_channel(26)
with patch("httpx.post", side_effect=httpx.ConnectError("down")):
    with Session(eng) as s:
        flipped = notify.mark_dead_committed(s, s.get(Channel, 26), "x", status=OAuthStatus.EXPIRED)
ok(flipped and get(26).oauth_status == OAuthStatus.EXPIRED, "Resend down: flip still committed, no raise")

ok(REFRESH not in log_buf.getvalue(), "no token anywhere in logs")
db_patch.stop()
print(f"\nALL {_checks} CHECKS PASSED")
