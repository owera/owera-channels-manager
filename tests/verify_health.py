"""Regression checks for the /health endpoint, its auth exemption, and the SPA
fallback's file-read containment.

Run: PYTHONPATH=. .venv/bin/python tests/verify_health.py

Covers the aggregate health snapshot (degraded logic + aggregate-only payload); that
/health and the OAuth consent callback are reachable WITHOUT auth while the rest of
the API still requires it — so the middleware exemptions can't silently widen to leak
authed routes, including via a Host header chosen to make an exempt path appear in
`request.url`; and that the SPA catch-all cannot be walked out of the dist directory
(the manager runs with no password at all in the documented LAN config, so that route
is an unauthenticated arbitrary-file read if containment regresses).

Uses an in-memory DB and FastAPI's TestClient (no real manager.db, no network, and the
app lifespan/scheduler are never started). Exits non-zero on the first failed assertion.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# A throwaway dist tree, pointed at BEFORE importing app.main because that module
# captures `settings.frontend_dist` at import time. Gives the SPA checks a known
# index.html body, a planted sibling secret to try to reach, and a mounted SPA route
# whether or not the real frontend happens to be built in this checkout. Cleanup is
# registered with atexit because ok() calls sys.exit on the first failure.
_TMP = Path(tempfile.mkdtemp(prefix="verify-health-dist-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
_DIST = _TMP / "dist"
(_DIST / "assets").mkdir(parents=True)
(_DIST / "nested").mkdir()
(_DIST / "index.html").write_text("<html>SPA INDEX</html>")
(_DIST / "assets" / "index-abc123.css").write_text("body{}")
(_DIST / "nested" / "deep.js").write_text("// deep asset")
_SECRET = _TMP / "token.json"
_SECRET.write_text("TOP-SECRET-REFRESH-TOKEN")
os.environ["MANAGER_FRONTEND_DIST"] = str(_DIST)

from fastapi.testclient import TestClient                             # noqa: E402
from sqlalchemy.pool import StaticPool                                # noqa: E402
from sqlmodel import Session, SQLModel, create_engine                 # noqa: E402

import app.main as main                                               # noqa: E402
from app.config import settings                                       # noqa: E402
from app.db import get_session                                        # noqa: E402
from app.main import _contained_file, _health_snapshot                # noqa: E402
from app.models import Channel, OAuthStatus, Video, VideoStatus       # noqa: E402


class _patched_process_headroom:
    """Context manager that stubs main._read_process_headroom so tests never fork a
    real sysctl/ps — the reading is exercised directly, at the seam the acceptance
    criteria calls out ('verified by mocking the reading')."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self._orig = main._read_process_headroom
        main._read_process_headroom = lambda: self.value

    def __exit__(self, *exc):
        main._read_process_headroom = self._orig

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def make_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False},
                         poolclass=StaticPool)


def seeded_engine():
    """3 channels (connected / expired / disconnected-but-paused) + 1 failed video."""
    engine = make_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
        s.add(Channel(slug="b", name="B", oauth_status=OAuthStatus.EXPIRED))
        s.add(Channel(slug="c", name="C", oauth_status=OAuthStatus.DISCONNECTED, paused=True))
        s.commit()
        s.add(Video(channel_id=1, topic_id=1, subject="x", status=VideoStatus.FAILED))
        s.commit()
    return engine


# --- unit: _health_snapshot --------------------------------------------------
print("_health_snapshot")
with _patched_process_headroom(None):
    with Session(seeded_engine()) as s:
        snap = _health_snapshot(s)
ok(snap["status"] == "degraded", "degraded when a channel is expired or a video failed")
ok(snap["channels_total"] == 3, "counts all channels")
ok(snap["channels_connected"] == 1, "counts connected channels")
ok(snap["channels_paused"] == 1, "counts paused channels")
ok(snap["channels_needing_attention"] == 1,
   "expired-and-not-paused needs attention; a paused channel does not")
ok(snap["videos_failed"] == 1, "counts failed videos")
ok(snap["system"]["processes"] is None,
   "a failed process-headroom reading is omitted, not fatal")
ok(set(snap.keys()) == {"status", "channels_total", "channels_connected",
                        "channels_paused", "channels_needing_attention", "videos_failed",
                        "system"},
   "payload is aggregate-only (no names, slugs, ids, or tokens)")

# healthy: all connected, nothing failed, healthy process headroom -> ok
h_engine = make_engine()
SQLModel.metadata.create_all(h_engine)
with Session(h_engine) as s:
    s.add(Channel(slug="a", name="A", oauth_status=OAuthStatus.CONNECTED))
    s.commit()
with _patched_process_headroom({"count": 100, "max": 2000, "pct_used": 5.0}):
    with Session(h_engine) as s:
        healthy_snap = _health_snapshot(s)
ok(healthy_snap["status"] == "ok",
   "status ok when all channels are connected, nothing failed, and processes have headroom")
ok(healthy_snap["system"]["processes"]["pct_used"] == 5.0,
   "process headroom reading is surfaced under system.processes")

# process-slot exhaustion alone flips status to degraded (2026-07-06 fork-exhaustion incident)
with _patched_process_headroom({"count": 1900, "max": 2000, "pct_used": 95.0}):
    with Session(h_engine) as s:
        exhausted_snap = _health_snapshot(s)
ok(exhausted_snap["status"] == "degraded",
   "status degrades when process-slot usage crosses the threshold, even with healthy channels")

# just under the threshold stays ok
with _patched_process_headroom({"count": 1699, "max": 2000, "pct_used": 84.9}):
    with Session(h_engine) as s:
        ok(_health_snapshot(s)["status"] == "ok",
           "status stays ok just under the degraded threshold")

# --- endpoint + auth exemption (TestClient) ----------------------------------
print("/health endpoint + auth exemption")
engine = seeded_engine()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"          # turn auth ON so the exemption is meaningful
client = TestClient(main.app)

with _patched_process_headroom({"count": 100, "max": 2000, "pct_used": 5.0}):
    r = client.get("/health")
ok(r.status_code == 200, "/health is reachable without auth")
ok(r.json()["status"] == "degraded", "/health returns the snapshot payload")

r2 = client.get("/api/channels")
ok(r2.status_code == 401, "other API routes still require auth (exemption did not widen)")

r3 = client.get("/api/channels", auth=("x", "testpw"))
ok(r3.status_code == 200, "an authenticated API request still passes")

# --- OAuth callback exemption (BACKLOG 8) -------------------------------------
# Google's consent redirect lands in a browser that may hold no Basic Auth session,
# so the callback must not 401 — and a stateless hit must stay inert.
print("OAuth callback auth exemption")
r4 = client.get("/api/channels/1/oauth/callback?error=access_denied&state=no-such-state")
ok(r4.status_code == 200, "OAuth callback is reachable without auth")
ok("Connection failed" in r4.text, "a stateless callback hit renders the failure page")
with Session(engine) as s:
    ok(s.get(Channel, 1).oauth_status == OAuthStatus.CONNECTED,
       "the unauthenticated stateless hit left the channel's status untouched")

# the exemption is exactly one GET path shape — nothing nearby leaks
ok(client.post("/api/channels/1/oauth/callback").status_code == 401,
   "non-GET on the callback path still requires auth")
ok(client.get("/api/channels/1/oauth/start").status_code == 401,
   "the sibling oauth/start route (which mints the states) is not covered by the "
   "exemption — GET, so the 401 comes from the path regex, not the method gate")
ok(client.get("/api/channels/9223372036854775808/oauth/callback").status_code == 401,
   "an id past SQLite's int64 range is not exempt (it would 500 on session.get, "
   "handing an anonymous caller a traceback-sized log write per request)")
ok(client.get("/api/channels/abc/oauth/callback").status_code == 401,
   "a non-numeric channel id does not match the exemption")
ok(client.get("/api/channels/%D9%A3/oauth/callback").status_code == 401,
   "a Unicode digit does not match the exemption ([0-9]+, not \\d+)")
ok(client.get("/api/channels/1/oauth/callback/extra").status_code == 401,
   "a longer path sharing the callback prefix does not match (fullmatch, not search)")
ok(client.get("/api/channels/1/oauth/callbackx").status_code == 401,
   "a path merely prefixed by the callback route does not match")

# --- the exemption must not be reachable through the Host header ---------------
# Starlette rebuilds request.url as f"{scheme}://{host_header}{path}", so a Host of
# "h/health?" makes request.url.path read "/health" while the router still serves
# the real target. Authorizing on that value let any GET route be fetched without
# credentials; the guard must read the routed path instead.
print("auth exemption cannot be spoofed via the Host header")
spoof = {"Host": "h/health?"}
ok(client.get("/api/channels", headers=spoof).status_code == 401,
   "a protected route with a Host spoofing /health still requires auth")
ok(client.get("/api/settings", headers=spoof).status_code == 401,
   "the settings payload is not reachable by Host-spoofing the exemption")
ok(client.post("/api/channels", headers=spoof, json={}).status_code == 401,
   "a write route with a spoofed Host still requires auth")
ok(client.get("/api/channels",
              headers={"Host": "h/api/channels/1/oauth/callback?"}).status_code == 401,
   "the callback exemption is not reachable by Host-spoofing either")
with _patched_process_headroom({"count": 100, "max": 2000, "pct_used": 5.0}):
    ok(client.get("/health", headers={"Host": "h/api/channels?"}).status_code == 200,
       "the real /health still passes regardless of the Host header it carries")

# unit: routed_path reports what the router dispatches on, not the Host-derived URL
class _Req:
    def __init__(self, scope):
        self.scope = scope

ok(main.routed_path(_Req({"path": "/api/channels", "root_path": ""})) == "/api/channels",
   "routed_path returns the scope path when no root_path is mounted")
ok(main.routed_path(_Req({"path": "/sub/health", "root_path": "/sub"})) == "/health",
   "routed_path strips a mounted root_path the way starlette.routing does")
ok(main.routed_path(_Req({"path": "/sub", "root_path": "/sub"})) == "",
   "routed_path maps a request for the mount root itself to the empty path")
ok(main.routed_path(_Req({"path": "/subtle/health", "root_path": "/sub"})) == "/subtle/health",
   "routed_path only strips root_path at a segment boundary")

# --- SPA fallback must not read outside dist (BACKLOG 16) ---------------------
# `spa()` joins user-controlled path text onto the dist dir and serves any resulting
# file. Three distinct primitives escaped it. Unit checks pin the helper (the only
# place all three are reachable — httpx normalizes a literal ".." away and eats the
# "//abs/path" form as an authority); the end-to-end block additionally drives the
# two spellings that DO survive httpx, `/%2e%2e/x` and `/%2fabs`, so the route wiring
# itself is covered rather than assumed.
print("SPA fallback file-read containment")
_dist_root = _DIST.resolve()

ok(_contained_file(_dist_root, "index.html") == _dist_root / "index.html",
   "a real file in dist is served")
ok(_contained_file(_dist_root, "assets/index-abc123.css")
   == _dist_root / "assets" / "index-abc123.css",
   "a hashed asset is served")
ok(_contained_file(_dist_root, "nested/deep.js") == _dist_root / "nested" / "deep.js",
   "a nested real asset is served")
ok(_contained_file(_dist_root, "") is None,
   "the empty path falls through to index.html rather than serving the dist dir")
ok(_contained_file(_dist_root, "board") is None,
   "a deep client-side route (no such file) falls through to index.html")
ok(_contained_file(_dist_root, "assets") is None,
   "a directory is not a file, so it falls through")

# primitive 1: parent-directory hops (uvicorn percent-decodes before routing, so
# /%2e%2e/x and /%2e%2e%2fx both arrive here as real ".." segments)
ok(_contained_file(_dist_root, "../token.json") is None,
   "a .. hop out of dist is refused")
ok(_contained_file(_dist_root, "../../../etc/passwd") is None,
   "the canonical /etc/passwd traversal is refused")
ok(_contained_file(_dist_root, "assets/../../token.json") is None,
   "a hop that leaves dist via a real subdirectory is refused")

# primitive 2: an ABSOLUTE rel — pathlib drops the left operand, so `dist / "/x"` IS
# "/x" and escapes with no ".." at all; a "reject any .." fix would miss this entirely.
ok(_contained_file(_dist_root, str(_SECRET)) is None,
   "an absolute path naming a real secret file is refused")

# primitive 3: a symlink planted inside dist that points out of it
_link = _DIST / "escape.json"
_link.symlink_to(_SECRET)
ok(_contained_file(_dist_root, "escape.json") is None,
   "a symlink inside dist pointing outside it is refused, not followed")
_link.unlink()

# unresolvable input must fall through, never raise into the request — each of the
# three guarded exception types is provoked by a different one of these
ok(_contained_file(_dist_root, "\x00") is None,
   "a NUL byte returns None instead of raising (ValueError from resolve)")
ok(_contained_file(_dist_root, "x" * 5000) is None,
   "an absurdly long name returns None instead of raising (OSError from is_file)")
(_DIST / "loopA").symlink_to(_DIST / "loopB")
(_DIST / "loopB").symlink_to(_DIST / "loopA")
ok(_contained_file(_dist_root, "loopA") is None,
   "a symlink loop returns None instead of raising (RuntimeError from resolve)")
(_DIST / "loopA").unlink()
(_DIST / "loopB").unlink()

# a regular file that exists but cannot be opened must not reach FileResponse, which
# would raise PermissionError from inside the response, outside the helper's guard
_noread = _DIST / "unreadable.js"
_noread.write_text("x")
_noread.chmod(0o000)
ok(_contained_file(_dist_root, "unreadable.js") is None,
   "an unreadable regular file falls through instead of 500ing in FileResponse")
_noread.chmod(0o644)
_noread.unlink()

# the root handed to the helper must already be resolved: reached through a symlinked
# ancestor, an unresolved root makes is_relative_to false for every file in dist —
# which is why spa() resolves it (pinned by the live-route checks below, which run
# under /tmp -> /private/tmp and so would fail if it did not).
_alias = _TMP / "dist-alias"
_alias.symlink_to(_DIST)
ok(_contained_file(_alias, "index.html") is None,
   "an UNRESOLVED root serves nothing, so the caller must resolve it")
ok(_contained_file(_alias.resolve(), "index.html") == _dist_root / "index.html",
   "the same root, resolved, serves normally")

# --- end-to-end through the real route, auth OFF (the LAN config where this route is
# reachable by anyone). These fail if spa() stops calling the helper OR stops resolving.
settings.app_password = ""
r5 = client.get("/%2e%2e/token.json")
ok("TOP-SECRET-REFRESH-TOKEN" not in r5.text,
   "unauthenticated /%2e%2e/token.json does not return the secret file's contents")
ok("SPA INDEX" in r5.text, "a .. traversal attempt falls through to index.html")
# %2f is decoded by starlette (not httpx), so this delivers a genuine absolute path —
# the one escape spelling reachable end-to-end. It leaked 200 + secret before the fix.
r6 = client.get("/%2f" + str(_SECRET).lstrip("/"))
ok("TOP-SECRET-REFRESH-TOKEN" not in r6.text,
   "an absolute-path escape via %2f does not return the secret through the live route")
ok("SPA INDEX" in r6.text, "the absolute-path attempt falls through to index.html")
# a real file served BY THE SPA ROUTE (not the /assets mount): the only check here that
# fails if the route stops consulting the helper at all
ok(client.get("/nested/deep.js").text == "// deep asset",
   "a real non-/assets file still serves through the SPA route")
ok(client.get("/assets/index-abc123.css").text == "body{}",
   "a real hashed asset still serves through the /assets StaticFiles mount")
ok("SPA INDEX" in client.get("/board").text,
   "a deep client-side route still serves index.html through the live route")
# index.html must carry no-cache whichever way it was reached (stale index -> blank screen)
ok("no-cache" in client.get("/board").headers.get("cache-control", ""),
   "the fallback index.html is served no-cache")
ok("no-cache" in client.get("/index.html").headers.get("cache-control", ""),
   "index.html requested by name is also served no-cache, not as a cacheable asset")

main.app.dependency_overrides.clear()
settings.app_password = _orig_pw

# --- unit: _read_process_headroom (real reading, not mocked) ------------------
print("_read_process_headroom (real sysctl/ps reading)")
real = main._read_process_headroom()
ok(real is None or (real["max"] > 0 and real["count"] > 0 and 0 <= real["pct_used"] <= 100),
   "real reading is either None (sysctl/ps unavailable) or a sane count/max/pct_used")

print(f"\nALL {_checks} CHECKS PASSED")
