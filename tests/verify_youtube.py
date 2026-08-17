"""Dependency-free regression checks for app/services/youtube.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_youtube.py

``youtube.py`` is the money-path YouTube client (793 lines). Consent/token
guards already live in ``verify_oauth_redirect.py`` / ``verify_reconnect.py``;
publish/analytics *callers* stub this module. This suite owns the untested
body those callers trust:

  - error classification (the 07-12 comment 403, the uploadLimitExceeded
    400-vs-403 trap, daily-cap vs short-term throttle, playlistNotFound)
  - ``_upload_body`` clamps + language tags (discovery for the PT-BR channel)
  - request wrappers driven against a local fake Data/Analytics service
    (upload stall vs classify, comment clamp, playlist/channel mapping,
    analytics normalisation + traffic-source best-effort)

No network, no real credentials, temp dirs only. Exits non-zero on the first
failed assertion.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httplib2
from googleapiclient.errors import HttpError

from app.config import settings
from app.services import youtube

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def http_error(status: int, reason: str = "", content: bytes | None = None) -> HttpError:
    resp = httplib2.Response({"status": status})
    resp.reason = "error"
    if content is not None:
        body = content
    elif reason:
        body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    else:
        body = b"{}"
    return HttpError(resp, body)


# Isolate path helpers from the live credentials/ tree.
_ORIG_CREDS = settings.credentials_dir
_TMP = tempfile.mkdtemp(prefix="verify-youtube-")
settings.credentials_dir = _TMP


_restored = False


def _restore_creds_dir():
    global _restored
    if _restored:
        return
    _restored = True
    settings.credentials_dir = _ORIG_CREDS
    shutil.rmtree(_TMP, ignore_errors=True)


atexit.register(_restore_creds_dir)


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------
print("module contracts")
ok(youtube.CATEGORY_SCIENCE_TECH == "28", "Science & Tech categoryId is 28")
ok(youtube.SCOPES == ["https://www.googleapis.com/auth/youtube"],
   "Data-API SCOPES is youtube-only (unpinned refresh; 07-12 force-ssl strip)")
ok("https://www.googleapis.com/auth/youtube.force-ssl" in youtube.CONSENT_SCOPES,
   "CONSENT_SCOPES includes force-ssl (commentThreads.insert)")
ok("https://www.googleapis.com/auth/yt-analytics.readonly" in youtube.CONSENT_SCOPES,
   "CONSENT_SCOPES includes analytics.readonly")
ok(all("force-ssl" not in s and "analytics" not in s for s in youtube.SCOPES),
   "SCOPES itself must not pin force-ssl or analytics (refresh would narrow)")
ok(youtube.SELECT_ALL_HINT == "click 'Select all' (\"Selecionar tudo\")",
   "SELECT_ALL_HINT EN+PT wording")
ok(youtube.QUOTA_UPLOAD == 1600, "upload costs 1600 units")
ok(youtube.QUOTA_PLAYLIST_INSERT == 50, "playlist insert costs 50")
ok(youtube.QUOTA_PLAYLISTITEM_INSERT == 50, "playlist item insert costs 50")
ok(youtube.QUOTA_CHANNEL_UPDATE == 50, "channel update costs 50")
ok(youtube.QUOTA_SUBSCRIPTION_WRITE == 50, "subscription write costs 50")
ok(youtube.QUOTA_THUMBNAIL_SET == 50, "thumbnail set costs 50")
ok(youtube.QUOTA_COMMENT_INSERT == 50, "comment insert costs 50")
ok(youtube.QUOTA_LIST == 1, "list costs 1")
ok(youtube.QUOTA_ANALYTICS_QUERY == 1, "analytics query costs 1")
ok(youtube._DAILY_CAP_REASONS == {
    "uploadlimitexceeded", "quotaexceeded", "dailylimitexceeded",
}, "daily-cap set is exactly the three YouTube daily reasons (no rate-limit)")


# ---------------------------------------------------------------------------
# _error_reason
# ---------------------------------------------------------------------------
print("\n_error_reason")
ok(youtube._error_reason(http_error(403, "uploadLimitExceeded")) == "uploadlimitexceeded",
   "JSON reason is lowercased")
ok(youtube._error_reason(http_error(400, "quotaExceeded")) == "quotaexceeded",
   "quotaExceeded extracts from JSON")
ok(youtube._error_reason(http_error(403, "dailyLimitExceeded")) == "dailylimitexceeded",
   "dailyLimitExceeded extracts from JSON")
ok(youtube._error_reason(http_error(403, "rateLimitExceeded")) == "ratelimitexceeded",
   "rateLimitExceeded extracts (classify must NOT treat it as a daily cap)")
ok(youtube._error_reason(http_error(403, "playlistNotFound")) == "playlistnotfound",
   "playlistNotFound extracts")

# JSON reason wins even when another field's text contains a daily-cap token.
# (json.loads rejects trailing garbage, so the token has to live inside the
# document — otherwise this pin is just the malformed-body scan again.)
mixed = http_error(403, content=json.dumps({
    "error": {"errors": [{
        "reason": "videoNotFound",
        "message": "looks like quotaexceeded but isn't the reason",
    }]},
}).encode())
ok(youtube._error_reason(mixed) == "videonotfound",
   "well-formed JSON reason wins over a daily-cap token in another field")

# Body-scan fallback: each daily-cap reason, isolated so set-iteration order
# cannot hide a dropped token.
for token in ("uploadlimitexceeded", "quotaexceeded", "dailylimitexceeded"):
    raw = http_error(400, content=f"not-json {token} trailing".encode())
    ok(youtube._error_reason(raw) == token,
       f"malformed body scans for {token}")

ok(youtube._error_reason(http_error(400, content=b"not-json UploadLimitExceeded"))
   == "uploadlimitexceeded",
   "body scan is case-insensitive (body is lowercased before match)")
ok(youtube._error_reason(http_error(400, content=b"not-json no-known-reason")) == "",
   "malformed body with no daily-cap token -> empty string")
ok(youtube._error_reason(http_error(400, content=b"")) == "",
   "empty body -> empty string")

# Missing errors[0].reason is a KeyError inside the try — fall through to
# body scan of the same (valid) document.
no_reason = http_error(403, content=json.dumps({
    "error": {"errors": [{}], "message": "quotaexceeded"},
}).encode())
ok(youtube._error_reason(no_reason) == "quotaexceeded",
   "JSON missing reason falls through to body scan")


# ---------------------------------------------------------------------------
# _classify — daily cap vs pass-through (status is irrelevant)
# ---------------------------------------------------------------------------
print("\n_classify")
for status, reason in (
    (400, "uploadLimitExceeded"),
    (403, "uploadLimitExceeded"),
    (403, "quotaExceeded"),
    (403, "dailyLimitExceeded"),
):
    classified = youtube._classify(http_error(status, reason))
    ok(isinstance(classified, youtube.QuotaExceeded),
       f"{reason} @{status} -> QuotaExceeded (not a hard fail)")
    ok(classified.reason == reason.lower(),
       f"{reason} @{status} carries lowercased .reason for cooldown sizing")

for status, reason in (
    (403, "rateLimitExceeded"),
    (403, "userRateLimitExceeded"),
    (403, "uploadRateLimitExceeded"),
    (400, "invalidVideoMetadata"),
    (404, "videoNotFound"),
):
    classified = youtube._classify(http_error(status, reason))
    ok(isinstance(classified, HttpError) and not isinstance(classified, youtube.QuotaExceeded),
       f"{reason} @{status} is NOT a daily cap (brief backoff, not a day cooldown)")

# Identity: classify returns the original HttpError object on the pass-through
# path (callers may inspect .resp).
passthrough = http_error(403, "rateLimitExceeded")
ok(youtube._classify(passthrough) is passthrough,
   "non-cap classify returns the same HttpError instance")


# ---------------------------------------------------------------------------
# is_playlist_missing
# ---------------------------------------------------------------------------
print("\nis_playlist_missing")
ok(youtube.is_playlist_missing(http_error(404, "playlistNotFound")) is True,
   "playlistNotFound @404 -> True")
ok(youtube.is_playlist_missing(http_error(403, "playlistNotFound")) is True,
   "playlistNotFound @403 -> True (reason, not status)")
ok(youtube.is_playlist_missing(http_error(404)) is True,
   "bare HTTP 404 with no reason -> True")
ok(youtube.is_playlist_missing(http_error(404, "quotaExceeded")) is True,
   "404 even with a different JSON reason -> True (status fallback)")
ok(youtube.is_playlist_missing(http_error(403, "playlistForbidden")) is False,
   "403 with a non-missing reason -> False")
ok(youtube.is_playlist_missing(http_error(400, "invalidPlaylistId")) is False,
   "400 invalid playlist is not 'missing'")
ok(youtube.is_playlist_missing(ValueError("nope")) is False,
   "non-HttpError -> False")
ok(youtube.is_playlist_missing(youtube.QuotaExceeded("cap", reason="quotaexceeded")) is False,
   "QuotaExceeded is not a missing playlist")
ok(youtube.is_playlist_missing(youtube.UploadStalled("stalled")) is False,
   "UploadStalled is not a missing playlist")


# ---------------------------------------------------------------------------
# _upload_body
# ---------------------------------------------------------------------------
print("\n_upload_body")
body = youtube._upload_body("T", "D", ["a"], "public", language_code="pt-BR")
ok(body["snippet"]["title"] == "T", "title forwarded")
ok(body["snippet"]["description"] == "D", "description forwarded")
ok(body["snippet"]["tags"] == ["a"], "tags forwarded")
ok(body["snippet"]["categoryId"] == youtube.CATEGORY_SCIENCE_TECH, "category is Science & Tech")
ok(body["snippet"].get("defaultLanguage") == "pt-BR", "defaultLanguage from language_code")
ok("defaultAudioLanguage" in body["snippet"]
   and body["snippet"]["defaultAudioLanguage"] == "pt-BR",
   "defaultAudioLanguage set too (PT-BR discovery)")
ok(body["status"]["privacyStatus"] == "public", "privacy forwarded on the language-tagged body")
body_priv = youtube._upload_body("T", "D", ["a"], "unlisted")
ok(body_priv["status"]["privacyStatus"] == "unlisted", "privacy forwarded")
ok(body_priv["status"]["selfDeclaredMadeForKids"] is False, "not made-for-kids")
ok("defaultLanguage" not in body_priv["snippet"]
   and "defaultAudioLanguage" not in body_priv["snippet"],
   "language fields omitted when language_code is omitted")

body_empty_lang = youtube._upload_body("T", "D", ["a"], "public", language_code="")
ok("defaultLanguage" not in body_empty_lang["snippet"]
   and "defaultAudioLanguage" not in body_empty_lang["snippet"],
   "empty language_code is falsy -> language fields omitted")

ok(youtube._upload_body(None, None, None, "private")["snippet"]["title"] == "",
   "None title -> empty string (not the string 'None')")
ok(youtube._upload_body(None, None, None, "private")["snippet"]["description"] == "",
   "None description -> empty string")
ok(youtube._upload_body(None, None, None, "private")["snippet"]["tags"] == [],
   "None tags -> []")

long_title = "t" * 150
ok(youtube._upload_body(long_title, "D", ["a"], "public")["snippet"]["title"]
   == "t" * 100, "title clamped to YouTube's 100-char limit")
long_desc = "d" * 6000
ok(len(youtube._upload_body("T", long_desc, ["a"], "public")["snippet"]["description"])
   == 5000, "description clamped to YouTube's 5000-char limit")
ok(youtube._upload_body("T", "D", [f"tag{i}" for i in range(50)], "public")["snippet"]["tags"]
   == [f"tag{i}" for i in range(30)], "tags clamped to 30")

# The language-omitted body must still carry the other snippet keys.
ok(set(body_priv["snippet"]) == {"title", "description", "tags", "categoryId"},
   "no-language snippet is exactly the four always-on keys")


# ---------------------------------------------------------------------------
# Path helpers + NeedsConnect gates (temp dir; never the live credentials/)
# ---------------------------------------------------------------------------
print("\npath helpers + NeedsConnect")
ok(youtube.channel_dir("ch1") == Path(_TMP) / "ch1", "channel_dir under credentials_dir")
ok(youtube.client_secret_path("ch1") == Path(_TMP) / "ch1" / "client_secret.json",
   "client_secret_path")
ok(youtube.token_path("ch1") == Path(_TMP) / "ch1" / "token.json", "token_path")
ok(youtube.has_client_secret("ghost") is False, "missing slug -> no client_secret")
ok(youtube.has_token("ghost") is False, "missing slug -> no token")

try:
    youtube.get_service("ghost")
    ok(False, "get_service missing secret must raise")
except youtube.NeedsConnect as e:
    ok("client_secret.json" in str(e) and "ghost" in str(e),
       "get_service missing secret names the slug + client_secret.json")

try:
    youtube.get_analytics_service("ghost")
    ok(False, "get_analytics_service missing secret must raise")
except youtube.NeedsConnect as e:
    ok("client_secret.json" in str(e) and "ghost" in str(e),
       "get_analytics_service missing secret names the slug + client_secret.json")

os.makedirs(Path(_TMP) / "secret-only", exist_ok=True)
(Path(_TMP) / "secret-only" / "client_secret.json").write_text("{}")
ok(youtube.has_client_secret("secret-only") is True, "secret-only has_client_secret")
ok(youtube.has_token("secret-only") is False, "secret-only has no token")
try:
    youtube.get_service("secret-only")
    ok(False, "get_service secret-but-no-token must raise")
except youtube.NeedsConnect as e:
    ok("token missing/expired" in str(e) and "secret-only" in str(e),
       "get_service no-token message is the reconnect recipe")
try:
    youtube.get_analytics_service("secret-only")
    ok(False, "get_analytics_service secret-but-no-token must raise")
except youtube.NeedsConnect as e:
    ok("token missing/expired" in str(e) and "secret-only" in str(e),
       "get_analytics_service no-token message is the reconnect recipe")

os.makedirs(Path(_TMP) / "both", exist_ok=True)
(Path(_TMP) / "both" / "client_secret.json").write_text("{}")
(Path(_TMP) / "both" / "token.json").write_text("{}")
ok(youtube.has_token("both") is True, "token.json present -> has_token True")
ok(youtube.has_client_secret("both") is True, "both files present")


# ---------------------------------------------------------------------------
# Fake Data API service
# ---------------------------------------------------------------------------
class _Exec:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _InsertReq:
    """videos().insert request: next_chunk() iterator."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def next_chunk(self):
        if not self._chunks:
            raise RuntimeError("next_chunk exhausted")
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeData:
    def __init__(self):
        self.calls = []
        self.insert_chunks = [(None, {"id": "ytvid1"})]
        self.comment_result = {"id": "cmt1"}
        self.comment_error = None
        self.thumb_error = None
        self.playlist_insert = {
            "id": "PLnewplaylistid012345678901234",
            "snippet": {"title": "T", "description": "D"},
            "status": {"privacyStatus": "unlisted"},
        }
        self.playlist_insert_error = None
        self.playlist_item_id = "item1"
        self.playlist_item_error = None
        self.playlist_pages = [{"items": [], "nextPageToken": None}]
        self.channel_list = {"items": []}
        self.channel_list_error = None
        self.channel_update = {"brandingSettings": {"channel": {}}}
        self.channel_update_error = None
        self.channel_update_bodies = []
        self.sub_pages = [{"items": [], "nextPageToken": None}]
        self.sub_error = None
        self.subscriber_pages = [{"items": [], "nextPageToken": None}]
        self.subscriber_error = None
        self.handle_list = {"items": []}
        self.handle_error = None
        self.sub_insert = {"id": "sub1"}
        self.sub_insert_error = None
        self.sub_delete_error = None
        self.deleted = []
        self.last_comment_body = None
        self.last_playlist_body = None
        self.last_playlist_item_body = None
        self.last_insert_body = None

    def insert(self, part, body, media_body):
        self.calls.append(("videos.insert", part, body))
        self.last_insert_body = body
        return _InsertReq(self.insert_chunks)

    def set(self, videoId, media_body):
        self.calls.append(("thumbnails.set", videoId))
        return _Exec(error=self.thumb_error)

    def list(self, **kw):
        self.calls.append(("list", kw))
        if kw.get("mine") is True and kw.get("part") == "snippet,status":
            page = self.playlist_pages.pop(0) if self.playlist_pages else {"items": []}
            return _Exec(page)
        if kw.get("part") == "snippet,statistics,brandingSettings":
            return _Exec(self.channel_list, error=self.channel_list_error)
        if kw.get("forHandle") is not None:
            return _Exec(self.handle_list, error=self.handle_error)
        if kw.get("mine") and kw.get("part") == "snippet":
            if self.sub_error:
                return _Exec(error=self.sub_error)
            page = self.sub_pages.pop(0) if self.sub_pages else {"items": []}
            return _Exec(page)
        if kw.get("mySubscribers"):
            if self.subscriber_error:
                return _Exec(error=self.subscriber_error)
            page = self.subscriber_pages.pop(0) if self.subscriber_pages else {"items": []}
            return _Exec(page)
        return _Exec({"items": []})

    def update(self, part, body):
        self.calls.append(("channels.update", part, body))
        self.channel_update_bodies.append(body)
        return _Exec(self.channel_update, error=self.channel_update_error)

    def execute_comment_insert(self, part, body):
        self.last_comment_body = body
        self.calls.append(("commentThreads.insert", part, body))
        return _Exec(self.comment_result, error=self.comment_error)

    def execute_playlist_insert(self, part, body):
        self.last_playlist_body = body
        self.calls.append(("playlists.insert", part, body))
        return _Exec(self.playlist_insert, error=self.playlist_insert_error)

    def execute_playlistitem_insert(self, part, body):
        self.last_playlist_item_body = body
        self.calls.append(("playlistItems.insert", part, body))
        return _Exec({"id": self.playlist_item_id}, error=self.playlist_item_error)

    def execute_sub_insert(self, part, body):
        self.calls.append(("subscriptions.insert", part, body))
        return _Exec(self.sub_insert, error=self.sub_insert_error)

    def delete(self, **kw):
        self.deleted.append(kw)
        return _Exec(error=self.sub_delete_error)


def _bind_insert(fake: FakeData, kind: str):
    """Return a thin object whose insert() routes to the right FakeData method."""

    class _Res:
        def insert(self, part, body, media_body=None):
            if kind == "videos":
                return fake.insert(part, body, media_body)
            if kind == "comment":
                return fake.execute_comment_insert(part, body)
            if kind == "playlists":
                return fake.execute_playlist_insert(part, body)
            if kind == "playlistItems":
                return fake.execute_playlistitem_insert(part, body)
            if kind == "subs":
                return fake.execute_sub_insert(part, body)
            raise AssertionError(kind)

        def set(self, **kw):
            return fake.set(**kw)

        def list(self, **kw):
            return fake.list(**kw)

        def update(self, **kw):
            return fake.update(**kw)

        def delete(self, **kw):
            return fake.delete(**kw)

    return _Res()


class YT:
    """Service object matching youtube.py's chained resource calls."""

    def __init__(self, fake: FakeData):
        self.fake = fake

    def videos(self):
        return _bind_insert(self.fake, "videos")

    def commentThreads(self):
        return _bind_insert(self.fake, "comment")

    def thumbnails(self):
        return _bind_insert(self.fake, "videos")

    def playlists(self):
        return _bind_insert(self.fake, "playlists")

    def playlistItems(self):
        return _bind_insert(self.fake, "playlistItems")

    def channels(self):
        return _bind_insert(self.fake, "videos")

    def subscriptions(self):
        return _bind_insert(self.fake, "subs")


# A tiny real file so MediaFileUpload can open it.
_MEDIA = Path(_TMP) / "clip.mp4"
_MEDIA.write_bytes(b"\x00\x00")
_PNG = Path(_TMP) / "thumb.png"
_PNG.write_bytes(b"\x89PNG")


# ---------------------------------------------------------------------------
# upload_video
# ---------------------------------------------------------------------------
print("\nupload_video")
fake = FakeData()
progress = []
fake.insert_chunks = [
    (SimpleNamespace(progress=lambda: 0.5), None),
    (None, {"id": "ytvid1"}),
]
vid = youtube.upload_video(
    YT(fake), str(_MEDIA), "Title", "Desc", ["t1"], "public",
    progress_cb=progress.append, language_code="pt-BR",
)
ok(vid == "ytvid1", "upload_video returns the YouTube video id")
ok(progress == [50], "progress_cb gets percent from status.progress()")
expected_body = youtube._upload_body(
    "Title", "Desc", ["t1"], "public", language_code="pt-BR")
ok(fake.last_insert_body == expected_body,
   "upload_video sends the full _upload_body (privacy/kids/clamps/both language keys)")
ok(any(c[0] == "videos.insert" and c[1] == "snippet,status" for c in fake.calls),
   "videos().insert part is snippet,status (privacy lives on status)")

# HttpError on a chunk is classified (daily cap -> QuotaExceeded).
fake = FakeData()
fake.insert_chunks = [http_error(403, "uploadLimitExceeded")]
try:
    youtube.upload_video(YT(fake), str(_MEDIA), "T", "D", [], "public")
    ok(False, "upload cap must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "uploadlimitexceeded",
       "upload_video classifies uploadLimitExceeded as QuotaExceeded")

# Non-cap HttpError is re-raised as HttpError (not QuotaExceeded, not stall).
fake = FakeData()
fake.insert_chunks = [http_error(400, "invalidVideoMetadata")]
try:
    youtube.upload_video(YT(fake), str(_MEDIA), "T", "D", [], "public")
    ok(False, "invalid metadata must raise")
except youtube.QuotaExceeded:
    ok(False, "invalid metadata is not a daily cap")
except HttpError:
    ok(True, "non-cap HttpError is re-raised as HttpError")

# OSError / httplib2 -> UploadStalled (the publish-loop retry path).
fake = FakeData()
fake.insert_chunks = [OSError("timed out")]
try:
    youtube.upload_video(YT(fake), str(_MEDIA), "T", "D", [], "public")
    ok(False, "OSError must stall")
except youtube.UploadStalled as e:
    msg = str(e)
    ok("OSError" in msg and "timed out" in msg, "stall message names OSError + cause")
    ok(str(settings.youtube_http_timeout_seconds) in msg,
       "stall message carries youtube_http_timeout_seconds")
    ok("upload stalled" in msg, "stall message says 'upload stalled'")
except OSError:
    ok(False, "OSError must be wrapped in UploadStalled (publish-loop retry path)")

fake = FakeData()
fake.insert_chunks = [httplib2.HttpLib2Error("reset")]
try:
    youtube.upload_video(YT(fake), str(_MEDIA), "T", "D", [], "public")
    ok(False, "HttpLib2Error must stall")
except youtube.UploadStalled as e:
    ok("HttpLib2Error" in str(e) and "reset" in str(e),
       "httplib2 error is a stall, not a hard fail")
except httplib2.HttpLib2Error:
    ok(False, "HttpLib2Error must be wrapped in UploadStalled")

# progress_cb is not called when status is None (final chunk).
fake = FakeData()
hits = []
fake.insert_chunks = [(None, {"id": "only"})]
ok(youtube.upload_video(YT(fake), str(_MEDIA), "T", "D", [], "public",
                       progress_cb=hits.append) == "only",
   "final chunk with status=None still returns the id")
ok(hits == [], "progress_cb skipped when status is None")

# Default progress_cb=None must not be invoked even when status is truthy.
fake = FakeData()
fake.insert_chunks = [
    (SimpleNamespace(progress=lambda: 0.25), None),
    (None, {"id": "noprog"}),
]
try:
    vid = youtube.upload_video(YT(fake), str(_MEDIA), "T", "D", [], "public")
except TypeError as e:
    ok(False, f"progress_cb=None must be skipped, not called ({e})")
    vid = None
ok(vid == "noprog", "upload with default progress_cb=None still returns the id")


# ---------------------------------------------------------------------------
# insert_comment / set_thumbnail
# ---------------------------------------------------------------------------
print("\ninsert_comment / set_thumbnail")
fake = FakeData()
ok(youtube.insert_comment(YT(fake), "vid1", "hello") == "cmt1",
   "insert_comment returns the thread id")
ok(fake.last_comment_body["snippet"]["videoId"] == "vid1", "comment videoId")
ok(fake.last_comment_body["snippet"]["topLevelComment"]["snippet"]["textOriginal"]
   == "hello", "comment text forwarded")

long_cmt = "c" * 10_000
fake = FakeData()
youtube.insert_comment(YT(fake), "vid1", long_cmt)
ok(fake.last_comment_body["snippet"]["topLevelComment"]["snippet"]["textOriginal"]
   == "c" * 9000, "comment text clamped to 9000")

fake = FakeData()
fake.comment_error = http_error(403, "quotaExceeded")
try:
    youtube.insert_comment(YT(fake), "vid1", "x")
    ok(False, "comment quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "insert_comment classifies daily-cap HttpError")

fake = FakeData()
youtube.set_thumbnail(YT(fake), "vid1", str(_PNG))
ok(("thumbnails.set", "vid1") in fake.calls, "set_thumbnail hits thumbnails.set")

fake = FakeData()
fake.thumb_error = http_error(403, "forbidden")
try:
    youtube.set_thumbnail(YT(fake), "vid1", str(_PNG))
    ok(False, "thumbnail 403 must raise")
except HttpError:
    ok(True, "set_thumbnail classifies (non-cap 403 stays HttpError)")
except youtube.QuotaExceeded:
    ok(False, "forbidden is not a daily cap")


# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------
print("\nplaylists")
fake = FakeData()
created = youtube.create_playlist(YT(fake), "My PL", "desc", "unlisted")
ok(created == {
    "yt_playlist_id": "PLnewplaylistid012345678901234",
    "title": "T",
    "description": "D",
    "privacy": "unlisted",
}, "create_playlist maps id/title/description/privacy")
ok(fake.last_playlist_body["snippet"]["title"] == "My PL", "create title forwarded")
ok(fake.last_playlist_body["snippet"]["description"] == "desc", "create desc forwarded")
ok(fake.last_playlist_body["status"]["privacyStatus"] == "unlisted", "create privacy")

fake = FakeData()
fake.playlist_insert_error = http_error(403, "quotaExceeded")
try:
    youtube.create_playlist(YT(fake), "X")
    ok(False, "create_playlist quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "create_playlist classifies daily cap")

fake = FakeData()
ok(youtube.add_to_playlist(YT(fake), "PLxx", "vid1") == "item1",
   "add_to_playlist returns the playlistItem id")
ok(fake.last_playlist_item_body["snippet"]["playlistId"] == "PLxx", "add playlistId")
ok(fake.last_playlist_item_body["snippet"]["resourceId"]
   == {"kind": "youtube#video", "videoId": "vid1"}, "add resourceId")

fake = FakeData()
fake.playlist_item_error = http_error(404, "playlistNotFound")
try:
    youtube.add_to_playlist(YT(fake), "PLdead", "vid1")
    ok(False, "add 404 must raise")
except HttpError as e:
    ok(youtube.is_playlist_missing(e) is True,
       "add_to_playlist 404 is a missing-playlist HttpError (publish loop heals)")

# list_playlists pagination + field mapping (missing description/privacy).
fake = FakeData()
fake.playlist_pages = [
    {"items": [{
        "id": "PL1",
        "snippet": {"title": "One"},
        "status": {"privacyStatus": "public"},
    }], "nextPageToken": "p2"},
    {"items": [{
        "id": "PL2",
        "snippet": {"title": "Two", "description": "d2"},
    }], "nextPageToken": None},
]
listed = youtube.list_playlists(YT(fake))
ok(listed == [
    {"yt_playlist_id": "PL1", "title": "One", "description": None, "privacy": "public"},
    {"yt_playlist_id": "PL2", "title": "Two", "description": "d2", "privacy": None},
], "list_playlists paginates and defaults missing description/privacy to None")
pl_lists = [c[1] for c in fake.calls
            if c[0] == "list" and c[1].get("part") == "snippet,status"]
ok(len(pl_lists) == 2, "list_playlists makes exactly two list calls")
ok(pl_lists[0].get("mine") is True, "list_playlists requests mine=True")
ok(pl_lists[0].get("pageToken") is None, "first playlist page has no pageToken")
ok(pl_lists[1].get("pageToken") == "p2",
   "second playlist page forwards nextPageToken (not a local pop-queue)")


# ---------------------------------------------------------------------------
# fetch_channel / update_branding
# ---------------------------------------------------------------------------
print("\nfetch_channel / update_branding")
fake = FakeData()
fake.channel_list = {"items": []}
ok(youtube.fetch_channel(YT(fake)) == {}, "no channel -> {}")

fake = FakeData()
fake.channel_list_error = http_error(403, "quotaExceeded")
try:
    youtube.fetch_channel(YT(fake))
    ok(False, "fetch_channel quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "fetch_channel classifies daily cap")

fake = FakeData()
fake.channel_list = {"items": [{
    "id": "UCabcdefghijklmnopqrstuv",
    "snippet": {
        "title": "Owera",
        "thumbnails": {"default": {"url": "http://img"}},
    },
    "statistics": {
        "subscriberCount": "12",
        "viewCount": "not-a-number",
        "videoCount": None,
        # hiddenSubscriberCount omitted
    },
    "brandingSettings": {"channel": {
        "title": "Owera",
        "description": "about",
        "keywords": "ai agents",
        "country": "BR",
        "defaultLanguage": "pt-BR",
    }},
}]}
ch = youtube.fetch_channel(YT(fake))
ok(ch["id"] == "UCabcdefghijklmnopqrstuv", "fetch_channel id")
ok(ch["title"] == "Owera", "fetch_channel title from snippet")
ok(ch["thumbnail"] == "http://img", "fetch_channel default thumbnail")
ok(ch["statistics"]["subscriber_count"] == 12, "subscriberCount coerced to int")
ok(ch["statistics"]["view_count"] == 0, "non-numeric viewCount -> 0")
ok(ch["statistics"]["video_count"] == 0, "None videoCount -> 0")
ok(ch["statistics"]["hidden_subscriber_count"] is False,
   "missing hiddenSubscriberCount -> False")
ok(ch["branding"] == {
    "title": "Owera", "description": "about", "keywords": "ai agents",
    "country": "BR", "default_language": "pt-BR",
}, "branding mapped (defaultLanguage -> default_language)")

# update_branding: None keeps current; "" is dropped from the replacement body.
fake = FakeData()
fake.channel_list = {"items": [{
    "id": "UCxx",
    "snippet": {},
    "statistics": {},
    "brandingSettings": {"channel": {
        "title": "Old", "description": "Keep me", "keywords": "k",
        "country": "US", "defaultLanguage": "en-US",
    }},
}]}
fake.channel_update = {"brandingSettings": {"channel": {
    "title": "New", "description": "Keep me", "keywords": "k",
    "country": "US", "defaultLanguage": "en-US",
}}}
out = youtube.update_branding(YT(fake), "UCxx", title="New")
ok(fake.channel_update_bodies[0]["id"] == "UCxx", "update body carries channel id")
sent = fake.channel_update_bodies[0]["brandingSettings"]["channel"]
ok(sent["title"] == "New", "explicit title overwrites")
ok(sent["description"] == "Keep me", "None description keeps current")
ok(sent.get("defaultLanguage") == "en-US",
   "default_language current remapped to defaultLanguage on the wire")
ok(out["title"] == "New", "update_branding returns the response branding")

fake = FakeData()
fake.channel_list = {"items": [{
    "id": "UCxx", "snippet": {}, "statistics": {},
    "brandingSettings": {"channel": {"title": "Old", "description": "Keep me"}},
}]}
fake.channel_update = {"brandingSettings": {"channel": {"title": "Old"}}}
youtube.update_branding(YT(fake), "UCxx", description="")
sent = fake.channel_update_bodies[0]["brandingSettings"]["channel"]
ok("description" not in sent,
   "explicit empty string is dropped from the replacement payload")
ok(sent.get("title") == "Old", "unrelated current fields still sent")

fake = FakeData()
fake.channel_list = {"items": [{
    "id": "UCxx", "snippet": {}, "statistics": {},
    "brandingSettings": {"channel": {"title": "Old"}},
}]}
fake.channel_update_error = http_error(403, "quotaExceeded")
try:
    youtube.update_branding(YT(fake), "UCxx", title="N")
    ok(False, "update_branding quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "update_branding classifies daily cap")


# ---------------------------------------------------------------------------
# resolve_channel_id
# ---------------------------------------------------------------------------
print("\nresolve_channel_id")
UC = "UCSH53DmossqLTUZdh2PE1Hg"
ok(len(UC) == 24, "fixture is a real-length UC id (UC + 22)")
fake = FakeData()
try:
    ok(youtube.resolve_channel_id(YT(fake), UC) == UC, "bare UC id returned as-is")
except ValueError as e:
    ok(False, f"bare UC id must not take the handle path ({e})")
try:
    ok(youtube.resolve_channel_id(YT(fake), f"https://www.youtube.com/channel/{UC}/videos") == UC,
       "UC id extracted from a /channel/ URL (no API call)")
except ValueError as e:
    ok(False, f"UC URL must not take the handle path ({e})")
ok(not any(c[0] == "list" and "forHandle" in c[1] for c in fake.calls),
   "UC-id path does not call channels().list")

try:
    youtube.resolve_channel_id(YT(fake), "")
    ok(False, "empty ref must raise")
except ValueError as e:
    ok("could not parse" in str(e), "empty ref -> parse ValueError")
try:
    youtube.resolve_channel_id(YT(fake), "   ")
    ok(False, "whitespace ref must raise")
except ValueError as e:
    ok("could not parse" in str(e), "whitespace ref -> parse ValueError")

fake = FakeData()
fake.handle_list = {"items": [{"id": "UChandle0000000000000001"}]}
ok(youtube.resolve_channel_id(YT(fake), "@OweraSoftware") == "UChandle0000000000000001",
   "@handle looks up forHandle")
handle_call = next(c for c in fake.calls if c[0] == "list" and "forHandle" in c[1])
ok(handle_call[1]["forHandle"] == "OweraSoftware",
   "@ prefix stripped before forHandle")

fake = FakeData()
fake.handle_list = {"items": [{"id": "UChandle0000000000000002"}]}
ok(youtube.resolve_channel_id(YT(fake), "https://youtube.com/@Owera.Software/videos")
   == "UChandle0000000000000002",
   "@handle extracted from a URL (dots allowed)")
dot_call = next(c for c in fake.calls if c[0] == "list" and "forHandle" in c[1])
ok(dot_call[1]["forHandle"] == "Owera.Software",
   "handle regex keeps the dot (not truncated at '.')")

fake = FakeData()
fake.handle_list = {"items": []}
try:
    youtube.resolve_channel_id(YT(fake), "@nobody")
    ok(False, "unknown handle must raise")
except ValueError as e:
    ok("no channel found" in str(e) and "@nobody" in str(e),
       "unknown handle -> no-channel ValueError naming the ref")

fake = FakeData()
fake.handle_error = http_error(403, "quotaExceeded")
try:
    youtube.resolve_channel_id(YT(fake), "@x")
    ok(False, "handle lookup quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "resolve_channel_id classifies daily cap")


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------
print("\nsubscriptions")
fake = FakeData()
fake.sub_pages = [
    {"items": [{
        "id": "s1",
        "snippet": {
            "title": "A",
            "description": "d",
            "resourceId": {"channelId": "UCa"},
            "thumbnails": {"default": {"url": "http://a"}},
        },
    }] * 50, "nextPageToken": "n2"},
    {"items": [{
        "id": "s2",
        "snippet": {"title": "B", "resourceId": {"channelId": "UCb"}},
    }] * 50, "nextPageToken": "n3"},
    {"items": [{
        "id": "s3",
        "snippet": {"title": "C", "resourceId": {"channelId": "UCc"}},
    }] * 50, "nextPageToken": None},
]
# max_items is checked AFTER a page is appended: a 50-item page with
# max_items=60 still returns 100. A dropped gate would fetch page 3 and
# return 150; a mid-page slice would return 60; the live post-page gate
# returns 100 and must not consume page 3.
got = youtube.list_subscriptions(YT(fake), max_items=60)
ok(len(got) == 100, "list_subscriptions max_items is a post-page gate (50+50, not 60)")
ok(got[0]["sub_id"] == "s1" and got[0]["channel_id"] == "UCa",
   "subscription mapping (sub_id + channel_id)")
ok(got[0]["thumbnail"] == "http://a", "subscription default thumbnail")
ok(got[50]["title"] == "B" and got[50]["description"] is None,
   "second page mapped; missing description -> None")
sub_lists = [c[1] for c in fake.calls
             if c[0] == "list" and c[1].get("part") == "snippet" and c[1].get("mine")]
ok(len(sub_lists) == 2,
   "post-page gate stops after page 2 (page 3 with next=n3 is not consumed)")
ok(sub_lists[0].get("pageToken") is None, "first subscription page has no pageToken")
ok(sub_lists[1].get("pageToken") == "n2",
   "second subscription page forwards nextPageToken")
ok(len(fake.sub_pages) == 1, "third page left unconsumed in the fake")

fake = FakeData()
fake.sub_error = http_error(403, "quotaExceeded")
try:
    youtube.list_subscriptions(YT(fake))
    ok(False, "list_subscriptions quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "list_subscriptions classifies daily cap")

fake = FakeData()
fake.subscriber_pages = [
    {"items": [{
        "subscriberSnippet": {
            "channelId": "UCsub", "title": "Fan",
            "thumbnails": {"default": {"url": "http://f"}},
        },
    }], "nextPageToken": None},
]
subs = youtube.list_subscribers(YT(fake))
ok(subs == [{"channel_id": "UCsub", "title": "Fan", "thumbnail": "http://f"}],
   "list_subscribers maps subscriberSnippet")

fake = FakeData()
fake.subscriber_error = http_error(403, "quotaExceeded")
try:
    youtube.list_subscribers(YT(fake))
    ok(False, "list_subscribers quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "list_subscribers classifies daily cap")
except HttpError:
    ok(False, "list_subscribers must classify daily cap (not re-raise raw HttpError)")

fake = FakeData()
ok(youtube.subscribe(YT(fake), "UCxx") == {"sub_id": "sub1", "channel_id": "UCxx"},
   "subscribe returns sub_id + requested channel_id")
sub_insert = next(c for c in fake.calls if c[0] == "subscriptions.insert")
ok(sub_insert[1] == "snippet", "subscribe insert part is snippet")
ok(sub_insert[2] == {"snippet": {"resourceId": {
    "kind": "youtube#channel", "channelId": "UCxx",
}}}, "subscribe insert body carries the requested channel id")
fake = FakeData()
fake.sub_insert_error = http_error(403, "quotaExceeded")
try:
    youtube.subscribe(YT(fake), "UCxx")
    ok(False, "subscribe quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "subscribe classifies daily cap")

fake = FakeData()
ok(youtube.unsubscribe(YT(fake), "sub1") is None, "unsubscribe returns None")
ok(fake.deleted == [{"id": "sub1"}], "unsubscribe deletes by sub id")
fake = FakeData()
fake.sub_delete_error = http_error(404, "subscriptionNotFound")
try:
    youtube.unsubscribe(YT(fake), "gone")
    ok(False, "unsubscribe 404 must raise")
except HttpError:
    ok(True, "unsubscribe classifies (non-cap stays HttpError)")


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
print("\nanalytics")


class FakeAnalytics:
    def __init__(self, rows_by_key):
        # rows_by_key: list of responses in call order, or a callable
        self.responses = list(rows_by_key)
        self.queries = []

    def reports(self):
        return self

    def query(self, **kw):
        self.queries.append(kw)
        if not self.responses:
            raise AssertionError(f"unexpected analytics query: {kw}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Exec(item)


# _analytics_row: empty rows -> {}; zip headers with first row.
ana = FakeAnalytics([{
    "rows": [],
    "columnHeaders": [{"name": "views"}],
}])
ok(youtube._analytics_row(ana, "UCc", "v1", "2026-01-01", "2026-01-02", "views") == {},
   "empty rows -> {}")

ana = FakeAnalytics([{
    "rows": [[10, 2.5]],
    "columnHeaders": [{"name": "views"}, {"name": "averageViewPercentage"}],
}])
ok(youtube._analytics_row(ana, "UCc", "v1", "2026-01-01", "2026-01-02", "views")
   == {"views": 10, "averageViewPercentage": 2.5},
   "_analytics_row zips headers to the first row")
ok(ana.queries[0]["ids"] == "channel==UCc", "analytics ids is channel==")
ok(ana.queries[0]["filters"] == "video==v1", "analytics filters is video==")
ok(ana.queries[0]["maxResults"] == 1, "analytics row query is maxResults=1")
ok(ana.queries[0]["dimensions"] == "video", "analytics row is per-video, not channel totals")
ok(ana.queries[0]["startDate"] == "2026-01-01"
   and ana.queries[0]["endDate"] == "2026-01-02",
   "analytics row forwards the caller's date range in order")

# fetch_video_analytics normalisation + the 07-09 impressions audit.
ana = FakeAnalytics([{
    "rows": [[9, 3, 40.0, 12.5, 2, 1, 0]],
    "columnHeaders": [
        {"name": "views"}, {"name": "estimatedMinutesWatched"},
        {"name": "averageViewPercentage"}, {"name": "averageViewDuration"},
        {"name": "likes"}, {"name": "comments"}, {"name": "subscribersGained"},
    ],
}])
got = youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["views"] == 9, "views")
ok(got["watch_time_minutes"] == 3, "estimatedMinutesWatched -> watch_time_minutes")
ok(got["avg_view_pct"] == 40.0, "averageViewPercentage -> avg_view_pct (float)")
ok(got["average_view_duration"] == 12.5, "averageViewDuration float")
ok(got["likes"] == 2 and got["comments"] == 1, "likes/comments")
ok(got["subscribers_gained"] == 0, "subscribersGained")
ok(got["impressions"] == 0 and got["ctr"] == 0.0,
   "impressions/ctr stay 0 (API does not expose them; 07-09 fabricated-default audit)")
metrics_arg = ana.queries[0]["metrics"]
ok(metrics_arg == (
    "views,estimatedMinutesWatched,averageViewPercentage,averageViewDuration,"
    "likes,comments,subscribersGained"
), "analytics metrics string is exactly the stored fields (a dropped likes "
   "would fabricate zeros forever)")
ok("impressions" not in metrics_arg and "impressionClickThroughRate" not in metrics_arg,
   "analytics query does not request the Studio-only impressions fields")

# Missing / None / bad casts -> 0 (never raise).
ana = FakeAnalytics([{"rows": None, "columnHeaders": []}])
got = youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["views"] == 0 and got["avg_view_pct"] == 0.0,
   "null rows -> zeros for every metric")

ana = FakeAnalytics([{
    "rows": [["bad", None]],
    "columnHeaders": [{"name": "views"}, {"name": "averageViewPercentage"}],
}])
got = youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(got["views"] == 0, "non-numeric views -> 0")
ok(got["avg_view_pct"] == 0.0, "None avg_view_pct -> 0.0 (float default)")

ana = FakeAnalytics([http_error(403, "quotaExceeded")])
try:
    youtube.fetch_video_analytics(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
    ok(False, "analytics quota must raise")
except youtube.QuotaExceeded as e:
    ok(e.reason == "quotaexceeded", "fetch_video_analytics classifies daily cap")

# fetch_traffic_sources: sources always; search terms only when YT_SEARCH views > 0.
print("fetch_traffic_sources")
ana = FakeAnalytics([{
    "rows": [["YT_SEARCH", 4, 10], ["EXT_URL", 2, 3]],
}, {
    "rows": [["agent memory", 3], ["rag", 1]],
}])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(traf["sources"]["YT_SEARCH"] == {"views": 4, "watch_min": 10}, "YT_SEARCH source")
ok(traf["sources"]["EXT_URL"] == {"views": 2, "watch_min": 3}, "EXT_URL source")
ok(traf["search_terms"] == {"agent memory": 3, "rag": 1},
   "search terms fetched because YT_SEARCH views > 0")
ok(ana.queries[0]["dimensions"] == "insightTrafficSourceType",
   "first traffic query is by source type")
ok(ana.queries[0]["metrics"] == "views,estimatedMinutesWatched",
   "traffic source query requests views+watch time")
ok(ana.queries[1]["dimensions"] == "insightTrafficSourceDetail",
   "second traffic query is search-term detail")
ok("insightTrafficSourceType==YT_SEARCH" in ana.queries[1]["filters"],
   "search-term query filters to YT_SEARCH")
ok(ana.queries[1]["metrics"] == "views", "search-term query is views-only")

# YT_SEARCH views == 0 -> do not spend a query on search terms.
ana = FakeAnalytics([{
    "rows": [["YT_SEARCH", 0, 0], ["EXT_URL", 5, 1]],
}])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(traf["sources"]["YT_SEARCH"]["views"] == 0, "zero-view YT_SEARCH is recorded")
ok(traf["search_terms"] == {}, "search terms skipped when YT_SEARCH views == 0")
ok(len(ana.queries) == 1, "no second query when YT_SEARCH has no views")

# Missing YT_SEARCH key entirely -> no search-term query.
ana = FakeAnalytics([{"rows": [["EXT_URL", 5, 1]]}])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok("YT_SEARCH" not in traf["sources"], "absent source is absent (not defaulted)")
ok(traf["search_terms"] == {} and len(ana.queries) == 1,
   "no YT_SEARCH key -> no search-term query")

# First-query exception swallows to whatever was gathered (empty).
ana = FakeAnalytics([RuntimeError("analytics down")])
try:
    traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
except Exception as e:
    ok(False, f"traffic first-query failure must not raise ({type(e).__name__})")
    traf = None
ok(traf == {"sources": {}, "search_terms": {}},
   "traffic first-query failure returns empty (never raises)")

# Search-term exception keeps the sources already gathered.
ana = FakeAnalytics([
    {"rows": [["YT_SEARCH", 4, 1]]},
    RuntimeError("detail down"),
])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(traf["sources"]["YT_SEARCH"]["views"] == 4,
   "search-term failure keeps sources")
ok(traf["search_terms"] == {}, "search-term failure leaves search_terms empty")

# Null / empty rows on the source query -> empty sources, no second query.
ana = FakeAnalytics([{"rows": None}])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(traf == {"sources": {}, "search_terms": {}}, "null traffic rows -> empty")

# None cells coerce via `or 0`.
ana = FakeAnalytics([{"rows": [["EXT_URL", None, None]]}])
traf = youtube.fetch_traffic_sources(ana, "UCc", "v1", "2026-01-01", "2026-01-02")
ok(traf["sources"]["EXT_URL"] == {"views": 0, "watch_min": 0},
   "None traffic cells -> 0")


# ---------------------------------------------------------------------------
# Cleanup + summary
# ---------------------------------------------------------------------------
_restore_creds_dir()
ok(settings.credentials_dir == _ORIG_CREDS, "credentials_dir restored")

print()
print(f"ALL {_checks} CHECKS PASSED")
