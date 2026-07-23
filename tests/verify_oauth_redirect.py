"""Dependency-free regression checks for the web OAuth consent path.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_oauth_redirect.py

Part 1 reproduces the redirect_uri_mismatch incident that kept ch2 disconnected
for ~3 days: oauth_start built the redirect_uri from the incoming Host header,
so a reconnect initiated through the reverse proxy (Host: channels.owera.com)
produced a non-loopback redirect_uri the Desktop OAuth client rejects. With
MANAGER_PUBLIC_BASE_URL set, the redirect_uri is pinned to a registered base no
matter which Host the request arrived on; unset keeps the old request-derived
behavior so localhost reconnects are unchanged.

Part 2 covers the verify-before-save grant guards (BACKLOG 4b): a web consent
that comes back without a refresh token, with partial scopes, or from the wrong
Google account must save NOTHING and leave oauth_status untouched — pre-4b the
callback saved first and looked later, so a botched re-consent clobbered the
working token and rotated the previous one out of token.json.bak. Drives the
real /oauth/start + /oauth/callback endpoints against a local mock of Google's
token endpoint (the only stub besides the identity lookup) — no network, no
creds, temp dirs only. Exits non-zero on the first failed assertion.
"""
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import reconnect
from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus
from app.routers import channels as channels_router
from app.services import notify, youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---- fixture: temp credentials dir with a Desktop-type client_secret --------
SLUG = "verify-oauth"
tmp = Path(tempfile.mkdtemp(prefix="verify-oauth-redirect-"))
settings.credentials_dir = str(tmp)
(tmp / SLUG).mkdir(parents=True)
(tmp / SLUG / "client_secret.json").write_text(json.dumps({
    "installed": {
        "client_id": "verify.apps.googleusercontent.com",
        "client_secret": "verify-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}))

# ---- fixture: app with just the channels router + a private in-memory DB ----
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    ch = Channel(slug=SLUG, name="Verify", oauth_status=OAuthStatus.EXPIRED)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    CH_ID = ch.id

app = FastAPI()
app.include_router(channels_router.router)


def _session_override():
    with Session(engine) as session:
        yield session


app.dependency_overrides[get_session] = _session_override
client = TestClient(app)


def start_redirect_uri(host: str) -> str:
    """POST /oauth/start as if the request arrived on `host`; return the
    redirect_uri embedded in the returned Google authorization URL."""
    r = client.post(f"/api/channels/{CH_ID}/oauth/start", headers={"host": host})
    assert r.status_code == 200, f"/oauth/start -> {r.status_code}: {r.text[:200]}"
    return parse_qs(urlparse(r.json()["auth_url"]).query)["redirect_uri"][0]


CALLBACK = f"/api/channels/{CH_ID}/oauth/callback"

print("redirect_uri derivation (public_base_url unset — legacy behavior)")
settings.public_base_url = ""
ok(start_redirect_uri("localhost:7070") == f"http://localhost:7070{CALLBACK}",
   "localhost Host still yields the loopback redirect_uri")
ok(start_redirect_uri("channels.owera.com").startswith("http://channels.owera.com/"),
   "proxied Host yields a non-loopback redirect_uri (the incident's mechanism)")

print("redirect_uri pinned (MANAGER_PUBLIC_BASE_URL set — the fix)")
settings.public_base_url = "http://localhost:7070"
ok(start_redirect_uri("channels.owera.com") == f"http://localhost:7070{CALLBACK}",
   "proxied Host now yields the pinned loopback redirect_uri")
ok(start_redirect_uri("localhost:7070") == f"http://localhost:7070{CALLBACK}",
   "localhost Host yields the same pinned redirect_uri")

settings.public_base_url = "http://localhost:7070/"
ok(start_redirect_uri("channels.owera.com") == f"http://localhost:7070{CALLBACK}",
   "trailing slash in the setting does not double the slash")

settings.public_base_url = ""


# ============================================================================
# Part 2 — verify-before-save grant guards on the web consent path (BACKLOG 4b)
# ============================================================================

settings.alert_webhook_url = ""  # hermetic: ERROR flips must never POST anywhere


# ---- verify_grant unit checks (no flow, fake creds) -------------------------
class FakeCreds:
    def __init__(self, refresh_token="r", granted_scopes=None, scopes=None):
        self.refresh_token = refresh_token
        self.granted_scopes = (youtube.CONSENT_SCOPES if granted_scopes is None
                               else granted_scopes)
        self.scopes = scopes


def rejected(creds, **kw):
    try:
        youtube.verify_grant(creds, **kw)
        return None
    except youtube.GrantRejected as e:
        return e


print("verify_grant guards (unit)")
IDENT = {"id": "UCbound", "title": "Bound Channel"}
ok(youtube.verify_grant(FakeCreds(), fetch_identity_fn=lambda c: dict(IDENT)) == IDENT,
   "full grant with matching identity returns the identity")
e = rejected(FakeCreds(refresh_token=None), fetch_identity_fn=lambda c: dict(IDENT))
ok(e and e.code == "no_refresh_token" and "refresh token" in str(e),
   "missing refresh token rejected (no_refresh_token)")
e = rejected(FakeCreds(granted_scopes=youtube.SCOPES),
             fetch_identity_fn=lambda c: dict(IDENT))
ok(e and e.code == "partial_scopes" and "yt-analytics.readonly" in str(e),
   "partial grant rejected, missing scopes named (partial_scopes)")
ok(youtube.verify_grant(FakeCreds(granted_scopes=youtube.SCOPES), allow_partial=True,
                        fetch_identity_fn=lambda c: dict(IDENT)) == IDENT,
   "allow_partial overrides exactly the scope guard")


def _boom(creds):
    raise RuntimeError("backendError 503")


e = rejected(FakeCreds(), fetch_identity_fn=_boom)
ok(e and e.code == "identity_check_failed" and "identity check failed" in str(e),
   "identity lookup failure rejected (identity_check_failed)")
e = rejected(FakeCreds(), fetch_identity_fn=lambda c: {"id": None, "title": None})
ok(e and e.code == "no_channel" and "no YouTube channel" in str(e),
   "account without a channel rejected (no_channel)")
e = rejected(FakeCreds(), expected_channel_id="UCother", expected_channel_title="Other",
             fetch_identity_fn=lambda c: dict(IDENT))
ok(e and e.code == "channel_mismatch" and "UCother" in str(e) and "UCbound" in str(e),
   "wrong-channel identity rejected, both ids named (channel_mismatch)")
ok(youtube.verify_grant(FakeCreds(), expected_channel_id="UCother", allow_rebind=True,
                        fetch_identity_fn=lambda c: dict(IDENT)) == IDENT,
   "allow_rebind overrides exactly the identity guard")

# ---- GrantCode constants / hint-dict drift (BACKLOG 4c-b) -------------------
# Every code the raise sites above actually produced is a registered GrantCode,
# and both caller hint dicts key only on registered codes — so a rename that
# desyncs a raise site from GRANT_CODES (or a hint dict from either) fails here
# instead of a dict.get(code, "") silently dropping the remediation string.
_raised = {"no_refresh_token", "partial_scopes", "identity_check_failed",
           "no_channel", "channel_mismatch"}
ok(youtube.GRANT_CODES == _raised,
   "GRANT_CODES exactly matches the codes verify_grant raises")
ok(all(getattr(youtube.GrantCode, n.upper()) == n for n in _raised),
   "each GrantCode constant equals its literal code string")
ok(set(channels_router._GRANT_HINTS) <= youtube.GRANT_CODES,
   "_GRANT_HINTS keys are all registered GrantCodes (no drift)")
ok(set(reconnect._CLI_HINTS) <= youtube.GRANT_CODES,
   "_CLI_HINTS keys are all registered GrantCodes (no drift)")

# ---- end-to-end: real /oauth/start + /oauth/callback ------------------------
# The only stubs are Google's token endpoint (local mock HTTP server) and the
# identity lookup (module attribute); the Flow, code exchange, verify_grant,
# save_token, and mark_connected are all the real code paths.
_token_response: dict = {}


class _TokenHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({k: v for k, v in _token_response.items()
                           if not k.startswith("_")}).encode()
        self.send_response(_token_response.get("_status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


_token_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
threading.Thread(target=_token_srv.serve_forever, daemon=True).start()

SLUG2 = "verify-consent"
(tmp / SLUG2).mkdir()
(tmp / SLUG2 / "client_secret.json").write_text(json.dumps({
    "installed": {
        "client_id": "verify.apps.googleusercontent.com",
        "client_secret": "verify-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": f"http://127.0.0.1:{_token_srv.server_port}/token",
        "redirect_uris": ["http://localhost"],
    }
}))
TOKEN = tmp / SLUG2 / "token.json"
BAK = tmp / SLUG2 / "token.json.bak"

with Session(engine) as s:
    ch2 = Channel(slug=SLUG2, name="Verify Consent", oauth_status=OAuthStatus.CONNECTED,
                  yt_channel_id="UCbound", yt_channel_title="Bound Channel")
    s.add(ch2)
    s.commit()
    s.refresh(ch2)
    CH2_ID = ch2.id

_identity: dict = {}


def _fake_identity(creds):
    if _identity.get("_raise"):
        raise RuntimeError(_identity["_raise"])
    return {k: v for k, v in _identity.items() if not k.startswith("_")}


youtube.identity_for_creds = _fake_identity


def seed_tokens():
    TOKEN.write_text(json.dumps({"token": "GOOD-OLD-TOKEN",
                                 "refresh_token": "GOOD-OLD-REFRESH"}))
    BAK.write_text(json.dumps({"token": "OLDER-BAK"}))


def channel_row():
    with Session(engine) as s:
        row = s.get(Channel, CH2_ID)
        return {"status": row.oauth_status, "error": row.oauth_error,
                "name": row.name, "yt_id": row.yt_channel_id}


def set_channel(**fields):
    with Session(engine) as s:
        row = s.get(Channel, CH2_ID)
        for k, v in fields.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def start_consent(channel_id=CH2_ID) -> str:
    """POST /oauth/start; return the state Google would echo on the redirect."""
    r = client.post(f"/api/channels/{channel_id}/oauth/start",
                    headers={"host": "localhost:7070"})
    assert r.status_code == 200, f"/oauth/start -> {r.status_code}: {r.text[:200]}"
    return parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]


def consent(token_resp: dict, identity: dict):
    """Drive a full web consent: /oauth/start then the redirect callback,
    echoing the real state the way Google does."""
    _token_response.clear()
    _token_response.update(token_resp)
    _identity.clear()
    _identity.update(identity)
    state = start_consent()
    return client.get(f"/api/channels/{CH2_ID}/oauth/callback?code=fake-code&state={state}")


def full_token(**over):
    d = {"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600,
         "refresh_token": "new-refresh", "scope": " ".join(youtube.CONSENT_SCOPES)}
    d.update(over)
    return d


def unchanged(before):
    return (TOKEN.read_bytes(), BAK.read_bytes()) == before


print("web consent: rejections save nothing and preserve oauth_status")
seed_tokens()
before = (TOKEN.read_bytes(), BAK.read_bytes())

r = consent(full_token(), {"id": "UCother", "title": "Other Channel"})
ok("Consent rejected" in r.text and "UCother" in r.text and "UCbound" in r.text,
   "wrong-account consent shows the mismatch (both channel ids)")
ok("Disconnect" in r.text, "mismatch page carries the web remediation hint")
row = channel_row()
ok(row["status"] == OAuthStatus.CONNECTED and row["error"] is None,
   "channel stays CONNECTED after wrong-account consent")
ok(unchanged(before), "token.json and .bak byte-identical after wrong-account consent")

r = consent(full_token(), {"id": "UCother", "title": "<img src=x onerror=alert(1)>"})
ok("<img" not in r.text and "&lt;img" in r.text,
   "Google-supplied channel title is HTML-escaped on the rejection page")
ok("window.close" not in r.text,
   "rejection page does not self-close (the message is the only trace the user gets)")

r = consent(full_token(), {"id": "UCother", "title": "T" * 500})
ok("Disconnect" in r.text, "remediation hint survives truncation of an oversized message")

r = consent(full_token(scope=" ".join(youtube.SCOPES)), dict(IDENT))
ok("Consent rejected" in r.text and "missing scope" in r.text,
   "partial-scope consent rejected (unchecked-checkbox trap)")
ok(channel_row()["status"] == OAuthStatus.CONNECTED and unchanged(before),
   "partial-scope consent left status and tokens untouched")

no_refresh = full_token()
del no_refresh["refresh_token"]
r = consent(no_refresh, dict(IDENT))
ok("Consent rejected" in r.text and "refresh token" in r.text,
   "refresh-token-less consent rejected")
ok(channel_row()["status"] == OAuthStatus.CONNECTED and unchanged(before),
   "refresh-token-less consent left status and tokens untouched")

r = consent(full_token(), {"_raise": "backendError 503"})
ok("Consent rejected" in r.text and "identity check failed" in r.text,
   "identity-lookup failure rejected as transient")
ok(channel_row()["status"] == OAuthStatus.CONNECTED and unchanged(before),
   "identity-lookup failure left status and tokens untouched")

print("web consent: replayed callback and exchange failure")
r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?code=fake-code&state=x")
ok("cancelled" in r.text and channel_row()["status"] == OAuthStatus.CONNECTED,
   "replayed callback (no pending flow) leaves the channel untouched")

r = consent({"_status": 500, "error": "internal_failure"}, dict(IDENT))
ok("Connection failed" in r.text, "token-exchange failure shows the failure page")
row = channel_row()
ok(row["status"] == OAuthStatus.ERROR and row["error"],
   "token-exchange failure flips the channel to ERROR (a real halted consent)")
ok(unchanged(before), "token-exchange failure wrote nothing")

print("web consent: verified happy path saves and connects")
set_channel(oauth_status=OAuthStatus.CONNECTED, oauth_error=None)
r = consent(full_token(), {"id": "UCbound", "title": "Bound Channel Renamed"})
ok("Connected" in r.text, "verified consent shows the success page")
ok("window.close" in r.text, "success page still self-closes")
saved = json.loads(TOKEN.read_text())
ok(saved.get("refresh_token") == "new-refresh", "token.json holds the new grant")
ok("GOOD-OLD-REFRESH" in BAK.read_text(),
   "previous token rotated into token.json.bak")
row = channel_row()
ok(row["status"] == OAuthStatus.CONNECTED and row["error"] is None,
   "channel CONNECTED with error cleared (mark_connected)")
ok(row["name"] == "Bound Channel Renamed" and row["yt_id"] == "UCbound",
   "identity re-bound: display name follows the real YouTube title")

print("web consent: DB flip failure after a verified save")
set_channel(oauth_status=OAuthStatus.EXPIRED, oauth_error="dead")
seed_tokens()
_orig_mark_connected = notify.mark_connected


def _boom_mark_connected(session, channel, identity):
    raise RuntimeError("database is locked")


notify.mark_connected = _boom_mark_connected
try:
    r = consent(full_token(), {"id": "UCbound", "title": "Bound Channel"})
finally:
    notify.mark_connected = _orig_mark_connected
ok("Token saved" in r.text and "do NOT redo" in r.text,
   "commit failure after save shows the do-not-redo-the-consent page")
ok(json.loads(TOKEN.read_text()).get("refresh_token") == "new-refresh",
   "the verified token IS on disk despite the failed status flip")
ok(channel_row()["status"] == OAuthStatus.EXPIRED,
   "only the status flip is missing (the next oauth-status probe repairs it)")

# ---- the repair itself: GET /oauth-status (BACKLOG 4c-c) --------------------
# Pre-4c-c the probe hand-rolled its flip to CONNECTED, so the repair it is
# documented for (above, and in the reconnect CLI's error text) was half-done:
# a consent whose mark_connected commit failed left the channel with a working
# token and no bound identity, and the probe never bound it — the dashboard
# kept showing the stale name and the next re-consent had no
# expected_channel_id for verify_grant's wrong-account check.
print("oauth-status probe: finishes the identity bind a failed consent left undone")
_orig_get_service, _orig_fetch_identity = youtube.get_service, youtube.fetch_identity
_probe: dict = {"service": object(), "identity": {"id": "UCbound", "title": "Bound Channel"},
                "calls": 0}


def _fake_get_service(slug):
    if _probe.get("dead"):
        raise youtube.NeedsConnect(
            f"token missing/expired for channel '{slug}' — reconnect required")
    return _probe["service"]


def _fake_fetch_identity(service):
    _probe["calls"] += 1
    _probe["got_service"] = service
    if _probe.get("raise"):
        raise RuntimeError(_probe["raise"])
    return dict(_probe["identity"])


def probe():
    return client.get(f"/api/channels/{CH2_ID}/oauth-status")


youtube.get_service, youtube.fetch_identity = _fake_get_service, _fake_fetch_identity
try:
    # Exactly the after-state of the failed-flip case above, for a channel that
    # never completed a first bind: good token on disk, status not flipped.
    set_channel(oauth_status=OAuthStatus.EXPIRED, oauth_error="dead",
                yt_channel_id=None, yt_channel_title=None, name="stale-name")
    r = probe()
    ok(r.json()["oauth_status"] == OAuthStatus.CONNECTED and r.json()["error"] is None,
       "probing a healthy token reports CONNECTED with the error cleared")
    row = channel_row()
    ok(row["status"] == OAuthStatus.CONNECTED and row["error"] is None,
       "and the repair is committed, not just reported")
    ok(row["yt_id"] == "UCbound" and row["name"] == "Bound Channel",
       "an unbound channel gets its identity bound through mark_connected")
    ok(_probe["calls"] == 1 and _probe.get("got_service") is _probe["service"],
       "the identity lookup ran once, against the service the probe just built")

    # The dashboard polls this endpoint every 2.5s while a reconnect is in
    # flight, so an already-bound channel must not spend a quota unit per tick.
    _probe["calls"] = 0
    set_channel(oauth_status=OAuthStatus.EXPIRED, oauth_error="dead")
    probe()
    probe()
    ok(channel_row()["status"] == OAuthStatus.CONNECTED,
       "an already-bound channel still flips CONNECTED")
    ok(_probe["calls"] == 0,
       "and spends no quota: no channels().list once the identity is bound")

    # get_service already proved the token refreshes, so a channels().list blip
    # must not turn a healthy probe into a dead-channel flip.
    set_channel(oauth_status=OAuthStatus.CONNECTED, oauth_error=None,
                yt_channel_id=None, yt_channel_title=None)
    _probe["raise"] = "backendError 503"
    r = probe()
    _probe.pop("raise")
    ok(r.json()["oauth_status"] == OAuthStatus.CONNECTED,
       "a failed identity lookup still reports CONNECTED (the token itself works)")
    ok(channel_row()["status"] == OAuthStatus.CONNECTED,
       "and never flips the channel dead")

    # An account with no YouTube channel attached must not write a null
    # identity over the row it was meant to repair.
    set_channel(oauth_status=OAuthStatus.EXPIRED, yt_channel_id=None,
                yt_channel_title=None, name="stale-name")
    _probe["identity"] = {"id": None, "title": None}
    probe()
    _probe["identity"] = {"id": "UCbound", "title": "Bound Channel"}
    row = channel_row()
    ok(row["status"] == OAuthStatus.CONNECTED and row["yt_id"] is None
       and row["name"] == "stale-name",
       "an identity with no channel id binds nothing and leaves the name alone")

    _probe["dead"] = True
    set_channel(oauth_status=OAuthStatus.CONNECTED, oauth_error=None,
                yt_channel_id="UCbound", yt_channel_title="Bound Channel")
    probe()
    _probe.pop("dead")
    row = channel_row()
    ok(row["status"] == OAuthStatus.EXPIRED and "reconnect" in (row["error"] or ""),
       "a dead token still flips through mark_dead_committed (unchanged)")
finally:
    youtube.get_service, youtube.fetch_identity = _orig_get_service, _orig_fetch_identity

# ============================================================================
# Part 3 — state-keyed pending flows (BACKLOG 4c-a)
# ============================================================================
# Pre-4c-a, _pending_flows was keyed by channel id: a second /oauth/start
# (double-click, two tabs) overwrote the first flow, so completing the FIRST
# consent failed the exchange and flipped a CONNECTED channel to ERROR — and
# any replayed ?error= redirect flipped it too.

print("state-keyed flows: double-click, superseded leftover, replayed ?error=")
set_channel(oauth_status=OAuthStatus.CONNECTED, oauth_error=None)
seed_tokens()
_token_response.clear()
_token_response.update(full_token())
_identity.clear()
_identity.update({"id": "UCbound", "title": "Bound Channel"})

state1 = start_consent()
state2 = start_consent()
ok(state1 != state2, "each /oauth/start mints its own state")
r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?code=fake-code&state={state1}")
ok("Connected" in r.text,
   "completing the FIRST consent succeeds despite a second start (double-click)")
ok(channel_row()["status"] == OAuthStatus.CONNECTED,
   "channel CONNECTED after the double-click consent")

r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?error=access_denied&state={state2}")
ok(channel_row()["status"] == OAuthStatus.CONNECTED,
   "cancelling the leftover second consent (superseded by the success) doesn't flip")

r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?error=access_denied&state={state1}")
ok(channel_row()["status"] == OAuthStatus.CONNECTED,
   "a browser-history replay of an ?error= redirect (consumed state) doesn't flip")

# A state minted for another channel's consent is not this channel's: it must
# neither complete nor fail this channel, and must stay pending for its owner.
state_other = start_consent(channel_id=CH_ID)
r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?error=access_denied&state={state_other}")
ok(channel_row()["status"] == OAuthStatus.CONNECTED,
   "another channel's state can't fail this channel")
ok(state_other in channels_router._pending_flows,
   "and the other channel's flow is still pending")
channels_router._pending_flows.pop(state_other, None)

channels_router._pending_flows["st-abandoned"] = channels_router._PendingFlow(
    CH2_ID, object(), time.monotonic() - channels_router._PENDING_FLOW_TTL - 1)
start_consent()
ok("st-abandoned" not in channels_router._pending_flows,
   "starts prune abandoned flows past the TTL")

# TTL is also enforced at consumption: an expired-but-unpruned flow (no start
# ever ran to sweep it) reads as stale — a session-restored ancient consent
# cancel must not flip the channel days later.
channels_router._pending_flows["st-expired"] = channels_router._PendingFlow(
    CH2_ID, object(), time.monotonic() - channels_router._PENDING_FLOW_TTL - 1)
r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?error=access_denied&state=st-expired")
ok(channel_row()["status"] == OAuthStatus.CONNECTED,
   "cancelling an expired pending consent doesn't flip the channel")
ok("st-expired" not in channels_router._pending_flows,
   "and the expired entry is consumed")

state_real = start_consent()
r = client.get(f"/api/channels/{CH2_ID}/oauth/callback?error=access_denied&state={state_real}")
row = channel_row()
ok(row["status"] == OAuthStatus.ERROR and "access_denied" in (row["error"] or ""),
   "a genuine cancel of a pending consent still flips to ERROR")

_token_srv.shutdown()
print(f"ALL {_checks} CHECKS PASSED")
