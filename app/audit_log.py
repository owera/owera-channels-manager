"""Append-only access audit log (V3, CTO/CoS 2026-11-13). docs/ACCESS_AUDIT.md.

One JSON line for every authenticated (or denied) /api request:

    {"ts":"2026-11-16T12:00:00.123Z","actor":"channels","auth":"token",
     "workspace_id":1,"workspace_slug":"owera","route":"/api/videos/{video_id}",
     "method":"GET","status":200,"outcome":"allowed"}

* File: ``MANAGER_AUDIT_LOG_PATH`` (default ``<repo>/audit/access.jsonl``,
  outside ``storage/`` and outside every served directory). Opened per write with
  ``O_WRONLY|O_APPEND|O_CREAT`` (mode 0640). The app has no route that reads,
  rewrites, truncates or deletes it, and ``tests/verify_access_audit.py`` greps
  for one. OS-level immutability (``chflags uappnd``) is an ops step.
* No query string, no body, no header, no password/token: only the route TEMPLATE.
* A write failure is logged at ERROR (``audit_write_failed``) and the request is
  served anyway (fail open). See the risks section in the docs.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import MANAGER_DIR, settings

logger = logging.getLogger("manager.audit")


def audit_path() -> Path:
    p = (getattr(settings, "audit_log_path", "") or "").strip()
    return Path(p) if p else MANAGER_DIR / "audit" / "access.jsonl"


def outcome_for(status: int) -> str:
    if status in (401, 403):
        return "denied"
    if status >= 500:
        return "error"
    if status >= 400:
        return "client_error"
    return "allowed"


def write(*, actor: str, auth: str, workspace_id, workspace_slug, route: str,
          method: str, status: int, reason: str | None = None) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "actor": actor, "auth": auth,
        "workspace_id": workspace_id, "workspace_slug": workspace_slug,
        "route": route, "method": method, "status": int(status),
        "outcome": outcome_for(int(status)),
    }
    if reason:
        row["reason"] = reason[:200]
    line = (json.dumps(row, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as e:
        logger.error("audit_write_failed path=%s err=%s", path, type(e).__name__)
