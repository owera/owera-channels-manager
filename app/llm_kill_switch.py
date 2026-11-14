"""Ops CLI: per-workspace LLM kill switch (S8) and release-check readout.

    PYTHONPATH=. uv run python -m app.llm_kill_switch status  [--workspace ID]
    PYTHONPATH=. uv run python -m app.llm_kill_switch disable --workspace ID
    PYTHONPATH=. uv run python -m app.llm_kill_switch enable  --workspace ID

``disable`` sets ``Workspace.llm_disabled`` = true: every LLM call for that
workspace is refused by ``llm_guard`` (internal workspaces too), effective on the
next call, no restart. ``status`` prints one JSON object per workspace with ids,
flags and the guard's verdict for a ``render`` call — never secrets or content.
An admin HTTP toggle is a follow-up after S4 (admin-only settings helper).
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import Workspace, utcnow
from app.services import llm_guard


def _row(w: Workspace) -> dict:
    verdict = llm_guard.block_reason(int(w.id), "render", ("video_title", "script"))
    return {
        "workspace_id": w.id, "slug": w.slug,
        "internal": llm_guard.is_allowed(w.id),
        "llm_disabled": bool(getattr(w, "llm_disabled", False)),
        "is_test": bool(getattr(w, "is_test", False)),
        "paid": llm_guard._is_paid(w),
        "terms_accepted": getattr(w, "terms_accepted_at", None) is not None,
        "privacy_accepted": getattr(w, "privacy_accepted_at", None) is not None,
        "external_release_flag": llm_guard.external_release_enabled(),
        "render_llm": "allowed" if verdict is False else (verdict or "not_allowlisted"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.llm_kill_switch")
    ap.add_argument("action", choices=["status", "disable", "enable"])
    ap.add_argument("--workspace", type=int)
    a = ap.parse_args(argv)
    if a.action != "status" and a.workspace is None:
        print("--workspace is required for disable/enable", file=sys.stderr)
        return 2
    init_db()
    with Session(engine) as s:
        if a.workspace is not None:
            w = s.get(Workspace, a.workspace)
            if w is None:
                print(f"workspace {a.workspace} not found", file=sys.stderr)
                return 1
            if a.action != "status":
                w.llm_disabled = a.action == "disable"
                w.updated_at = utcnow()
                s.add(w)
                s.commit()
                s.refresh(w)
            rows = [w]
        else:
            rows = list(s.exec(select(Workspace).order_by(Workspace.id)).all())
        for w in rows:
            print(json.dumps(_row(w), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
