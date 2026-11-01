# WS4 — Channels Manager on shared claw0 (early access)

Informational ops notes for Owera EA on **claw0**. No secrets in this repo.

## Network (Cloudflare tunnel)

- Tunnel name: **`portal-caso`**
- Origin: manager on claw0 **`:7070`** (bind `MANAGER_HOST=0.0.0.0`, `MANAGER_PORT=7070`)
- Public host (example): `https://channels.owera.com` — OAuth redirect is fixed in GCP as  
  `https://channels.owera.com/api/oauth/google/callback`

## Platform OAuth (off by default)

| Env | Purpose |
|-----|---------|
| `MANAGER_GOOGLE_WEB_OAUTH=0` | **Default.** Platform routes return 404; legacy per-channel `client_secret.json` upload. |
| `MANAGER_GOOGLE_CLIENT_ID` | Owera **Web** OAuth client (claw0 only — never commit). |
| `MANAGER_GOOGLE_CLIENT_SECRET` | Web client secret (claw0 only). |
| `MANAGER_OAUTH_REDIRECT_URI` | Must match GCP console (default channels.owera.com callback). |
| `MANAGER_TOKEN_ENC_KEY` | 32-byte urlsafe base64 or JSON keyring for encrypted refresh tokens (PR-D). |

Do **not** enable `MANAGER_GOOGLE_WEB_OAUTH=1` until operator sign-off.

## Multi-workspace (off by default)

| Env | Purpose |
|-----|---------|
| `MANAGER_MULTI_WORKSPACE=0` | **Default.** Single default workspace `owera`. |
| `MANAGER_ALLOW_WORKSPACE_HEADER=0` | **Default.** `X-Workspace-Slug` disabled in production. |
| `MANAGER_ALLOW_WORKSPACE_HEADER=1` | **Dev only** — local/Vite without Basic Auth username. |

When `MANAGER_MULTI_WORKSPACE=1`, the API scopes data by **Workspace**. Today the workspace slug is taken from **HTTP Basic Auth username**. That is only safe if **Cloudflare Access (or the tunnel)** authenticates users before traffic reaches the manager — otherwise any client could pick another tenant’s slug. Longer term: bind workspace to the authenticated identity, not a client-supplied header.

## Per-workspace quota (PR-G)

`Workspace.daily_youtube_quota_cap` (default 9000) aggregates YouTube Data API unit spend across all channels in that workspace for the Pacific quota day. Global `MANAGER_YOUTUBE_DAILY_QUOTA_CAP` remains a hard ceiling.

## Legacy channels (ch1/ch2)

`credential_source=legacy_file` — tokens stay under `credentials/<slug>/token.json`. Platform encrypted store is for `platform_oauth` only.

## Verify (no network)

```bash
PYTHONPATH=. uv run python tests/verify_workspace.py
PYTHONPATH=. uv run python tests/verify_token_store.py
PYTHONPATH=. uv run python tests/verify_platform_oauth.py
PYTHONPATH=. uv run python tests/verify_features.py
PYTHONPATH=. uv run python tests/verify_workspace_quota.py
```
