"""Per-channel YouTube integration: OAuth (youtube scope), upload, playlists, quota.

Generalizes channel/upload.py to:
  - one credential set per channel (credentials/<slug>/{client_secret,token}.json)
  - the broader `youtube` scope (needed for playlist create / add)
  - playlist list/create and add-video-to-playlist
  - quota-aware error classification
"""

import os
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
QUOTA_LIST = 1


class NeedsConnect(Exception):
    """Token missing or unrefreshable — UI must trigger an interactive connect."""


class QuotaExceeded(Exception):
    """YouTube daily quota hit — caller should stop publishing for this channel today."""


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


def _classify(e: HttpError) -> Exception:
    status = getattr(e.resp, "status", None)
    content = (e.content or b"").lower()
    # Quota (403) and the per-channel daily upload cap (400 uploadLimitExceeded)
    # are both transient daily limits — surface them as QuotaExceeded so the
    # publish loop leaves the video APPROVED to retry next day, rather than
    # marking it permanently FAILED (which nothing re-picks automatically).
    if status == 403 and b"quota" in content:
        return QuotaExceeded(str(e))
    if status == 400 and b"uploadlimitexceeded" in content:
        return QuotaExceeded(str(e))
    return e
