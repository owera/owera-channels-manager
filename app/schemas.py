"""Request DTOs. Responses return SQLModel rows directly."""

from typing import Any, Optional

from pydantic import BaseModel


class ChannelCreate(BaseModel):
    name: str
    slug: str
    default_privacy: str = "public"
    default_skip_gate: bool = False
    daily_render_budget: int = 6
    daily_publish_budget: int = 6


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    default_privacy: Optional[str] = None
    default_skip_gate: Optional[bool] = None
    daily_render_budget: Optional[int] = None
    daily_publish_budget: Optional[int] = None
    default_render_profile_id: Optional[int] = None
    paused: Optional[bool] = None


class PlaylistCreate(BaseModel):
    title: str
    description: str = ""
    privacy: str = "public"


class ProfileCreate(BaseModel):
    name: str
    channel_id: Optional[int] = None
    engine: str = "mpt"
    params: dict[str, Any] = {}


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    engine: Optional[str] = None
    params: Optional[dict[str, Any]] = None


# ---- Topics (content themes) ----
class TopicCreate(BaseModel):
    channel_id: int
    name: str
    theme_prompt: Optional[str] = None
    content_format: str = "short"           # "short" (vertical Shorts) | "long" (16:9 long-form)
    render_profile_id: Optional[int] = None
    create_playlist: bool = True            # auto-create a YouTube playlist named after the topic
    playlist_id: Optional[int] = None       # or link an existing one instead


class TopicUpdate(BaseModel):
    name: Optional[str] = None
    theme_prompt: Optional[str] = None
    content_format: Optional[str] = None
    render_profile_id: Optional[int] = None
    playlist_id: Optional[int] = None
    active: Optional[bool] = None
    weight: Optional[int] = None             # growth-agent steering (see Topic.weight)


class GenerateBody(BaseModel):
    count: int = 8


# ---- Videos (produced units) ----
class VideoCreate(BaseModel):
    topic_id: int
    subject: str
    queue: bool = False                     # True -> go straight to queued, else draft


class VideoUpdate(BaseModel):
    subject: Optional[str] = None
    render_profile_id: Optional[int] = None
    skip_gate: Optional[bool] = None
    privacy: Optional[str] = None
    overrides: Optional[dict[str, Any]] = None
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None


class RejectBody(BaseModel):
    reason: str = ""


class ReorderBody(BaseModel):
    channel_id: int
    ordered_ids: list[int]


# ---- Trend signals (research → smart adoption) ----
class TrendCreate(BaseModel):
    term: str
    description: Optional[str] = None
    source: Optional[str] = None
    channel_id: Optional[int] = None
    language: Optional[str] = None
    content_format: str = "short"
    momentum: Optional[str] = None            # rising | hot | fading | evergreen
    score: float = 0.0                        # 0..100
    status: Optional[str] = None              # researched | watching | adopted | rejected
    decision_reason: Optional[str] = None


class TrendUpdate(BaseModel):
    description: Optional[str] = None
    source: Optional[str] = None
    channel_id: Optional[int] = None
    language: Optional[str] = None
    content_format: Optional[str] = None
    momentum: Optional[str] = None
    score: Optional[float] = None
    status: Optional[str] = None
    decision_reason: Optional[str] = None


class TrendAdoptBody(BaseModel):
    channel_id: Optional[int] = None          # target channel (falls back to the trend's)
    content_format: Optional[str] = None      # override the trend's suggested format
    idea_count: int = 8                       # ideas to seed on the new topic
    produce_count: int = 3                    # how many to auto-produce (queue to render)
    theme_prompt: Optional[str] = None        # override the auto-derived theme prompt


class SettingsUpdate(BaseModel):
    render_concurrency: Optional[int] = None
    publish_drip_minutes: Optional[int] = None
    scheduler_paused: Optional[bool] = None
    topic_autogen_enabled: Optional[bool] = None
    topic_autogen_min_pending: Optional[int] = None
    topic_autogen_target: Optional[int] = None


# ---- YouTube channel administration ----
class BrandingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    keywords: Optional[str] = None            # space-separated; multiword keywords quoted
    country: Optional[str] = None             # ISO 3166-1 alpha-2, e.g. "US"
    default_language: Optional[str] = None    # BCP-47, e.g. "en"


class SubscribeBody(BaseModel):
    channel: str                              # channel id, @handle, or channel URL
