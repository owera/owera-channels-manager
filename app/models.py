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
    kind: str                                     # render|publish|metadata|playlist_add|generate|oauth
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


class VideoMetric(SQLModel, table=True):
    """A point-in-time per-video YouTube Analytics snapshot, recorded ~daily by the
    analytics loop. The time series powers the leaderboard the growth agent learns from."""
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    views: int = 0
    impressions: int = 0
    ctr: float = 0.0                      # impressionClickThroughRate (0..1)
    avg_view_pct: float = 0.0             # averageViewPercentage (0..100)
    watch_time_minutes: int = 0          # estimatedMinutesWatched
    likes: int = 0
    comments: int = 0
    subscribers_gained: int = 0
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
