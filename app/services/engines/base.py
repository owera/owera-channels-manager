"""Engine contract shared by every video-generation backend.

An *engine* turns a queued Video into a rendered MP4 on disk. The manager owns the
lifecycle (queue/quota/review/publish); an engine only has to expose three things,
modelled on MoneyPrinterTurbo's async task queue so the blocking scheduler tick can
``submit`` once and ``poll`` on later ticks without ever blocking:

  submit(video, params) -> handle      # opaque str, stored on Video.mpt_task_id
  poll(handle)          -> dict         # {state, progress, script}
  final_path(handle)    -> Path         # the finished MP4 (read directly)

States are normalised across engines; the values intentionally match MPT's own task
states (app/models/const.py) so the MPT adapter is a pure pass-through.
"""

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

# Normalised task states (identical to MPT's so its adapter needs no remapping).
STATE_FAILED = -1
STATE_COMPLETE = 1
STATE_PROCESSING = 4


@runtime_checkable
class Engine(Protocol):
    name: str

    def submit(self, video, params: dict) -> str:
        """Kick off a render and return an opaque handle to track it."""
        ...

    def poll(self, handle: str) -> dict:
        """Return ``{state, progress, script}`` for an in-flight render.

        ``state`` is one of the STATE_* constants, ``progress`` an int 0-100, and
        ``script`` the narration text if the engine produced one (else None).
        """
        ...

    def final_path(self, handle: str) -> Path:
        """Absolute path to the finished MP4 for ``handle`` (may not exist yet)."""
        ...
