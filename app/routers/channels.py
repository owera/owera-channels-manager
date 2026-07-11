import re

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, utcnow
from app.schemas import ChannelCreate, ChannelUpdate
from app.services import notify, youtube

router = APIRouter(prefix="/api/channels", tags=["channels"])

# In-memory OAuth flows awaiting their redirect callback (keyed by channel id).
# Single-user, short-lived (a consent completes in seconds) — fine in memory.
_pending_flows: dict[int, object] = {}


def _callback_html(title: str, message: str, ok: bool) -> str:
    color = "#c9f24e" if ok else "#f7768e"
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{title}</title>
<style>body{{background:#08090b;color:#cdd3da;font-family:ui-monospace,monospace;
display:grid;place-items:center;height:100vh;margin:0}}
.box{{text-align:center;border:1px solid #272d35;border-radius:6px;padding:40px 56px;background:#121519}}
h1{{color:{color};font-size:18px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 12px}}
p{{color:#6c7681;font-size:13px;margin:0}}</style></head>
<body><div class=box><h1>{title}</h1><p>{message}</p></div>
<script>setTimeout(()=>window.close(),2500)</script></body></html>"""


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


@router.get("")
def list_channels(session: Session = Depends(get_session)):
    return session.exec(select(Channel).order_by(Channel.id)).all()


@router.post("", status_code=201)
def create_channel(body: ChannelCreate, session: Session = Depends(get_session)):
    slug = _slugify(body.slug or body.name)
    if session.exec(select(Channel).where(Channel.slug == slug)).first():
        raise HTTPException(409, f"channel slug '{slug}' already exists")
    from app.db import ensure_default_profile
    default_profile = ensure_default_profile(session)
    ch = Channel(
        slug=slug, name=body.name, default_privacy=body.default_privacy,
        default_skip_gate=body.default_skip_gate,
        daily_render_budget=body.daily_render_budget,
        daily_publish_budget=body.daily_publish_budget,
        default_render_profile_id=default_profile.id,
    )
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


@router.get("/{channel_id}")
def get_channel(channel_id: int, session: Session = Depends(get_session)):
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    return ch


@router.patch("/{channel_id}")
def update_channel(channel_id: int, body: ChannelUpdate, session: Session = Depends(get_session)):
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(ch, k, v)
    ch.updated_at = utcnow()
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


@router.delete("/{channel_id}", status_code=204)
def delete_channel(channel_id: int, session: Session = Depends(get_session)):
    ch = session.get(Channel, channel_id)
    if ch:
        session.delete(ch)
        session.commit()


@router.post("/{channel_id}/credentials")
def upload_credentials(channel_id: int, file: UploadFile = File(...),
                       session: Session = Depends(get_session)):
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    cdir = youtube.channel_dir(ch.slug)
    cdir.mkdir(parents=True, exist_ok=True)
    youtube.client_secret_path(ch.slug).write_bytes(file.file.read())
    return {"ok": True}


@router.post("/{channel_id}/oauth/start")
def oauth_start(channel_id: int, request: Request, session: Session = Depends(get_session)):
    """Begin the redirect-based consent: return the Google authorization URL for the
    user's own browser to open. No server-side browser needed."""
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    if not youtube.has_client_secret(ch.slug):
        raise HTTPException(400, "upload client_secret.json first")
    # MANAGER_PUBLIC_BASE_URL pins the redirect_uri (rationale on the setting).
    base = settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/channels/{channel_id}/oauth/callback"
    try:
        flow = youtube.build_flow(ch.slug, redirect_uri)
        url = youtube.authorization_url(flow)
    except Exception as e:
        raise HTTPException(400, f"could not start OAuth: {e}")
    _pending_flows[channel_id] = flow
    return {"auth_url": url}


def _fail_consent(session: Session, ch: Channel, error: str) -> None:
    """A failed consent halts publishing just like an expiry: flip to ERROR
    through the choke point so a previously-CONNECTED channel alerts (a
    reconnect of an already-dead one stays silent)."""
    notify.mark_dead_committed(session, ch, error, status=OAuthStatus.ERROR)


@router.get("/{channel_id}/oauth/callback")
def oauth_callback(channel_id: int, request: Request, code: str | None = None,
                   error: str | None = None, session: Session = Depends(get_session)):
    """Google redirects here after consent. Exchange the code, store the token,
    capture the channel identity, and show a small close-me page."""
    ch = session.get(Channel, channel_id)
    flow = _pending_flows.pop(channel_id, None)
    if error or not code or not flow or not ch:
        # Flip only on a real failure of a pending consent. A hit with no
        # pending flow and no error (page refresh after success, replayed
        # redirect, restart-emptied _pending_flows) says nothing about the
        # token — leave the channel's status untouched.
        if ch and (error or flow):
            _fail_consent(session, ch, error or "consent was cancelled or timed out")
        return HTMLResponse(_callback_html("Connection failed", error or "consent cancelled", False))
    try:
        identity = youtube.finish_flow(ch.slug, flow, code)
    except Exception as e:
        _fail_consent(session, ch, str(e))
        return HTMLResponse(_callback_html("Connection failed", str(e)[:200], False))
    ch.yt_channel_id = identity.get("id")
    ch.yt_channel_title = identity.get("title")
    # Consolidate: the channel's display name follows the real YouTube title.
    if identity.get("title"):
        ch.name = identity["title"]
    ch.oauth_status = OAuthStatus.CONNECTED
    ch.oauth_error = None
    ch.updated_at = utcnow()
    session.add(ch); session.commit()
    return HTMLResponse(_callback_html(
        "Connected", f"{identity.get('title') or ch.name} is linked. You can close this tab.", True))


@router.post("/{channel_id}/disconnect")
def disconnect(channel_id: int, session: Session = Depends(get_session)):
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    youtube.disconnect(ch.slug)
    # Operator-initiated: deliberately bypasses notify.mark_dead — an
    # intentional disconnect must not page anyone.
    ch.oauth_status = OAuthStatus.DISCONNECTED
    ch.yt_channel_id = None
    ch.yt_channel_title = None
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


@router.get("/{channel_id}/oauth-status")
def oauth_status(channel_id: int, session: Session = Depends(get_session)):
    ch = session.get(Channel, channel_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    try:
        youtube.get_service(ch.slug)
        ch.oauth_status = OAuthStatus.CONNECTED
        ch.oauth_error = None
        session.add(ch)
        session.commit()
    except Exception as e:  # NeedsConnect, refresh failure, bad/old-scope token, etc.
        notify.mark_dead_committed(session, ch, str(e))
    return {"oauth_status": ch.oauth_status, "error": ch.oauth_error,
            "has_client_secret": youtube.has_client_secret(ch.slug)}
