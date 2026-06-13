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
    playlist_id: Optional[int] = Field(default=None, foreign_key="playlist.id")
    render_profile_id: Optional[int] = Field(default=None, foreign_key="renderprofile.id")
    active: bool = True
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


class Settings(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    render_concurrency: int = 1
    publish_drip_minutes: int = 30
    scheduler_paused: bool = False
