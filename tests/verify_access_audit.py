"""V3: per-actor tokens + workspace scope + append-only access audit log.

Run: PYTHONPATH=. .venv/bin/python tests/verify_access_audit.py
No network, in-memory DB, temp registry + temp audit file. Test tokens only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MANAGER_SPA_GOOGLE_LOGIN", "0")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

import app.main as main_mod  # noqa: E402
from app import actor_auth, audit_log  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import Channel, Workspace  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


tmp = Path(tempfile.mkdtemp())
AUDIT = tmp / "audit" / "access.jsonl"
REG = tmp / "actor-tokens.json"
TOK = {"channels": "tok-channels-" + "a" * 32, "po": "tok-po-" + "b" * 32,
       "cto": "tok-cto-" + "c" * 32, "tb": "tok-tb-" + "d" * 32}
LEGACY = "legacy-shared-test-password"
REG.write_text(json.dumps({"actors": [
    {"actor": "channels", "sha256": actor_auth.sha256_hex(TOK["channels"]),
     "workspaces": ["owera"], "roles": ["channels"]},
    {"actor": "po-cm", "sha256": actor_auth.sha256_hex(TOK["po"]),
     "workspaces": ["owera"], "read_only": True},
    {"actor": "cto", "sha256": actor_auth.sha256_hex(TOK["cto"]), "workspaces": ["*"]},
    {"actor": "tenant-b-tester", "sha256": actor_auth.sha256_hex(TOK["tb"]),
     "workspaces": ["tenant-b"]},
]}))
saved = {k: getattr(settings, k) for k in ("app_password", "multi_workspace", "actor_tokens_file",
                                           "audit_log_path", "legacy_shared_password",
                                           "spa_google_login", "allow_workspace_header")}
settings.app_password = LEGACY
settings.multi_workspace = True
settings.allow_workspace_header = False
settings.actor_tokens_file = str(REG)
settings.audit_log_path = str(AUDIT)
settings.legacy_shared_password = True
settings.spa_google_login = False

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="tenant-b", name="Tenant B"))
    s.add(Channel(id=1, workspace_id=1, slug="os", name="OS"))
    s.add(Channel(id=4, workspace_id=2, slug="b", name="B"))
    s.commit()


def _sess():
    with Session(engine) as session:
        yield session


app = main_mod.app
app.dependency_overrides[get_session] = _sess
client = TestClient(app)


def lines():
    if not AUDIT.exists():
        return []
    return [json.loads(l) for l in AUDIT.read_text().splitlines() if l.strip()]


try:
    print("token actors: scope")
    r = client.get("/api/channels", auth=("owera+channels", TOK["channels"]))
    ok(r.status_code == 200 and [c["slug"] for c in r.json()] == ["os"],
       "channels token on owera → 200, only owera's channels")
    L = lines()[-1]
    ok(L["actor"] == "channels" and L["auth"] == "token" and L["workspace_id"] == 1
       and L["workspace_slug"] == "owera" and L["route"] == "/api/channels"
       and L["method"] == "GET" and L["status"] == 200 and L["outcome"] == "allowed"
       and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", L["ts"]),
       "audit line: actor, auth, workspace id+slug, route template, method, UTC ts, outcome")
    r = client.get("/api/channels", auth=("tenant-b", TOK["channels"]))
    ok(r.status_code == 403, "channels token claiming workspace tenant-b → 403")
    L = lines()[-1]
    ok(L["outcome"] == "denied" and L["actor"] == "channels" and "tenant-b" in L.get("reason", ""),
       "denied access is audited with actor + reason")
    r = client.get("/api/channels", auth=("owera+vm", TOK["channels"]))
    ok(r.status_code == 403, "channels token acting as role vm → 403")
    r = client.get("/api/channels", auth=("tenant-b", TOK["tb"]))
    ok(r.status_code == 200 and [c["slug"] for c in r.json()] == ["b"],
       "tenant-b token sees only tenant-b")
    r = client.get("/api/channels", auth=("owera", TOK["tb"]))
    ok(r.status_code == 403, "tenant-b token on owera → 403")
    r = client.get("/api/channels", auth=("owera", TOK["po"]))
    ok(r.status_code == 200, "read-only PO token can GET its workspace")
    r = client.post("/api/channels", auth=("owera", TOK["po"]), json={"slug": "x", "name": "x"})
    ok(r.status_code == 403 and "read-only" in r.text, "read-only PO token POST → 403")
    r = client.get("/api/settings", auth=("owera+channels", TOK["channels"]))
    ok(r.status_code == 403 and "global/admin" in r.text,
       "workspace-scoped token on a global/admin route → 403 (fail closed)")
    r = client.get("/api/settings", auth=("admin", TOK["cto"]))
    ok(r.status_code == 200, "cto (*) token on a global route → 200")
    ok(lines()[-1]["actor"] == "cto" and lines()[-1]["route"] == "/api/settings", "cto access audited")
    r = client.get("/api/channels", auth=("tenant-b", TOK["cto"]))
    ok(r.status_code == 200, "cto (*) token reaches any workspace")

    print("legacy shared password (migration)")
    r = client.get("/api/channels", auth=("owera+channels", LEGACY))
    ok(r.status_code == 200 and lines()[-1]["actor"] == "legacy-shared"
       and lines()[-1]["auth"] == "legacy-shared", "legacy password still works as actor legacy-shared")
    settings.legacy_shared_password = False
    r = client.get("/api/channels", auth=("owera+channels", LEGACY))
    ok(r.status_code == 401, "MANAGER_LEGACY_SHARED_PASSWORD=0 cuts the shared password")
    settings.legacy_shared_password = True

    print("unauthenticated / no secrets in the log")
    r = client.get("/api/channels?state=SECRET-QS", auth=("owera", "wrong-" + "z" * 20))
    ok(r.status_code == 401 and lines()[-1]["actor"] == "unauthenticated"
       and lines()[-1]["outcome"] == "denied", "bad password → 401, audited as unauthenticated")
    raw = AUDIT.read_text()
    ok(all(t not in raw for t in TOK.values()) and LEGACY not in raw and "SECRET-QS" not in raw
       and "wrong-" not in raw, "audit log has no token, password or query string")
    r = client.get(f"/api/channels/{4}", auth=("owera+channels", TOK["channels"]))
    ok(lines()[-1]["route"] == "/api/channels/{channel_id}", "route TEMPLATE logged, not the raw path")
    n = len(lines())
    client.get("/health")
    ok(len(lines()) == n, "/health (unauthenticated liveness) is not audited")

    print("append-only")
    before = AUDIT.read_bytes()
    client.get("/api/channels", auth=("owera", TOK["cto"]))
    ok(AUDIT.read_bytes().startswith(before), "the file only grows (append)")
    r = client.get("/audit/access.jsonl", auth=("admin", TOK["cto"]))
    ok(b'"actor"' not in r.content, "the audit file is not served by any route")
    users = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
                   if re.search(r"audit_path\(|access\.jsonl|audit_log_path", p.read_text())
                   and p.name not in ("audit_log.py", "config.py"))
    ok(users == [], f"no other module touches the audit file path: {users}")
    src = (ROOT / "app" / "audit_log.py").read_text()
    ok("O_APPEND" in src and not re.search(r"O_TRUNC|\.unlink\(|write_text\(|open\([^)]*['\"]w['\"]", src),
       "audit writer is O_APPEND only (no truncate/unlink/rewrite)")

    print("registry hygiene")
    bad = tmp / "bad.json"
    bad.write_text("{not json")
    settings.actor_tokens_file = str(bad)
    ok(actor_auth.actor_for_token(TOK["cto"]) is None, "malformed registry → no token actors (fail closed)")
    r = client.get("/api/channels", auth=("owera", LEGACY))
    ok(r.status_code == 200, "…legacy still works while enabled")
    settings.actor_tokens_file = str(REG)
    bad.write_text(json.dumps({"actors": [{"actor": "x", "sha256": "abc", "workspaces": ["owera"]},
                                          {"actor": "legacy-shared", "sha256": "0" * 64,
                                           "workspaces": ["*"]}]}))
    settings.actor_tokens_file = str(bad)
    ok(actor_auth._load_records() == [], "short hash / reserved actor name records are skipped")
    settings.actor_tokens_file = str(REG)
    import io
    import contextlib
    sys_stdin = sys.stdin
    sys.stdin = io.StringIO(TOK["cto"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = actor_auth.main(["hash"])
    sys.stdin = sys_stdin
    ok(rc == 0 and out.getvalue().strip() == actor_auth.sha256_hex(TOK["cto"]),
       "python -m app.actor_auth hash prints the SHA-256 (registry stores hashes only)")
finally:
    for k, v in saved.items():
        setattr(settings, k, v)
    app.dependency_overrides.pop(get_session, None)

print(f"\nALL {_checks} CHECKS PASSED")
