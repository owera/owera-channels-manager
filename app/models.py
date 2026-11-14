"""Data model — the single source of truth.

Hierarchy:  Channel ─┬─ Topic (content theme, owns a playlist)
                      │     └─ Video (produced from the theme, lands in the playlist)
                      └─ RenderProfile (reusable VideoParams presets)

Topics are managed in the channel's **content settings**; the queue/board shows the
**Videos** produced from those topics.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Video lifecycle:
#   draft -> queued -> rendering -> rendered -> (review | approved)
#         -> publishing -> published   (+ failed, rejected)
# Generated ideas arrive as `draft`; you "produce" the ones you want (-> queued).
class VideoStatus:
    DRAFT = "draft"
    QUEUED = "queued"
    RENDERING = "rendering"
    RENDERED = "rendered"
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    REJECTED = "rejected"


# Durable publish craft gate (anti-nonsense). Publish loop only selects pass.
# pending = not yet evaluated / needs operator or auto-eval;
# fail = blocked (mute, empty script, banned title, Gate A/B/C);
# pass = explicit craft gate clearance required before publish picks the row.
class CraftReview:
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"


class OAuthStatus:
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    EXPIRED = "expired"
    ERROR = "error"


class Workspace(SQLModel, table=True):
    """Early-access tenant on shared claw0 CM. EA tenant #1 shares this instance."""
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    name: str
    # Per-workspace YouTube Data API quota cap (units/day); PR-G enforces in publish loop.
    daily_youtube_quota_cap: int = 9000
    subscription_status: str = "trialing"
    onboarding_complete: bool = False
    owner_email: Optional[str] = Field(default=None, index=True)
    stripe_customer_id: Optional[str] = Field(default=None, index=True)
    stripe_subscription_id: Optional[str] = Field(default=None, index=True)
    welcome_email_sent: bool = False
    closed_at: Optional[datetime] = None
    # Subscription state from Stripe webhooks (P6). stripe_state_event_created is
    # the Stripe `event.created` (unix s) of the last applied state change; an
    # older event that arrives late is ignored, so state follows Stripe's order.
    stripe_state_event_created: Optional[int] = None
    stripe_raw_status: Optional[str] = None
    subscription_current_period_end: Optional[datetime] = None
    subscription_cancel_at_period_end: bool = False
    last_invoice_paid_at: Optional[datetime] = None
    # S4: per-workspace automation pause (render submit/auto-produce, publish,
    # analytics, autofill skip this workspace's channels). The global
    # Settings.scheduler_paused stays the admin (ws 1) kill switch for everyone.
    scheduler_paused: bool = False
    # S5: ops-provisioned E2E workspace (app.provision_test_workspace), no Stripe.
    # Never has a fake paid_at; activation_report flags it.
    is_test: bool = Field(default=False)
    login_ambiguous_owner_count: Optional[int] = None
    login_ambiguous_owner_last_at: Optional[datetime] = None
    # S1 activation timestamps (tz-aware UTC). paid_at = first CM checkout's
    # Stripe event.created (A0); first_draft_at = first successful render of any
    # video in this workspace (A3). Both written once, never overwritten.
    paid_at: Optional[datetime] = None
    first_draft_at: Optional[datetime] = None
    # S8 LLM guard: per-workspace kill switch (ops: python -m app.llm_kill_switch).
    # True = every LLM call for this workspace is refused, internal ones included.
    llm_disabled: bool = Field(default=False)
    # S8 release check inputs (tz-aware UTC). No UI writes them yet; null = not
    # accepted. Read only when MANAGER_LLM_EXTERNAL_RELEASE is on.
    terms_accepted_at: Optional[datetime] = None
    privacy_accepted_at: Optional[datetime] = None
    # S8 (PO CM 14/11): last LLM guard refusal for this workspace (flag ON, non-internal
    # only), shown to the user as a "Drafts paused: …" banner. Cleared by the next
    # allowed call. Written only by llm_guard (never by request handlers).
    llm_blocked_reason: Optional[str] = None
    llm_blocked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class StripeEvent(SQLModel, table=True):
    """Processed Stripe webhook events (idempotency by event id)."""
    id: str = Field(primary_key=True, max_length=255)
    event_type: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class StripeCheckoutSession(SQLModel, table=True):
    """One row per provisioned Checkout Session (cs_…). The primary key makes a
    second delivery for the same session (a different event id, or a second
    webhook endpoint) a no-op even under concurrent delivery."""
    id: str = Field(primary_key=True, max_length=255)
    event_id: str = Field(max_length=255)
    workspace_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class OAuthPending(SQLModel, table=True):
    """Single-use OAuth state + PKCE verifier (survives process restart)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    state_hash: str = Field(index=True)
    workspace_id: int = Field(default=1, foreign_key="workspace.id", index=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    code_verifier: str
    redirect_uri: str
    expires_at: datetime
    consumed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class CredentialSource:
    LEGACY_FILE = "legacy_file"
    PLATFORM_OAUTH = "platform_oauth"


class OAuthCredential(SQLModel, table=True):
    """Platform OAuth refresh token at rest (one row per channel). Legacy ch1/ch2 use token.json."""
    channel_id: int = Field(primary_key=True, foreign_key="channel.id")
    workspace_id: int = Field(foreign_key="workspace.id", index=True)
    key_id: str = Field(default="v1", max_length=32)
    ciphertext_b64: str
    updated_at: datetime = Field(default_factory=utcnow)


class Channel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(default=1, foreign_key="workspace.id", index=True)
    slug: str = Field(index=True)
    name: str
    credential_source: str = Field(default=CredentialSource.LEGACY_FILE)
    yt_channel_id: Optional[str] = None
    yt_channel_title: Optional[str] = None
    oauth_status: str = OAuthStatus.DISCONNECTED
    oauth_error: Optional[str] = None

    default_render_profile_id: Optional[int] = Field(default=None, foreign_key="renderprofile.id")
    default_skip_gate: bool = False
    default_privacy: str = "public"
    daily_render_budget: int = 6
    daily_publish_budget: int = 6
    paused: bool = False

    # Free-text niche / category for the channel (Generics v1). Included in idea/script
    # prompts so agents can steer tone without preset enums.
    niche: Optional[str] = None

    # Set when a YouTube daily cap is hit; the publish loop skips this channel until
    # then. tz-aware UTC; reset model depends on which cap (see quota.cooldown_until_for).
    cooldown_until: Optional[datetime] = None

    # Audience-peak drip: when set, the publish loop only publishes inside these
    # channel-local windows ("HH:MM-HH:MM,HH:MM-HH:MM…", interpreted in publish_tz —
    # IANA name, unset = UTC). A small channel's best algorithmic test is its first
    # hours, so don't spend it at audience-dead hours. Unset/empty = publish anytime.
    publish_windows: Optional[str] = None
    publish_tz: Optional[str] = None

    # Explicit test/smoke/dogfood marker (P7). A test channel never sends the
    # contact@ access-lost email. Operators set it by hand; default False.
    is_test: bool = False

    # Google access lifecycle (P3, naive UTC like utcnow()). access_lost_at is set
    # when access is definitively gone — operator/user disconnect, a deletion
    # request, a workspace close, or a refresh that failed with invalid_grant —
    # and cleared on reconnect. The purge job deletes the channel's YouTube API
    # data after 7 days (user-initiated) or 30 days (Google revoke / RefreshError).
    access_lost_at: Optional[datetime] = None
    access_lost_reason: Optional[str] = None
    youtube_data_purged_at: Optional[datetime] = None
    # When oauth_status last flipped (dedupe key for the access-lost email).
    oauth_status_changed_at: Optional[datetime] = None
    # S7: successful grant/refresh only; stale is an observation, not OAuth status.
    last_refresh_ok_at: Optional[datetime] = None
    token_refresh_stale_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChannelAlert(SQLModel, table=True):
    """Persisted dedupe for the access-lost email (P7): one row per
    channel + status + transition time. The primary key makes it once-only."""
    key: str = Field(primary_key=True, max_length=255)
    channel_id: int = Field(index=True)
    status: str
    transition_at: datetime
    delivered: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class RenderProfile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    channel_id: Optional[int] = Field(default=None, foreign_key="channel.id")  # null = shared
    engine: str = "mpt"                           # which render engine these params target
    params_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Playlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    yt_playlist_id: str = Field(index=True)
    title: str
    description: Optional[str] = None
    privacy: Optional[str] = None
    last_synced_at: Optional[datetime] = None


class Topic(SQLModel, table=True):
    """A channel content theme. Owns a playlist; videos are generated from it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    name: str                                     # e.g. "RAG", "AI Agents"
    theme_prompt: Optional[str] = None            # guidance for generating video ideas
    content_format: str = "short"                 # "short" (vertical Shorts) | "long" (16:9 long-form)
    playlist_id: Optional[int] = Field(default=None, foreign_key="playlist.id")
    render_profile_id: Optional[int] = Field(default=None, foreign_key="renderprofile.id")
    active: bool = True
    # Growth-agent steering knob: scales how aggressively autofill tops up this topic's
    # idea queue. 1 = normal; >1 = a proven winner gets refilled more; 0 = soft-pause
    # (no new ideas) without deactivating the topic.
    weight: int = 1
    position: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Video(SQLModel, table=True):
    """A single produced video — the render/review/publish unit. Belongs to a topic;
    publishes into that topic's playlist."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    topic_id: int = Field(foreign_key="topic.id", index=True)
    subject: str
    status: str = Field(default=VideoStatus.DRAFT, index=True)
    position: int = 0

    render_profile_id: Optional[int] = Field(default=None, foreign_key="renderprofile.id")
    overrides_json: Optional[str] = None
    skip_gate: Optional[bool] = None              # null -> inherit channel default
    privacy: Optional[str] = None                 # null -> inherit channel default

    # render results
    engine: Optional[str] = None                  # frozen at submit: which engine rendered this
    mpt_task_id: Optional[str] = None             # opaque engine handle (MPT task id / HF job id)
    render_progress: int = 0
    video_path: Optional[str] = None
    thumb_path: Optional[str] = None
    script: Optional[str] = None
    # JSON snapshot of the creative choices this video was made with (beat mix, theme,
    # voice, bgm, script length, …) — the "treatment" signal joined to VideoMetric for
    # learning what drives engagement. Recorded at finalize; captured now (can't backfill).
    creation_config: Optional[str] = None

    # Durable craft gate for the publish loop (see CraftReview). Default pending
    # so legacy approved rows cannot publish until evaluated to pass.
    craft_review: str = Field(default="pending", index=True)

    # Operator hold (P0 2026-09-29, extended 2026-10-28 #1458): publish loop skips
    # held APPROVED rows; render_loop skips held QUEUED. Runway/queue counts
    # exclude held APPROVED. Orthogonal to craft_review. Only hold|unhold set it.
    held: bool = Field(default=False, index=True)
    held_at: Optional[datetime] = None

    # Consecutive renders whose Video Maker gate had a check classified
    # kind="measurement" (unverifiable, #70). At MEASUREMENT_CAP_N the render is
    # capped (craft gate FAIL → VM review). A render with no measurement entry
    # resets it. Survives requeue and creation_config rewrites (CTO 06/10 2c).
    measurement_streak: int = Field(default=0)

    # gate / metadata
    title: Optional[str] = None
    description: Optional[str] = None
    tags_json: Optional[str] = None
    metadata_generated: bool = False
    approved_at: Optional[datetime] = None
    # S1/A4: who approved (review_guard.actor_of — SPA session / Basic actor;
    # "auto:skip_gate" for the render loop's auto-approve). Null when unknown or
    # when approved_at is cleared.
    approved_by: Optional[str] = None
    rejected_reason: Optional[str] = None

    # publish results
    yt_video_id: Optional[str] = None
    published_at: Optional[datetime] = None
    added_to_playlist: bool = False

    # error / retry
    error: Optional[str] = None
    retry_count: int = 0
    last_attempt_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class JobRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: Optional[int] = Field(default=None, foreign_key="video.id", index=True)
    channel_id: Optional[int] = Field(default=None, foreign_key="channel.id", index=True)
    kind: str                                     # render|publish|metadata|playlist_add|generate|oauth|delete
    status: str                                   # started|success|error
    detail: Optional[str] = None
    quota_cost: int = 0
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ChannelMetric(SQLModel, table=True):
    """A point-in-time snapshot of a channel's public YouTube statistics, recorded
    daily by the scheduler so the UI can show subscriber/view/video trends."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    subscriber_count: int = 0
    view_count: int = 0
    video_count: int = 0
    captured_at: datetime = Field(default_factory=utcnow, index=True)


_NULLABLE_METRIC = {"default": None, "nullable": True}


class VideoMetric(SQLModel, table=True):
    """A point-in-time per-video YouTube Analytics snapshot, recorded ~daily by the
    analytics loop. The time series powers the leaderboard the growth agent learns from."""
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    # Metric columns are NULL (not 0) when the API had not reported the video
    # yet (empty answer inside the 72h lag — analytics_loop). Consumers must
    # treat NULL as "no data", never as zero (youtube_admin._latest_metrics).
    # Python default stays 0; the SQL column default is dropped because
    # SQLAlchemy fires a column default for an explicit None (NULL would be
    # silently written as 0).
    views: Optional[int] = Field(default=0, sa_column_kwargs=_NULLABLE_METRIC)
    # impressions / ctr: the Analytics API v2 does not expose them (2026-07-09
    # audit). Default NULL, never an invented 0 (S8a / LegalOps L4).
    impressions: Optional[int] = Field(default=None, sa_column_kwargs=_NULLABLE_METRIC)
    ctr: Optional[float] = Field(default=None, sa_column_kwargs=_NULLABLE_METRIC)  # (0..1)
    avg_view_pct: Optional[float] = Field(default=0.0, sa_column_kwargs=_NULLABLE_METRIC)  # (0..100)
    watch_time_minutes: Optional[int] = Field(default=0, sa_column_kwargs=_NULLABLE_METRIC)
    # seconds watched per view (avg); pairs with avg_view_pct
    average_view_duration: Optional[float] = Field(default=0.0, sa_column_kwargs=_NULLABLE_METRIC)
    likes: Optional[int] = Field(default=0, sa_column_kwargs=_NULLABLE_METRIC)
    comments: Optional[int] = Field(default=0, sa_column_kwargs=_NULLABLE_METRIC)
    subscribers_gained: Optional[int] = Field(default=0, sa_column_kwargs=_NULLABLE_METRIC)
    # JSON: {"sources": {trafficSourceType: {views, watch_min}}, "search_terms": {term: views}}
    # — where views come from (browse/suggested/search/external), the attribution data
    # the subscriber-growth loop optimizes against. Null when the video has no views yet.
    traffic_json: Optional[str] = None
    captured_at: datetime = Field(default_factory=utcnow, index=True)


class TrendStatus:
    RESEARCHED = "researched"     # found + scored this run
    WATCHING = "watching"         # promising but not adopted yet
    ADOPTED = "adopted"           # turned into a topic (adopted_topic_id set)
    REJECTED = "rejected"         # decided not worth it


class TrendSignal(SQLModel, table=True):
    """A trending topic the growth agent researched (via WebSearch) and scored for
    'smart adoption'. Persisted so adoption is deduped across days and learnable: an
    adopted trend links to a Topic, whose videos' analytics show whether it paid off."""
    id: Optional[int] = Field(default=None, primary_key=True)
    term: str                                     # human-facing trend, e.g. "LangGraph"
    term_norm: str = Field(index=True)            # lowercased/trimmed, for dedup
    description: Optional[str] = None              # what it is + why it matters
    source: Optional[str] = None                  # e.g. "WebSearch: HN/PyPI"
    channel_id: Optional[int] = Field(default=None, foreign_key="channel.id", index=True)
    language: Optional[str] = None                # "en" | "pt"
    content_format: str = "short"                 # suggested format for adoption
    momentum: Optional[str] = None               # rising | hot | fading | evergreen
    score: float = 0.0                            # smart-adoption score, 0..100
    status: str = Field(default=TrendStatus.RESEARCHED, index=True)
    decision_reason: Optional[str] = None         # why adopt / watch / reject
    adopted_topic_id: Optional[int] = Field(default=None, foreign_key="topic.id")
    first_seen_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)


class Settings(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    render_concurrency: int = 1
    publish_drip_minutes: int = 30
    scheduler_paused: bool = False
    # Auto-refill: when a topic's pending (draft+queued) videos drop below the
    # threshold, generate more video ideas for it.
    topic_autogen_enabled: bool = False
    topic_autogen_min_pending: int = 3
    # Ceiling: stop refilling a topic's idea bench once it has this many pending
    # (draft+queued) videos. Bounds the board's IDEAS column so it can't grow daily.
    topic_autogen_target: int = 6
    # How many days of render work to keep in the idea bench (DRAFT+QUEUED) per channel.
    # Channel board cap = daily_render_budget × board_horizon_days. Autofill and manual
    # idea generation both stop once the channel hits this inventory level.
    board_horizon_days: int = 2
