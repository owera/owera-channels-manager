"""One-command loopback OAuth reconnect for a channel (BACKLOG #4).

Usage (on the Mac that runs the manager):
    PYTHONPATH=. uv run python -m app.reconnect <slug-or-id> [--port 8077]
        [--no-browser] [--timeout 600] [--force] [--allow-partial]

Runs the Desktop-client loopback consent flow end-to-end — the only flow these
OAuth clients accept (the portal path needs MANAGER_PUBLIC_BASE_URL and a
browser on this machine anyway). It opens/prints the authorization URL, captures
the redirect on http://localhost:<port>/, exchanges the code, then — in this
order, learned from the 2026-07-05/11 reconnect incidents — verifies the grant
BEFORE touching anything on disk:

  1. a refresh token was issued (otherwise the new token dies within the hour);
  2. every consent scope was granted (the checkboxes are UNCHECKED by default —
     a partial grant would quietly re-break comments/analytics/publishing);
  3. the consented Google account owns the SAME YouTube channel this slug is
     bound to (three look-alike accounts + a brand-account picker make consenting
     as the wrong identity a real hazard).

Only then is token.json written (atomically, previous token kept as
token.json.bak) and the channel flipped CONNECTED in the DB. The manager needs
no restart: get_service() re-reads token.json per call and the publish loop
picks the channel up on its next tick; a brief SQLite write is safe while the
manager runs.

Remote consent (browser not on this machine): run with --no-browser, tunnel the
port (ssh -L 8077:localhost:8077 you@mac), and open the printed URL locally.
"""

import argparse
import sys
import time
import wsgiref.simple_server
import wsgiref.util

from sqlmodel import Session, select

from app.models import Channel
from app.services import notify, youtube

SCOPE_REMINDER = (
    "NOTE: on Google's consent screen the scope checkboxes are UNCHECKED by default —\n"
    "click 'Select all' (\"Selecionar tudo\") before Continue, or the grant comes back\n"
    "denied/partial and nothing is saved."
)

_SUCCESS_HTML = (b"<html><body style='font-family:sans-serif;text-align:center;"
                 b"padding-top:20vh'><h3>Consent received.</h3>"
                 b"<p>You can close this tab and return to the terminal.</p></body></html>")

# Test seam: the one call that needs a live Google grant.
_fetch_identity = youtube.identity_for_creds

# CLI-flavored remediation appended per GrantRejected.code — the escape hatches
# only the terminal offers (the web callback hints Disconnect-first instead).
_CLI_HINTS = {
    youtube.GrantCode.PARTIAL_SCOPES: " (or pass --allow-partial to save anyway)",
    youtube.GrantCode.CHANNEL_MISMATCH: " Pass --force to re-bind the channel intentionally.",
}
assert set(_CLI_HINTS) <= youtube.GRANT_CODES, "stale GrantRejected code in _CLI_HINTS"


class ReconnectError(Exception):
    """Operator-readable failure; the CLI prints it and exits 1. Every message
    states exactly what was and wasn't changed."""


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args):
        pass


def _resolve_channel(session: Session, ident: str) -> Channel:
    # Slug first: a slug can be all-digits, and it is the documented argument.
    ch = session.exec(select(Channel).where(Channel.slug == ident)).first()
    if ch is None and ident.isdecimal():
        ch = session.get(Channel, int(ident))
    if ch is None:
        slugs = ", ".join(c.slug for c in session.exec(select(Channel).order_by(Channel.id)))
        raise ReconnectError(f"no channel '{ident}' — available: {slugs or '(none)'}")
    return ch


def _consent(slug: str, port: int, open_browser: bool, timeout: int):
    """Serve http://localhost:<port>/ until the consent redirect arrives, then
    exchange the code. Unlike InstalledAppFlow.run_local_server (which handles
    exactly one request), stray connections — browser preconnects, port probes,
    favicon fetches — don't consume the flow; only the deadline aborts it."""
    captured: list[str] = []

    def wsgi_app(environ, start_response):
        if "code=" in environ.get("QUERY_STRING", "") or \
           "error=" in environ.get("QUERY_STRING", ""):
            captured.append(wsgiref.util.request_uri(environ))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [_SUCCESS_HTML]

    wsgiref.simple_server.WSGIServer.allow_reuse_address = False  # fail fast if busy
    server = wsgiref.simple_server.make_server("localhost", port, wsgi_app,
                                               handler_class=_QuietHandler)
    try:
        flow = youtube.build_flow(slug, f"http://localhost:{server.server_port}/")
        url, _state = youtube.authorization_url(flow)  # the exact knobs the web consent uses
        print(SCOPE_REMINDER)
        print(f"\nOpen this URL in a browser signed into the channel's Google "
              f"account:\n{url}\n")
        if open_browser:
            import webbrowser
            webbrowser.open(url, new=1, autoraise=True)
        deadline = time.monotonic() + timeout
        server.timeout = 1  # wake up each second to re-check the deadline
        while not captured and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not captured:
        raise ReconnectError(
            f"no consent redirect arrived on port {server.server_port} within "
            f"{timeout}s — flow abandoned; nothing was changed")
    # oauthlib validates state here and insists on an https authorization
    # response (the loopback transport guard is already relaxed in youtube.py).
    flow.fetch_token(authorization_response=captured[0].replace("http", "https", 1))
    return flow.credentials


def reconnect(session: Session, ident: str, port: int = 8077, open_browser: bool = True,
              timeout: int = 600, force: bool = False, allow_partial: bool = False) -> Channel:
    """Drive the full loopback consent for one channel. Returns the updated
    Channel row; raises ReconnectError on any failure, with a message that says
    exactly what state was left behind."""
    ch = _resolve_channel(session, ident)
    if not youtube.has_client_secret(ch.slug):
        raise ReconnectError(
            f"missing {youtube.client_secret_path(ch.slug)} — upload client_secret.json first")

    try:
        creds = _consent(ch.slug, port, open_browser, timeout)
    except ReconnectError:
        raise
    except OSError as e:
        if "address already in use" in str(e).lower():
            raise ReconnectError(
                f"port {port} is busy — pass --port <other> (and re-point any SSH tunnel)")
        raise ReconnectError(f"could not serve the loopback redirect: {e} — "
                             "nothing was changed")
    except Exception as e:
        # access_denied, mismatching state, token-endpoint failures, …
        raise ReconnectError(f"consent failed: {e} — nothing was changed")

    # The shared verify-before-save guards (youtube.verify_grant — also the web
    # consent path); only the flag hints are CLI-specific.
    try:
        identity = youtube.verify_grant(
            creds, expected_channel_id=ch.yt_channel_id,
            expected_channel_title=ch.yt_channel_title, label=f"'{ch.slug}'",
            allow_partial=allow_partial, allow_rebind=force,
            fetch_identity_fn=_fetch_identity)
    except youtube.GrantRejected as e:
        raise ReconnectError(str(e) + _CLI_HINTS.get(e.code, ""))

    try:
        youtube.save_token(ch.slug, creds)
    except OSError as e:
        raise ReconnectError(f"could not write the token: {e} — existing token untouched")

    prev_status = ch.oauth_status
    try:
        notify.mark_connected(session, ch, identity)
    except Exception as e:
        # The token IS on disk and valid; only the status flip is missing.
        raise ReconnectError(
            f"token SAVED but the DB update failed ({e}) — the channel may still show "
            f"'{prev_status}'. Hit GET /api/channels/{ch.id}/oauth-status (or open the "
            "dashboard) to flip it connected; do NOT redo the consent.")
    # Outside the guarded block: after a successful commit, a refresh failure
    # must not masquerade as "the DB update failed".
    session.refresh(ch)
    return ch


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.reconnect",
        description="Reconnect a channel's YouTube OAuth via the loopback consent flow.")
    ap.add_argument("channel", help="channel slug or numeric id")
    ap.add_argument("--port", type=int, default=8077,
                    help="loopback redirect port (default 8077; must be free)")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open a browser; just print the URL (SSH-tunnel use)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for the consent redirect (default 600)")
    ap.add_argument("--force", action="store_true",
                    help="allow binding to a DIFFERENT YouTube channel than the current one")
    ap.add_argument("--allow-partial", action="store_true",
                    help="save the token even if some consent scopes were not granted")
    args = ap.parse_args(argv)

    # The auth URL must reach the operator even when stdout is piped/redirected
    # (block buffering would hold it back until exit).
    sys.stdout.reconfigure(line_buffering=True)

    from app.db import engine  # deferred: tests drive reconnect() with their own session
    with Session(engine) as session:
        try:
            ch = reconnect(session, args.channel, port=args.port,
                           open_browser=not args.no_browser, timeout=args.timeout,
                           force=args.force, allow_partial=args.allow_partial)
        except ReconnectError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
    print(f"CONNECTED: {ch.slug} -> {ch.yt_channel_title} ({ch.yt_channel_id}). "
          "Token saved (previous kept as token.json.bak); no manager restart needed — "
          "the publish loop picks it up on its next tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
