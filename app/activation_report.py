"""S1 — ops-only activation report: A0–A6 done/at per workspace, as JSON.

    PYTHONPATH=. uv run python -m app.activation_report            # buyer + test workspaces
    PYTHONPATH=. uv run python -m app.activation_report --workspace <slug|id>
    PYTHONPATH=. uv run python -m app.activation_report --exclude-test --include-owera

Read-only: never writes, never migrates. If the S1 columns are missing (code
not deployed/restarted yet) it exits 2 instead of altering the DB.
Definitions: ``app/services/activation.py``.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text
from sqlmodel import Session, select

_REQUIRED = {"workspace": ("is_test", "paid_at", "first_draft_at"),
             "video": ("approved_by",)}


def _missing_columns(engine) -> list[str]:
    missing = []
    with engine.connect() as conn:
        for table, cols in _REQUIRED.items():
            have = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            missing += [f"{table}.{c}" for c in cols if c not in have]
    return missing


def build(session: Session, *, workspace: str | None = None, include_test: bool = True,
          include_owera: bool = False) -> dict:
    from app.models import Workspace
    from app.services import activation

    if workspace:
        q = select(Workspace).where(Workspace.slug == workspace)
        if workspace.isdigit():
            q = select(Workspace).where(Workspace.id == int(workspace))
        ws = session.exec(q).first()
        if ws is None:
            return {"ok": False, "error": f"workspace {workspace!r} not found"}
        rows = [activation.report(session, ws)]
    else:
        rows = activation.report_all(session, include_test=include_test,
                                     include_owera=include_owera)
    return {"ok": True, "workspaces": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.activation_report",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", help="slug or id (default: all except owera)")
    ap.add_argument("--exclude-test", action="store_true", help="skip is_test workspaces")
    ap.add_argument("--include-owera", action="store_true", help="include workspace 1")
    args = ap.parse_args(argv)

    from app.db import engine
    missing = _missing_columns(engine)
    if missing:
        print(json.dumps({"ok": False, "error": "schema missing S1 columns; deploy/restart first",
                          "missing": missing}, indent=2))
        return 2
    with Session(engine) as session:
        out = build(session, workspace=args.workspace, include_test=not args.exclude_test,
                    include_owera=args.include_owera)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
