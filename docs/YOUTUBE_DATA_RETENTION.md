# YouTube data retention, access-lost alerts, refresh diagnostics

Covers the 30-day deletion rule (YouTube API Services Developer Policies
§III.E.4, Google API Services User Data Policy), the contact@ alert when a
real channel loses access, and the refresh-error logging.

## When access counts as lost

`Channel.access_lost_at` (naive UTC) starts the clock. It is stamped by:

| Event | Where | `access_lost_reason` |
|---|---|---|
| Disconnect in the app (`POST /api/channels/{id}/disconnect`) | `notify.mark_operator_disconnect` | `disconnected` |
| A token refresh fails with `invalid_grant` (revoked at Google, or the Testing-mode 7-day expiry) | `notify.mark_dead` via the `NeedsConnect` message | `invalid_grant` |

The first stamp wins. A reconnect (`notify.mark_connected`) clears it, which
cancels a pending purge. An `EXPIRED` flip **without** `invalid_grant` (e.g.
token row missing) does not start the clock in this PR (follow-up P3b).

## Purge job

`app/services/youtube_purge.py`, registered in the existing APScheduler
(`youtube_purge`, every 6 h, first run 5 min after start). No env var.

A channel is purged when `access_lost_at` is ≥ 29 days old and it is not
`connected`, so with the 6 h tick the data is gone within 30 days.
Idempotent: `youtube_data_purged_at >= access_lost_at` means done.

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
