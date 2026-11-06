# Generics v1 (YouTube-only) — agent / PO surface

Contract: same **HTTP Basic Auth** API as the dashboard (`Authorization: Basic …`, password = `MANAGER_APP_PASSWORD`). No MCP, no new auth modes.

## 1. Free-text niche / category

- **Field:** `Channel.niche` (nullable string), returned on `GET /api/channels` and `GET /api/channels/{id}`.
- **Write:** `PATCH /api/channels/{id}` with `{ "niche": "…" }` or `{ "niche": null }` to clear.
- **Effect:** Included in idea prompts (`generate_ideas`) and in metadata generation (`metadata.generate`) so titles/descriptions match the declared positioning.

## 2. Topics — CRUD, prioritize, pause

| Action | HTTP |
|--------|------|
| List | `GET /api/topics?channel_id=` |
| Create | `POST /api/topics` |
| Read | `GET /api/topics/{id}` |
| Update | `PATCH /api/topics/{id}` — `name`, `theme_prompt`, `content_format`, `render_profile_id`, `playlist_id`, **`weight`**, **`active`** |
| Delete | `DELETE /api/topics/{id}` (only if no videos) |
| Generate ideas | `POST /api/topics/{id}/generate` |
| **Reorder (priority)** | `POST /api/topics/reorder` body `{ "channel_id": N, "ordered_ids": [topic_id, …] }` |

**Pause semantics (existing product language):**

- **`weight <= 0`:** “parked” — `POST …/generate` returns `generated: 0` with reason `topic is parked`; autofill and auto-produce skip the topic.
- **`active: false`:** topic excluded from autofill and auto-produce (still editable).

## 3. Steer next video (batch) + review / hold / skip

**Steer the next idea batch** (optional body on generate):

```json
POST /api/topics/{id}/generate
{
  "count": 8,
  "steer_topic": "…",
  "steer_title": "…",
  "steer_angle": "…",
  "steer_notes": "…"
}
```

Server merges these into a single steering block for that generate only (channel `niche` still applies).

**Review path (unchanged):** list/filter `GET /api/videos`, edit `PATCH /api/videos/{id}`, produce `POST /api/videos/{id}/produce`, approve/reject/hold/unhold as in `docs/AGENT.md`. **Skip** an idea: `DELETE /api/videos/{id}` on a draft, or `POST /api/videos/{id}/reject` depending on status.

**Operator hold (#1458):** `POST /api/videos/{id}/hold` / `…/unhold` for approved (publish) or queued (render) rows.

## 4. Observe

`GET /api/agent/state` includes `topics` with `active`, `weight`, and ordered topic list via `GET /api/topics?channel_id=` (sorted by `position`).

## Smoke (curl)

```bash
export BASE=https://…
export MANAGER_APP_PASSWORD=…
AUTH='-su owera:$MANAGER_APP_PASSWORD'

curl $AUTH "$BASE/api/channels/1"   # expect niche in JSON
curl $AUTH -H 'Content-Type: application/json' \
  -d '{"niche":"Custom niche text for this channel"}' \
  -X PATCH "$BASE/api/channels/1"

curl $AUTH -H 'Content-Type: application/json' \
  -d '{"count":4,"steer_angle":"beginner-friendly, one concrete takeaway"}' \
  -X POST "$BASE/api/topics/2/generate"

curl $AUTH -H 'Content-Type: application/json' \
  -d '{"channel_id":1,"ordered_ids":[3,2,1]}' \
  -X POST "$BASE/api/topics/reorder"
```
