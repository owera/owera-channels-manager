# Channels Manager — HTTP agent surface (A2)

Machine clients use **HTTP Basic Auth** against the same API as the dashboard SPA. There is no MCP layer, no API keys, and no browserless Google OAuth in this cut.

Connect flow below is the **machine contract** validated in O3/O4 smoke at tip `3b70814` (platform Google Web OAuth). Tip/merge/deploy and flag flips are operator-only — agents document and exercise HTTP only.

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

Human SPA Google login (when `MANAGER_SPA_GOOGLE_LOGIN=1`) uses a browser **`cm_session` cookie** for `/api/*`; it does not use Basic. `POST` or `GET` **`/api/auth/logout`** clears that session; machine agents are unchanged and still use Basic as above.

OAuth **callback GETs are exempt from Basic Auth** (human browser lands there after Google redirect): `GET /api/oauth/google/callback` (platform) and `GET /api/channels/{id}/oauth/callback` (legacy per-channel). Starting OAuth and polling channel state still require Basic Auth.

Dev-only: `X-Workspace-Slug` when `MANAGER_ALLOW_WORKSPACE_HEADER=1` and the Basic username is empty.

## Observe

| Endpoint | Purpose |
|----------|---------|
| `GET /api/features` | Flags (`multi_workspace`, OAuth modes, …) |
| `GET /api/dashboard` | Per-channel pipeline counts, quota, publish ETA |
| `GET /api/agent/state` | Settings + dashboard + topics + issues + recent runs |
| `GET /api/agent/issues` | Operational triage digest |
| `GET /api/videos` | List videos (optional `channel_id`, `topic_id`, `status`) |

**Generics v1 (YouTube topics, niche, steer-next batch):** see [`docs/GENERICS_V1.md`](GENERICS_V1.md) for `Channel.niche`, topic reorder, generate steering fields, and pause semantics (`weight` / `active`).

Example isolation smoke (`MANAGER_MULTI_WORKSPACE=1`):

```bash
curl -su "tenant-b:$MANAGER_APP_PASSWORD" "$BASE/api/dashboard"
curl -su "owera:$MANAGER_APP_PASSWORD" "$BASE/api/dashboard"
```

Tenant B must not see owera channels or videos.

## Connect (human in the loop)

Google consent requires a **real human browser**. Agents **start** platform OAuth, hand `auth_url` to a person, then **poll channel JSON** until the row shows connected. Do not drive headless consent or parse callback HTML as the contract.

Requires deployment flag `MANAGER_GOOGLE_WEB_OAUTH=1` (when off, `POST /api/oauth/google/start` returns **404**). Confirm with `GET /api/features` → `google_web_oauth: true`.

Use Basic username **`{slug}`** for create/start/poll (e.g. `tenant-b`, `owera`) — not `{slug}+channels` or `{slug}+vm`.

### Happy path (agent loop)

1. **Ensure a channel row** in the target workspace (create if missing):

   `POST /api/channels` with JSON `name`, `slug` (slug derived from name if omitted), and budgets ( **`0` is valid** to stall render/publish).

2. **Start platform OAuth:**

   `POST /api/oauth/google/start` with body `{ "channel_id": <id> }` → `{ "auth_url": "https://..." }`.

3. **Human** opens `auth_url` in a normal browser and completes Google consent (agent cannot complete this step).

4. **Poll until connected** — primary contract is channel JSON, not the callback page:

   - **`GET /api/channels/{id}`** (repeat until done), or filter **`GET /api/channels`** to the target id.
   - Read **`oauth_status`**, **`credential_source`**, **`yt_channel_id`**, **`yt_channel_title`**, **`oauth_error`** on that row only.
   - Success when **`oauth_status` is `connected`**, **`credential_source` is `platform_oauth`**, and YouTube id + title are bound on **that** channel row.

   **`GET /api/channels/{id}/oauth-status` exists** but is a **secondary helper**: it live-probes YouTube and may flip a channel to a dead status on probe failure. Prefer plain **`GET /api/channels/{id}`** while waiting for consent; use `oauth-status` only when repairing a known “token saved, status stuck” case (callback HTML mentions this).

5. **Do not** treat callback HTML as machine output. Google redirects to `GET /api/oauth/google/callback`, which returns **HTML** success/reject pages for the human tab. Machines finish by polling step 4.

### Callback HTML (browser-only)

| Callback | Auth | Response |
|----------|------|----------|
| `GET /api/oauth/google/callback` | none (exempt) | HTML “Connected” or “Consent rejected” / failure |
| `GET /api/channels/{id}/oauth/callback` | none (exempt) | HTML (legacy Desktop-client reconnect path) |

On **`GrantRejected`** (e.g. **`channel_mismatch`**), the callback shows a reject page; **`verify_grant` runs before any token is saved**, so a healthy row keeps its prior **`oauth_status`** and **`credential_source`**.

### Isolation and `channel_mismatch`

- Binding is **per CM channel row + workspace**. If the YouTube channel is already linked to a **different** CM channel, consent can complete in the browser but **`finish_flow` rejects with `GrantRejected` / `channel_mismatch`** — no token on the target row.
- **Cross-workspace:** polling or starting OAuth with the wrong Basic username (wrong `{slug}`) for another tenant’s `channel_id` → **404**, not 403.
- **Same workspace:** connecting one row does not rewrite sibling rows; channels that already use **`legacy_file`** stay on file-backed tokens unless explicitly reconnected on that row (O3/O4 proof: one tenant channel moved to **`platform_oauth`** while other workspace channels kept **`legacy_file`**).

### Example (placeholders)

```bash
export BASE="https://channels.example.com"
export MANAGER_APP_PASSWORD="…"
WS="tenant-b"   # workspace slug

# Create (skip if row exists)
curl -su "$WS:$MANAGER_APP_PASSWORD" -H 'Content-Type: application/json' \
  -d '{"name":"My Channel","slug":"my-channel","daily_render_budget":0,"daily_publish_budget":0}' \
  "$BASE/api/channels"

CH=42   # channel id from create or list

curl -su "$WS:$MANAGER_APP_PASSWORD" -H 'Content-Type: application/json' \
  -d "{\"channel_id\": $CH}" \
  "$BASE/api/oauth/google/start"
# → human opens auth_url from JSON

until curl -sf -su "$WS:$MANAGER_APP_PASSWORD" "$BASE/api/channels/$CH" \
  | grep -q '"oauth_status":"connected"'; do sleep 3; done
curl -su "$WS:$MANAGER_APP_PASSWORD" "$BASE/api/channels/$CH"
# expect oauth_status=connected, credential_source=platform_oauth, yt_channel_id set
```

### Legacy per-channel OAuth (optional)

When **`MANAGER_GOOGLE_WEB_OAUTH=0`**, use **`POST /api/channels/{id}/oauth/start`** (after `client_secret.json` upload) and poll the same channel JSON fields. That path uses loopback/Desktop redirect semantics documented in the operator runbook — not the platform Web OAuth contract above.

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

## Explicit non-goals (A1 + A2)

- MCP tools or agent-specific alternate transports
- Per-agent API keys or Bearer tokens
- Headless / unattended Google OAuth consent
- `POST /api/workspaces` or workspace creation APIs
- Agent-driven deploy, feature-flag flips, or treating OAuth callback HTML as structured API output (A2)
