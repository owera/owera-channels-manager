"""Per-actor credentials + workspace scope (V3, CTO/CoS 2026-11-13). docs/ACCESS_AUDIT.md.

Replaces the single shared Basic password with one token per actor. Each token
is scoped to the workspaces (and optional role suffixes / read-only) that actor
needs. Clients keep the same Basic form, only the password changes:

    curl -u "owera+channels:<channels token>" …

Registry: a JSON file (``MANAGER_ACTOR_TOKENS_FILE``, default
``credentials/actor-tokens.json``, which is gitignored and chmod 600) that holds
**only SHA-256 hashes** of the tokens, never a token:

    {"actors": [
      {"actor": "channels", "sha256": "<64 hex>", "workspaces": ["owera"],
       "roles": ["channels"], "read_only": false},
      {"actor": "cto", "sha256": "<64 hex>", "workspaces": ["*"]}
    ]}

``workspaces: ["*"]`` = every workspace AND the global/admin routes (settings, music,
trends, youtube admin, playlists, profiles, thumbnails, media). Any other scope
covers ONLY workspace-scoped routes for the listed slugs (fail closed).

Migration: while ``MANAGER_LEGACY_SHARED_PASSWORD=1`` (default) the old shared
``MANAGER_APP_PASSWORD`` keeps working as actor ``legacy-shared`` with scope "*".
The CTO cuts it by setting ``MANAGER_LEGACY_SHARED_PASSWORD=0``.
Token generation and the .env / registry file stay with the CTO
(``python -m app.actor_auth hash`` prints the SHA-256 of a token read from stdin).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.config import MANAGER_DIR, settings

logger = logging.getLogger("manager.actor_auth")

LEGACY_ACTOR = "legacy-shared"
ROLE_ACTORS = frozenset({"channels", "vm", "admin"})


@dataclass(frozen=True)
class Actor:
    name: str
    method: str                       # token | legacy-shared | spa-google | open
    workspaces: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    read_only: bool = False

    @property
    def is_admin(self) -> bool:
        return "*" in self.workspaces


def tokens_file() -> Path:
    p = (getattr(settings, "actor_tokens_file", "") or "").strip()
    return Path(p) if p else MANAGER_DIR / "credentials" / "actor-tokens.json"


_cache: dict = {"mtime": None, "path": None, "records": []}


def _load_records() -> list[dict]:
    """Registry records (cached by mtime). A malformed file → NO token actors
    (fail closed; legacy still works if enabled) + one ERROR log."""
    path = tokens_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, path=str(path), records=[])
        return []
    if _cache["mtime"] == mtime and _cache["path"] == str(path):
        return _cache["records"]
    recs: list[dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for r in data.get("actors") or []:
            name = str(r.get("actor") or "").strip()
            digest = str(r.get("sha256") or "").strip().lower()
            ws = [str(w).strip() for w in (r.get("workspaces") or []) if str(w).strip()]
            if not name or len(digest) != 64 or not ws or name == LEGACY_ACTOR:
                logger.error("actor_tokens: skipping invalid record for actor=%r", name)
                continue
            int(digest, 16)
            recs.append({"actor": name, "sha256": digest, "workspaces": ws,
                         "roles": [str(x).strip().lower() for x in (r.get("roles") or [])],
                         "read_only": bool(r.get("read_only", False))})
    except Exception as e:  # noqa: BLE001
        logger.error("actor_tokens: %s unreadable/invalid (%s) — no token actors",
                     path, type(e).__name__)
        recs = []
    _cache.update(mtime=mtime, path=str(path), records=recs)
    return recs


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def actor_for_token(password: str) -> Actor | None:
    if not password:
        return None
    digest = sha256_hex(password)
    hit = None
    for r in _load_records():
        # compare every record (constant work per request, no early exit on match)
        if secrets.compare_digest(digest, r["sha256"]):
            hit = r
    if hit is None:
        return None
    return Actor(name=hit["actor"], method="token", workspaces=frozenset(hit["workspaces"]),
                 roles=frozenset(hit["roles"]), read_only=hit["read_only"])


def legacy_enabled() -> bool:
    return bool(getattr(settings, "legacy_shared_password", True))


def legacy_actor() -> Actor:
    return Actor(name=LEGACY_ACTOR, method="legacy-shared", workspaces=frozenset({"*"}))


def authenticate_basic(password: str) -> Actor | None:
    """Token actor first; then the legacy shared password (if still enabled)."""
    a = actor_for_token(password)
    if a is not None:
        return a
    if (legacy_enabled() and settings.app_password
            and secrets.compare_digest(password, settings.app_password)):
        return legacy_actor()
    return None


def scope_denial(actor: Actor, *, workspace_slug: str | None, role: str | None,
                 workspace_scoped: bool, method: str) -> str | None:
    """None when allowed, else the 403 reason. Fail closed."""
    if actor.method != "token":
        return None   # legacy-shared / SPA / open dev keep today's behavior
    if actor.read_only and method.upper() not in ("GET", "HEAD", "OPTIONS"):
        return f"actor '{actor.name}' is read-only"
    if role in ROLE_ACTORS and role not in actor.roles and not actor.is_admin:
        return f"actor '{actor.name}' may not act as role '{role}'"
    if actor.is_admin:
        return None
    if not workspace_scoped:
        return f"actor '{actor.name}' is not scoped for global/admin routes"
    if not workspace_slug or workspace_slug not in actor.workspaces:
        return f"actor '{actor.name}' is not scoped for workspace '{workspace_slug}'"
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["hash"]:
        print("usage: printf %s \"$TOKEN\" | python -m app.actor_auth hash", file=sys.stderr)
        return 2
    tok = sys.stdin.read().strip()
    if len(tok) < 32:
        print("token too short (use >= 32 random chars)", file=sys.stderr)
        return 2
    print(sha256_hex(tok))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
