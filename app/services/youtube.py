"""Per-channel YouTube integration: OAuth (youtube scope), upload, playlists, quota.

Generalizes channel/upload.py to:
  - one credential set per channel (credentials/<slug>/{client_secret,token}.json)
  - the broader `youtube` scope (needed for playlist create / add)
  - playlist list/create and add-video-to-playlist
  - quota-aware error classification
"""

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

# The redirect-based flow uses an http loopback redirect (http://localhost:7070/...)
# and the authorization response arrives over http — relax oauthlib's https-only
# guard, and don't fail when Google returns a broadened granted-scope set.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.config import settings

# Full youtube scope so we can manage playlists, not just upload. Analytics read is
# requested only at CONSENT time. The Data-API path (get_service) loads creds with NO
# scope pin: google-auth sends the pinned list in the refresh grant and Google narrows
# the refreshed access token to exactly that list, so pinning SCOPES here silently
# stripped already-granted force-ssl on every refresh (403 on all first comments
# 2026-07-12) and then persisted the narrowed list back into token.json. Unpinned,
# a refresh returns every scope the refresh token granted — old youtube-only tokens
# keep publishing, re-consented tokens keep force-ssl + analytics.
SCOPES = ["https://www.googleapis.com/auth/youtube"]
# Everything requested at consent time. force-ssl is required for comment endpoints
# (commentThreads.insert — the author first-comment machinery 403s without it).
CONSENT_SCOPES = SCOPES + [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
CATEGORY_SCIENCE_TECH = "28"

# Estimated quota costs (units) — YouTube Data API v3.
QUOTA_UPLOAD = 1600
QUOTA_PLAYLIST_INSERT = 50
QUOTA_PLAYLISTITEM_INSERT = 50
QUOTA_CHANNEL_UPDATE = 50
QUOTA_SUBSCRIPTION_WRITE = 50
QUOTA_THUMBNAIL_SET = 50
QUOTA_COMMENT_INSERT = 50
QUOTA_LIST = 1
QUOTA_ANALYTICS_QUERY = 1


class NeedsConnect(Exception):
    """Token missing or unrefreshable — UI must trigger an interactive connect."""


class GrantRejected(Exception):
    """A freshly-exchanged consent grant failed verification BEFORE anything was
    saved — no token written, no channel state changed. `code` identifies which
    guard fired so each caller (web callback, reconnect CLI) can append its own
    remediation hint (Disconnect-first vs --force / --allow-partial)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class GrantCode:
    """Stable identifiers for which guard raised a GrantRejected. The raise sites
    (verify_grant, below) and the per-caller remediation-hint dicts (channels.py
    _GRANT_HINTS, reconnect.py _CLI_HINTS) all key off these constants instead of
    bare string literals, so a rename fails loudly at import instead of a
    dict.get(code, "") silently dropping the hint (BACKLOG 4c-b)."""

    NO_REFRESH_TOKEN = "no_refresh_token"
    PARTIAL_SCOPES = "partial_scopes"
    IDENTITY_CHECK_FAILED = "identity_check_failed"
    NO_CHANNEL = "no_channel"
    CHANNEL_MISMATCH = "channel_mismatch"


# Every code verify_grant can raise — hint dicts assert their keys are a subset
# of this, so a hint keyed on a stale/misspelled code is caught by the suite.
GRANT_CODES = frozenset({
    GrantCode.NO_REFRESH_TOKEN, GrantCode.PARTIAL_SCOPES,
    GrantCode.IDENTITY_CHECK_FAILED, GrantCode.NO_CHANNEL,
    GrantCode.CHANNEL_MISMATCH,
})


class QuotaExceeded(Exception):
    """A YouTube daily cap was hit — caller should stop publishing for this channel
    until it resets. `reason` is the YouTube error reason (e.g. 'uploadlimitexceeded'
    or 'quotaexceeded'); the publish loop uses it to size the cooldown, since the two
    caps reset differently (rolling 24h vs midnight Pacific)."""

    def __init__(self, *args, reason: str = ""):
        super().__init__(*args)
        self.reason = reason


class UploadStalled(Exception):
    """A resumable-upload chunk raised a socket/connection error (not an HTTP error) —
    a transient network condition. The publish loop retries it (up to publish_max_retries)
    rather than failing the video outright."""


def channel_dir(slug: str) -> Path:
    return Path(settings.credentials_dir) / slug


def client_secret_path(slug: str) -> Path:
    return channel_dir(slug) / "client_secret.json"


def token_path(slug: str) -> Path:
    return channel_dir(slug) / "token.json"


def has_client_secret(slug: str) -> bool:
    return client_secret_path(slug).exists()


def has_token(slug: str) -> bool:
    return token_path(slug).exists()


def _load_creds(slug: str, scopes: Optional[list] = None) -> Optional[Credentials]:
    tp = token_path(slug)
    if not tp.exists():
        return None
    raw = tp.read_text()
    info = json.loads(raw)
    if scopes is None:
        # Unpinned load (see SCOPES note): drop the stored scopes field too —
        # from_authorized_user_info falls back to it, and tokens persisted by the
        # pre-fix refresh path carry a narrowed list that would keep re-narrowing.
        info.pop("scopes", None)
    creds = Credentials.from_authorized_user_info(info, scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Refresh token revoked/expired (invalid_grant). Return None so get_service
            # raises the clean NeedsConnect that callers already handle — instead of
            # leaking a RefreshError that leaves an upload stuck in PUBLISHING (mislabeled
            # a "stall" by the recovery loop) and never flags the channel for reconnect.
            return None
        try:
            changed = tp.read_text() != raw if tp.exists() else True
        except OSError:
            changed = True
        if changed:
            # Someone replaced the token while we were refreshing (e.g. the
            # reconnect CLI saving a fresh grant). The new token wins on disk;
            # our refreshed creds are still good for this one call.
            return creds
        _write_atomic(tp, creds.to_json())
        return creds
    return None


def get_service(slug: str):
    """Build an authenticated YouTube client. Raises NeedsConnect if not usable —
    never opens a browser here (that only happens in connect_interactive)."""
    if not has_client_secret(slug):
        raise NeedsConnect(f"missing client_secret.json for channel '{slug}'")
    creds = _load_creds(slug)
    if creds is None:
        raise NeedsConnect(f"token missing/expired for channel '{slug}' — reconnect required")
    return build("youtube", "v3", credentials=creds)


def build_flow(slug: str, redirect_uri: str) -> Flow:
    """Create an OAuth Flow for the redirect-based consent (no server-side browser)."""
    cs = client_secret_path(slug)
    if not cs.exists():
        raise NeedsConnect(f"upload client_secret.json for channel '{slug}' first")
    return Flow.from_client_secrets_file(str(cs), scopes=CONSENT_SCOPES, redirect_uri=redirect_uri)


def authorization_url(flow: Flow) -> tuple[str, str]:
    """Returns (url, state). The state is the CSRF token Google echoes back on
    the redirect — the web callback pairs redirects with their pending flow by
    it; the reconnect CLI validates it inside fetch_token instead."""
    url, state = flow.authorization_url(
        access_type="offline",          # request a refresh token
        include_granted_scopes="true",
        prompt="consent",               # force refresh-token issuance on re-consent
    )
    return url, state


def _write_atomic(tp: Path, text: str) -> None:
    """Write-then-rename so a crash mid-write can never truncate a token file.
    The tmp name carries the pid so two writers (web consent + reconnect CLI,
    or a loop's refresh persist) never collide on the intermediate file."""
    tmp = tp.parent / f"{tp.name}.{os.getpid()}.tmp"
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(tp)


def save_token(slug: str, creds: Credentials) -> None:
    """Persist consent credentials atomically, keeping the previous token as
    token.json.bak — a bad re-consent must never destroy the only working token.
    (The copy, not a rename, keeps token.json present throughout so a concurrent
    get_service never sees the file missing.)"""
    channel_dir(slug).mkdir(parents=True, exist_ok=True)
    tp = token_path(slug)
    if tp.exists():
        _write_atomic(tp.parent / (tp.name + ".bak"), tp.read_text())
    _write_atomic(tp, creds.to_json())


def identity_for_creds(creds: Credentials) -> dict:
    """The YouTube channel identity behind a set of credentials."""
    return fetch_identity(build("youtube", "v3", credentials=creds))


def verify_grant(creds: Credentials, *, expected_channel_id: Optional[str] = None,
                 expected_channel_title: Optional[str] = None,
                 label: str = "this channel",
                 allow_partial: bool = False, allow_rebind: bool = False,
                 fetch_identity_fn: Optional[Callable[[Credentials], dict]] = None) -> dict:
    """Verify a freshly-exchanged grant BEFORE anything touches disk or DB — the
    three checks learned from the 2026-07-05/11 reconnect incidents (rationale
    in app/reconnect.py's docstring): a refresh token was issued, every consent
    scope was granted, and the consented account owns the SAME YouTube channel
    (`expected_channel_id`) the slug is bound to. Returns the {id, title}
    identity on success; raises GrantRejected (with .code) otherwise."""
    if not creds.refresh_token:
        raise GrantRejected(
            "Google returned no refresh token — the grant would die within the "
            "hour. Token NOT saved; re-run the consent.", code=GrantCode.NO_REFRESH_TOKEN)
    # granted_scopes is what the user actually ticked; absent means the grant
    # matched the request (RFC 6749 §5.1), so falling back to the requested set
    # is the spec reading, not a fail-open.
    granted = creds.granted_scopes or creds.scopes or []
    missing = sorted(set(CONSENT_SCOPES) - set(granted))
    if missing and not allow_partial:
        raise GrantRejected(
            "grant is missing scope(s): " + ", ".join(missing) + ". Token NOT "
            "saved — re-run and click 'Select all' (\"Selecionar tudo\") on the "
            "consent screen.", code=GrantCode.PARTIAL_SCOPES)
    try:
        identity = (fetch_identity_fn or identity_for_creds)(creds)
    except Exception as e:
        raise GrantRejected(
            f"token exchanged but the identity check failed: {e} — token NOT "
            "saved; existing token untouched. If this looks transient, just "
            "re-run.", code=GrantCode.IDENTITY_CHECK_FAILED)
    if not identity.get("id"):
        raise GrantRejected(
            "consented account/brand has no YouTube channel attached — wrong "
            "pick on the account chooser? Token NOT saved.", code=GrantCode.NO_CHANNEL)
    if expected_channel_id and identity["id"] != expected_channel_id and not allow_rebind:
        raise GrantRejected(
            f"consented account owns YouTube channel {identity['id']} "
            f"({identity.get('title')}), but {label} is bound to "
            f"{expected_channel_id} ({expected_channel_title}). Wrong Google "
            "account on the picker? Token NOT saved.", code=GrantCode.CHANNEL_MISMATCH)
    return identity


def finish_flow(slug: str, flow: Flow, code: str, *,
                expected_channel_id: Optional[str] = None,
                expected_channel_title: Optional[str] = None) -> dict:
    """Exchange the authorization code, verify the grant, and only then persist
    the token; returns the verified identity. Order matters (BACKLOG 4b): the
    pre-existing save-then-look order meant a wrong-account or partial-scope web
    consent clobbered a working token and rotated the previous one out of
    token.json.bak. GrantRejected propagates with nothing written."""
    flow.fetch_token(code=code)
    identity = verify_grant(flow.credentials,
                            expected_channel_id=expected_channel_id,
                            expected_channel_title=expected_channel_title,
                            label=f"'{slug}'")
    save_token(slug, flow.credentials)
    return identity


def fetch_identity(service) -> dict:
    """channels().list(mine=True) -> {id, title}."""
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return {"id": None, "title": None}
    it = items[0]
    return {"id": it["id"], "title": it["snippet"]["title"]}


def disconnect(slug: str) -> None:
    """Remove every credential artifact — including the .bak kept by save_token
    and any stranded .tmp — so an operator disconnect leaves no live refresh
    token behind on disk."""
    tp = token_path(slug)
    for p in [tp, tp.parent / (tp.name + ".bak"), *tp.parent.glob(tp.name + ".*.tmp")]:
        if p.exists():
            p.unlink()


# ---- Publishing ----------------------------------------------------------

def _upload_body(title: str, description: str, tags: list[str], privacy: str,
                 language_code: Optional[str] = None) -> dict:
    """videos().insert body. language_code (BCP-47, e.g. 'pt-BR') sets both
    defaultLanguage and defaultAudioLanguage so YouTube can match the video to the
    right language audience — critical for the PT-BR channel's discovery."""
    snippet = {
        "title": (title or "")[:100],
        "description": (description or "")[:5000],
        "tags": (tags or [])[:30],
        "categoryId": CATEGORY_SCIENCE_TECH,
    }
    if language_code:
        snippet["defaultLanguage"] = language_code
        snippet["defaultAudioLanguage"] = language_code
    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }


def upload_video(service, video_path: str, title: str, description: str,
                 tags: list[str], privacy: str,
                 progress_cb: Optional[Callable[[int], None]] = None,
                 language_code: Optional[str] = None) -> str:
    body = _upload_body(title, description, tags, privacy, language_code)
    # 2 MB chunks so the resumable upload reports incremental progress.
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True,
                            chunksize=2 * 1024 * 1024)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            raise _classify(e)
        except (OSError, httplib2.HttpLib2Error) as e:
            # socket timeout / connection reset / DNS — a stalled upload, not an API error.
            raise UploadStalled(
                f"upload stalled (no progress in {settings.youtube_http_timeout_seconds}s): "
                f"{type(e).__name__}: {e}")
        if status and progress_cb:
            progress_cb(int(status.progress() * 100))
    return response["id"]


def insert_comment(service, video_id: str, text: str) -> str:
    """Post an author top-level comment on a video (the 'first comment' engagement
    seed — the creator's comment sits on top of an empty comment section). Callers
    treat failures as best-effort, like thumbnails."""
    try:
        resp = service.commentThreads().insert(
            part="snippet",
            body={"snippet": {
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": text[:9000]}},
            }},
        ).execute()
    except HttpError as e:
        raise _classify(e)
    return resp["id"]


def set_thumbnail(service, video_id: str, png_path: str) -> None:
    """Upload a custom thumbnail for a video (requires a phone-verified channel —
    otherwise YouTube returns 403, which callers treat as best-effort)."""
    media = MediaFileUpload(png_path, mimetype="image/png")
    try:
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
    except HttpError as e:
        raise _classify(e)


def list_playlists(service) -> list[dict]:
    out: list[dict] = []
    page_token = None
    while True:
        resp = service.playlists().list(
            part="snippet,status", mine=True, maxResults=50, pageToken=page_token
        ).execute()
        for it in resp.get("items", []):
            out.append({
                "yt_playlist_id": it["id"],
                "title": it["snippet"]["title"],
                "description": it["snippet"].get("description"),
                "privacy": it.get("status", {}).get("privacyStatus"),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def create_playlist(service, title: str, description: str = "", privacy: str = "public") -> dict:
    try:
        resp = service.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": privacy},
            },
        ).execute()
    except HttpError as e:
        raise _classify(e)
    return {
        "yt_playlist_id": resp["id"],
        "title": resp["snippet"]["title"],
        "description": resp["snippet"].get("description"),
        "privacy": resp.get("status", {}).get("privacyStatus"),
    }


def add_to_playlist(service, playlist_id: str, video_id: str) -> str:
    try:
        resp = service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
    except HttpError as e:
        raise _classify(e)
    return resp["id"]


# ---- Channel administration: metrics, branding, subscriptions ------------

def fetch_channel(service) -> dict:
    """The authenticated channel's snippet + public statistics + branding (one call,
    1 quota unit). Returns {} if the account has no channel."""
    try:
        resp = service.channels().list(
            part="snippet,statistics,brandingSettings", mine=True
        ).execute()
    except HttpError as e:
        raise _classify(e)
    items = resp.get("items", [])
    if not items:
        return {}
    it = items[0]
    snip = it.get("snippet", {})
    stats = it.get("statistics", {})
    chan = it.get("brandingSettings", {}).get("channel", {})

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return {
        "id": it["id"],
        "title": snip.get("title"),
        "thumbnail": snip.get("thumbnails", {}).get("default", {}).get("url"),
        "statistics": {
            "subscriber_count": _int(stats.get("subscriberCount")),
            "view_count": _int(stats.get("viewCount")),
            "video_count": _int(stats.get("videoCount")),
            "hidden_subscriber_count": bool(stats.get("hiddenSubscriberCount")),
        },
        "branding": {
            "title": chan.get("title"),
            "description": chan.get("description"),
            "keywords": chan.get("keywords"),          # space-separated; multiword quoted
            "country": chan.get("country"),
            "default_language": chan.get("defaultLanguage"),
        },
    }


def update_branding(service, channel_id: str, *, title=None, description=None,
                    keywords=None, country=None, default_language=None) -> dict:
    """Update the channel's public branding. channels.update REPLACES the
    brandingSettings.channel object, so we merge the requested fields over the
    current ones to avoid clobbering anything left unset (1 read + 50 units)."""
    current = fetch_channel(service).get("branding", {}) or {}
    merged = {
        "title": title if title is not None else current.get("title"),
        "description": description if description is not None else current.get("description"),
        "keywords": keywords if keywords is not None else current.get("keywords"),
        "country": country if country is not None else current.get("country"),
        "defaultLanguage": default_language if default_language is not None
        else current.get("default_language"),
    }
    channel = {k: v for k, v in merged.items() if v not in (None, "")}
    try:
        resp = service.channels().update(
            part="brandingSettings", body={"id": channel_id,
                                           "brandingSettings": {"channel": channel}},
        ).execute()
    except HttpError as e:
        raise _classify(e)
    chan = resp.get("brandingSettings", {}).get("channel", {})
    return {
        "title": chan.get("title"), "description": chan.get("description"),
        "keywords": chan.get("keywords"), "country": chan.get("country"),
        "default_language": chan.get("defaultLanguage"),
    }


def list_subscriptions(service, max_items: int = 200) -> list[dict]:
    """Channels this account is subscribed to (the ones it follows)."""
    out: list[dict] = []
    page = None
    while True:
        try:
            resp = service.subscriptions().list(
                part="snippet", mine=True, maxResults=50,
                order="alphabetical", pageToken=page,
            ).execute()
        except HttpError as e:
            raise _classify(e)
        for it in resp.get("items", []):
            sn = it.get("snippet", {})
            out.append({
                "sub_id": it["id"],
                "channel_id": sn.get("resourceId", {}).get("channelId"),
                "title": sn.get("title"),
                "description": sn.get("description"),
                "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url"),
            })
        page = resp.get("nextPageToken")
        if not page or len(out) >= max_items:
            return out


def list_subscribers(service, max_items: int = 100) -> list[dict]:
    """Recent subscribers to this channel (read-only — the API can't add/remove them)."""
    out: list[dict] = []
    page = None
    while True:
        try:
            resp = service.subscriptions().list(
                part="subscriberSnippet", mySubscribers=True,
                maxResults=50, pageToken=page,
            ).execute()
        except HttpError as e:
            raise _classify(e)
        for it in resp.get("items", []):
            sn = it.get("subscriberSnippet", {})
            out.append({
                "channel_id": sn.get("channelId"),
                "title": sn.get("title"),
                "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url"),
            })
        page = resp.get("nextPageToken")
        if not page or len(out) >= max_items:
            return out


def resolve_channel_id(service, ref: str) -> str:
    """Turn a channel id, /channel/UC… URL, or @handle into a UC… channel id."""
    ref = (ref or "").strip()
    m = re.search(r"(UC[0-9A-Za-z_-]{22})", ref)
    if m:
        return m.group(1)
    hm = re.search(r"@([A-Za-z0-9._-]+)", ref)
    handle = (hm.group(1) if hm else ref).lstrip("@")
    if not handle:
        raise ValueError(f"could not parse a channel from '{ref}'")
    try:
        resp = service.channels().list(part="id", forHandle=handle).execute()
    except HttpError as e:
        raise _classify(e)
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"no channel found for '{ref}'")
    return items[0]["id"]


def subscribe(service, channel_id: str) -> dict:
    try:
        resp = service.subscriptions().insert(
            part="snippet",
            body={"snippet": {"resourceId": {"kind": "youtube#channel",
                                             "channelId": channel_id}}},
        ).execute()
    except HttpError as e:
        raise _classify(e)
    return {"sub_id": resp["id"], "channel_id": channel_id}


def unsubscribe(service, sub_id: str) -> None:
    try:
        service.subscriptions().delete(id=sub_id).execute()
    except HttpError as e:
        raise _classify(e)


# ---- Analytics (YouTube Analytics API v2, owner reports) ------------------

def get_analytics_service(slug: str):
    """YouTube Analytics API v2 client. Requests the analytics scope, so it only
    works once the channel has been re-consented for it; otherwise raises NeedsConnect
    or the API returns an insufficient-scope error — callers treat that as 'skip'."""
    if not has_client_secret(slug):
        raise NeedsConnect(f"missing client_secret.json for channel '{slug}'")
    creds = _load_creds(slug, CONSENT_SCOPES)
    if creds is None:
        raise NeedsConnect(f"token missing/expired for channel '{slug}' — reconnect required")
    return build("youtubeAnalytics", "v2", credentials=creds)


def _analytics_row(analytics, channel_yt_id, video_id, start_date, end_date, metrics) -> dict:
    resp = analytics.reports().query(
        ids=f"channel=={channel_yt_id}", startDate=start_date, endDate=end_date,
        dimensions="video", filters=f"video=={video_id}", metrics=metrics, maxResults=1,
    ).execute()
    rows = resp.get("rows") or []
    if not rows:
        return {}
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    return dict(zip(headers, rows[0]))


def fetch_video_analytics(analytics, channel_yt_id: str, video_id: str,
                          start_date: str, end_date: str) -> dict:
    """Per-video analytics over [start_date, end_date] (YYYY-MM-DD). Returns a
    normalized dict ready for a VideoMetric row.

    NOTE (2026-07-09 audit): thumbnail impressions / impressionClickThroughRate are
    NOT exposed by the YouTube Analytics API v2 targeted queries — every query form
    returns 400 "Unknown identifier (impressions)". The old second query silently
    failed forever, so every impressions/ctr=0 stored before this date was a
    fabricated default, not a measurement. Impressions/CTR stay 0 in VideoMetric
    (Studio-only data); use traffic_json (browse/suggested/search views) as the
    discovery signal instead."""
    raw: dict = {}
    try:
        raw.update(_analytics_row(
            analytics, channel_yt_id, video_id, start_date, end_date,
            "views,estimatedMinutesWatched,averageViewPercentage,averageViewDuration,"
            "likes,comments,subscribersGained"))
    except HttpError as e:
        raise _classify(e)

    def _n(key, cast=int):
        v = raw.get(key)
        try:
            return cast(v) if v is not None else cast(0)
        except (TypeError, ValueError):
            return cast(0)

    return {
        "views": _n("views"),
        "impressions": _n("impressions"),
        "ctr": _n("impressionClickThroughRate", float),
        "avg_view_pct": _n("averageViewPercentage", float),
        "average_view_duration": _n("averageViewDuration", float),
        "watch_time_minutes": _n("estimatedMinutesWatched"),
        "likes": _n("likes"),
        "comments": _n("comments"),
        "subscribers_gained": _n("subscribersGained"),
    }


def fetch_traffic_sources(analytics, channel_yt_id: str, video_id: str,
                          start_date: str, end_date: str) -> dict:
    """Where a video's views come from: {'sources': {type: {views, watch_min}},
    'search_terms': {term: views}}. Answers the question the channel has never been
    able to ask — is anything coming from browse/suggested/search, or only external?
    Search terms are fetched only when YT_SEARCH actually has views. Best-effort:
    partial data beats no data; failures return whatever was gathered."""
    out: dict = {"sources": {}, "search_terms": {}}
    try:
        resp = analytics.reports().query(
            ids=f"channel=={channel_yt_id}", startDate=start_date, endDate=end_date,
            dimensions="insightTrafficSourceType", filters=f"video=={video_id}",
            metrics="views,estimatedMinutesWatched", maxResults=25,
        ).execute()
        for row in resp.get("rows") or []:
            out["sources"][str(row[0])] = {"views": int(row[1] or 0),
                                           "watch_min": int(row[2] or 0)}
    except Exception:
        return out
    if out["sources"].get("YT_SEARCH", {}).get("views", 0) > 0:
        try:
            resp = analytics.reports().query(
                ids=f"channel=={channel_yt_id}", startDate=start_date, endDate=end_date,
                dimensions="insightTrafficSourceDetail",
                filters=f"video=={video_id};insightTrafficSourceType==YT_SEARCH",
                metrics="views", sort="-views", maxResults=10,
            ).execute()
            for row in resp.get("rows") or []:
                out["search_terms"][str(row[0])] = int(row[1] or 0)
        except Exception:
            pass
    return out


# Daily-cap reasons: both the API project quota and the per-channel upload cap.
# Surfaced as QuotaExceeded so the publish loop leaves the video APPROVED to retry
# after a cooldown, instead of marking it permanently FAILED (which nothing re-picks).
# Deliberately EXCLUDES short-term throttles (rateLimitExceeded / userRateLimitExceeded
# / uploadRateLimitExceeded) — those want a brief backoff, not a day-long cooldown.
_DAILY_CAP_REASONS = {"uploadlimitexceeded", "quotaexceeded", "dailylimitexceeded"}


def _error_reason(e: HttpError) -> str:
    """The YouTube error 'reason' (e.g. 'uploadLimitExceeded'), lowercased.

    Match on this, NOT the HTTP status: uploadLimitExceeded is returned as 400 *or*
    403 depending on the path, and its message ("The user has exceeded the number of
    videos they may upload.") contains no "quota" substring — so status/keyword
    matching silently misclassifies it as a hard failure. Falls back to scanning the
    raw body so a response-shape change can't hide a known reason."""
    import json
    try:
        return json.loads(e.content.decode("utf-8"))["error"]["errors"][0]["reason"].lower()
    except Exception:
        body = (e.content or b"").decode("utf-8", "ignore").lower()
        return next((r for r in _DAILY_CAP_REASONS if r in body), "")


def _classify(e: HttpError) -> Exception:
    reason = _error_reason(e)
    if reason in _DAILY_CAP_REASONS:
        return QuotaExceeded(str(e), reason=reason)
    return e


def is_playlist_missing(e) -> bool:
    """True if an HttpError means the target playlist no longer exists on YouTube
    (deleted out-of-band), i.e. reason 'playlistNotFound' or a bare HTTP 404. The
    publish loop uses this to drop a stale local->YouTube playlist mapping so it gets
    recreated, instead of 404-looping on every future publish for that topic."""
    if not isinstance(e, HttpError):
        return False
    if _error_reason(e) == "playlistnotfound":
        return True
    return getattr(getattr(e, "resp", None), "status", None) == 404
