"""Dependency-free regression checks for topic_playlist.ensure_topic_playlist.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_topic_playlist.py

`ensure_topic_playlist` is the lazy playlist-creation choke point: production and
publish both call it, so a regression here means videos land in no playlist (or a
duplicate playlist gets minted, burning 50 quota units, on every tick). It never
had a direct test. Covers every branch:
  - the early returns that must NOT touch the YouTube API (no topic, already
    mapped, channel not CONNECTED)
  - a create_playlist failure logs an error and returns None without mapping the
    topic or leaving a half-written Playlist row
  - get_service NeedsConnect on a CONNECTED channel flips through
    notify.mark_dead_committed (EXPIRED if the token file is present,
    DISCONNECTED if gone), alerts once, mints nothing, and does not log a
    playlist_add error — render is often the first YouTube call during a
    publishing lull (3b-c); a generic create_playlist raise still does not flip
  - the happy path creates the Playlist, maps topic.playlist_id to the new DB
    FK (an int, not the 34-char yt id), and logs the 50-unit quota cost
  - theme_prompt=None is passed to the API as "" (the `or ""` guard)

Uses an in-memory SQLite DB and stubs the YouTube calls — no network, no creds.
Exits non-zero on the first failed assertion.
"""
import sys

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

# Importing app.models defines every table=True model, registering them all on
# SQLModel.metadata so create_all() below builds the full schema.
from app.models import Channel, JobRun, OAuthStatus, Playlist, Topic
from app.services import topic_playlist, youtube

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


def make_topic(session, channel, **kw):
    t = Topic(channel_id=channel.id, name=kw.pop("name", "RAG"), **kw)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def jobruns(session):
    return session.exec(select(JobRun)).all()


def playlists(session):
    return session.exec(select(Playlist)).all()


# Stub the two YouTube seams for every case. get_service records the slug it was
# asked for; create_playlist records its args and returns a valid-looking row.
_ORIG_GET, _ORIG_CREATE = youtube.get_service, youtube.create_playlist
_GOOD_PL = "PL" + "Q" * 32  # a structurally-valid 34-char YouTube playlist id
_calls = {"get_service": [], "create_playlist": []}


def _fake_get_service(slug):
    _calls["get_service"].append(slug)
    return object()  # an opaque service handle; create_playlist stub ignores it


def _fake_create_playlist(service, title, description="", privacy="public"):
    _calls["create_playlist"].append((title, description, privacy))
    return {"yt_playlist_id": _GOOD_PL, "title": title,
            "description": description, "privacy": privacy}


def _reset_calls():
    _calls["get_service"].clear()
    _calls["create_playlist"].clear()


youtube.get_service = _fake_get_service
youtube.create_playlist = _fake_create_playlist

try:
    # --- early returns that must not hit the API -----------------------------
    print("early returns (no YouTube API call)")

    # topic is None -> None, nothing touched
    s = fresh_session()
    ch = make_channel(s)
    _reset_calls()
    ok(topic_playlist.ensure_topic_playlist(s, None, ch) is None,
       "a None topic returns None")
    ok(_calls["create_playlist"] == [], "None topic never calls create_playlist")

    # topic already mapped -> returns the existing FK, no API call, no new row
    s = fresh_session()
    ch = make_channel(s)
    pl = Playlist(channel_id=ch.id, yt_playlist_id=_GOOD_PL, title="RAG")
    s.add(pl); s.commit(); s.refresh(pl)
    t = make_topic(s, ch, playlist_id=pl.id)
    _reset_calls()
    ok(topic_playlist.ensure_topic_playlist(s, t, ch) == pl.id,
       "an already-mapped topic returns its existing playlist FK")
    ok(_calls["create_playlist"] == [], "already-mapped topic never calls create_playlist")
    ok(len(playlists(s)) == 1, "already-mapped topic mints no second playlist")

    # channel not CONNECTED -> None, no API call, no row (will retry later)
    for bad in (OAuthStatus.EXPIRED, OAuthStatus.DISCONNECTED):
        s = fresh_session()
        ch = make_channel(s, oauth_status=bad)
        t = make_topic(s, ch)
        _reset_calls()
        ok(topic_playlist.ensure_topic_playlist(s, t, ch) is None,
           f"a {bad} channel returns None instead of creating")
        ok(_calls["create_playlist"] == [],
           f"a {bad} channel never calls create_playlist")
        ok(playlists(s) == [], f"a {bad} channel creates no playlist row")
        s.refresh(t)
        ok(t.playlist_id is None, f"a {bad} channel leaves topic unmapped")

    # --- create_playlist failure ---------------------------------------------
    print("create_playlist failure -> logged error, no mapping")

    def _raise_create(*a, **k):
        raise RuntimeError("boom from the API")

    youtube.create_playlist = _raise_create
    s = fresh_session()
    ch = make_channel(s)
    t = make_topic(s, ch, name="Agents")
    _reset_calls()
    ok(topic_playlist.ensure_topic_playlist(s, t, ch) is None,
       "a create_playlist error returns None")
    ok(_calls["get_service"] == [ch.slug], "the failing path still resolved the service by slug")
    ok(playlists(s) == [], "a failed create leaves no half-written Playlist row")
    s.refresh(t)
    ok(t.playlist_id is None, "a failed create leaves the topic unmapped")
    errs = [j for j in jobruns(s) if j.kind == "playlist_add" and j.status == "error"]
    ok(len(errs) == 1, "a failed create logs exactly one playlist_add error")
    ok("Agents" in (errs[0].detail or "") and "failed" in (errs[0].detail or ""),
       "the error log names the topic and says it failed")
    ok(errs[0].quota_cost == 0, "a failed create records no quota spend")
    s.refresh(ch)
    ok(ch.oauth_status == OAuthStatus.CONNECTED,
       "a generic create_playlist error does not flip the channel dead")
    youtube.create_playlist = _fake_create_playlist  # restore for the happy path

    # --- happy path ----------------------------------------------------------
    print("happy path -> playlist created, topic mapped, quota logged")
    s = fresh_session()
    ch = make_channel(s, slug="ch-happy", default_privacy="unlisted")
    t = make_topic(s, ch, name="Vector DBs", theme_prompt="how embeddings work")
    _reset_calls()
    ret = topic_playlist.ensure_topic_playlist(s, t, ch)
    s.refresh(t)
    new_pl = s.get(Playlist, t.playlist_id) if t.playlist_id is not None else None
    ok(_calls["get_service"] == ["ch-happy"], "get_service is resolved by the channel slug")
    ok(_calls["create_playlist"] == [("Vector DBs", "how embeddings work", "unlisted")],
       "create_playlist gets the topic name, theme prompt, and channel default privacy")
    ok(new_pl is not None, "a Playlist row is created")
    ok(ret == new_pl.id, "the return value is the new DB playlist id (the FK)")
    ok(isinstance(ret, int) and ret != _GOOD_PL,
       "the return is the integer FK, not the 34-char YouTube id")
    ok(new_pl.yt_playlist_id == _GOOD_PL, "the Playlist row stores the real YouTube id")
    ok(new_pl.channel_id == ch.id, "the Playlist belongs to the channel")
    ok(new_pl.last_synced_at is not None, "the new Playlist is stamped last_synced_at")
    ok(t.playlist_id == new_pl.id, "the topic is mapped to the new playlist FK")
    succ = [j for j in jobruns(s) if j.kind == "playlist_add" and j.status == "success"]
    ok(len(succ) == 1, "the happy path logs exactly one playlist_add success")
    ok(succ[0].quota_cost == youtube.QUOTA_PLAYLIST_INSERT,
       "the success log records the 50-unit playlist-insert quota cost")

    # --- theme_prompt=None is normalized to "" before the API call -----------
    print("theme_prompt=None -> passed to the API as empty string")
    s = fresh_session()
    ch = make_channel(s)
    t = make_topic(s, ch, name="No Prompt", theme_prompt=None)
    _reset_calls()
    topic_playlist.ensure_topic_playlist(s, t, ch)
    ok(_calls["create_playlist"] == [("No Prompt", "", "public")],
       "a None theme_prompt is sent as '' (never the string 'None')")

    # --- NeedsConnect on a CONNECTED channel flips dead (3b-c) ---------------
    # The generic `except Exception` used to log playlist_add and leave
    # CONNECTED; the next metrics/analytics tick caught it. Render is often
    # the first YouTube call during a publishing lull, so this is the alert.
    print("NeedsConnect -> mark_dead, no playlist mint, no playlist_add error")

    from app.services import notify

    def _raise_needs(slug):
        _calls["get_service"].append(slug)
        raise youtube.NeedsConnect(f"token dead for {slug}")

    _ORIG_HAS = youtube.has_token
    youtube.get_service = _raise_needs
    youtube.has_token = lambda slug: True
    _alerts = []
    _orig_alert = notify.alert_dead

    def _capture_alert(*a, **k):
        _alerts.append((a, k))

    notify.alert_dead = _capture_alert
    try:
        s = fresh_session()
        ch = make_channel(s, slug="ch-dead")
        sibling = make_channel(s, slug="ch-sib")
        t = make_topic(s, ch, name="Agents")
        _reset_calls()
        ok(topic_playlist.ensure_topic_playlist(s, t, ch) is None,
           "NeedsConnect returns None")
        ok(_calls["create_playlist"] == [],
           "NeedsConnect never calls create_playlist")
        ok(_calls["get_service"] == ["ch-dead"],
           "NeedsConnect still resolved the service by slug")
        ok(playlists(s) == [], "NeedsConnect leaves no Playlist row")
        s.refresh(t)
        ok(t.playlist_id is None, "NeedsConnect leaves the topic unmapped")
        s.refresh(ch)
        ok(ch.oauth_status == OAuthStatus.EXPIRED,
           "NeedsConnect flips a CONNECTED channel to EXPIRED (committed)")
        s.refresh(sibling)
        ok(sibling.oauth_status == OAuthStatus.CONNECTED,
           "NeedsConnect leaves a sibling CONNECTED channel untouched")
        ok([j for j in jobruns(s) if j.kind == "playlist_add"] == [],
           "NeedsConnect is not a playlist_add error (the flip is the signal)")
        ok(len(_alerts) == 1, "NeedsConnect alerts exactly once")

        _reset_calls()
        ok(topic_playlist.ensure_topic_playlist(s, t, ch) is None,
           "a repeat on the now-EXPIRED channel still returns None")
        ok(_calls["get_service"] == [],
           "a repeat never re-probes get_service (early-return on not CONNECTED)")
        ok(len(_alerts) == 1, "a repeat stays silent")

        youtube.has_token = lambda slug: False
        s2 = fresh_session()
        ch2 = make_channel(s2, slug="ch-gone")
        t2 = make_topic(s2, ch2)
        _alerts.clear()
        _reset_calls()
        ok(topic_playlist.ensure_topic_playlist(s2, t2, ch2) is None,
           "missing token file still returns None")
        s2.refresh(ch2)
        ok(ch2.oauth_status == OAuthStatus.DISCONNECTED,
           "missing token file classifies DISCONNECTED, not EXPIRED")
        ok(len(_alerts) == 1, "missing token file alerts exactly once")
    finally:
        youtube.get_service = _fake_get_service
        youtube.has_token = _ORIG_HAS
        notify.alert_dead = _orig_alert
        youtube.create_playlist = _fake_create_playlist
finally:
    youtube.get_service, youtube.create_playlist = _ORIG_GET, _ORIG_CREATE

print(f"\nALL {_checks} CHECKS PASSED")
