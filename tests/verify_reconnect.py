"""Dependency-free regression checks for the loopback reconnect CLI (BACKLOG #4).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_reconnect.py

Bakes in the lessons from the 2026-07-05/11 reconnect incidents: the ad-hoc
helper wrote token.json BEFORE verifying anything, so a wrong-account consent
(three look-alike Google accounts + a brand picker), a partial grant (the scope
checkboxes are unchecked by default), or a grant with no refresh token could
clobber the only working token. These checks drive app.reconnect.reconnect()
through a REAL loopback flow — a real wsgiref redirect server, a real oauthlib
code exchange against a local mock of Google's token endpoint — and pin that:

  - a good consent writes token.json atomically (0600), keeps the previous
    token as token.json.bak, and flips the channel CONNECTED with its identity;
  - stray loopback connections (browser preconnects, port probes, favicon
    fetches) do NOT abort a pending consent — the InstalledAppFlow
    run_local_server failure mode the CLI deliberately avoids;
  - no refresh token / missing scopes / wrong-channel identity / identity-fetch
    failure / no-channel account each abort BEFORE any disk or DB change;
  - --force and --allow-partial override exactly their own guard;
  - a consent that never arrives times out cleanly, changing nothing;
  - unknown channels and a missing client_secret.json fail with clear messages;
  - disconnect() removes token.json.bak and stranded tmp files too;
  - _load_creds' refresh persist won't clobber a token replaced mid-refresh.

Only the identity lookup (Google Data API) is stubbed; everything else is the
real code path. No network beyond 127.0.0.1, temp dirs only, in-memory DB.
Exits non-zero on the first failed assertion.
"""
import contextlib
import io
import json
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import reconnect as rc
from app.config import settings
from app.models import Channel, OAuthStatus

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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- a local stand-in for Google's token endpoint --------------------------

ALL_SCOPES = ("https://www.googleapis.com/auth/youtube "
              "https://www.googleapis.com/auth/youtube.force-ssl "
              "https://www.googleapis.com/auth/yt-analytics.readonly")

token_response = {}  # mutated per scenario


class _TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.dumps(token_response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def set_token_response(**overrides):
    token_response.clear()
    token_response.update({
        "access_token": "fake-access", "refresh_token": "fake-refresh",
        "expires_in": 3600, "token_type": "Bearer", "scope": ALL_SCOPES,
    })
    for k, v in overrides.items():
        if v is None:
            token_response.pop(k, None)
        else:
            token_response[k] = v


def write_client_secret(slug: str, token_uri: str):
    cdir = Path(settings.credentials_dir) / slug
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "fake-client-id.apps.googleusercontent.com",
        "client_secret": "fake-secret",
        "auth_uri": "https://accounts.google.example/auth",
        "token_uri": token_uri,
        "redirect_uris": ["http://localhost"],
    }}))


def token_file(slug: str) -> Path:
    return Path(settings.credentials_dir) / slug / "token.json"


# ---- flow driver ------------------------------------------------------------

def drive(session, ident, hit_redirect=True, pre_redirect=None, **kw):
    """Run reconnect() in a thread and play the browser: parse the printed auth
    URL, run pre_redirect(port) if given (stray-traffic injection), then hit
    the loopback redirect with a code + the flow's state."""
    out, result = io.StringIO(), {}

    def run():
        try:
            with contextlib.redirect_stdout(out):
                result["channel"] = rc.reconnect(session, ident, open_browser=False, **kw)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=run, daemon=True)  # daemon: a hang can't pin the suite
    t.start()
    if hit_redirect:
        url = None
        for _ in range(200):  # the auth URL appears once the server is bound
            m = re.search(r"https://accounts\.google\.example/auth\?\S+", out.getvalue())
            if m:
                url = m.group(0)
                break
            time.sleep(0.02)
        if url:  # no URL means reconnect() errored first; the caller's check says how
            if pre_redirect:
                pre_redirect(kw["port"])
            state = parse_qs(urlparse(url).query)["state"][0]
            redirect = f"http://127.0.0.1:{kw['port']}/?state={state}&code=fake-code"
            urllib.request.urlopen(redirect, timeout=10).read()
    t.join(timeout=30)
    ok(not t.is_alive(), f"reconnect thread finished for '{ident}'")
    return result, out.getvalue()


def make_channel(session, **kw):
    ch = Channel(slug=kw.pop("slug", "ch-test"), name=kw.pop("name", "Test"), **kw)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


def main():
    tmp = tempfile.mkdtemp(prefix="verify-reconnect-")
    settings.credentials_dir = tmp

    server = HTTPServer(("127.0.0.1", 0), _TokenHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    token_uri = f"http://127.0.0.1:{server.server_port}/token"

    rc._fetch_identity = lambda creds: {"id": "UCnew", "title": "New Title"}

    print("== success path: consent -> verify -> save -> CONNECTED ==")
    session = fresh_session()
    ch = make_channel(session, slug="ch2", oauth_status=OAuthStatus.EXPIRED,
                      oauth_error="invalid_grant", yt_channel_id="UCnew",
                      yt_channel_title="Old Title")
    write_client_secret("ch2", token_uri)
    token_file("ch2").write_text('{"old": "token"}')
    set_token_response()
    result, out = drive(session, "ch2", port=free_port())
    ok("channel" in result, f"reconnect succeeded (got {result.get('error')!r})")
    ok("Selecionar tudo" in out, "select-all consent reminder printed before the URL")
    session.refresh(ch)
    ok(ch.oauth_status == OAuthStatus.CONNECTED, "channel flipped CONNECTED")
    ok(ch.oauth_error is None, "oauth_error cleared")
    ok(ch.yt_channel_title == "New Title" and ch.name == "New Title",
       "identity title applied to the row")
    saved = json.loads(token_file("ch2").read_text())
    ok(saved.get("refresh_token") == "fake-refresh", "token.json holds the new refresh token")
    ok((token_file("ch2").stat().st_mode & 0o777) == 0o600, "token.json written 0600")
    ok((token_file("ch2").parent / "token.json.bak").read_text() == '{"old": "token"}',
       "previous token preserved as token.json.bak")
    ok(not list(token_file("ch2").parent.glob("*.tmp")),
       "atomic write leaves no tmp file")

    print("== stray connections must not abort a pending consent ==")

    def stray_traffic(port):
        # 1. preconnect: TCP connect, send nothing, close (the exact pattern
        #    that kills InstalledAppFlow.run_local_server's single request)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.close()
        # 2. a request with no consent params (favicon-style probe)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5).read()
        except Exception:
            pass
        time.sleep(0.3)  # let the server chew through both before the real redirect

    set_token_response()
    result, _ = drive(session, "ch2", port=free_port(), pre_redirect=stray_traffic)
    ok("channel" in result,
       f"consent survives a preconnect + paramless probe (got {result.get('error')!r})")

    print("== numeric-id resolution works too ==")
    set_token_response()
    result, _ = drive(session, str(ch.id), port=free_port())
    ok("channel" in result, "reconnect by numeric id succeeded")

    print("== wrong-account guard: identity mismatch aborts before any write ==")
    session2 = fresh_session()
    ch2 = make_channel(session2, slug="ch1", oauth_status=OAuthStatus.EXPIRED,
                       yt_channel_id="UCbound", yt_channel_title="Bound Channel")
    write_client_secret("ch1", token_uri)
    token_file("ch1").write_text('{"working": "token"}')
    set_token_response()
    result, _ = drive(session2, "ch1", port=free_port())
    err = result.get("error")
    ok(isinstance(err, rc.ReconnectError) and "UCbound" in str(err) and "--force" in str(err),
       f"mismatch raises ReconnectError naming both channels (got {err!r})")
    ok(token_file("ch1").read_text() == '{"working": "token"}',
       "working token untouched on mismatch")
    session2.refresh(ch2)
    ok(ch2.oauth_status == OAuthStatus.EXPIRED, "DB status untouched on mismatch")

    print("== --force re-binds intentionally ==")
    set_token_response()
    result, _ = drive(session2, "ch1", port=free_port(), force=True)
    ok("channel" in result, "force reconnect succeeded")
    session2.refresh(ch2)
    ok(ch2.yt_channel_id == "UCnew" and ch2.oauth_status == OAuthStatus.CONNECTED,
       "channel re-bound to the new identity")

    print("== no refresh token -> abort, nothing saved ==")
    session3 = fresh_session()
    make_channel(session3, slug="ch3", oauth_status=OAuthStatus.EXPIRED)
    write_client_secret("ch3", token_uri)
    set_token_response(refresh_token=None)
    result, _ = drive(session3, "ch3", port=free_port())
    ok(isinstance(result.get("error"), rc.ReconnectError)
       and "refresh token" in str(result["error"]),
       "missing refresh token raises ReconnectError")
    ok(not token_file("ch3").exists(), "no token.json written without a refresh token")

    print("== partial grant -> abort unless --allow-partial ==")
    set_token_response(scope="https://www.googleapis.com/auth/youtube")
    result, _ = drive(session3, "ch3", port=free_port())
    err = result.get("error")
    ok(isinstance(err, rc.ReconnectError) and "force-ssl" in str(err)
       and "Select all" in str(err),
       f"partial grant names the missing scopes (got {err!r})")
    ok(not token_file("ch3").exists(), "no token.json written on partial grant")
    set_token_response(scope="https://www.googleapis.com/auth/youtube")
    result, _ = drive(session3, "ch3", port=free_port(), allow_partial=True)
    ok("channel" in result, "--allow-partial saves a partial grant")
    ok(token_file("ch3").exists(), "token.json written with --allow-partial")

    print("== identity fetch failure -> abort, token not saved ==")
    session4 = fresh_session()
    make_channel(session4, slug="ch4", oauth_status=OAuthStatus.EXPIRED)
    write_client_secret("ch4", token_uri)
    set_token_response()

    def boom(creds):
        raise RuntimeError("401 identity probe failed")

    rc._fetch_identity = boom
    result, _ = drive(session4, "ch4", port=free_port())
    ok(isinstance(result.get("error"), rc.ReconnectError)
       and "NOT saved" in str(result["error"]),
       "identity failure raises ReconnectError, token NOT saved")
    ok(not token_file("ch4").exists(), "no token.json written when identity check fails")

    print("== account with no YouTube channel -> abort ==")
    rc._fetch_identity = lambda creds: {"id": None, "title": None}
    set_token_response()
    result, _ = drive(session4, "ch4", port=free_port())
    ok(isinstance(result.get("error"), rc.ReconnectError)
       and "no YouTube channel" in str(result["error"]),
       "channel-less account raises ReconnectError")
    ok(not token_file("ch4").exists(), "no token.json written for a channel-less account")
    rc._fetch_identity = lambda creds: {"id": "UCnew", "title": "New Title"}

    print("== timeout: consent never arrives -> clean abort, nothing changed ==")
    result, _ = drive(session4, "ch4", port=free_port(), hit_redirect=False, timeout=1)
    err = result.get("error")
    ok(isinstance(err, rc.ReconnectError) and "within 1s" in str(err),
       f"timeout raises a clean ReconnectError (got {err!r})")
    ok(not token_file("ch4").exists(), "timeout leaves no token behind")

    print("== busy port -> actionable error ==")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    result, _ = drive(session4, "ch4", port=busy, hit_redirect=False, timeout=2)
    err = result.get("error")
    ok(isinstance(err, rc.ReconnectError) and "--port" in str(err),
       f"busy port suggests --port (got {err!r})")
    blocker.close()

    print("== unknown channel / missing client_secret ==")
    try:
        rc.reconnect(session4, "nope", port=free_port(), open_browser=False)
        ok(False, "unknown channel should raise")
    except rc.ReconnectError as e:
        ok("ch4" in str(e), "unknown channel lists available slugs")
    make_channel(session4, slug="bare")
    try:
        rc.reconnect(session4, "bare", port=free_port(), open_browser=False)
        ok(False, "missing client_secret should raise")
    except rc.ReconnectError as e:
        ok("client_secret.json" in str(e), "missing client_secret names the fix")

    print("== save_token / disconnect / refresh-race units ==")
    from app.services import youtube

    class FakeCreds:
        def __init__(self, payload):
            self.payload = payload

        def to_json(self):
            return self.payload

    youtube.save_token("unit-slug", FakeCreds('{"v": 1}'))
    ok(token_file("unit-slug").read_text() == '{"v": 1}', "first save writes token.json")
    youtube.save_token("unit-slug", FakeCreds('{"v": 2}'))
    ok(token_file("unit-slug").read_text() == '{"v": 2}'
       and (token_file("unit-slug").parent / "token.json.bak").read_text() == '{"v": 1}',
       "second save rotates the previous token into token.json.bak")
    (token_file("unit-slug").parent / "token.json.12345.tmp").write_text("stranded")
    youtube.disconnect("unit-slug")
    ok(not list(token_file("unit-slug").parent.glob("token.json*")),
       "disconnect removes token.json, .bak, and stranded tmp files")

    # _load_creds must not clobber a token replaced while it was refreshing:
    # simulate by making refresh() itself swap the file (worst-case timing).
    slug5 = "race-slug"
    tf = token_file(slug5)
    tf.parent.mkdir(parents=True, exist_ok=True)
    stale = json.dumps({"token": "at", "refresh_token": "old-rt",
                        "expiry": "2020-01-01T00:00:00Z",
                        "client_id": "x", "client_secret": "y",
                        "token_uri": "https://oauth2.googleapis.example/token"})
    tf.write_text(stale)
    from google.oauth2.credentials import Credentials as _GCreds
    orig_refresh = _GCreds.refresh

    def racing_refresh(self, request):
        tf.write_text('{"new": "grant"}')  # a reconnect lands mid-refresh
        self.token = "refreshed-at"
        self.expiry = None  # valid again

    _GCreds.refresh = racing_refresh
    try:
        creds = youtube._load_creds(slug5)
    finally:
        _GCreds.refresh = orig_refresh
    ok(creds is not None and tf.read_text() == '{"new": "grant"}',
       "refresh persist yields to a token replaced mid-refresh (new grant wins)")

    server.shutdown()
    print(f"\nALL {_checks} CHECKS PASSED")


if __name__ == "__main__":
    main()
