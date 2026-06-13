"""Engine registry + per-video engine resolution.

Engines are selected on a RenderProfile. A video's effective engine is taken from the
most specific profile in the same order params are resolved in render_loop:
    video.render_profile_id -> topic.render_profile_id -> channel.default_render_profile_id
falling back to the default ("mpt") when no profile is set.
"""

from app.models import Channel, RenderProfile, Topic, Video
from app.services.engines.base import (  # re-exported for render_loop
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_PROCESSING,
    Engine,
)
from app.services.engines.hyperframes import HyperFramesEngine
from app.services.engines.mpt import MPTEngine

DEFAULT_ENGINE = "mpt"

_ENGINES: dict[str, Engine] = {
    MPTEngine.name: MPTEngine(),
    HyperFramesEngine.name: HyperFramesEngine(),
}


def get_engine(name: str | None) -> Engine:
    return _ENGINES.get(name or DEFAULT_ENGINE, _ENGINES[DEFAULT_ENGINE])


def engine_names() -> list[str]:
    return list(_ENGINES.keys())


def resolve_engine(session, video: Video, topic: Topic | None, channel: Channel | None) -> str:
    """First profile (video -> topic -> channel) that names an engine wins."""
    profile_ids = [
        video.render_profile_id,
        topic.render_profile_id if topic else None,
        channel.default_render_profile_id if channel else None,
    ]
    for pid in profile_ids:
        if not pid:
            continue
        profile = session.get(RenderProfile, pid)
        if profile and profile.engine:
            return profile.engine
    return DEFAULT_ENGINE


__all__ = [
    "STATE_COMPLETE",
    "STATE_FAILED",
    "STATE_PROCESSING",
    "Engine",
    "get_engine",
    "engine_names",
    "resolve_engine",
    "DEFAULT_ENGINE",
]
