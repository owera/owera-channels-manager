"""Basic Auth username admin → workspace owera when multi_workspace is on."""
from __future__ import annotations

import base64
import sys

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.requests import Request

from app.config import settings
from app.models import Workspace
from app.workspace import get_workspace, resolve_workspace_slug

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def _request_basic(username: str, password: str = "secret") -> Request:
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Basic {cred}".encode())],
    }
    return Request(scope)


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(slug="tenant-b", name="Tenant B"))
    s.commit()

_saved_multi = settings.multi_workspace
_saved_header = settings.allow_workspace_header

try:
    settings.multi_workspace = True
    settings.allow_workspace_header = False

    slug = resolve_workspace_slug(_request_basic("admin"))
    ok(slug == "owera", "multi_workspace: Basic Auth admin resolves slug to owera")
    with Session(engine) as s:
        ws = get_workspace(s, slug)
        ok(ws.slug == "owera", "multi_workspace: admin loads owera workspace row")

    slug_b = resolve_workspace_slug(_request_basic("tenant-b"))
    ok(slug_b == "tenant-b", "multi_workspace: other usernames unchanged")

    with Session(engine) as s:
        admin_rows = s.exec(select(Workspace).where(Workspace.slug == "admin")).all()
        ok(len(admin_rows) == 0, "no admin workspace row exists")

    settings.multi_workspace = False
    slug_off = resolve_workspace_slug(_request_basic("admin"))
    ok(slug_off == "owera", "multi_workspace off: always owera regardless of Basic Auth")
finally:
    settings.multi_workspace = _saved_multi
    settings.allow_workspace_header = _saved_header

print(f"\nALL {_checks} CHECKS PASSED")
