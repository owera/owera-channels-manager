# Access audit log + per-actor credentials (V3)

Status: CTO/CoS 2026-11-13. Draft PR, target Mon 16/11. The CTO generates the
tokens and edits `.env` and the registry. This PR contains no real secret.

## 1. Per-actor tokens (replace the shared Basic password)

Clients keep the Basic form. Only the password changes, so the username still
selects workspace and role:

```bash
curl -u "owera+channels:$CHANNELS_TOKEN" https://channels.owera.com/api/videos/1477
```

Registry file: `MANAGER_ACTOR_TOKENS_FILE`, default
`~/src/owera-channels-manager/credentials/actor-tokens.json`. `credentials/` is
already gitignored; chmod 600. It holds **SHA-256 hashes only**:

```json
{"actors": [
  {"actor": "cto",          "sha256": "<64 hex>", "workspaces": ["*"]},
  {"actor": "channels",     "sha256": "<64 hex>", "workspaces": ["owera"], "roles": ["channels"]},
  {"actor": "vm",           "sha256": "<64 hex>", "workspaces": ["owera"], "roles": ["vm"]},
  {"actor": "channels-dev", "sha256": "<64 hex>", "workspaces": ["owera", "tenant-b"]},
  {"actor": "cos",          "sha256": "<64 hex>", "workspaces": ["owera"], "read_only": true},
  {"actor": "po-cm",        "sha256": "<64 hex>", "workspaces": ["owera"], "read_only": true},
  {"actor": "growth-agent", "sha256": "<64 hex>", "workspaces": ["owera"]},
  {"actor": "run-check",    "sha256": "<64 hex>", "workspaces": ["owera"], "read_only": true},
  {"actor": "code-agent",   "sha256": "<64 hex>", "workspaces": ["owera"]}
]}
```

How the CTO makes one entry: `TOKEN=$(openssl rand -hex 32)`, hand `$TOKEN` to the
actor through its secret store, and put `printf %s "$TOKEN" | .venv/bin/python -m app.actor_auth hash` in `sha256`.
The registry is re-read when its mtime changes, so no restart is needed. A malformed
file means no token actors (fail closed), and legacy keeps working while it's enabled.

| Env (`~/src/owera-channels-manager/.env`) | Format | Default |
|---|---|---|
| `MANAGER_ACTOR_TOKENS_FILE` | absolute path to the JSON above | `<repo>/credentials/actor-tokens.json` |
| `MANAGER_LEGACY_SHARED_PASSWORD` | `1` / `0` | `1`: shared `MANAGER_APP_PASSWORD` = actor `legacy-shared`, scope `*` |
| `MANAGER_AUDIT_LOG_PATH` | absolute path | `<repo>/audit/access.jsonl` (gitignored) |

Cutover: (1) deploy with no env change, so every caller is `legacy-shared`;
(2) issue the tokens and move each actor over, watching the audit log until
`legacy-shared` disappears; (3) set `MANAGER_LEGACY_SHARED_PASSWORD=0` and restart.

### Scope rules (`app/access_scope.py`, enforced on every included router)

| Rule | Effect |
|---|---|
| `workspaces: ["*"]` | every workspace + global/admin routes |
| other scope, workspace-scoped route (uses `current_workspace`) | slug from the username must be in the list, else 403 |
| other scope, **global/admin route** | 403 (fail closed) |
| `read_only: true` | only GET/HEAD/OPTIONS |
| username role suffix `+channels` / `+vm` / `admin` | must be in `roles` (or scope `*`), else 403 |
| `legacy-shared`, SPA Google session, local dev (no auth configured) | unchanged |

### Scope matrix (proposed, actor × workspace)

| actor | owera (1) | tenant-b (2) | buyer workspaces | global/admin routes | write |
|---|---|---|---|---|---|
| cto | ✔ | ✔ | ✔ | ✔ | ✔ |
| channels (bot, role channels) | ✔ | — | — | — ¹ | ✔ |
| vm (role vm) | ✔ | — | — | — ¹ | ✔ |
| channels-dev | ✔ | ✔ (O3/O4 tests) | — | — | ✔ |
| cos | ✔ | — | — | — | read-only |
| po-cm | ✔ | — | — | — | read-only |
| growth-agent | ✔ | — | — | — ¹ | ✔ |
| run-check | ✔ | — | — | — | read-only |
| code-agent | ✔ | — | — | — | ✔ |
| buyer (SPA Google) | — | — | own only | as today ² | ✔ |
| legacy-shared (until cut) | ✔ | ✔ | ✔ | ✔ | ✔ |

¹ Today these global routes have **no workspace check**. A scoped token can't reach
them until they're workspace-scoped (follow-up, see Risks):
- `/api/settings`, `/api/music`, `/api/trends`, `/api/profiles`, `/api/params`, `/api/runs`
- `/api/topics*`
- `/api/channels/{id}/playlists*`
- `/api/channels/{id}/youtube|branding|metrics|subscriptions|subscribers|monetization|video-analytics*`
- `/api/videos/{id}/video|thumb|thumbnail`

² The SPA Google session behavior is unchanged in this PR.

## 2. Audit log (append-only)

Each authenticated or denied `/api` request writes one line. `/health`, the OAuth
callbacks, the Stripe webhook, `/api/features` and `/api/auth/*` aren't audited
because they're unauthenticated by design.

```json
{"ts":"2026-11-16T12:00:00.123Z","actor":"channels","auth":"token","workspace_id":1,"workspace_slug":"owera","route":"/api/videos/{video_id}","method":"GET","status":200,"outcome":"allowed"}
{"ts":"2026-11-16T12:00:01.456Z","actor":"channels","auth":"token","workspace_id":null,"workspace_slug":"tenant-b","route":"/api/channels","method":"GET","status":403,"outcome":"denied","reason":"actor 'channels' is not scoped for workspace 'tenant-b'"}
```

- `ts` is UTC.
- `outcome` is one of `allowed` | `denied` (401/403) | `client_error` | `error` (5xx).
- `route` is the route **template**. There's no query string, body, header, token or password.
- The file is written with `O_WRONLY|O_APPEND|O_CREAT` (0640). No route reads, rewrites or
  deletes it, and the test greps for one. It lives outside `storage/` and every served directory.
- Ops hardening (CTO, claw0): `chflags uappnd ~/src/owera-channels-manager/audit/access.jsonl`
  makes it append-only at the OS level, even for the app user. Rotation then needs the flag cleared.

## Risks

- **Global routes without a workspace check (pre-existing).** Any shared-password
  caller can read any channel's YouTube data by id
  (`/api/channels/{id}/youtube|metrics|video-analytics…`). Scoped tokens fail closed
  there, so the bots that need them (channels, growth agent) stay on legacy or get `*` until a
  follow-up adds `current_workspace` + `require_channel` to those routes.
- **Audit write fails open.** A write error logs `audit_write_failed` at ERROR and still
  serves the request. Watch for it in the manager log.
- Basic username = workspace slug is unchanged. With a scoped token, claiming
  another slug is now a 403.
- Direct DB / file access on claw0 bypasses all of this. See `docs/OPS_GAP_CLAW0_DIRECT_ACCESS.md`.
- SPA Google users aren't scope-checked beyond today's behavior.
