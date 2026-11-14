"""S7: day-6 token-renewal check.

- a successful platform refresh stamps Channel.last_refresh_ok_at (and
  OAuthCredential.updated_at); a failed refresh (RefreshError) does not;
- the initial connect (notify.mark_connected) stamps it;
- the job: day 5 = no flag, day 7 = flag (WARNING + token_refresh_stale_at),
  one WARNING per stale episode, internal workspace info-only, the next good
  refresh clears the marker;
- the job never refreshes, never calls Google, never changes oauth_status;
- the log line carries no token, client secret or email;
- the marker is visible on the dashboard / agent-state channel entry.

Fake clock throughout. Run: PYTHONPATH=. uv run python tests/verify_token_refresh_age.py
"""
from __future__ import annotations

import base64
import io
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("MANAGER_APP_PASSWORD", "")

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from sqlmodel import Session, SQLModel, create_engine

import app.db as db_mod
from app.config import settings
from app.models import Channel, CredentialSource, OAuthCredential, OAuthStatus, Workspace
from app.services import notify, scheduler, token_refresh_age, token_store, youtube

_checks = 0
REFRESH = "1//refresh-SECRET-token"
ACCESS = "ya29.ACCESS-SECRET"
EMAIL = "buyer@example.com"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


settings.token_enc_key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
settings.google_client_id = "web-client-id"
settings.google_client_secret = "web-client-SECRET"
settings.credentials_dir = tempfile.mkdtemp()
settings.alert_webhook_url = ""
settings.token_age_internal_workspace_ids = "1"

d = tempfile.mkdtemp()
eng = create_engine(f"sqlite:///{d}/t.db", connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(eng)
db_patch = patch.object(db_mod, "engine", eng)
db_patch.start()

T0 = datetime(2026, 11, 1, 12, 0, 0)        # naive UTC, like utcnow()
CLOCK = [T0]


def clock():
    return CLOCK[0]


clock_patches = [patch.object(token_refresh_age, "_clock", clock),
                 patch.object(token_store, "utcnow", clock),
                 patch.object(notify, "utcnow", clock)]
for p in clock_patches:
    p.start()

log_buf = io.StringIO()
handler = logging.StreamHandler(log_buf)
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)

with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="buyer", name="Buyer Co", owner_email=EMAIL))
    s.add(Workspace(id=3, slug="closed", name="Closed", closed_at=T0))
    s.commit()


def mk_channel(cid, ws):
    with Session(eng) as s:
        ch = Channel(id=cid, workspace_id=ws, slug=f"ch{cid}", name=f"Chan {cid}",
                     credential_source=CredentialSource.PLATFORM_OAUTH,
                     oauth_status=OAuthStatus.DISCONNECTED, created_at=T0, updated_at=T0)
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return ch


def connect(cid):
    """The real connect sequence tail: save the grant, then flip CONNECTED."""
    with Session(eng) as s:
        ch = s.get(Channel, cid)
        token_store.save_credentials(s, ch, Credentials(
            token=ACCESS, refresh_token=REFRESH, token_uri="https://oauth2.googleapis.com/token",
            client_id="web-client-id", client_secret="web-client-SECRET"))
        notify.mark_connected(s, ch, {"id": f"UC{cid}", "title": f"Chan {cid}"})


def get(cid):
    with Session(eng) as s:
        return s.get(Channel, cid)


def expired_creds():
    c = Credentials(token="old", refresh_token=REFRESH, token_uri="https://oauth2.googleapis.com/token",
                    client_id="web-client-id", client_secret="web-client-SECRET")
    c.expiry = datetime.utcnow() - timedelta(hours=1)
    return c


def fake_refresh_ok(self, request):
    self.token = ACCESS
    self.expiry = datetime.utcnow() + timedelta(hours=1)


def refresh(cid, *, fail=False, own_session=True):
    side = RefreshError("invalid_grant: Token has been expired or revoked.",
                        {"error": "invalid_grant"}) if fail else None
    kw = {"side_effect": side} if fail else {"new": fake_refresh_ok}
    with patch.object(Credentials, "refresh", **kw):
        if own_session:
            with Session(eng) as s:
                out = youtube._refresh_and_persist(f"ch{cid}", expired_creds(),
                                                   channel=s.get(Channel, cid), session=s)
                s.commit()
        else:
            # Channel attached to a different session than the one save_credentials
            # opens (session=None path) — must not raise or lose the credential save.
            with Session(eng) as caller:
                out = youtube._refresh_and_persist(f"ch{cid}", expired_creds(),
                                                   channel=caller.get(Channel, cid), session=None)
    return out


def tick_at(at):
    CLOCK[0] = at
    return token_refresh_age.tick()


# ============================ stamping ======================================
print("stamp: initial connect and successful refresh; never on failure")
mk_channel(10, 2)
mk_channel(11, 2)
mk_channel(1, 1)
mk_channel(30, 3)
ok(get(10).last_refresh_ok_at is None, "new column is NULL before any grant")
connect(10); connect(11); connect(1); connect(30)
ok(get(10).last_refresh_ok_at == T0, "initial OAuth connect stamps last_refresh_ok_at")

CLOCK[0] = T0 + timedelta(days=1)
with Session(eng) as s:
    cred_before = s.get(OAuthCredential, 10).updated_at
ok(refresh(10) is not None, "successful refresh returns creds")
ok(get(10).last_refresh_ok_at == T0 + timedelta(days=1), "refresh ok stamps last_refresh_ok_at")
with Session(eng) as s:
    ok(s.get(OAuthCredential, 10).updated_at == T0 + timedelta(days=1) > cred_before,
       "save_credentials now updates OAuthCredential.updated_at")

CLOCK[0] = T0 + timedelta(days=2)
ok(refresh(10, own_session=False) is not None, "refresh via session=None path with a foreign-session channel works")
ok(get(10).last_refresh_ok_at == T0 + timedelta(days=2), "session=None path stamps too")
with Session(eng) as s:
    ok(s.get(OAuthCredential, 10).updated_at == T0 + timedelta(days=2), "credential save not rolled back")

CLOCK[0] = T0 + timedelta(days=3)
ok(refresh(10, fail=True) is None, "failed refresh returns None")
ok(get(10).last_refresh_ok_at == T0 + timedelta(days=2), "failed refresh does NOT stamp")
with Session(eng) as s:
    ok(s.get(OAuthCredential, 10).updated_at == T0 + timedelta(days=2), "failed refresh does not touch the credential")

# ============================ job ===========================================
print("job: day 5 no flag, day 7 flag, no Google, no status change")


def boom(*a, **k):
    raise AssertionError("token_refresh_age must not call Google / refresh")


google_guards = [patch.object(Credentials, "refresh", boom),
                 patch.object(youtube, "get_service", boom),
                 patch.object(youtube, "_refresh_and_persist", boom),
                 patch("googleapiclient.discovery.build", boom),
                 patch("google.auth.transport.requests.Request", boom),
                 patch("httpx.post", boom), patch("httpx.get", boom),
                 patch("requests.Session.request", boom)]
for g in google_guards:
    g.start()

# ch10 last ok = T0+2d; ch11 = T0 (connect only); ch1 internal = T0; ch30 closed ws.
log_buf.truncate(0); log_buf.seek(0)
ok(tick_at(T0 + timedelta(days=5)) == 0, "day 5 since connect: nothing flagged")
ok(get(11).token_refresh_stale_at is None and "token_refresh_stale" not in log_buf.getvalue(),
   "day 5: no marker, no log line")
ok(tick_at(T0 + timedelta(days=6)) == 0, "exactly 6 days: not yet (> 6 days rule)")

log_buf.truncate(0); log_buf.seek(0)
day7 = T0 + timedelta(days=7)
ok(tick_at(day7) == 1, "day 7: exactly one external channel flagged (ch11)")
ok(get(11).token_refresh_stale_at == day7, "marker token_refresh_stale_at persisted")
ok(get(10).token_refresh_stale_at is None, "ch10 refreshed on day 2 → 5 days old → not flagged")
ok(get(1).token_refresh_stale_at is None, "internal workspace 1: no marker (info-only)")
ok(get(30).token_refresh_stale_at is None, "closed workspace: skipped")
logs = log_buf.getvalue()
warn = [ln for ln in logs.splitlines() if ln.startswith("token_refresh_stale ")]
ok(warn == ["token_refresh_stale channel_id=11 workspace_id=2 days=7.0"], f"exact WARNING line: {warn}")
ok("token_refresh_stale_internal channel_id=1 workspace_id=1 days=7.0" in logs, "internal gets an INFO line")
for secret in (REFRESH, ACCESS, "web-client-SECRET", EMAIL, "Chan 11", "ch11"):
    ok(secret not in logs, f"log has no secret/PII: {secret!r}")
for cid in (10, 11, 1):
    ok(get(cid).oauth_status == OAuthStatus.CONNECTED, f"ch{cid} oauth_status unchanged")

log_buf.truncate(0); log_buf.seek(0)
ok(tick_at(day7 + timedelta(hours=6)) == 0, "next tick: no second WARNING for the same episode")
ok(get(11).token_refresh_stale_at == day7, "marker keeps its first stamp")
ok("token_refresh_stale channel_id=11" not in log_buf.getvalue(), "no duplicate WARNING")

# legacy upgrade path: NULL last_refresh_ok_at falls back to connect time, then created_at
with Session(eng) as s:
    c = s.get(Channel, 10)
    c.last_refresh_ok_at = None
    c.oauth_status_changed_at = None
    s.add(c); s.commit()
ok(tick_at(day7) == 1 and get(10).token_refresh_stale_at == day7,
   "NULL last_refresh_ok_at and connect time → created_at reference (pre-S7 rows)")

for g in google_guards:
    g.stop()

# ============================ clear =========================================
print("clear: next good refresh clears the marker")
CLOCK[0] = day7 + timedelta(hours=7)
ok(refresh(11) is not None, "ch11 refreshes")
ok(get(11).token_refresh_stale_at is None, "marker cleared on good refresh")
ok(get(11).last_refresh_ok_at == day7 + timedelta(hours=7), "last_refresh_ok_at moved")
ok(tick_at(day7 + timedelta(hours=12)) == 0 and get(11).token_refresh_stale_at is None,
   "job does not re-flag right after the refresh")
with Session(eng) as s:
    c = s.get(Channel, 11)
    c.token_refresh_stale_at = day7
    s.add(c); s.commit()
CLOCK[0] = day7 + timedelta(days=1)
ok(refresh(11, fail=True) is None and get(11).token_refresh_stale_at == day7,
   "failed refresh does not clear the marker")
connect(11)
ok(get(11).token_refresh_stale_at is None, "a reconnect also clears it")

# ============================ ops surface ===================================
print("ops: dashboard / agent-state channel entry exposes the marker")
from app.routers import queue as queue_router  # noqa: E402

with Session(eng) as s:
    c = s.get(Channel, 10)  # flagged above
    entry = queue_router._token_refresh_entry(c)
ok(entry["stale"] is True and entry["stale_at"] == day7, "token_refresh.stale / stale_at")
ok(entry["last_refresh_ok_at"] is None and isinstance(entry["age_days"], float), "age_days from reference")
ok(REFRESH not in repr(entry) and ACCESS not in repr(entry), "entry carries no token")

# ============================ config & scheduler =============================
print("config + scheduler registration")
settings.token_age_internal_workspace_ids = "1,x"
ok(token_refresh_age.internal_workspace_ids() == frozenset(), "malformed internal list → all external")
settings.token_age_internal_workspace_ids = "1"
ok(token_refresh_age.TICK_HOURS == 6, "tick every 6 h")
src = open(scheduler.__file__).read()
ok('id="token_refresh_age"' in src and "token_refresh_age.TICK_HOURS" in src, "job registered in scheduler")
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
with patch.object(BackgroundScheduler, "start", lambda self: None):
    scheduler.start()
    ok(scheduler._scheduler.get_job("token_refresh_age") is not None, "scheduler has the job")
    scheduler._scheduler = None

for p in clock_patches:
    p.stop()
db_patch.stop()
print(f"\nverify_token_refresh_age: {_checks} checks passed")
