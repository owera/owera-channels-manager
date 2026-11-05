# SPA Google Sign-In (human login)

Separate from **YouTube Connect** (`MANAGER_GOOGLE_WEB_OAUTH`, `/api/oauth/google/*`) and from **agent HTTP Basic** (`/api/*` with `owera:password`).

## GCP (project `618513602444`)

Create a **Web application** OAuth client for human portal login (not the YouTube Connect client).

| Setting | Value |
|---------|--------|
| Authorized JavaScript origins | `https://channels.owera.com`, `http://127.0.0.1:7070` |
| Authorized redirect URIs | `https://channels.owera.com/api/auth/google/callback`, `http://127.0.0.1:7070/api/auth/google/callback` |

Callback route on the manager: **`GET /api/auth/google/callback`** only.

Scopes: OpenID `openid`, `email`, `profile` (handled by the server OAuth start URL).

## Env (claw0 — never commit secrets)

| Env | Purpose |
|-----|---------|
| `MANAGER_SPA_GOOGLE_LOGIN=1` | Enable Google login for SPA; agents still use Basic. |
| `MANAGER_SPA_GOOGLE_CLIENT_ID` | Web client ID from GCP. |
| `MANAGER_SPA_GOOGLE_CLIENT_SECRET` | Web client secret. |
| `MANAGER_SPA_GOOGLE_REDIRECT_URI` | Must match GCP (prod default in `.env.example`). |
| `MANAGER_SESSION_SECRET` | Random secret for signed session cookie (`cm_session`). |
| `MANAGER_APP_PASSWORD` | **Keep set** for machine/agent Basic on `/api/*`. |
| `MANAGER_MULTI_WORKSPACE=1` | Recommended on claw0 so session maps to workspace `owera`. |

## Allowlist (v1)

Only **`rodrigo@owera.com`** → workspace slug **`owera`**. Other Google accounts receive HTTP 403 on callback.

## Verify

```bash
PYTHONPATH=. uv run python tests/verify_spa_google_login.py
PYTHONPATH=. uv run python tests/verify_agent_a1.py
PYTHONPATH=. uv run python tests/verify_platform_oauth.py
```
