"""Manager configuration. Values come from environment / manager/.env."""

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
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

    # Stripe subscription provisioning (env: MANAGER_STRIPE_WEBHOOK_SECRET).
    # Signing secret from Stripe Dashboard → Webhooks (whsec_…). Empty = route returns 503.
    stripe_webhook_secret: str = ""

    # Payment Link ids whose checkouts provision a CM workspace
    # (env: MANAGER_STRIPE_CM_PAYMENT_LINKS, comma-separated). The Stripe account
    # is shared with OSI one-time links, so a checkout from any other link is
    # ignored. A checkout with no payment_link provisions only in subscription mode.
    stripe_cm_payment_links: str = "plink_1UL1ccADrWLjH4u9HD9GC4n4"

    # Resend transactional email (env: MANAGER_RESEND_API_KEY). Welcome mail after checkout.
    resend_api_key: str = ""

    # Optional alert webhook (env: MANAGER_ALERT_WEBHOOK_URL). When a channel's
    # OAuth token flips CONNECTED -> EXPIRED, app/services/notify.py POSTs a JSON
    # payload (Slack "text" + Discord "content" keys) here, on top of the ERROR
    # log line it always writes. Empty = log-only.
    alert_webhook_url: str = ""

    # Optional HTTP Basic Auth password for public access. Any username + this
    # password is accepted. Empty string = no auth (safe for local-only use).
    app_password: str = ""

    # OAuth grant verification: empty = use youtube.CONSENT_SCOPES (WS4 v1 tier).
    oauth_required_scopes: list[str] = []

    # Shared claw0: scope API data by Workspace (EA tenant #1 = default workspace).
    multi_workspace: bool = False
    allow_workspace_header: bool = False

    # Platform OAuth (PR-E): inert unless MANAGER_GOOGLE_WEB_OAUTH=1.
    google_web_oauth: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    oauth_redirect_uri: str = "https://channels.owera.com/api/oauth/google/callback"
    token_enc_key: str = ""

    # SPA human login (Google Sign-In → session cookie). Separate Web OAuth client
    # from platform YouTube Connect (MANAGER_GOOGLE_*). GCP project 618513602444.
    spa_google_login: bool = False
    spa_google_client_id: str = ""
    spa_google_client_secret: str = ""
    spa_google_redirect_uri: str = "https://channels.owera.com/api/auth/google/callback"
    # Signed session cookie (required when spa_google_login=1 on claw0).
    session_secret: str = ""
    # Match SessionMiddleware https_only (set True behind HTTPS in production).
    session_cookie_secure: bool = False

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

    # LLM: Grok Build CLI headless (`grok -p` / `--single`). Live claw0:
    # ~/.local/bin/grok → ~/.grok/bin/grok (1.0.5). launchd PATH already has
    # ~/.local/bin; OIDC via `grok login`. No Anthropic / LiteLLM / XAI_API_KEY.
    grok_bin: str = "grok"                    # env: MANAGER_GROK_BIN
    grok_timeout_seconds: int = 600           # env: MANAGER_GROK_TIMEOUT_SECONDS
    # 300s was enough for shorts (~6k-char storyboard prompts) but overnight
    # longs (09-01 v1213/v1223, ~10k-char prompts) hit grok.Timeout at 300s
    # exactly and the worker swallowed it into kinetic-text fallback.
    # Light budget for short completions (idea strip / metadata / digest-adjacent).
    # Storyboard + script compose keep grok_timeout_seconds (600).
    grok_timeout_seconds_light: int = 180     # env: MANAGER_GROK_TIMEOUT_SECONDS_LIGHT
    # Model / reasoning-effort pins for `grok -p` (grok >= 1.0.41 flags `-m` and
    # `--reasoning-effort`). 2026-09-22: the CLI's server-side default model flipped
    # grok-4.6-build -> grok-4.7-build ("grok-4.7-launch" campaign, effort "high").
    # Storyboard compose reasoning doubled (median ~16k -> ~33k reasoning tokens,
    # first token after ~400s, wall median ~286s -> ~480s, p90 ~567s), so ~1/3 of
    # compose calls crossed the 600s HARD timeout on BOTH channels (v1297..v1356).
    # Empty string = no flag (CLI default), i.e. the old behavior.
    grok_model: str = ""                      # env: MANAGER_GROK_MODEL (e.g. "grok-4.6")
    # Effort for the storyboard compose call only (the step that times out).
    # Accepted values per the CLI model catalog: low | medium | high | xhigh.
    grok_compose_reasoning_effort: str = "medium"  # env: MANAGER_GROK_COMPOSE_REASONING_EFFORT
    # Effort for light completions: idea strip (video_gen.generate_ideas, used
    # by autofill + trends adopt) and metadata._llm_fallback. Default "low" so
    # grok-4.7 does not spend the CLI default "high" (CTO: 2.5–8 min with ~27
    # tools) against grok_timeout_seconds_light (180s; claw0 palliative is 420s).
    # Same mechanism as compose: passed as ``--reasoning-effort`` through
    # llm.complete(); empty string = no flag (kill switch, CLI default).
    # Accepted values (grok 1.0.77 catalog): none | minimal | low | medium |
    # high | xhigh | max. env: MANAGER_GROK_EFFORT_IDEAS
    #
    # Tools (documented, NOT default): these prompts are structured text and
    # do not obviously need the CLI tool set. grok 1.0.77 also accepts
    # --disable-web-search, --no-subagents, --max-turns 1, --tools, and
    # --disallowed-tools. Leave them off until we have evidence the tool set
    # (not just high effort) is what blows the light budget.
    grok_effort_ideas: str = "low"                # env: MANAGER_GROK_EFFORT_IDEAS
    # LLM hard guard (app/services/llm_guard.py, docs/LLM_GUARD.md): comma-separated
    # Owera-internal workspace ids allowed to reach the LLM. The consumer `grok login`
    # session may train on prompts, so Google-sourced data from any other workspace
    # must never reach it (Google Limited Use). "1" = slug "owera". Empty → all blocked.
    llm_internal_workspace_ids: str = "1"     # env: MANAGER_LLM_INTERNAL_WORKSPACE_IDS
    # LLM backend (docs/LLM_BACKEND.md). "cli" (default) = Grok CLI `grok -p` on the
    # consumer `grok login` session — unchanged behavior. "api" = xAI API
    # (Enterprise ToS) POST {llm_api_base_url}/responses with XAI_API_KEY, usage
    # JSONL + monthly USD cap (fail closed). Anything else = cli.
    llm_backend: str = Field(default="cli", validation_alias=AliasChoices(
        "MANAGER_LLM_BACKEND", "LLM_BACKEND"))
    # Never logged / never in errors; SecretStr keeps it out of repr(settings).
    xai_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices(
        "XAI_API_KEY", "MANAGER_XAI_API_KEY"))
    llm_api_base_url: str = "https://api.x.ai/v1"   # env: MANAGER_LLM_API_BASE_URL
    # Model the api backend uses when the CLI would use its own default (grok_model
    # empty). Live claw0 pins MANAGER_GROK_MODEL=grok-4.7, which maps 1:1.
    llm_api_default_model: str = "grok-4.7"   # env: MANAGER_LLM_API_DEFAULT_MODEL
    # Monthly (UTC calendar month) spend cap in USD for the api backend. 0/unset in
    # api mode → every call blocked (fail closed).
    llm_monthly_usd_cap: float = Field(default=0.0, validation_alias=AliasChoices(
        "MANAGER_LLM_MONTHLY_USD_CAP", "LLM_MONTHLY_USD_CAP"))
    # One JSON line per api call / agent run: tokens, model, call type, workspace,
    # UTC ts, cost — never prompt/response content. Empty = storage_dir/llm-usage.jsonl.
    llm_usage_path: str = ""                  # env: MANAGER_LLM_USAGE_PATH
    # JSON override of the price table (USD per 1M tokens), e.g.
    # {"grok-4.7": {"input": 2.0, "cached_input": 0.5, "output": 6.0,
    #  "long_threshold": 200000, "long_input": 4.0, "long_cached_input": 1.0, "long_output": 12.0}}
    llm_price_table_json: str = ""            # env: MANAGER_LLM_PRICE_TABLE_JSON
    # Headless grok agents (growth / code / run-check) on the api backend: usage per
    # run isn't returned to us, so each run is charged this estimate against the cap.
    llm_agent_run_usd_estimate: float = 1.0   # env: MANAGER_LLM_AGENT_RUN_USD_ESTIMATE
    # edge-tts NoAudioReceived is an upstream flake (2026-09-26 21:00-21:53 BRT: 4 of
    # 8 en-US calls, then clean). Retry in-process before failing the whole render so
    # a short blip does not burn the video's render retry budget in ~2 minutes (v1360).
    tts_attempts: int = 3                     # env: MANAGER_TTS_ATTEMPTS
    tts_retry_backoff_seconds: list[int] = [5, 20]  # sleep before attempt 2, 3, ...
    # edge-tts synthesis endpoint (2026-10-12): since Oct 2026 the legacy host
    # speech.platform.bing.com answers the synthesis WebSocket with 403 (rany2/edge-tts
    # #490; ch1 #1448/#1461/#1462/#1463). Edge 155 synthesizes through
    # api.msedgeservices.com, which still accepts the legacy client token (given as
    # Ocp-Apim-Subscription-Key) + legacy Sec-MS-GEC. edge-tts 7.2.8 (latest on PyPI)
    # still points at the old host; the fix is upstream PR #491 (unreleased).
    # "" = use the library default (old behavior). The voice list stays on the old host.
    tts_edge_wss_url: str = (                 # env: MANAGER_TTS_EDGE_WSS_URL
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        "?Ocp-Apim-Subscription-Key={token}"
    )
    # Auto-produce circuit breaker (2026-10-12): a FAILED render frees a render slot
    # (rendered_today counts successes only), so _auto_produce pulled a fresh draft
    # after every failure — ch1 burned 7 drafts overnight on the same 403. After a
    # final (non-transient-retry) render failure, stop promoting drafts on that
    # channel for this long, or until a render succeeds / the failed video is
    # re-queued by an operator. 0 disables the breaker.
    auto_produce_fail_cooldown_hours: int = 12  # env: MANAGER_AUTO_PRODUCE_FAIL_COOLDOWN_HOURS

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

    # Review / publish runway signals (issues digest + dashboard; signal-only —
    # nothing here changes a budget, the mix, or auto-approves anything).
    # 2026-09-27 RR published 4/5 with two craft-PASS renders idle in review.
    ops_tz: str = "America/Fortaleza"         # the operator's day (review deadline tz)
    review_decide_by: str = "10:45"           # HH:MM ops_tz: review_ready decided before the 11:00 window
    runway_buffer: int = 1                    # runway target = daily_publish_budget + this (5 + 1 = 6)

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

    # --- VO-sentence cards (per brand) ---------------------------------------
    # Brands whose shorts get the deterministic VO-sentence board: one whole
    # spoken sentence per mid card, snapped to speech (enter at the first word,
    # exit ≤ last word + STALE_CARD_MAX_S). Brands not
    # listed keep the LLM compare / term_define / list board.
    # env: MANAGER_VO_SENTENCE_CARD_BRANDS='["os","rr"]'.
    # Global kill switch: craft.OS_VO_SENTENCE_CARDS = False (RR #1455).
    vo_sentence_card_brands: list[str] = ["os", "rr"]
    # Of those, brands whose VO-sentence board is timed against the Gate B
    # budgets: compose sync lead + card_sync marker at CARD_ENTER_LEAD_MAX_S
    # (0.3s; quotes 0) instead of 1.1s, long-pause closing under the blank /
    # stale budgets, hook join when the next sentence misses the 2.5s first
    # cut. Default RR only (PT ≈0.94s pauses, #1455) — OS boards, markers and
    # preflight stay exactly as before. env: MANAGER_GATE_TIMING_BRANDS='["rr"]'.
    gate_timing_brands: list[str] = ["rr"]

    # --- Spoken-sentence pace for generated short scripts (per channel) ---------
    # key -> {"target_s": prompt target, "cap_s": deterministic hard cap} (seconds
    # spoken, first→last word; app/services/sentence_pace.py estimator). Key
    # lookup order: "channel:<id>", channel slug, brand. A channel with no entry
    # keeps the generator exactly as before (OS). PO CM cut 10 Nov 2026: RR
    # 2.2s / 2.5s. env: MANAGER_SENTENCE_PACE='{"rr":{"target_s":2.2,"cap_s":2.5}}'.
    sentence_pace: dict[str, dict[str, float]] = {"rr": {"target_s": 2.2, "cap_s": 2.5}}

    # --- Auto-produce draft policy (per channel; app/services/autoproduce_policy.py)
    # key ("channel:<id>", slug, brand) -> {"max_age_days", "not_before" (ISO UTC),
    # "hook_preflight", "measure_spoken"}. Drafts that miss go to rejected with
    # "auto-produce skipped (stale draft): <reason>" instead of taking a render
    # slot; autofill then refills the topic with fresh, hook-checked ideas. A
    # channel with no entry keeps auto-produce exactly as before (OS). RR
    # 2026-11-13: 5 Sept/Oct drafts burned 5 slots on Gate B hook pace.
    # env: MANAGER_AUTO_PRODUCE_DRAFT_POLICY='{"rr":{"max_age_days":14,"hook_preflight":true}}'.
    auto_produce_draft_policy: dict[str, dict] = {
        "rr": {"max_age_days": 14, "hook_preflight": True, "measure_spoken": True}}

    # --- Per-topic policy flags (shared structure; app/services/topic_flags.py) --
    # flag name -> list of Topic ids it applies to ("*" = every topic). env:
    # MANAGER_TOPIC_FLAGS as a JSON object, e.g.
    #   MANAGER_TOPIC_FLAGS='{"vm_pass_exempt": [12, 31]}'
    # A flag missing from the env object keeps its code default
    # (topic_flags.DEFAULTS), so overriding one flag never silently drops another.
    #   vm_pass_required (default ["*"] = ALL topics): approve (API / growth agent
    #     / manual), skip-gate auto-approve, retry-republish and publish need the
    #     VM's Gate B PASS (POST /api/videos/{id}/vm-pass) on the CURRENT render;
    #     409 "held: needs vm_pass" otherwise (Rodrigo 08/10 after #1449/#1450,
    #     OS topic 47, were auto-approved and published without Gate B). A list
    #     of ids (e.g. [47]) narrows it to those topics; [] turns it off.
    #   vm_pass_exempt (default []): topics exempt from vm_pass_required.
    #   literal_cards (default [47], OS "named tool"): the storyboard never
    #     invents on-screen evidence — rule 2b (required snippet) off; log /
    #     error / command / quote / stat cards ONLY from the video's explicit
    #     allowlist (overrides "on_screen_allow", see craft.on_screen_allow);
    #     anything else becomes a plain text card; provided overrides
    #     "on_screen" cards verbatim; attribution / stat values only when the
    #     script says them (else Gate B FAIL = render blocked); no version on 2
    #     consecutive cards; the ≤2.5s first cut (VM FAIL P0 #1449, CMO 08/10).
    #   tight_mid_cards (default [47]): mid cards hold ≤2.48s (≈2.60s measured
    #     with the fade) instead of 2.80 (≈2.92) — generator only; Gate B keeps 2.80.
    #   allowlist_required (default [47]): approve 409 "held: …" and Gate B FAIL
    #     when the video has no overrides on_screen_allow.
    topic_flags: dict[str, list[int | str]] = {"vm_pass_required": ["*"], "vm_pass_exempt": [],
                                               "literal_cards": [47], "tight_mid_cards": [47],
                                               "allowlist_required": [47],
                                               "gate_b_preflight": [47]}


settings = Settings()


def ensure_dirs() -> None:
    for p in (settings.credentials_dir, settings.storage_dir,
              str(Path(settings.storage_dir) / "videos"),
              str(Path(settings.storage_dir) / "grok-scratch"),
              settings.hyperframes_storage_dir):
        Path(p).mkdir(parents=True, exist_ok=True)


def load_dotenv_into_env() -> None:
    """Load manager/.env (and fall back to channel/.env) into os.environ.

    Manager LLM calls go through `grok -p` (OIDC via `grok login`), not LiteLLM /
    Anthropic / XAI_API_KEY. This loader is for the rest of the process env
    (MANAGER_APP_PASSWORD, etc.). A non-interactive launchd service does not
    inherit a login shell, so .env is the source of those values.
    """
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
