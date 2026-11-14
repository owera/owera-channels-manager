# Workspace isolation (S4)

Every route that touches a channel, video, topic, playlist, trend or render
profile resolves the caller's workspace (`app.workspace.current_workspace`)
and checks ownership before doing anything else: before a YouTube token is
loaded, before an LLM call, before a write.

## Rule

- A resource that belongs to **another workspace answers 404**, with the same
  body as a missing row. Never 403: the response must not reveal that the id
  exists elsewhere.
- Deletes of a missing topic/profile now answer 404 too (they used to be a
  silent 204), so "missing" and "foreign" cannot be told apart.
- Strict isolation applies to ws 1 as well: owera does not see buyer rows on
  these routes. The admin role covers the **global settings** and
  **shared (channel-less) rows** only.

Helpers (`app/workspace.py`): `require_channel`, `require_video` (existing),
`require_topic`, `require_trend`, `require_profile(write=)`,
`is_admin_workspace`.

## Routes covered

| Area | Routes |
|---|---|
| youtube_admin (12) | `GET /youtube`, `PUT /branding`, `GET /metrics`, `POST /metrics/refresh`, `GET/POST /subscriptions`, `DELETE /subscriptions/{id}`, `GET /subscribers`, `GET /monetization`, `GET /video-analytics`, `GET /video-analytics/by-topic`, `POST /video-analytics/refresh` (all under `/api/channels/{id}`) |
| topics | `GET/POST /api/topics`, `GET/PATCH/DELETE /api/topics/{id}`, `POST /api/topics/{id}/generate` (`/reorder` was already scoped). Create/PATCH also reject a foreign `playlist_id` / `render_profile_id`. |
| playlists | `GET/POST /api/channels/{id}/playlists`, `POST …/sync` |
| media | `GET /api/videos/{id}/video`, `GET /api/videos/{id}/thumb` |
| thumbnails | `POST/DELETE /api/videos/{id}/thumbnail` |
| trends | list (filtered), `POST` (upsert dedupe per workspace), `GET/PATCH /{id}`, `POST /{id}/adopt` (target channel checked) |
| profiles | list (shared + own), `POST`, `GET/PATCH/DELETE /{id}` |
| settings | `GET/PATCH /api/settings`, and the `settings` block of `/api/agent/state` |

Shared rows:

- **Render profiles** with `channel_id = NULL` (e.g. `Default`) are readable by
  every workspace and writable only by ws 1. A buyer must create profiles on
  one of its own channels.
- **Trends** with `channel_id = NULL` are the growth agent's research rows and
  belong to ws 1. A buyer must pass one of its own channels.

## Settings decision

`Settings` is one global row (render concurrency, publish drip, autogen knobs,
global scheduler kill switch). It drives the whole claw0 pipeline and today
only serves ws 1 (Rodrigo's operation), so it stays global and becomes
**admin-only (ws 1)**. Only the pause is per workspace:

- New column `Workspace.scheduler_paused` (added by `_add_missing_columns`,
  default 0, no data migration).
- ws 1: `GET/PATCH /api/settings` unchanged (plus `scope: "global"`).
  `scheduler_paused` there is still the global kill switch.
- Any other workspace: `GET` returns
  `{scope: "workspace", workspace_id, scheduler_paused, youtube_quota_reset_at}`.
  `PATCH` accepts only `scheduler_paused` (its own pause). Any global key in the
  body → **404** and nothing is applied.
- Loops: render (`_auto_produce`, `_rebalance_queued_mix`, `_submit_new`),
  publish, analytics and autofill skip channels whose workspace is paused.
  Renders already in flight finish (`_advance_in_flight` is not gated), so a
  paid render is not orphaned. The queue plan labels cards
  "workspace paused". The global pause still stops everything first.
- **Not gated by any pause (global or per workspace), on purpose:**
  `metrics_loop` (one read-only `channels.list` snapshot per connected channel
  per day; no LLM, no upload), `token_refresh_age` (DB-only day-6 token check,
  no Google call) and `youtube_purge` (the 30-day / 7-day data deletion must run
  regardless). A pause stops automation that acts on a channel, not observation
  or compliance jobs. Documented in each `tick()`.

### Runs (`GET /api/runs`, `recent_runs` in `/api/agent/state`)

`JobRun` has no `workspace_id`; ownership comes from the row's channel (or, for
a channel-less row, its video's channel). `job_run_scope(ws)` in
`app/workspace.py` applies that in SQL. Rows with neither channel nor video
(global maintenance) are visible to ws 1 only. A foreign or missing
`channel_id` / `video_id` filter → the same 404.

### Shared BGM pool (`/api/music`)

`settings.bgm_dir` is one directory of background tracks used by every render;
it is not workspace data. All `/api/music` routes are **admin-only (ws 1)**
(router dependency `require_admin_workspace`); any other workspace gets 404.

Tests: `tests/verify_workspace_isolation.py` (buyer A on buyer B's resources,
one check per route, plus settings, pause and ws 1 regressions).
