"""MoneyPrinterTurbo engine adapter — pure delegation to the existing MPTClient.

Behaviour is byte-for-byte what render_loop did before the engine abstraction; this
just normalises the poll() shape to the common {state, progress, script} contract.
"""

from pathlib import Path

from app.services.mpt_client import mpt


class MPTEngine:
    name = "mpt"

    def submit(self, video, params: dict) -> str:
        return mpt.submit(params)

    def poll(self, handle: str) -> dict:
        t = mpt.poll(handle)
        return {
            "state": t.get("state"),
            "progress": int(t.get("progress") or 0),
            "script": t.get("script"),
        }

    def final_path(self, handle: str) -> Path:
        return mpt.local_final_path(handle)
