import html
import logging
import re
import threading
import time
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, utcnow
from app.schemas import ChannelCreate, ChannelUpdate
from app.services import notify, youtube

logger = logging.getLogger("manager.channels")

router = APIRouter(prefix="/api/channels", tags=["channels"])

# In-memory OAuth flows awaiting their redirect callback, keyed by the flow's
# `state` (the CSRF token Google echoes back on success AND error redirects).
# Keying by state — not channel id — means a second /oauth/start (double-click,
# two tabs) can't orphan the first consent, and a replayed callback (browser
# history revisiting an old ?error= redirect) matches no pending entry, so it
# can't flip a healthy channel (BACKLOG 4c-a). Single-user, short-lived — fine
# in memory. All access goes through the three helpers below: these sync
# routes run on FastAPI's threadpool, so the lock keeps consumption atomic (a
# double-delivered redirect must not 500 or exchange the same code twice).
class _PendingFlow(NamedTuple):
    channel_id: int
    flow: object
    started_at: float  # time.monotonic()


_pending_flows: dict[str, _PendingFlow] = {}
_pending_flows_lock = threading.Lock()

# Abandoned starts (clicked but never consented) accumulate instead of
# overwriting each other: age out anything older than one very generous
# consent (2FA + unverified-app interstitials included), and cap the registry
# outright — the endpoint is reachable without auth when MANAGER_APP_PASSWORD
# is unset, and each entry pins a live Flow object.
_PENDING_FLOW_TTL = 30 * 60
_PENDING_FLOWS_MAX = 32


def _remember_flow(state: str, channel_id: int, flow: object) -> None:
    now = time.monotonic()
    with _pending_flows_lock:
        for st in [st for st, e in _pending_flows.items()
                   if now - e.started_at > _PENDING_FLOW_TTL]:
            del _pending_flows[st]
        _pending_flows[state] = _PendingFlow(channel_id, flow, now)
        while len(_pending_flows) > _PENDING_FLOWS_MAX:
            del _pending_flows[next(iter(_pending_flows))]  # oldest insertion


def _pop_pending_flow(state: str | None, channel_id: int):
    """Consume and return the pending flow for `state`, iff it belongs to
    `channel_id` and hasn't aged out. A state minted for another channel's
    consent stays pending for that channel; an expired entry is consumed but
    not returned, so a session-restored ancient consent (completed OR
    cancelled) reads as stale rather than acting on the channel."""
    with _pending_flows_lock:
        entry = _pending_flows.get(state) if state else None
        if entry is None or entry.channel_id != channel_id:
            return None
        del _pending_flows[state]
    if time.monotonic() - entry.started_at > _PENDING_FLOW_TTL:
        return None
    return entry.flow


def _supersede_flows(channel_id: int) -> None:
    """Drop every start still pending for a channel that just got a verified
    token (the other tab of a double-click): cancelling that stale consent
    screen later must not flip the freshly-connected channel to ERROR."""
    with _pending_flows_lock:
        for st in [st for st, e in _pending_flows.items()
                   if e.channel_id == channel_id]:
            del _pending_flows[st]


def _callback_html(title: str, message: str, ok: bool) -> str:
    color = "#c9f24e" if ok else "#f7768e"
    # Both strings land in HTML text nodes and carry remote content (the error
    # query param; Google-supplied channel titles inside GrantRejected messages).
    # Failure pages stay open — the message is the only trace the user gets.
    title, message = html.escape(title, quote=False), html.escape(message, quote=False)
    close = "<script>setTimeout(()=>window.close(),2500)</script>" if ok else ""
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{title}</title>
<style>body{{background:#08090b;color:#cdd3da;font-family:ui-monospace,monospace;
display:grid;place-items:center;height:100vh;margin:0}}
.box{{text-align:center;border:1px solid #272d35;border-radius:6px;padding:40px 56px;background:#121519}}
h1{{color:{color};font-size:18px;letter-spacing:.1em;text-transform:uppercase;margin:0 0 12px}}
p{{color:#6c7681;font-size:13px;margin:0}}</style></head>
<body><div class=box><h1>{title}</h1><p>{message}</p></div>
{close}</body></html>"""


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
        url, state = youtube.authorization_url(flow)
    except Exception as e:
        raise HTTPException(400, f"could not start OAuth: {e}")
    _remember_flow(state, channel_id, flow)
    return {"auth_url": url}


# Web-flavored remediation per GrantRejected.code (the CLI appends --force /
# --allow-partial hints instead; codes without an entry already self-explain).
_GRANT_HINTS = {
    "channel_mismatch": " Disconnect the channel first if you really mean to "
                        "re-bind it (or use the reconnect CLI with --force).",
}


def _fail_consent(session: Session, ch: Channel, error: str) -> None:
    """A failed consent halts publishing just like an expiry: flip to ERROR
    through the choke point so a previously-CONNECTED channel alerts (a
    reconnect of an already-dead one stays silent)."""
    notify.mark_dead_committed(session, ch, error, status=OAuthStatus.ERROR)


@router.get("/{channel_id}/oauth/callback")
def oauth_callback(channel_id: int, request: Request, code: str | None = None,
                   state: str | None = None, error: str | None = None,
                   session: Session = Depends(get_session)):
    """Google redirects here after consent. Exchange the code, store the token,
    capture the channel identity, and show a small close-me page."""
    ch = session.get(Channel, channel_id)
    flow = _pop_pending_flow(state, channel_id)
    if error or not code or not flow or not ch:
        # Flip only on a real failure of a consent that was actually pending.
        # A hit whose state matches nothing (page refresh after success,
        # browser-history replay of an old ?error= redirect, restart-emptied
        # _pending_flows) says nothing about the token — leave the channel's
        # status untouched.
        if ch and flow:
            _fail_consent(session, ch, error or "consent was cancelled or timed out")
        return HTMLResponse(_callback_html("Connection failed", error or "consent cancelled", False))
    try:
        identity = youtube.finish_flow(ch.slug, flow, code,
                                       expected_channel_id=ch.yt_channel_id,
                                       expected_channel_title=ch.yt_channel_title)
    except youtube.GrantRejected as e:
        # The grant failed verification BEFORE anything was written: the
        # existing token and oauth_status still describe the last working
        # credential, so a healthy channel keeps publishing through a
        # botched re-consent — do NOT flip status here. The log line is the
        # durable trace (the callback page is the only other one). Sibling
        # pending starts deliberately survive a rejection (unlike a success)
        # so the operator can retry from the other tab with the right
        # account; the cost is that cancelling that tab instead still flips
        # (the re-probe-before-flip in BACKLOG 4c would remove that too).
        logger.warning("consent for channel '%s' rejected (%s): %s", ch.slug, e.code, e)
        msg = str(e)[:400] + _GRANT_HINTS.get(e.code, "")
        return HTMLResponse(_callback_html("Consent rejected", msg, False))
    except Exception as e:
        _fail_consent(session, ch, str(e))
        return HTMLResponse(_callback_html("Connection failed", str(e)[:200], False))
    _supersede_flows(channel_id)
    display = identity.get("title") or ch.name
    try:
        notify.mark_connected(session, ch, identity)
    except Exception as e:
        # Same contract the CLI prints: the token IS saved and valid, only the
        # status flip is missing — a re-consent would rotate the good token
        # into token.json.bak for nothing.
        logger.error("channel '%s': token saved but the status update failed: %s", ch.slug, e)
        return HTMLResponse(_callback_html(
            "Token saved, status update failed",
            f"The new token for {display} is saved and working, but the dashboard status "
            "could not be updated — do NOT redo the consent. Open the dashboard (or the "
            "channel's oauth-status endpoint) to refresh it.", False))
    return HTMLResponse(_callback_html(
        "Connected", f"{display} is linked. You can close this tab.", True))


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
