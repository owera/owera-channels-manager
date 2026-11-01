import html
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Channel, OAuthStatus, Workspace, utcnow
from app.schemas import ChannelCreate, ChannelUpdate
from app.services import notify, oauth_pending, youtube
from app.workspace import channels_for_workspace, current_workspace, require_channel as require_ws_channel

logger = logging.getLogger("manager.channels")

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _channel_workspace_id(ch: Channel) -> int:
    return ch.workspace_id or 1


def _rebuild_flow(ch: Channel, pending) -> object:
    flow = youtube.build_flow(ch.slug, pending.redirect_uri)
    flow.code_verifier = pending.code_verifier
    return flow


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


def _require_int(fields: dict, key: str, minimum: int, hint: str) -> None:
    """Reject JSON null / bool / below-floor ints before they hit the DB.

    ``int`` columns on Channel are NOT NULL. ``setattr(..., None)`` persists
    SQL NULL, and the next ``published_today >= channel.daily_publish_budget``
    (or ``rendered_today + in_flight >= channel.daily_render_budget``)
    TypeErrors the tick. JSON bools are rejected earlier by ChannelCreate
    / ChannelUpdate (lax int would coerce false→0, a silent stall); the
    bool check here is defense in depth for non-HTTP callers.
    """
    if key not in fields:
        return
    v = fields[key]
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise HTTPException(400, hint)


@router.get("")
def list_channels(
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    return session.exec(channels_for_workspace(ws)).all()


@router.post("", status_code=201)
def create_channel(
    body: ChannelCreate,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    slug = _slugify(body.slug or body.name)
    if session.exec(
        select(Channel).where(Channel.workspace_id == ws.id, Channel.slug == slug)
    ).first():
        raise HTTPException(409, f"channel slug '{slug}' already exists")
    fields = body.model_dump()
    # 0 is a legal stall (#27/#28). Negative is the same skip as 0 but a
    # typo — 400 rather than a silent park from day one. Null is already
    # 422 (ChannelCreate budgets are required ints). JSON bool is rejected
    # earlier by ChannelCreate._reject_bool_budget.
    _require_int(fields, "daily_render_budget", 0,
                 "daily_render_budget must be >= 0 "
                 "(null TypeErrors the render tick: "
                 "rendered_today + in_flight >= budget)")
    _require_int(fields, "daily_publish_budget", 0,
                 "daily_publish_budget must be >= 0 "
                 "(null TypeErrors the publish tick: "
                 "published_today >= budget)")
    from app.db import ensure_default_profile
    default_profile = ensure_default_profile(session)
    ch = Channel(
        workspace_id=ws.id,
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
def get_channel(
    channel_id: int,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    return require_ws_channel(session, ws, channel_id)


@router.patch("/{channel_id}")
def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    ch = require_ws_channel(session, ws, channel_id)
    fields = body.model_dump(exclude_unset=True)
    # 0 is a legal stall (#27/#28). null TypeErrors the publish/render ticks
    # (published_today >= budget; rendered_today + in_flight >= budget).
    # Negative is the same skip as 0 but a typo — 400 rather than a silent park.
    _require_int(fields, "daily_render_budget", 0,
                 "daily_render_budget must be >= 0 "
                 "(null TypeErrors the render tick: "
                 "rendered_today + in_flight >= budget)")
    _require_int(fields, "daily_publish_budget", 0,
                 "daily_publish_budget must be >= 0 "
                 "(null TypeErrors the publish tick: "
                 "published_today >= budget)")
    # Refuse malformed publish windows here: the loop fails open on a bad stored
    # spec (mistimed publishes beat a silently-stalled channel), so the API must
    # be the place a typo actually surfaces. Empty strings clear the field.
    if fields.get("publish_windows"):
        from app.services.publish_loop import parse_windows
        if parse_windows(fields["publish_windows"]) is None:
            raise HTTPException(400, "invalid publish_windows — expected "
                                     '"HH:MM-HH:MM" ranges separated by commas, '
                                     'e.g. "12:00-13:30,19:00-20:30"')
    if fields.get("publish_tz"):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(fields["publish_tz"])
        except Exception:
            raise HTTPException(400, f"unknown publish_tz {fields['publish_tz']!r} — "
                                     'use an IANA name like "America/Sao_Paulo"')
    for key in ("publish_windows", "publish_tz"):
        if key in fields and not (fields[key] or "").strip():
            fields[key] = None
    for k, v in fields.items():
        setattr(ch, k, v)
    ch.updated_at = utcnow()
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


@router.delete("/{channel_id}", status_code=204)
def delete_channel(
    channel_id: int,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    ch = require_ws_channel(session, ws, channel_id)
    session.delete(ch)
    session.commit()


@router.post("/{channel_id}/credentials")
def upload_credentials(
    channel_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    ch = require_ws_channel(session, ws, channel_id)
    cdir = youtube.channel_dir(ch.slug)
    cdir.mkdir(parents=True, exist_ok=True)
    youtube.client_secret_path(ch.slug).write_bytes(file.file.read())
    return {"ok": True}


@router.post("/{channel_id}/oauth/start")
def oauth_start(
    channel_id: int,
    request: Request,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    """Begin the redirect-based consent: return the Google authorization URL for the
    user's own browser to open. No server-side browser needed."""
    ch = require_ws_channel(session, ws, channel_id)
    if not youtube.has_client_secret(ch.slug):
        raise HTTPException(400, "upload client_secret.json first")
    # MANAGER_PUBLIC_BASE_URL pins the redirect_uri (rationale on the setting).
    base = settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/channels/{channel_id}/oauth/callback"
    try:
        verifier, challenge = oauth_pending.new_pkce_pair()
        flow = youtube.build_flow(ch.slug, redirect_uri)
        url, state = youtube.authorization_url(
            flow, code_challenge=challenge, code_verifier=verifier)
    except Exception as e:
        raise HTTPException(400, f"could not start OAuth: {e}")
    oauth_pending.create_pending(
        session,
        state=state,
        channel_id=channel_id,
        workspace_id=_channel_workspace_id(ch),
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    return {"auth_url": url}


# Web-flavored remediation per GrantRejected.code (the CLI appends --force /
# --allow-partial hints instead; codes without an entry already self-explain).
_GRANT_HINTS = {
    youtube.GrantCode.CHANNEL_MISMATCH: " Disconnect the channel first if you "
        "really mean to re-bind it (or use the reconnect CLI with --force).",
}
assert set(_GRANT_HINTS) <= youtube.GRANT_CODES, "stale GrantRejected code in _GRANT_HINTS"


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
    pending = None
    flow = None
    if ch and state:
        pending = oauth_pending.consume_pending(
            session,
            state=state,
            channel_id=channel_id,
            workspace_id=_channel_workspace_id(ch),
        )
        if pending:
            try:
                flow = _rebuild_flow(ch, pending)
            except Exception:
                flow = None
    if error or not code or not flow or not ch:
        if ch and pending and (error or not code):
            _fail_consent(session, ch, error or "consent was cancelled or timed out")
        return HTMLResponse(_callback_html("Connection failed", error or "consent cancelled", False))
    try:
        identity = youtube.finish_flow(session, ch, flow, code)
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
    except youtube.StatusFlipFailed as e:
        # Same contract the CLI prints: the token IS saved and valid, only the
        # status flip is missing — a re-consent would rotate the good token
        # into token.json.bak for nothing. Token-wise this consent succeeded,
        # so sibling pending starts are superseded like any success.
        oauth_pending.supersede_channel(session, channel_id)
        display = e.identity.get("title") or ch.name
        logger.error("channel '%s': token saved but the status update failed: %s", ch.slug, e)
        return HTMLResponse(_callback_html(
            "Token saved, status update failed",
            f"The new token for {display} is saved and working, but the dashboard status "
            "could not be updated — do NOT redo the consent. Open the dashboard (or the "
            "channel's oauth-status endpoint) to refresh it.", False))
    except Exception as e:
        _fail_consent(session, ch, str(e))
        return HTMLResponse(_callback_html("Connection failed", str(e)[:200], False))
    oauth_pending.supersede_channel(session, channel_id)
    display = identity.get("title") or ch.name
    return HTMLResponse(_callback_html(
        "Connected", f"{display} is linked. You can close this tab.", True))


@router.post("/{channel_id}/disconnect")
def disconnect(
    channel_id: int,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    ch = require_ws_channel(session, ws, channel_id)
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


def _probe_connected(session: Session, ch: Channel, service) -> None:
    """Record a channel whose token just probed healthy as CONNECTED.

    This endpoint is the documented repair when a consent saved a good token
    but its mark_connected commit failed — both the callback page and the
    reconnect CLI point the operator here — and that failure is precisely the
    case that leaves the channel with a working token and NO bound YouTube
    identity. Flipping the status alone left the repair half-done: yt_channel_id
    stayed empty, so the dashboard kept showing the stale name and the next
    re-consent had no expected_channel_id for verify_grant's wrong-account
    check. So when the identity is missing, finish the job through the same
    notify.mark_connected choke point a successful consent uses.

    An already-bound channel takes the cheap in-place flip: the dashboard polls
    this endpoint every 2.5s while a reconnect is in flight, and
    channels().list costs a quota unit per call.
    """
    if not ch.yt_channel_id:
        try:
            identity = youtube.fetch_identity(service)
        except Exception as e:
            # Best-effort: get_service already proved the token refreshes, so a
            # channels().list blip must not turn a healthy probe into a
            # dead-channel flip. Fall through to the plain status flip.
            identity = {}
            logger.warning("channel '%s': connected, but binding the YouTube "
                           "identity failed: %s", ch.slug, e)
        if identity.get("id"):
            notify.mark_connected(session, ch, identity)
            return
    ch.oauth_status = OAuthStatus.CONNECTED
    ch.oauth_error = None
    session.add(ch)
    session.commit()


@router.get("/{channel_id}/oauth-status")
def oauth_status(
    channel_id: int,
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    ch = require_ws_channel(session, ws, channel_id)
    try:
        _probe_connected(session, ch, youtube.get_service(ch.slug, channel=ch, session=session))
    except Exception as e:  # NeedsConnect, refresh failure, bad/old-scope token, etc.
        notify.mark_dead_committed(session, ch, str(e))
    return {"oauth_status": ch.oauth_status, "error": ch.oauth_error,
            "has_client_secret": youtube.has_client_secret(ch.slug)}
