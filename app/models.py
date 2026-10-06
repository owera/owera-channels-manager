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


class Channel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    name: str
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

    # Set when a YouTube daily cap is hit; the publish loop skips this channel until
    # then. tz-aware UTC; reset model depends on which cap (see quota.cooldown_until_for).
    cooldown_until: Optional[datetime] = None

    # Audience-peak drip: when set, the publish loop only publishes inside these
    # channel-local windows ("HH:MM-HH:MM,HH:MM-HH:MM…", interpreted in publish_tz —
    # IANA name, unset = UTC). A small channel's best algorithmic test is its first
    # hours, so don't spend it at audience-dead hours. Unset/empty = publish anytime.
    publish_windows: Optional[str] = None
    publish_tz: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


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

    # Operator hold on an APPROVED video (P0 2026-09-29): the publish loop skips
    # held rows (selection + craft sweep) and runway/queue counts exclude them.
    # Orthogonal to status and craft_review — unhold resumes the drip with the
    # same artifact, no re-render. Only POST /api/videos/{id}/hold|unhold set it.
    held: bool = Field(default=False, index=True)
    held_at: Optional[datetime] = None

    # gate / metadata
    title: Optional[str] = None
    description: Optional[str] = None
    tags_json: Optional[str] = None
    metadata_generated: bool = False
    approved_at: Optional[datetime] = None
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
    impressions: Optional[int] = Field(default=0, sa_column_kwargs=_NULLABLE_METRIC)
    ctr: Optional[float] = Field(default=0.0, sa_column_kwargs=_NULLABLE_METRIC)  # (0..1)
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


class ReachReport(SQLModel, table=True):
    """One YouTube Reporting API report (``channel_reach_basic_a1``) that the reach
    loop downloaded and applied — the processed-report ledger that makes the loop
    safe to re-run. A report id is applied at most once; a *backfill* (Google
    re-issuing the same day under a NEW report id, newer create_time) replaces
    that day's VideoReachDaily rows wholesale (reach_loop._apply_report)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    report_id: str = Field(index=True, unique=True)   # Reporting API report id
    job_id: str
    report_date: str = Field(index=True)              # YYYY-MM-DD (Pacific day covered)
    create_time: str                                  # RFC 3339, as returned by the API
    rows: int = 0                                     # CSV data rows read
    videos: int = 0                                   # distinct video ids aggregated
    ctr_unit: Optional[str] = None                    # "fraction" | "percent" (as detected)
    processed_at: datetime = Field(default_factory=utcnow)


class VideoReachDaily(SQLModel, table=True):
    """Thumbnail impressions + clicks for one YouTube video on one (Pacific) day,
    from the Reporting API. The Analytics API v2 targeted queries cannot return
    these (400 "Unknown identifier"), so this is their only source. Overwritten
    per (channel, day) by the newest report for that day — never accumulated —
    so re-runs and backfills cannot double count. ``clicks`` = impressions × CTR
    is kept so multi-day CTR is the impression-weighted ratio, not a mean of
    daily ratios. ``video_id`` is null for YouTube videos the manager does not
    know (uploaded elsewhere)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    video_id: Optional[int] = Field(default=None, foreign_key="video.id", index=True)
    yt_video_id: str = Field(index=True)
    day: str = Field(index=True)                      # YYYY-MM-DD
    impressions: int = 0
    clicks: float = 0.0
    ctr: float = 0.0                                  # (0..1) clicks / impressions
    report_id: str
    report_create_time: str
    updated_at: datetime = Field(default_factory=utcnow)


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
