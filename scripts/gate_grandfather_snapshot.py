#!/usr/bin/env python3
"""Generate the closed OS publish-grandfather list (CTO runs once at deploy).

Selects OS videos with status approved (queued-for-publish / scheduled
drip; there is no separate scheduled status) or publishing, whose
``vm_pass`` matches the **current render artifact** (path + size:mtime
fingerprint — not a git SHA). #1328 is always excluded.
Always excludes #1328. Prints ids + count. Writes a versioned JSON file
(default: app/data/gate_grandfather.json). The CTO pastes the printed
list into the PR after deploy.

  PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py
  PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py --exclude 1328
  PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default=None, help="SQLite path (default: settings.db_path)")
    p.add_argument("--out", default=None,
                   help="JSON output (default: app/data/gate_grandfather.json)")
    p.add_argument("--exclude", default="1328",
                   help="comma-separated video ids to drop (default: 1328)")
    p.add_argument("--dry-run", action="store_true", help="print only, do not write")
    return p.parse_args(argv)


def _exclude_set(raw: str) -> set[int]:
    from app.services.gate_grandfather import ALWAYS_EXCLUDE
    out = set(ALWAYS_EXCLUDE)
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.db:
        os.environ["MANAGER_DB_PATH"] = str(Path(args.db).resolve())
    from sqlmodel import Session, select

    from app.config import settings
    from app.models import Channel, Video, VideoStatus
    from app.services.craft import brand_of
    from app.services.gate_grandfather import DEFAULT_PATH, vm_pass_matches_master

    db = Path(args.db).resolve() if args.db else Path(settings.db_path)
    out = Path(args.out).resolve() if args.out else DEFAULT_PATH
    exclude = _exclude_set(args.exclude)

    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{db}")
    kept, skipped = [], []
    with Session(engine) as session:
        channels = {c.id: c for c in session.exec(select(Channel)).all()}
        rows = session.exec(select(Video).where(
            Video.status.in_([VideoStatus.APPROVED, VideoStatus.PUBLISHING]))).all()
        for v in rows:
            ch = channels.get(v.channel_id)
            brand = brand_of(ch.slug, ch.name, channel_id=ch.id) if ch else None
            if brand != "os":
                continue
            why = None
            if v.id in exclude:
                why = "excluded"
            elif not vm_pass_matches_master(v):
                why = "vm_pass mismatch / missing for current master"
            if why:
                skipped.append({"id": v.id, "reason": why, "status": v.status})
            else:
                kept.append(int(v.id))
    kept = sorted(set(kept))
    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "ids": kept,
        "excluded": sorted(exclude),
        "skipped": skipped,
        "count": len(kept),
        "note": "Closed list. Only these OS ids use the old gate at publish, and only with a valid vm_pass.",
    }
    print(f"grandfather ids ({len(kept)}): {kept}")
    print(f"excluded/skipped: {skipped}")
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
