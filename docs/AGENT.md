# Channels Manager — HTTP agent surface (A1)

Machine clients use **HTTP Basic Auth** against the same API as the dashboard SPA. There is no MCP layer, no API keys, and no browserless Google OAuth in this cut.

## Auth

Set `MANAGER_APP_PASSWORD` in the deployment environment. Every request (except `/health` and OAuth callback URLs) must send:

```http
Authorization: Basic base64(username:password)
```

When `MANAGER_MULTI_WORKSPACE=0` (default), all data lives in the **owera** workspace; the username still selects the **review actor** when using compound or legacy forms below.

When `MANAGER_MULTI_WORKSPACE=1`, the Basic **username** selects the workspace and (optionally) the review actor:

| Username | Workspace | Actor (review audit / guard) |
|----------|-----------|------------------------------|
| `{slug}` | `{slug}` | `{slug}` — observe, connect, hold/unhold (no Gate B role) |
| `{slug}+channels` | `{slug}` | `channels` — approve, CM teaser requeue |
| `{slug}+vm` (+`video-maker` / +`videomaker`) | `{slug}` | `vm` — record `vm-pass` |
| `channels` | `owera` | `channels` (legacy) |
| `vm` / `video-maker` / `videomaker` | `owera` | `vm` (legacy) |
| `admin` | `owera` (alias) | `admin` |

Password is always `MANAGER_APP_PASSWORD`. Wrong password → **401**. Unknown workspace slug → **404**. Cross-workspace channel/video id → **404** (not 403).

Dev-only: `X-Workspace-Slug` when `MANAGER_ALLOW_WORKSPACE_HEADER=1` and the Basic username is empty.

## Observe

| Endpoint | Purpose |
|----------|---------|
| `GET /api/features` | Flags (`multi_workspace`, OAuth modes, …) |
| `GET /api/dashboard` | Per-channel pipeline counts, quota, publish ETA |
| `GET /api/agent/state` | Settings + dashboard + topics + issues + recent runs |
| `GET /api/agent/issues` | Operational triage digest |
| `GET /api/videos` | List videos (optional `channel_id`, `topic_id`, `status`) |

Example isolation smoke (`MANAGER_MULTI_WORKSPACE=1`):

```bash
curl -su "tenant-b:$MANAGER_APP_PASSWORD" "$BASE/api/dashboard"
curl -su "owera:$MANAGER_APP_PASSWORD" "$BASE/api/dashboard"
```

Tenant B must not see owera channels or videos.

## Connect (human in the loop)

Google consent requires a browser. The agent only **starts** the flow and **polls** status.

1. `POST /api/oauth/google/start` (when `MANAGER_GOOGLE_WEB_OAUTH=1`) → `{ "auth_url": "..." }` — open in a human browser.
2. After consent, poll `GET /api/channels/{id}/oauth-status` (or channel list) until connected.
3. Per-channel reconnect: `POST /api/channels/{id}/oauth/start` when enabled.

Use workspace username `{slug}` (not `+channels`) for connect routes.

## Review

Scoped to the video’s workspace; use `{slug}+channels` or legacy `channels` on owera for approve.

| Action | Method |
|--------|--------|
| Approve | `POST /api/videos/{id}/approve` |
| Reject | `POST /api/videos/{id}/reject` |
| Hold / unhold | `POST /api/videos/{id}/hold`, `…/unhold` |
| VM Gate B | `POST /api/videos/{id}/vm-pass` (`{slug}+vm` or legacy `vm`) |
| Requeue / retry | `POST /api/videos/{id}/requeue`, `…/retry` |

Approve moves the row to **approved**; `publish_loop` picks it up on the channel drip (no separate publish POST).

## OpenAPI

With Basic Auth:

- `GET /openapi.json` — machine-readable schema
- `GET /docs` — Swagger UI

```bash
curl -su "owera:$MANAGER_APP_PASSWORD" "$BASE/openapi.json" | head
```

## Explicit non-goals (A1)

- MCP tools or agent-specific alternate transports
- Per-agent API keys or Bearer tokens
- Headless / unattended Google OAuth consent
- `POST /api/workspaces` or workspace creation APIs
