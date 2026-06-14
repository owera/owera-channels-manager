"""Per-channel YouTube integration: OAuth (youtube scope), upload, playlists, quota.

Generalizes channel/upload.py to:
  - one credential set per channel (credentials/<slug>/{client_secret,token}.json)
  - the broader `youtube` scope (needed for playlist create / add)
  - playlist list/create and add-video-to-playlist
  - quota-aware error classification
"""

import os
import re
from pathlib import Path
from typing import Callable, Optional

# The redirect-based flow uses an http loopback redirect (http://localhost:7000/...)
# and the authorization response arrives over http — relax oauthlib's https-only
# guard, and don't fail when Google returns a broadened granted-scope set.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from app.config import settings

# Full youtube scope so we can manage playlists, not just upload.
SCOPES = ["https://www.googleapis.com/auth/youtube"]
CATEGORY_SCIENCE_TECH = "28"

# Estimated quota costs (units) — YouTube Data API v3.
QUOTA_UPLOAD = 1600
QUOTA_PLAYLIST_INSERT = 50
QUOTA_PLAYLISTITEM_INSERT = 50
QUOTA_CHANNEL_UPDATE = 50
QUOTA_SUBSCRIPTION_WRITE = 50
QUOTA_LIST = 1


class NeedsConnect(Exception):
    """Token missing or unrefreshable — UI must trigger an interactive connect."""


class QuotaExceeded(Exception):
    """A YouTube daily cap was hit — caller should stop publishing for this channel
    until it resets. `reason` is the YouTube error reason (e.g. 'uploadlimitexceeded'
    or 'quotaexceeded'); the publish loop uses it to size the cooldown, since the two
    caps reset differently (rolling 24h vs midnight Pacific)."""

    def __init__(self, *args, reason: str = ""):
        super().__init__(*args)
        self.reason = reason


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


def _load_creds(slug: str) -> Optional[Credentials]:
    tp = token_path(slug)
    if not tp.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tp.write_text(creds.to_json())
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
    return Flow.from_client_secrets_file(str(cs), scopes=SCOPES, redirect_uri=redirect_uri)


def authorization_url(flow: Flow) -> str:
    url, _state = flow.authorization_url(
        access_type="offline",          # request a refresh token
        include_granted_scopes="true",
        prompt="consent",               # force refresh-token issuance on re-consent
    )
    return url


def finish_flow(slug: str, flow: Flow, code: str) -> dict:
    """Exchange the authorization code for tokens, persist them, capture identity."""
    flow.fetch_token(code=code)
    creds = flow.credentials
    channel_dir(slug).mkdir(parents=True, exist_ok=True)
    token_path(slug).write_text(creds.to_json())
    service = build("youtube", "v3", credentials=creds)
    return fetch_identity(service)


def fetch_identity(service) -> dict:
    """channels().list(mine=True) -> {id, title}."""
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return {"id": None, "title": None}
    it = items[0]
    return {"id": it["id"], "title": it["snippet"]["title"]}


def disconnect(slug: str) -> None:
    tp = token_path(slug)
    if tp.exists():
        tp.unlink()


# ---- Publishing ----------------------------------------------------------

def upload_video(service, video_path: str, title: str, description: str,
                 tags: list[str], privacy: str,
                 progress_cb: Optional[Callable[[int], None]] = None) -> str:
    body = {
        "snippet": {
            "title": (title or "")[:100],
            "description": (description or "")[:5000],
            "tags": (tags or [])[:30],
            "categoryId": CATEGORY_SCIENCE_TECH,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
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
        if status and progress_cb:
            progress_cb(int(status.progress() * 100))
    return response["id"]


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
