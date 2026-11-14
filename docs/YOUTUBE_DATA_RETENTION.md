# YouTube data retention, access-lost alerts, refresh diagnostics

Covers the deletion rules in LegalOps cláusula 7 v2 (YouTube API Services
Developer Policies III.D.2.3.a, III.E.4.g and III.D.2.3.b; Google API
Services User Data Policy), the contact@ alert when a real channel loses
access, and the refresh-error logging.

## When access counts as lost

`Channel.access_lost_at` (naive UTC) starts the clock. It is stamped by:

| Event | Where | `access_lost_reason` | Deadline |
|---|---|---|---|
| Disconnect in the app (`POST /api/channels/{id}/disconnect`) | `notify.mark_operator_disconnect` | `disconnected` | 7 days (III.D.2.3.a) |
| User asks us to delete (`POST /api/channels/{id}/request-deletion`) | `notify.mark_deletion_requested` | `deletion_requested` | 7 days (III.E.4.g) |
| Workspace closed (`POST /api/workspace/close`) | `notify.mark_workspace_closed` | `workspace_closed` | 7 days (III.E.4.g) |
| A token refresh fails with `invalid_grant` (revoked on Google's security page, or the token will not renew) | `notify.mark_dead` via the `NeedsConnect` message | `invalid_grant` | 30 days (III.D.2.3.b) |
| Any other `connected → expired/disconnected` flip (P3b) | `notify.mark_dead` | `expired` / `disconnected` | 30 days |
| Already dead before this shipped: `expired`, or `disconnected` still holding metrics/credential (P3b) | `youtube_purge.backfill_lost_access`, each tick | `expired_backfill` / `disconnected_backfill` | 30 days from first tick after deploy |

The first stamp wins. A later user-initiated event (disconnect, deletion
request, workspace close) can only *shorten* a 30-day clock to 7 days from
the original stamp. A reconnect (`notify.mark_connected`) clears the clock,
which cancels a pending purge. `error` (failed consent) never starts the
clock. The backfill starts the 30-day clock at the first tick after deploy.

The access token is dropped immediately on disconnect, a deletion request
and a workspace close (revoked at Google once #213 is stacked).

## Purge job

`app/services/youtube_purge.py`, registered in the existing APScheduler
(`youtube_purge`, every 6 h, first run 5 min after start). No env var.

A channel is purged when `access_lost_at + 7 days` (user-initiated reasons)
or `access_lost_at + 30 days` (Google revoke / RefreshError) has been
reached and it is not `connected`. A tick at exactly that instant deletes
the data. Idempotent: `youtube_data_purged_at >= access_lost_at` means done.

Deleted: `ChannelMetric` rows, `VideoMetric` rows (analytics, traffic sources,
search terms), the stored OAuth credential and legacy token files,
`Channel.yt_channel_title`, and `Channel.name` when it still equals the
YouTube title (reset to the slug).

Kept: the workspace, the channel row and settings, topics, scripts, renders,
`Video` rows (our production records, incl. `yt_video_id` of videos CM
uploaded), CM-created `Playlist` rows, and `yt_channel_id` (identity binding
against reconnecting the wrong channel). LegalOps can ask to widen this.

One log line per purge with row counts (`manager.youtube_purge`), no names or tokens.

## Access-lost email (contact@)

When a **real** channel flips `connected → expired` (`notify.alert_dead`) or
is disconnected in the app while connected, one email goes to
`contact@owera.com` from `channels@mail.owera.com` via Resend
(`stripe_email.send_email`, `MANAGER_RESEND_API_KEY`). It names the channel,
workspace, status, cause, OAuth error code and time (America/Belem + UTC).
Never a token; never rodrigo@ (blocklisted in `stripe_email`).

- **Real channel** = `paused` is false **and** `is_test` is false.
  `Channel.is_test` (default false) is the only test criterion; set it by hand
  for smoke/dogfood/consent channels, e.g.
  `UPDATE channel SET is_test = 1 WHERE id IN (…);` (operator decision).
- **Once per transition**: `ChannelAlert` row keyed
  `channel_id:status:oauth_status_changed_at` (primary key). Retries, a second
  worker or a restart do not re-send. A reconnect followed by a new loss is a
  new transition and emails again.
- `MANAGER_RESEND_API_KEY` unset → warning in the log, nothing sent, no row.
- A Resend failure is logged and swallowed; the status flip is already committed.
- `error` status (failed consent) does not email.

## Refresh diagnostics

`youtube._refresh_and_persist` no longer swallows `RefreshError` silently. It logs
`token refresh failed for channel '<slug>' (id=<n>): RefreshError code=<code>`
(`invalid_grant`, `invalid_client`, …, never the token), and the code is appended
to the `NeedsConnect` message: `… reconnect required (refresh failed: invalid_grant)`.

`/health` adds two aggregate counts (status logic unchanged):
`channels_expired` and `channels_never_connected` (disconnected, no identity,
access never lost, i.e. onboarding not finished).

## Day-6 token-renewal check (S7)

`Channel.last_refresh_ok_at` (naive UTC) is stamped on every successful
platform-OAuth refresh (`token_store.save_credentials`, which now also bumps
`OAuthCredential.updated_at`) and on every verified connect/reconnect
(`notify.mark_connected`). A failed refresh (`RefreshError`) never stamps it.
Legacy `token.json` refreshes (internal ch1/ch2) are not stamped.

`app/services/token_refresh_age.py`, APScheduler job `token_refresh_age`, every
6 h (first run 6 min after start). DB only: it never refreshes, never calls
Google, never changes `oauth_status`. For each `connected` channel outside
`MANAGER_TOKEN_AGE_INTERNAL_WORKSPACE_IDS` (default `1`; malformed or empty means
every workspace is watched; deliberately separate from the LLM allowlist) whose
reference time (`last_refresh_ok_at`, else `oauth_status_changed_at`, else
`created_at`) is more than 6 days old:

- one WARNING per stale episode, e.g.
  `token_refresh_stale channel_id=11 workspace_id=2 days=7.0` (no token, name or email);
- `Channel.token_refresh_stale_at` is set, and cleared by the next successful refresh or reconnect.

Internal workspaces get an INFO `token_refresh_stale_internal …` line and no marker.
Closed workspaces are skipped. Test channels (`is_test`) and paused channels are
watched, because the E2E day-8 check runs on a test workspace.

Ops surface: each channel entry in `/api/dashboard` and `/api/agent/state` has
`token_refresh: {last_refresh_ok_at, stale_at, stale, age_days}`.

**Testing mode:** while the Google OAuth app is in Testing, refresh tokens die
7 days after the grant, so this WARNING is expected for every external channel
around day 6 until the app is in Production. In Production, a flag means
renewal is broken. A channel nobody uses for 6+ days can also be flagged,
because refreshes only happen on use. In practice the metrics loop builds a
client for every connected channel every few hours, so this is rare.
