"""Manager configuration. Values come from environment / manager/.env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

MANAGER_DIR = Path(__file__).resolve().parent.parent          # .../manager
REPO_DIR = MANAGER_DIR.parent                                 # .../ai-engineering-youtube-channel
MPT_DIR = REPO_DIR / "MoneyPrinterTurbo"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(MANAGER_DIR / ".env"),
        env_prefix="MANAGER_",
        extra="ignore",
    )

    # Network. Manager binds all interfaces for LAN access; MPT stays internal.
    host: str = "0.0.0.0"
    port: int = 7070

    # Base URL for the OAuth redirect_uri built by oauth_start (env:
    # MANAGER_PUBLIC_BASE_URL). Empty = derive from the incoming request, which
    # breaks behind the reverse proxy: a channels.owera.com Host produces a
    # non-loopback redirect_uri the Desktop OAuth client rejects
    # (redirect_uri_mismatch). Set it to a base the client accepts — e.g.
    # http://localhost:7070 — so reconnects started from the portal work too.
    public_base_url: str = ""

    # Optional alert webhook (env: MANAGER_ALERT_WEBHOOK_URL). When a channel's
    # OAuth token flips CONNECTED -> EXPIRED, app/services/notify.py POSTs a JSON
    # payload (Slack "text" + Discord "content" keys) here, on top of the ERROR
    # log line it always writes. Empty = log-only.
    alert_webhook_url: str = ""

    # Optional HTTP Basic Auth password for public access. Any username + this
    # password is accepted. Empty string = no auth (safe for local-only use).
    app_password: str = ""

    # MoneyPrinterTurbo engine
    mpt_base_url: str = "http://127.0.0.1:8080"
    mpt_storage_dir: str = str(MPT_DIR / "storage" / "tasks")

    # HyperFrames engine (local HTML->MP4 CLI run via npx; no service to host)
    hyperframes_version: str = "0.6.97"           # pinned; CLI contract validated against it
    hyperframes_render_quality: str = "standard"  # draft | standard | high
    hyperframes_storage_dir: str = str(MANAGER_DIR / "storage" / "hyperframes")
    # Background-music pool shared with MPT; falls back to channel/music/ if absent.
    bgm_dir: str = str(MPT_DIR / "resource" / "songs")

    # Paths (managed by us)
    db_path: str = str(MANAGER_DIR / "manager.db")
    credentials_dir: str = str(MANAGER_DIR / "credentials")
    storage_dir: str = str(MANAGER_DIR / "storage")
    frontend_dist: str = str(MANAGER_DIR / "frontend" / "dist")

    # LLM (script/metadata fallback) — reuse the same key the rest of the repo uses
    anthropic_api_key: str = ""
    litellm_model: str = "anthropic/claude-opus-4-8"

    # HuggingFace token for MusicGen music generation (env: MANAGER_HF_TOKEN or HF_TOKEN)
    hf_token: str = ""
    # BGM pool auto-replenish thresholds
    bgm_pool_min: int = 5       # trigger replenish when pool drops below this
    bgm_pool_target: int = 15   # fill up to this many tracks

    # Scheduler defaults (also editable per-row in the Settings table)
    render_tick_seconds: int = 15
    publish_tick_seconds: int = 60
    render_poll_seconds: int = 10
    render_timeout_seconds: int = 2400        # 40 min hard cap per render
    publish_timeout_seconds: int = 900        # recover a video stuck 'publishing' past this
    publish_max_retries: int = 5              # after this many stuck-publish recoveries, mark the video failed instead of re-queuing (a permanently-stalling upload must not block the channel forever)
    youtube_http_timeout_seconds: int = 120   # socket timeout on YouTube HTTP calls so a stalling resumable upload FAILS FAST (and retries) instead of hanging the whole publish_timeout window
    youtube_daily_quota_cap: int = 9000       # safety cap below the ~10k API quota
    metrics_tick_hours: int = 6               # channel-stats snapshot cadence (≤1/day each)
    analytics_tick_hours: int = 12            # per-video analytics snapshot cadence (≤1/day each)
    autofill_tick_minutes: int = 20           # how often to top up low topic idea queues
    autofill_batch: int = 8                   # ideas generated per topic refill

    # --- Composition (HyperFrames storyboard) -----------------------------------
    # Which beat types the LLM may use and the validator will accept. Any beat whose
    # type is not listed here is downgraded to "statement", so this list is the rollout
    # switch. All types are live (Phase A pure-HTML/CSS beats + Phase B/C code/command/
    # diagram, verified to render under the pinned hyperframes). To roll back a type,
    # remove it here; MANAGER_COMPOSITION_BEAT_TYPES can override via env (JSON array).
    composition_beat_types: list[str] = [
        "hook", "statement", "stat", "compare", "list", "term_define", "quote", "cta",
        "code", "command", "diagram",
    ]
    # Post-render blank-frame guard: a sampled 32x32 gray frame with pixel stddev
    # below this (0-255 scale) counts as "blank". A render is rejected only when
    # ALL sampled frames are blank (avoids false positives on dark hook frames).
    composition_blank_stddev: float = 4.0
    # "storyboard" = new typed-beat path; "legacy" = old clip-array path (kill switch).
    composition_version: str = "storyboard"


settings = Settings()


def ensure_dirs() -> None:
    for p in (settings.credentials_dir, settings.storage_dir,
              str(Path(settings.storage_dir) / "videos"),
              settings.hyperframes_storage_dir):
        Path(p).mkdir(parents=True, exist_ok=True)


def load_dotenv_into_env() -> None:
    """Mirror produce.py's loader: ~/.bashrc guards exports behind an interactive
    check, so a non-interactive service won't inherit ANTHROPIC_API_KEY. Load
    manager/.env (and fall back to channel/.env) into os.environ for litellm."""
    import os

    for env_path in (MANAGER_DIR / ".env", REPO_DIR / "channel" / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    # Bridge the manager setting (MANAGER_ANTHROPIC_API_KEY) to the bare name
    # litellm reads. Without this, a key set via the manager's own MANAGER_*
    # convention is loaded into settings but never reaches the LLM call.
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key)
