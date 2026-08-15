# Code Agent Backlog

Ranked by leverage — highest first. The code agent (`run/code-agent-playbook.md`) takes the top item it
can finish end-to-end in one cycle, ships it as a gated commit straight to `main`, and checks it off.
Re-rank freely as reality changes. Format per item: **why** · **approach** · **caution** · **acceptance**.

Caution legend: `normal` = standard gate · `HIGH` = money-path file, isolated commit + new regression
test · `GATED` = needs an operator step (OAuth/account/external) — ship the safely-inert code part and
flag the operator step in the commit body.

---

### 1. ✅ DONE (code shipped to main 2026-07-08; operator step pending) Fix portal OAuth reconnect (`redirect_uri_mismatch`) — HIGH
- **resolution (2026-07-08):** `MANAGER_PUBLIC_BASE_URL` setting added; when set, `oauth_start` pins
  the redirect_uri to it regardless of the incoming Host (unset = old request-derived behavior, so
  localhost reconnects are unchanged). Regression suite: `tests/verify_oauth_redirect.py`.
  **Operator step:** set `MANAGER_PUBLIC_BASE_URL=http://localhost:7070` in `.env` to activate
  (Desktop OAuth clients only accept loopback redirects — consenting from another machine needs an
  SSH tunnel to :7070, per the reconnect recipe in project memory).
- **urgency note (2026-07-06):** reduced — ch2's app is now published to Production with a fresh
  token, so reconnects should be rare. Still worth fixing so the dashboard button works when needed.
- **why:** reconnect from `channels.owera.com` fails; only `localhost` works. Root cause: uvicorn runs
  without `--proxy-headers`, so the `channels.owera.com` Host header makes `oauth_start` build a
  non-loopback `redirect_uri` the Desktop OAuth client rejects. This is why ch2 was down for ~3 days.
- **approach:** run uvicorn with `--proxy-headers --forwarded-allow-ips=127.0.0.1`, and/or add a
  `MANAGER_PUBLIC_BASE_URL` setting that `app/routers/channels.py:oauth_start` uses to build a stable,
  registered `redirect_uri`. Keep the Desktop-client loopback path working for local reconnects.
- **caution:** touches `app/routers/channels.py` (oauth) + launch config — isolated PR, add a test that
  asserts the generated `redirect_uri` for a given Host/base-url.
- **acceptance:** reconnect initiated from the portal produces a redirect_uri Google accepts; localhost
  path unchanged; regression test green.

### 2. ✅ DONE (code shipped to main 2026-07-10) Self-healing OAuth alert on token expiry — HIGH
- **resolution (2026-07-10):** `app/services/notify.py` fires exactly one alert per
  CONNECTED→EXPIRED flip (ERROR log always; webhook POST when `MANAGER_ALERT_WEBHOOK_URL` is set —
  Slack-compatible payload, best-effort, 5s timeout, never raises into the caller). Hooked at both
  transition sites: `publish_loop._publish_one` (NeedsConnect) and the dashboard
  `GET /oauth-status` probe. Alert body carries a ready reconnect recipe honoring
  `MANAGER_PUBLIC_BASE_URL`. Regression suite: `tests/verify_notify.py` (24 checks).
  **Operator (optional):** set `MANAGER_ALERT_WEBHOOK_URL` in `.env` to get pushed alerts
  (Slack/Discord incoming-webhook compatible); without it the alert is a log line + the existing digest.
- **why:** a revoked token today only surfaces in the issues digest; nobody is pinged, so ch2 died
  silently. Detection exists (`362691a`) but is passive.
- **approach:** when a channel flips to `EXPIRED`, emit an alert (log + optional Slack/push webhook via a
  configurable URL) containing a ready reconnect link. Reuse the issues-digest signal in
  `app/services/issues.py`; add a small notifier util.
- **caution:** touches `app/services/youtube.py`/`issues.py` (HIGH) — isolated PR + test the trigger.
- **acceptance:** simulated expiry produces exactly one alert with a working reconnect link; no alert
  while healthy.

### 3. ✅ DONE (PR #6, merged 2026-07-05) Regression tests for the publish-path incidents — normal (high value)
- **why:** every real outage was in the publish/upload path, which has thin tests.
- **approach:** add dependency-free checks (extend `tests/verify_storyboard.py` or a new
  `tests/verify_publish.py`) reproducing: upload-stall retry cap, quota-cooldown handling, revoked-token →
  `NeedsConnect` → channel skipped, drip spacing. Pure unit-level where possible (no live YouTube).
- **caution:** normal (tests only).
- **acceptance:** new checks pass and would have caught the historical failures.

### 3b. ✅ DONE (code shipped to main 2026-07-11) Close the remaining silent-death detectors — HIGH
- **resolution (2026-07-11):** `notify.mark_dead()` / `mark_dead_committed()` are now the single
  choke point: capture prev status, classify via `dead_status_for` (EXPIRED = token file present,
  DISCONNECTED = gone), flip + **commit first**, then alert exactly once on any CONNECTED→dead
  transition. Wired at: publish loop, metrics loop (the publishing-lull detector), analytics loop
  (with a narrow-scope re-probe so a missing analytics scope only skips, never kills), admin 409s
  (`youtube_admin._connected`), playlist sync/create 400s, the oauth-status probe, and failed
  consents (CONNECTED→ERROR now alerts; stale/replayed callbacks no longer touch status). Operator
  `/disconnect` deliberately stays silent. Regression suite: `tests/verify_notify.py` (62 checks).
- **follow-ups (accepted tradeoffs, do separately if they bite):** (a) a *transient* RefreshError
  (Google 5xx during token refresh) is coerced to NeedsConnect by `_load_creds`, so a blip can
  false-positive a flip+page — distinguishing `invalid_grant` from transport errors is a youtube.py
  (HIGH) change; (b) cancelling a re-consent of a healthy CONNECTED channel still flips it to ERROR
  (pre-existing) — now alerted, but a re-probe-before-flip would avoid halting it; (c)
  `topic_playlist.ensure_topic_playlist` still swallows NeedsConnect (next probe catches the death
  one tick later); (d) missing client_secret.json with a token present classifies EXPIRED — the
  alert's Error text carries the real cause.
- **why:** the expiry alert (#2) only fires from sites that flip `oauth_status`. Review of all token
  consumers found paths where a dead channel still dies silently: `metrics_loop.record_snapshot` and
  the analytics loop swallow `NeedsConnect` at INFO without flipping status (so during a publishing
  lull nothing alerts); a CONNECTED channel whose token *file* vanishes flips to DISCONNECTED (route)
  or is misclassified EXPIRED (publish loop) — DISCONNECTED never alerts; a failed reconnect consent
  sets ERROR, halting publishing unalerted, and the later ERROR→EXPIRED flip is guard-suppressed;
  admin endpoints 409 on NeedsConnect without flipping status.
- **approach:** consolidate the transition into one choke point (e.g. `notify.mark_dead(channel,
  new_status, error)` that captures prev, assigns, and alerts on any CONNECTED→dead flip except the
  operator `/disconnect` route), then have metrics/analytics/admin NeedsConnect handlers call it.
- **caution:** touches the loops + oauth classification (HIGH) — isolated commit + extend
  `tests/verify_notify.py` per site.
- **acceptance:** killing a token alerts within one metrics/analytics tick even with no queued
  videos; token-file loss and failed-consent halts alert too; still exactly one alert per incident.

### 4. ✅ DONE (code shipped to main 2026-07-12) Bake the loopback reconnect helper into the app — normal
- **resolution (2026-07-12):** `PYTHONPATH=. uv run python -m app.reconnect <slug-or-id>` runs the
  whole Desktop-client loopback consent (port 8077 default, `--no-browser` for the SSH-tunnel
  recipe; own redirect loop, so stray connections/preconnects can't abort a pending consent the way
  `run_local_server`'s single-request server can). Encodes the 07-05/11 incident lessons: verifies
  refresh-token issuance, full scope grant (unchecked-checkbox trap; `--allow-partial` to override)
  and same-YouTube-channel identity (`--force` to re-bind) BEFORE writing anything;
  `youtube.save_token` writes atomically (0600), keeps the old token as `token.json.bak`, and the
  web consent path shares it; `_load_creds`' refresh persist is now atomic too and yields to a token
  replaced mid-refresh; `disconnect()` removes `.bak`/stranded tmp files; the dead-channel alert
  recipe now leads with the CLI. No manager restart needed. Documented in README (LAN notes + Tips).
  Regression suite: `tests/verify_reconnect.py` (47 checks, real loopback server + real code
  exchange against a local mock token endpoint). Follow-up spun off as 4b.
- **why:** reconnect currently needs an ad-hoc external script; make it first-class.
- **approach:** add an endpoint/CLI that runs the localhost-loopback consent flow and writes the token,
  bypassing the portal-Host and basic-auth-callback issues. Reuse `youtube.build_flow` / `finish_flow`.
- **caution:** touches oauth (HIGH) — isolated PR.
- **acceptance:** documented one-command reconnect; `/verify` shows a channel going connected.

### 4b. ✅ DONE (code shipped to main 2026-07-16) Extend the reconnect grant guards to the web consent path — HIGH
- **resolution (2026-07-16):** `youtube.verify_grant` is the shared verify-before-save guard block
  (refresh-token issuance, full scope grant, same-channel identity — raises `GrantRejected` with a
  `.code` BEFORE anything touches disk or DB); `finish_flow` reordered to exchange → verify → save,
  so a wrong-account/partial-scope/dead web consent saves NOTHING, leaves `oauth_status` untouched
  (a healthy channel keeps publishing through a botched re-consent), and the callback page says
  exactly why (per-code remediation hints: web says Disconnect-first, CLI keeps --force /
  --allow-partial). `notify.mark_connected` is the CONNECTED counterpart of `mark_dead_committed`;
  both the web callback and the reconnect CLI now flip status through it. Regression suite:
  `tests/verify_oauth_redirect.py` grew 5 → 32 checks (verify_grant unit guards + end-to-end
  /oauth/start → /oauth/callback against a local mock of Google's token endpoint).
- **why (from #4's code review, 2026-07-12):** the CLI now verifies refresh-token issuance, full
  scope grant, and same-channel identity BEFORE saving; the web path (`oauth_callback` →
  `youtube.finish_flow`) still saves first and verifies nothing, so a wrong-account or
  partial-scope UI reconnect can still save a bad token and rebind the channel — and a second bad
  consent rotates the last good token out of `token.json.bak`. Pre-existing behavior, out of #4's
  focused scope.
- **approach:** reorder `finish_flow` to exchange → verify identity → save; hoist the CLI's guard
  block into a shared helper both paths call (natural sibling: a `mark_connected` counterpart to
  `notify.mark_dead_committed`, which would also de-duplicate the CONNECTED-update block copied
  between `oauth_callback` and `app/reconnect.py`). Surface mismatch/partial-grant on the callback
  error page instead of saving.
- **caution:** touches `channels.py` oauth + `youtube.py` (HIGH) — isolated commit, extend
  `tests/verify_oauth_redirect.py`/`verify_reconnect.py`.
- **acceptance:** a wrong-account or partial-scope web consent saves nothing and shows why; the
  happy path still connects; suites green.

### 4c. Consent-path hardening follow-ups (from 4b's code review, 2026-07-16) — normal
- **why:** accepted-with-rationale findings from shipping 4b, none blocking but each a real papercut.
- **items, roughly by leverage:**
  (a) ✅ DONE (code shipped to main 2026-07-17) — `_pending_flows` is now keyed by OAuth `state`
  (`_PendingFlow` NamedTuple values; all access through lock-guarded `_remember_flow` /
  `_pop_pending_flow` / `_supersede_flows`): a double-clicked `/oauth/start` no longer orphans the
  first consent; a replayed `?error=` hit (browser-history) matches no pending entry and leaves the
  channel untouched (the old always-flip-on-error pin in `verify_notify.py` was replaced); a
  verified success supersedes the channel's other pending starts; a 30-min TTL enforced at
  consumption plus a 32-entry cap bound the registry. Accepted residuals: a cancel arriving after a
  manager restart is now silent (the next probe catches a genuinely dead token), and sibling starts
  deliberately survive a GrantRejected so the other tab can retry with the right account —
  cancelling that tab instead still flips (the re-probe-before-flip idea from 3b would remove that
  too). Suites: `verify_oauth_redirect.py` 32 → 50, `verify_notify.py` 62 → 68 checks.
  (Original problem, for context: keyed by channel id, a second `/oauth/start` overwrote the
  pending flow, completing the FIRST consent failed the exchange and flipped a CONNECTED channel
  to ERROR, and any `?error=` hit with no pending flow flipped too.)
  (b) ✅ DONE (code shipped to main 2026-07-18) — `youtube.GrantCode` holds the five
  code constants and `youtube.GRANT_CODES` the registered set; all five `verify_grant`
  raise sites and both hint dicts (`_GRANT_HINTS`, `_CLI_HINTS`) key off the constants,
  each hint dict now `assert`s its keys ⊆ `GRANT_CODES` at import, and
  `tests/verify_oauth_redirect.py` (50 → 54 checks) asserts the raise-site codes, the
  constants' literal values, and both hint dicts against `GRANT_CODES` — so a rename that
  desyncs a raise site or a hint dict fails loudly instead of `dict.get(code, "")`
  silently dropping the remediation string.
  (c) ✅ DONE (code shipped to main 2026-07-23) — `GET /oauth-status` was the designated repair
  when a consent saved a good token but its `mark_connected` commit failed (both the callback page
  and the reconnect CLI point the operator here), yet its hand-rolled flip only set the status: the
  channel kept a working token with NO bound identity, so the dashboard showed the stale name and
  the next re-consent had no `expected_channel_id` for `verify_grant`'s wrong-account check. The
  probe now routes an **unbound** channel through `notify.mark_connected` with a freshly fetched
  identity, finishing the repair; an already-bound channel keeps the cheap in-place flip because
  the dashboard polls this endpoint every 2.5s during a reconnect and `channels().list` costs a
  quota unit per call. Identity binding is best-effort — `get_service` already proved the token
  refreshes, so a `channels().list` blip logs and falls through to the plain flip rather than
  flipping a healthy channel dead. Suite: `verify_oauth_redirect.py` 54 → 64 checks.
  (d) ✅ DONE (code shipped to main 2026-08-01, with (e) — the helper made the seam's only
  consumer disappear) — `verify_grant` lost the `fetch_identity_fn` param and `reconnect.py` its
  `_fetch_identity` alias; every suite now stubs the one live-Google call by patching
  `youtube.identity_for_creds`, the same seam the e2e sections already used.
  (e) ✅ DONE (code shipped to main 2026-08-01) — `youtube.complete_consent(session, channel,
  creds, allow_partial=, allow_rebind=)` is now the ONLY consent-completion sequence: verify_grant
  before anything touches disk (4b) → save_token → flip through notify.mark_connected, with a flip
  failure wrapped in `StatusFlipFailed` (carrying the verified identity) because it is the one
  outcome where state WAS changed — web callback and reconnect CLI both finish through it and only
  word the failures differently (page vs CLI hints). `finish_flow(session, channel, flow, code)`
  is the web entry: exchange, then complete_consent. Wiring pins in both suites (a counting wrapper
  around complete_consent) make a hand-resequenced reimplementation fail the suite, plus ordering
  pins: a save failure aborts BEFORE the flip (mark_connected provably never called), a flip
  failure leaves the saved token + the do-not-redo recipe, and the flip-failure page names the
  channel from the identity riding on the exception (distinct title, so a ch.name fallback fails).
  Suites: `verify_oauth_redirect.py` 64 → 66, `verify_reconnect.py` 47 → 58 checks.
  (f) ✅ DONE (code shipped to main 2026-08-07) — `youtube.SELECT_ALL_HINT` is the
  single shared phrase (`click 'Select all' ("Selecionar tudo")`); `verify_grant`'s
  partial_scopes message and `reconnect.SCOPE_REMINDER` both embed it, so a wording
  fix can't desync the two operator-facing strings. Suite: `verify_oauth_redirect.py`
  pins the constant's EN+PT wording, its presence in the partial_scopes rejection,
  and that SCOPE_REMINDER embeds the constant (no local copy).
- **caution:** (a)/(c)/(e) touch the oauth flow (HIGH, isolated commits); (b)/(d)/(f) normal.
- **acceptance:** per-item; suites stay green.

### 5. ✅ DONE (PR #7, merged 2026-07-05) `/health` endpoint — normal
- **why:** no machine-readable health signal for uptime checks.
- **approach:** add `GET /health` (no auth) returning per-channel oauth, publish-today vs budget, failed
  count, quota headroom, board-inventory days — sourced from the existing dashboard/issues services.
- **caution:** normal (additive, read-only).
- **acceptance:** `/health` returns accurate JSON; `/verify` drives it.

### 6. ✅ DONE operationally (2026-07-05/06) Analytics-scope backfill flow — normal
- **resolution:** both channels were re-consented with the `yt-analytics.readonly` scope during the
  OAuth reconnects; per-video analytics has been flowing since 07-04 (ch1 100/110, ch2 78/93 measured).
  The in-app detect-and-reconsent flow is no longer needed while both tokens hold.
- **why:** per-video analytics is `measured:0` because channels weren't consented for the analytics scope.
- **approach:** detect missing `yt-analytics.readonly` grant and surface a one-click re-consent; backfill
  once granted.
- **caution:** oauth-adjacent (HIGH) — isolated PR.
- **acceptance:** a channel missing the scope is flagged; after grant, analytics populate.

### 7. Test coverage for least-covered service modules — normal (recurring)
- **why:** broaden the safety net so future auto-changes are safer (compounds the loop's own gate).
- **approach:** pick the least-covered `app/services/*` module each cycle; add meaningful branch tests.
- **caution:** normal.
- **acceptance:** new tests pass and exercise real branches (not smoke).
- **progress:** `issues.py` (previously zero direct coverage) — `tests/verify_issues.py`
  (63 checks, 2026-07-19): all four pure helpers, every branch of the `_failed_action`
  publish-retry decision table, and `detect()` across all buckets (failed/rejected age
  gates, stuck render/publish/review timeouts, oauth/cooldown/quota-wall escalations, the
  auto-vs-needs-operator split, 24h error-signature grouping, board overflow, informational
  board_inventory excluded from totals, and the filesystem-backed BGM-pool low signal).
  `quota.py` (previously zero direct coverage) — `tests/verify_quota.py` (37 checks,
  2026-07-20): the money-path daily counters/accounting. Time helpers
  (`_next_pt_midnight_utc`/`next_quota_reset`/`_quota_day_start`/`_day_start` tz-awareness +
  forward-only) and the upload-limit-rolling-24h vs quota-Pacific-midnight branch of
  `cooldown_until_for` (case-insensitive); the DB counters against a controlled
  JobRun/Video/Topic set — the quota-day vs UTC-day boundary, kind/status/channel filters,
  `quota_spent_today` coalesce, the `published_long_today` Topic join, `in_flight_renders`,
  `last_publish_at`, `daily_limit_hit`'s `quota exceeded:%` match, and `log()` truncation.
  `topic_playlist.py` (previously zero direct coverage) — `tests/verify_topic_playlist.py`
  (32 checks, 2026-07-22): `ensure_topic_playlist`, the lazy playlist-creation choke point
  hit by both production and publish. Every branch — the three early returns that must NOT
  touch the YouTube API (None topic, already-mapped topic returns its int FK with no second
  playlist minted, non-CONNECTED channel), the create_playlist-raises path (logs one
  `playlist_add` error naming the topic, no half-written Playlist row, topic left unmapped,
  zero quota), the happy path (Playlist row with the real 34-char yt id + `last_synced_at`,
  `topic.playlist_id` mapped to the new integer FK, one `playlist_add` success logging the
  50-unit `QUOTA_PLAYLIST_INSERT`), and the `theme_prompt=None -> ""` normalization.
  `mpt_client.py` + `engines/mpt.py` (previously zero direct coverage) —
  `tests/verify_mpt_client.py` (54 checks, 2026-07-31): the render pipeline's
  HTTP seam to MoneyPrinterTurbo, driven against a real local mock of MPT's
  REST API (verify_reconnect's pattern). `build_video_params` merge semantics
  (later layer wins, per-layer None-strip vs falsy-value override — `0`/
  `False`/`""` DO override, absent `None` layers skipped, subject forced last
  over any layer, DEFAULT_PARAMS and input layers never mutated), the pinned
  MPT task-state constants render_loop/engines compare on, base-url
  rstrip + `/api/v1` normalisation, and every caller-facing HTTP contract:
  `ping`'s never-raises single-item-page liveness probe (settings page),
  `submit`'s task_id extraction / MPTError on missing-empty-null id / HTTP
  errors propagating to render_loop's log-and-skip, `poll`'s null-data → `{}`
  normalisation (engines call `.get` on it), `social_metadata`'s
  swallow-everything-to-None (dead MPT can't fail a publish; payload shape
  pinned), `list_musics`' `[]`-fallback, `local_final_path`'s
  `<dir>/<task>/final-<n>.mp4` shape, and MPTEngine's `content_format` strip
  + `{state, progress, script}` poll normalisation. Every behavior
  mutation-verified: 14 hand-built semantic mutants (None-strip removal,
  subject-before-layers, aliased DEFAULT_PARAMS, dropped task_id guard,
  swallowed submit errors, un-normalised poll, raising social_metadata,
  always-True ping, `/api/v2` prefix typo, kept trailing slash, drifted state
  constant, engine content_format leak, dropped progress coercion, wrong
  final filename) — 14/14 killed, each by its intended discriminating check
  (battery run with bytecode caching disabled after two same-size mutations
  exposed stale-pyc false kills).
  `render_loop.py` non-budget lifecycle (previously only the budget gates
  were covered) — `tests/verify_render.py` 18 → 70 checks (2026-08-02): the
  paths that guard auto-publish. Startup recovery
  (`recover_orphaned_renders`: in-process orphans re-queued clean —
  handle/progress/error reset, one JobRun each; `engine=None` counts as
  in-process, only `mpt` survives a restart and keeps its task);
  `_advance_in_flight` (timeout fails hung renders on tz-aware AND naive
  `last_attempt_at` without ever polling them — the aware leg driven via an
  in-session aware value because SQLite strips tzinfo on the round-trip; an
  engine outage leaves the video untouched for the next tick with no error
  JobRun; falsy polled progress keeps the previous value; transient engine
  errors re-queue with the handle cleared at retry_count 0 AND 1 under the
  2-retry bound — two distinct transient signatures pinned, one
  rate-limit-only — while exhausted/non-transient errors fail immediately;
  a failure without a message gets the per-engine default); `_finalize` as
  the last gate before APPROVED → publish (complete-but-missing artifact
  fails; a blank render fails but the copied artifact is kept for
  inspection, and the blank check provably judges the COPY, not the
  engine's source file; happy path pins the storage copy, thumbnail
  best-effort, script/creation_config adoption, and metadata-generation
  wiring — `language=` provably reaches the channel's real pt-BR voice
  language, not a constant None, plus never overwriting pre-set fields and
  the `metadata_generated` short-circuit; skip-gate matrix routes
  REVIEW/APPROVED with `approved_at`); the `_profile_params` /
  `_format_overrides` helpers (a corrupt profile JSON must resolve to `{}`,
  never block rendering); and `tick()`'s scheduler_paused gate. Every new
  behavior mutation-verified: 19 hand-built semantic mutants of
  `render_loop.py` run from an isolated repo copy with bytecode caching
  disabled (08-01 battery lessons applied) — 19/19 killed, each by its
  intended discriminating check, module `__file__` pinned to the copy; 3 of
  the 19 came from the adversarial review, which caught them SURVIVING the
  first battery (vacuous language pin, endpoints-only retry bound,
  aware-leg-never-ran) — checks fixed until each was killed.
  `scheduler.py` (previously zero direct coverage) — `tests/verify_scheduler.py`
  (71 checks, 2026-08-03): the APScheduler heartbeat every loop depends on —
  a regression here is a loop that silently never ticks again. `_safe`'s
  tick-crash containment (a raising tick neither propagates nor goes
  unlogged: one ERROR record naming the tick, traceback attached);
  `_music_replenish_tick`'s strictly-below-min gate counted through the REAL
  `music_gen.pool_count` (only `techno_*.wav` is pool; mp3s/foreign wavs
  aren't; missing dir = 0); `start()` driven against a real
  BackgroundScheduler — six jobs by id, each interval pinned to ITS settings
  field via distinct sentinel values (a crossed wire fails), max_instances=1
  + coalesce=True on every job, staggered first runs (metrics 30s /
  analytics 60s / autofill 45s / music 120s after boot; render/publish wait
  a full interval), UTC pinned honestly (TZ forced to Pacific/Kiritimati
  before start(), so a dropped timezone="UTC" fails even on a UTC host),
  idempotent second start(); ALL SIX job funcs driven directly, each stub
  raising its own exception class — per-loop routing, containment (kills a
  narrowed `except RuntimeError`), and the crash log naming the right tick;
  shutdown() provably STOPS the scheduler (not just clears the global),
  double-shutdown no-op, start-after-shutdown rebuilds all six. Mutation-
  verified: 26 hand-built semantic mutants run from an isolated repo copy
  with bytecode caching disabled, module `__file__` pinned — 26/26 killed,
  each by its intended discriminating check. 8 of the 26 came from the
  adversarial reviewer, which caught them SURVIVING the 52-check first cut
  (func identity/containment was proven only for the render job; shutdown
  never checked `sch.running`; the UTC pin was vacuous on UTC hosts) —
  suite extended to 71 checks until each died. Accepted residual:
  shutdown(wait=False) vs wait=True is unobservable without a mid-flight
  job (flagged as known-uncovered, not assumed covered).
  `music_gen.py` (previously only pool_count via the scheduler suite) —
  `tests/verify_music_gen.py` (525 checks, 2026-08-06): the local numpy
  techno synth + BGM pool manager that keeps the render pipeline from
  going silent. Pure helpers (`_hz_scale` octave math, `_osc` four shapes
  + unknown→sine fallback, `_add_at` end-of-buffer clip); sound
  primitives (kick/hihat/clap/bass/pad/lead float32 length + energy);
  effects (reverb/delay/filter preserve length, delay past signal is
  dry-only, section envelope mid-section gains); TECHNO_STYLES contract
  (exactly 31 presets — module comment still says 30 — every key present,
  root/scale/rhythm/hh/wave resolve against the live tables, lead=True
  implies non-empty lead_pat, unique descs); `generate_techno` (sample
  count = duration*SR, float32, peak ≤0.80, seed reproducibility, short
  flat-envelope path vs long sectional arc with intro energy < drop
  energy, rhythm none/halfstep/dotted all non-silent, all 31 styles
  synthesise clean 2s audio); `pool_count` (missing/empty → 0, only
  techno_*.wav case-insensitive, mp3/foreign-stem/nested ignored);
  `list_tracks` (three audio exts sorted, size_kb/created shape);
  `_write_wav`/`generate_and_save` (mono 16-bit 44100 PCM,
  techno_<ms>.wav under bgm_dir with mkdir -p); `replenish` (at-target
  no-op writes nothing, deficit arithmetic, target= override, raising
  generate_and_save logs error JobRun and continues — partial success).
  `metadata.py` (previously only `finalize_description` via growth/chapters)
  — `tests/verify_metadata.py` (67 checks, 2026-08-07): the publish-path
  title/description/tags choke point. `_from_meta` (title fallback + 100-char
  clamp, caption+hashtag description, `#` strip, EXTRA_TAGS always appended,
  None/empty/missing fields); `generate` MPT happy path (platform
  youtube vs youtube_shorts, language name → BCP-47 via `_LANGUAGE_MPT_CODES`,
  script=None → `""`, unknown language → en-US); MPT-None → litellm fallback
  (short vs long prompts, HARD RULE language clause, 4000-char script cap,
  fenced-JSON strip); both-dead → last-resort heuristic (subject[:100] title
  so review is never blocked); MPT empty-dict is falsy → fallback (not
  `_from_meta` of `{}`); residual `finalize_description` edges (es/unknown
  → EN CTA, case-insensitive BCP-47 prefix, None/whitespace base,
  channel-only/playlist-only, chapters already present not re-appended while
  CTA still lands, chapters-before-CTA order, 5000-char clamp after append,
  bare channel URL without `sub_confirmation=1` is NOT treated as finalized).
  Mutation-verified: 15/15 hand-built semantic mutants killed from an
  isolated copy (bytecode caching off).
  `metrics_loop.py` (previously only NeedsConnect wiring via notify) —
  `tests/verify_metrics.py` (40 checks, 2026-08-08): the daily public-stats
  probe and silent-death alert path during a publishing lull.
  `_snapshot_due` (no row / today's aware+SQLite-naive / yesterday /
  cross-channel isolation / exact-midnight boundary); `record_snapshot`
  happy path (ChannelMetric fields, 1-unit metrics success JobRun,
  get_service(slug)); missing/None/empty statistics → zeros;
  NeedsConnect → EXPIRED flip + no metric (contract pin; full alert
  semantics in verify_notify); transient skip leaves CONNECTED;
  `tick()` CONNECTED-only + due-only filter (EXPIRED/DISCONNECTED/ERROR
  never probed; second same-day tick is a no-op). Mutation-verified:
  12/12 hand-built semantic mutants killed from an isolated copy
  (bytecode caching off, module `__file__` pinned).
  `analytics_loop.py` (previously only `_publish_reserve` + NeedsConnect
  wiring via notify) — `tests/verify_analytics.py` (89 checks, 2026-08-09):
  the per-video Analytics snapshot pass the growth agent steers by.
  `_snapshot_due` (no row / today's aware+SQLite-naive / yesterday /
  cross-video isolation / midnight boundary); `_mature` (None / young /
  ≥24h boundary / SQLite-naive); `record_video_snapshot` happy path
  (VideoMetric fields, 2-unit success JobRun, published_at→created_at
  date range, traffic fetched only when views>0, empty sources leave
  traffic_json None, traffic exception best-effort); generic failure →
  None + error JobRun (quota_cost=0); QuotaExceeded propagates;
  `_dead_token_error` (NeedsConnect / healthy / transient); `_snapshot_channel`
  (immature/not-due/no-yt-id/non-PUBLISHED filters, newest-first order,
  force bypasses due, first hard-fail aborts, mid soft-fail continues,
  QuotaExceeded mid-pass breaks, pre-emptive quota-cap stop, dead-token
  flip vs missing-scope skip); `tick()` CONNECTED+yt_channel_id filter
  and scheduler_paused no-op. Mutation-verified: 15/15 hand-built
  semantic mutants killed from an isolated copy (bytecode caching off).
  `thumbnail.py` (previously only palette identity via storyboard) —
  `tests/verify_thumbnail.py` (87 checks, 2026-08-10): the publish-path
  custom-thumbnail CTR lever (HyperFrames card → ffmpeg still → PNG).
  Module contracts (1280x720 out, 1920x1080 render, palette IS
  theme.PALETTE, 240s timeout); `_hook_text` (LLM happy path, quote/
  multi-line strip order, word-count 2–8 + ≤60-char gates + inclusive
  bounds, exception → title fallback, title-over-subject, whitespace-
  title strips to sentinel without consulting subject, empty →
  "Watch This", content_format short/long prompt pin, max_tokens=100);
  `_thumbnail_html` (dimensions, accent/bg injection, HTML-escape of
  the hook slot, gsap timeline shell); `_render`/`_extract_frame`
  command contracts (npx hyperframes@version, quality/quiet/env pins,
  ffmpeg scale=1280:720 + -ss 0.4, nonzero and missing-out raises);
  `make_thumbnail_png` (happy path writes gsap+index.html, topic_id
  palette selection + wrap, content_format forward, any failure →
  None never raises). Mutation-verified: hand-built semantic mutants
  killed from an isolated copy (bytecode caching off).
  `video_gen.py` (previously only language helpers via verify_growth;
  `generate_ideas` zero direct coverage — autofill only stubs it) —
  `tests/verify_video_gen.py` (100 checks, 2026-08-11): the title/idea
  choke point shared by autofill, topics API, and trends API. Module
  contracts (pt/en/es voice tables + BCP-47 pairs); `language_from_voice`
  / `code_from_voice` (None/empty, known voices, case-insensitive
  prefix, unknown → None, first-segment-only); `channel_language` /
  `channel_language_code` (None id, missing channel, unbound/missing
  profile, corrupt JSON swallow, empty params_json, happy path for
  pt/en/es); `generate_ideas` (short vs long prompt branches, language
  HARD RULE — the 07-07 EN-on-PT incident fix — theme_prompt guidance,
  existing-title avoid list last-60 window + '(none yet)', bullet/
  number/quote strip, case-insensitive dedupe existing+within-response,
  n cap incl. n=0 and n=-1 max(0,n) guard — the 07-26 overshoot,
  empty/None LLM content, litellm model + drop_params pins, forbidden-
  opener brief pins). Mutation-verified: 20/20 hand-built semantic
  mutants killed from an isolated copy (bytecode caching off).
  `engines/worker.py` (previously only `_has_visible_frames` / `_looks_valid`
  / `_creation_config` via storyboard) — `tests/verify_worker.py` (198
  checks, 2026-08-13): the HyperFrames render pipeline run_job executes
  on a daemon thread. Module contracts (aspects, accent==theme.PALETTE,
  `_esc is theme.esc`, five templates, 1800s timeout, bundled gsap);
  `_word_count_bounds` short band vs long floor/cap; `_generate_script`
  with `_llm` stubbed (short vs long prompts, R7 spoken-CTA, 07-07 HARD
  RULE language, quote-strip, out-of-band retry / empty-retry keeps
  original); `_pick_template` subject-hash; `_clips_from_json` unwrap +
  clamps; `_validate_clips` overlap/bounds; `_assemble_composition` +
  every template `_looks_valid` (HTML-escape, w=1/2/3 styles); fallback
  / `_key_lines`; `_voice` gender-suffix; `_creation_config` never-raises
  + volume-0 pin; `_pick_bgm` explicit-off / techno-prefer / named /
  handle-hash; `_probe_duration` / `_has_visible_frames` subprocess-
  stubbed (dea9405 blank class); `_render` / `_mux` command contracts;
  `_generate_composition` storyboard vs legacy vs exception→"";
  `run_job` happy / unknown-aspect / invalid-HTML fallback / render-
  raise rebuild / blank-frame rebuild / STATE_FAILED. Mutation-verified:
  hand-built semantic mutants killed from an isolated copy (bytecode
  caching off, module `__file__` pinned).
  `engines/hyperframes.py` (previously zero direct coverage; only MPT's
  engine adapter was covered) — `tests/verify_hyperframes.py` (66 checks,
  2026-08-12): the local HTML/CSS→MP4 render adapter render_loop polls.
  Module contracts (name, STATE_* identity with base, final_path shape,
  get_engine registry wiring); `_job_dir`/`_status_path` under
  `settings.hyperframes_storage_dir`; `write_status` create/merge-preserve/
  corrupt-JSON recovery/explicit None clear; `poll` missing+corrupt →
  PROCESSING/0 (never failed), happy-path field forwarding including
  error+creation_config, progress int coercion + null/missing→0, missing
  state→PROCESSING, STATE_FAILED error string; `submit` 32-char hex handle,
  job dir + initial status, daemon thread args (handle/job_dir/subject/
  params COPY by identity), unique handles, pre-mux final.mp4 absent;
  write_status→poll round-trip. Mutation-verified: 17/17 hand-built
  semantic mutants killed from an isolated copy (bytecode caching off,
  module `__file__` pinned).
  `engines/storyboard.py` (previously only parse/align/validate smoke +
  palette identity + all-types HTML scrape — 40 checks against 1031 lines)
  — `tests/verify_storyboard.py` 40 → 207 checks (2026-08-14): the typed-
  beat composition engine HyperFrames renders. Module contracts (4..14
  beats, 0.12 gap, 0.5 min-dur, 2.0/1.8 tail/mid floors, 1.1s row-step,
  5.5s drift, `_RENDERERS` == `_BEAT_SPECS`); clip helpers including the
  07-09 `_code_line_clip` indent pin; `_coerce_beat` every type +
  salvage/None (w-clamp, highlight ints-only, diagram layout fallback,
  cta default "Subscribe", 5-item list cap); parse fenced/prose unwrap +
  `_MAX_BEATS` clamp; `theme.fold` PT diacritics + `_find_subseq` empty-
  needle; align tail/mid floor (07-16 bunched-close) + interpolation +
  tiny-clip infeasible floors; `_wrap` R4 drift + last-beat no-fade;
  `render_list` 07-29 step cap (last row at 2.45s not 7.24s); numeric
  vs non-numeric `render_stat`; rendered code indent + `hl` class;
  `_diagram_svg` fanout `<path>` vs pipeline `<line>` + portrait viewBox
  760; `_follow_verb` EN/PT/ES; `_variety_ok` ≥2-rich floor; prompts
  (rule 2b only when Phase-B allowed, PACING 8s DRAG, spoken-ask
  ending); `compose()` stubbed-llm (CTA force, language, unparseable
  exactly-2-calls, variety retry, default allowlist drops code,
  validate-fail even-space fallback). Mutation-verified: 20/20 + 4/4
  review-derived semantic mutants killed from an isolated copy
  (bytecode caching off, module `__file__` pinned).

### 8. ✅ DONE (code shipped to main 2026-07-29) Remove the basic-auth-on-callback smell + document reconnect — normal
- **resolution (2026-07-29):** `app/main.py`'s `basic_auth` middleware exempts exactly
  `GET /api/channels/{[0-9]+}/oauth/callback` (fullmatch on a compiled regex, GET only),
  alongside the existing `/health` exemption. Safe because the callback authenticates
  itself: it acts only when `state` matches a pending flow (unguessable, single-use,
  30-min TTL — the 4c(a) registry), and a stateless hit renders the failure page without
  touching tokens or status; minting flows (`POST …/oauth/start`) stays auth-guarded.
  README's reconnect tip now says the callback hop is deliberately password-exempt.
  Suite: `tests/verify_health.py` 25 → 36 checks (callback reachable + inert without
  auth; POST / oauth-start / non-numeric id / Unicode digit / out-of-int64 id / longer
  path / prefix-only path all still 401 — the exemption cannot silently widen).
  The id pattern is `[0-9]{1,18}`, not `[0-9]+`: review found that an id past SQLite's
  int64 range makes `session.get` raise, so an unbounded pattern would let an anonymous
  caller turn each request into a 500 with a ~19KB traceback in the log (measured).
- **prerequisite found while shipping this:** the middleware matched exemptions against
  `request.url.path`, which Starlette rebuilds from the **Host header** — so any
  exemption was bypassable with `Host: h/health?` (any route, no credentials). Fixed
  first, in its own commit (`af0e5db`), by matching the routed `scope["path"]`; this
  item's exemption is built on that.
- **why:** the OAuth callback path goes through Basic Auth, which complicates browser reconnects.
- **approach:** exempt the `/oauth/callback` path from the basic-auth middleware (safe: it validates
  `state`), and document the reconnect flow in `docs/`.
- **caution:** touches `app/main.py` auth (HIGH) — isolated PR + test that the callback path is reachable
  without auth while everything else still 401s.
- **acceptance:** callback reachable post-consent without Basic Auth; all other routes still guarded.

### 9. ✅ DONE (PR #8, merged 2026-07-06) Fix parallel-append conflicts on the cycle log — normal
- **why:** the playbook appends one line per cycle to `run/code-experiments.jsonl`; two in-flight
  code-agent PRs both append after the same line and collide on merge.
- **approach:** add `.gitattributes` with `run/code-experiments.jsonl merge=union` so git keeps both
  sides' appended lines automatically.
- **caution:** normal (repo config; no runtime surface).
- **acceptance:** a two-branch append merges without conflict, both lines retained.

### 11. ✅ DONE (code shipped to main 2026-07-24) R7 spoken-CTA experiment (SUBSCRIBER OFFENSIVE) — normal (high value)
- **resolution (2026-07-24):** both script prompts (`worker._generate_script` short + long) now
  mandate a FINAL spoken line that is an EXPLICIT follow/subscribe ask (imperative, in the
  script's language) welded to a named next-value — a tease without the ask fails, generic
  'like and subscribe' without the next-value fails. The storyboard side aligns: the cta
  type-doc tells the LLM to anchor the cta cue on the closing ask and make `sub` the SAME
  promise the narrator speaks, and system-prompt rule 6's ending plan was re-pointed from
  "~8-10 words before the end" to the ask's first words (~10-16; found by adversarial review —
  the stale higher-priority rule would have cued the card mid-ask). Gated on 3 draft renders
  (EN + 2 PT): asks natural + in-language ("Follow now, because tomorrow I'm showing you the
  chunking fix…" / "Segue o canal, porque amanhã eu mostro…"), visual sub = spoken promise
  ("Tomorrow: the chunking fix"), openers/pacing intact (min beat ≥1.91s, cta ≤7.07s < 8s drag
  line). Experiment logged in `run/experiments.jsonl` predicting `subscribers_gained` up.
  Residuals: `_words_clip` silently truncates an over-long cta `sub` at 6 words (log it if it
  bites); short-form guard stays [50,140] — at `paragraph_number`≥3 the mandated extra line can
  push compliant scripts past 140 and force a retry (default n=2 lands ~100w, clear of both).
- **why:** R7's signal is literally `subscribers_gained` and it has never been tested; narration has
  no follow-ask at all. The 5 videos that ever gained subs all delivered deep specific value — a
  contextual one-line ask at the close converts exactly that moment.
- **approach:** `worker.py` script prompts (short `:435`, long `:421`): add a final-line directive —
  one contextual, non-generic follow ask tied to the value just delivered, in the channel language
  (e.g. "Sigo publicando isso todo dia — inscreve-te pra não perder a parte 3"). Align the visual
  CTA beat sub-text. Ship as a gated experiment logged in `run/experiments.jsonl` predicting
  `subscribers_gained` up.
- **caution:** normal (prompt change; render-judge gate).
- **acceptance:** golden-set renders show the ask in-language, natural, ≤1 line; experiment logged.

### 12. ✅ DONE (code shipped to main 2026-07-26; inert until windows are set) Publish windows — audience-peak drip (SUBSCRIBER OFFENSIVE) — HIGH
- **resolution (2026-07-26):** `Channel.publish_windows` ("HH:MM-HH:MM,…", start inclusive / end
  exclusive, past-midnight wrap supported) + `Channel.publish_tz` (IANA name, unset = UTC), checked
  by `publish_loop._window_ok` in `tick()` right after the cooldown gate (before the DB-aggregate
  guards, so an out-of-window channel costs nothing). Unset = drip-whenever (today's behavior) —
  **the feature ships inert; activating it is the growth agent's / operator's call:**
  `PATCH /api/channels/{id}` with e.g. `{"publish_windows": "12:00-13:30,19:00-20:30",
  "publish_tz": "America/Sao_Paulo"}` (ch2 ≈ 12:00 & 19:00 BRT; ch1 ≈ 9:00-12:00 ET per the
  campaign analysis). The PATCH rejects malformed specs/tz names with a 400; a bad value that
  reaches the DB anyway FAILS OPEN in the loop (publish anytime + one warning) — a typo must never
  silently stall a channel. `/api/videos/publish-plan` mirrors the gate via
  `publish_loop.next_window_open`, so board ETAs land inside windows (found in review — the ETA
  endpoint mirrors every tick() gate). Suite: `tests/verify_publish.py` 25 → 70 checks incl. a real
  `tick()` blocked-outside/publishes-inside pair. Accepted residuals: a window wholly inside a DST
  spring-forward gap publishes nothing that one day (audience-peak windows don't sit at 2-3am);
  a swapped "19:00-18:30" reads as a valid ~23.5h wrap window (degrades toward publishing).
  Native `publishAt` scheduling remains future work.
- **why:** publishing is drip-whenever; small channels get their best algorithmic test in the first
  hours, so publishing at audience-dead hours wastes it.
- **approach:** per-channel allowed publish windows (ch2 ≈ 12:00 & 19:00 BRT; ch1 ≈ 9:00–12:00 ET) as
  channel fields checked in `publish_loop.tick` alongside `_drip_ok`; native `publishAt` scheduling
  later.
- **caution:** touches `publish_loop.py` (HIGH) — isolated commit + regression test in
  `tests/verify_publish.py`.
- **acceptance:** videos only publish inside the window; test proves the gate; drip otherwise unchanged.

### 13. ✅ DONE (code shipped to main 2026-07-25) Long-form chapters in descriptions (SUBSCRIBER OFFENSIVE) — normal
- **resolution (2026-07-25):** `app/services/chapters.py` derives `M:SS <headline>` chapter lines at
  publish time from the beat divs the storyboard bakes into the render job dir's `index.html`
  (`storage/hyperframes/<handle>/` outlives the render, so the already-approved long-form bench gets
  chapters too; MPT/fallback renders quietly get none). Per-beat-type headline extraction against
  the real renderer markup — stat count-up placeholder "0" never titles a chapter, the cta card is
  skipped, angle brackets transliterate to ‹ › (the YouTube API rejects raw </> in descriptions —
  found by the pre-ship review; a 400 would strand the video with the bad description already
  committed). YouTube's chapter-render rules enforced: first at 0:00, ≥10s spacing on displayed
  seconds, ≥10s final chapter, ≥3 survivors else no block at all.
  `metadata.finalize_description(chapter_lines=…)` inserts the localized "⏱ Capítulos:/Chapters:"
  block before the CTA links, idempotent across publish retries; `publish_loop._publish_one` wires
  it for `content_format=long` only, best-effort (a chapters surprise can never fail a publish).
  Suite: `tests/verify_chapters.py` (30 checks, coupled to the real `build_index_html` markup).
  Residual (accepted, pre-existing mechanism): the 5000 description cap clips chars not bytes, and
  a near-cap description can truncate the chapters/CTA tail — unreachable at today's ≤800-char
  captions.
- **why:** chapters lift long-form retention and search; beat timings already exist in the storyboard.
- **approach:** derive `MM:SS <beat headline>` lines from storyboard beat starts at metadata/publish
  time for `content_format=long`; append to description before the CTA block.
- **caution:** normal.
- **acceptance:** a long video's description carries valid ascending chapters; YouTube renders them.

### 14. ✅ DONE (tool shipped to main 2026-07-28; live run = operator step) ch2 back-catalog backfill tool (SUBSCRIBER OFFENSIVE) — normal
- **resolution (2026-07-28):** `run/backfill_ch2_metadata.py`, two-step and operator-in-the-loop
  because it mutates live published videos. Ground truth first: **115** ch2 published videos
  (06-14→07-11, everything before Phase 1's language fix 9c9a8f7) lack `defaultLanguage`/
  `defaultAudioLanguage` and the localized CTA/links block — but their DB/live titles are mostly
  already PT (MPT wrote PT even under the en-US directive), so "regenerate everything" would churn
  proven titles. The tool therefore defaults to **retag_only** (keep live title/tags; add pt-BR
  language tags + `finalize_description`'s CTA/links block, chapters for long-form) and proposes
  **regenerate** (fresh PT-BR via `metadata.generate`) only when live title+description show no PT
  signal (looks_en heuristic, ≥2-stopword threshold). Plan mode (default) is strictly READ-ONLY:
  ranks by latest measured views, fetches LIVE snippets (`videos.list`), writes a reviewable plan
  file (`run/agent-reports/backfill-ch2-metadata-plan.json`; edit `proposed`, set `skip:true`).
  `--apply` replays the reviewed plan: re-fetches live, repairs records without a second update if
  the video is already localized (crash resume), skips on title/description drift, merges onto the
  live snippet (categoryId preserved), `videos().update` (50u), hard cap 10/quota-day counted via
  prefixed JobRuns, DB row synced, plan stamped per entry. Suite: `tests/verify_backfill.py`
  (48 checks: read-only plan pin, cap + negative-limit clamp, drift, repair, quota-day boundary).
  **Operator step:** `PYTHONPATH=. uv run python run/backfill_ch2_metadata.py` → review the plan →
  `--apply` (≤10/day, ~2 days for the top 20). Accepted residuals: description/tag caps are chars
  not bytes (shared pre-existing mechanism, backlog 13 residual); two concurrent `--apply` runs
  could double the cap (single-operator CLI).
- **why:** ~20 top ch2 videos carry EN-biased metadata from the hardcoded en-US era; they're the
  channel's best assets and undiscoverable in PT.
- **approach:** one-shot script (`run/backfill_ch2_metadata.py`): regenerate title/description in
  PT-BR (`metadata.generate` with language), re-apply via `videos().update` (≈50u each, ≤10/day to
  respect quota), `finalize_description` links included; dry-run mode first; log each change.
- **caution:** touches live published videos — dry-run + operator-reviewed list before the real run.
- **acceptance:** top-20 list updated over ~2 days; titles/descriptions visibly PT-BR on YouTube.

### 10. ✅ DONE (code shipped to main 2026-07-09) Surface process-slot exhaustion before it breaks the pipeline — normal
- **resolution (2026-07-09):** `GET /health` now includes `system.processes` (`count`/`max`/`pct_used`
  from `kern.maxproc` + a `ps -A` count via two cheap subprocess reads); `status` flips to `degraded`
  at ≥85% usage. A failed reading (sysctl/ps unavailable — the exact failure mode being watched for)
  returns `None` rather than crashing `/health` or flipping status. Regression suite:
  `tests/verify_health.py` (mocked-reading + real-reading checks).
- **why:** on 2026-07-06 the Mac ran out of process slots ("fork: Resource temporarily unavailable")
  mid-supervisor-run; rendering/publishing spawn subprocesses, so exhaustion silently threatens the
  drip. It recovered on its own, but nothing would have alerted anyone.
- **approach:** add a `system` block to `GET /health` (process count via `len(psutil.pids())` or
  `os.listdir('/proc')`-equivalent — on macOS use `sysctl kern.maxproc` + a cheap `ps` count or
  `psutil` if already a dep; degrade status when usage >85%). Keep it dependency-light.
- **caution:** normal (additive, read-only) — but measure without forking if possible (the failure
  mode is precisely that forking stops working).
- **acceptance:** /health reports process headroom and flips to degraded at the threshold; verified
  by mocking the reading.

### 15. ✅ DONE (code shipped to main 2026-07-27; tz diagnosis refuted — real cause was the in-flight gate) Unify job-timestamp timezone in budget accounting — normal
- **resolution (2026-07-27):** the symptom was real (8 rendered vs a 5/day budget on both channels
  in the 07-26 00:00–00:27 UTC burst) but the timezone diagnosis was wrong: `JobRun.created_at`
  rows are stored in **UTC**, not local time — verified against ground truth (video 751's render
  JobRun reads `2026-07-27 00:06:06` and its `video.mp4` mtime is `2026-07-26 21:06:01` GMT-3,
  the same instant; `models.utcnow()` is tz-aware UTC and SQLite stores the UTC wall time).
  Audit of every `created_at >=` comparison in `quota.py`: all `since` values are tz-aware UTC
  wall times, consistent with storage — no naive-vs-UTC mismatch exists anywhere. The **real
  bug**: `render_loop._submit_new` gated on `rendered_today >= budget`, i.e. *completed* renders
  only, so with `render_concurrency=4` it started a new render after each success while 4 were
  still in flight — submission only stopped once 5 had COMPLETED, by which point 3 more were
  running: budget+concurrency−1 = 8. (`_auto_produce` already subtracted in-flight work from its
  headroom; the submit gate didn't.) Fix: `quota.in_flight_renders` takes an optional
  `channel_id`, and the gate is now `rendered_today + in_flight_renders(channel) >= budget`.
  The pre-ship review found `/api/videos/queue-plan` (which documents that it mirrors this
  gate for board card labels) still computed `slots_today` from completed renders only — it
  now subtracts in-flight too, and its budget-full reason spells out `done+rendering/budget`.
  Suites: `tests/verify_render.py` 10 → 18 checks (incident replay pins exactly 5 renders at
  budget 5 / concurrency 4 — was 8 before the fix; within-tick oversubmission at
  concurrency > budget; per-channel isolation with a budget small enough to catch a
  global-count regression; the queue-plan mirror), `tests/verify_quota.py` 37 → 39.
- **why (observed 2026-07-26):** both channels rendered **8 videos against a 5/day render budget**
  in the 00:15–00:24 UTC burst. `JobRun.created_at` rows are written in local time (GMT-3: the
  00:15 UTC renders show as `2026-07-25 21:15`) while `quota._day_start()` returns UTC midnight
  and `_count` compares `created_at >= since` directly — so for the first 3 hours of every UTC
  day the render-budget counter reads renders as "yesterday" and undercounts, letting the loop
  overshoot the budget. Harmless to YT quota (renders are a local throttle) but it wastes compute,
  distorts `rendered_today` in `/api/agent/state`, and the same naive-vs-UTC comparison pattern
  may bite any other `_count`-based gate.
- **approach:** store all JobRun timestamps as UTC (or make `_day_start`/`_quota_day_start`
  produce naive-local datetimes consistently); audit every `created_at >=` comparison in
  `quota.py` / `issues.py`; add a regression check with a frozen clock around the midnight edge.
- **caution:** normal — but verify publish accounting (`_quota_day_start`, Pacific midnight)
  still lines up with YouTube's quota reset after the change; publish drip is a money path.
- **acceptance:** a render jobrun written at 00:15 UTC counts toward that UTC day's
  `rendered_today`; nightly burst never exceeds `daily_render_budget`; verify suite covers the
  midnight boundary.

### 16. ✅ DONE (code shipped to main 2026-07-30) Contain the SPA fallback's file reads (path traversal) — HIGH
- **resolution (2026-07-30):** `main._contained_file(root, rel)` is the containment
  choke point: it resolves `root / rel` and returns it only if `is_relative_to(root)`,
  `is_file()`, and `os.access(R_OK)`; any `OSError`/`ValueError`/`RuntimeError` returns
  None so the SPA route falls through to index.html instead of 500ing. `spa()` resolves
  the dist root **per request** and routes every read through it. Containment is checked
  by RESOLVING, never by scanning the input for "..", because there were **three** escape
  primitives, not one: (1) `..` hops (uvicorn percent-decodes the target before routing,
  so `/%2e%2e/x` and `/%2e%2e%2fx` both arrive as real parent hops); (2) an **absolute**
  `rel` — found while shipping, not in the original write-up — where
  `Path("dist") / "/etc/hosts"` *is* `/etc/hosts` because pathlib drops the left operand,
  so `//etc/hosts` escaped with no `..` at all, and the intuitive "reject any '..'" fix
  would have left it wide open (mutation-verified: that mutant is caught only by the
  absolute-path checks); (3) a symlink inside dist pointing out of it, which `resolve()`
  collapses and rejects. The filesystem-error guard wraps **both** `resolve()` and
  `is_file()` — a 5000-char segment raises `OSError` from the `stat()` inside `is_file()`,
  which would have handed an anonymous caller a 500 plus a traceback-sized log write per
  request (same class as item 8's out-of-int64 id finding); the new test caught that
  during development, and each of the three guarded exception types is now provoked by a
  different pinned check. The `/assets` StaticFiles mount was verified NOT vulnerable
  (Starlette already contains it — 404s on every vector), so the fix stayed confined to
  the fallback. Suite: `tests/verify_health.py` 36 → 62 checks; every check was
  mutation-verified against 9 broken implementations, and `/nested/deep.js` is the one
  that discriminates route wiring (without it, a `spa()` that never calls the helper —
  a total static-file outage — passed the whole suite).
  Measured impact on the real layout, pre-fix: both channels' `credentials/*/token.json`
  (Google refresh tokens), both `client_secret.json`, `.env`, and the whole 5MB
  `manager.db` were all readable through this route; post-fix all six are refused while
  all real Vite dist files still serve and every deep client-side route still falls
  through. Two review findings fixed pre-ship beyond the traversal itself: the dist root
  is resolved per request rather than pinned at import (a pinned root would serve a
  **stale** index.html while new hashed assets fell through into it if a deploy ever
  atomic-swaps a `dist` symlink — a regression vs the old code, which followed the link),
  and any `index.html` spelling now carries `_NO_CACHE` (resolving first widened the set
  of spellings reaching the cacheable asset branch, contradicting the no-cache invariant
  the surrounding comment exists for). Accepted residuals: containment is by path so a
  **hardlink** inside dist to an outside file is still served (needs local write access to
  dist, which already means owning the SPA); and starlette's `FileResponse` re-stats, so a
  file deleted between the check and the send yields a 500 (pre-existing TOCTOU, unchanged).
- **why (found by the pre-ship review of item 8, 2026-07-29):** the SPA catch-all
  `@app.get("/{full_path:path}")` in `app/main.py` does `candidate = _dist / full_path` and
  returns `FileResponse(candidate)` for any `candidate.is_file()`, with no check that the
  resolved path stays inside `_dist`. uvicorn percent-decodes the target before routing, so
  `GET /%2e%2e/%2e%2e/etc/passwd` reads arbitrary files the manager's user can read —
  `credentials/*/token.json` included. Today it sits behind Basic Auth (and item 8's exemption
  cannot reach it — the regex only admits the callback path), so the exposure is: anyone who
  can already authenticate, and **anyone at all when `MANAGER_APP_PASSWORD` is unset**, which
  is the documented LAN-access configuration. Pre-existing, unrelated to item 8.
- **approach:** resolve and contain before serving — `candidate = (_dist / full_path).resolve()`,
  serve only if `candidate.is_relative_to(_dist.resolve())` and is a file, else fall through to
  `index.html`. Consider `follow_symlinks=False` semantics for the dist dir too.
- **caution:** touches `app/main.py` (HIGH) — isolated commit + regression test.
- **acceptance:** `/%2e%2e/%2e%2e/etc/passwd` and `/../.env` return index.html (or 404), never
  file contents; real hashed assets and deep client-side routes still serve correctly; a check
  in `tests/verify_health.py` pins traversal attempts with auth disabled.

### 18. ✅ DONE (code shipped to main 2026-08-15) Overnight render deaths retry instead of dying — normal
- **resolution (2026-08-15):** `_retry_or_fail` is the single re-queue/fail choke
  point for both the loop wall-clock timeout and engine-reported failures.
  A hang past `render_timeout_seconds` now re-queues (same `retry_count < 2`
  budget as a 529) instead of writing terminal `FAILED` / `"render timed out"`.
  `_TRANSIENT` also matches the 08-13 shapes that slipped through: `litellm.Timeout`,
  `timed out`, `InternalServerError`, `Server disconnected`. Ground truth from
  `manager.db` (read-only): 16 of 18 failed videos since 08-12 were the wall-clock
  path at retry_count=0; the other two were v956 (litellm 600s) and v962 (Anthropic
  disconnect), both at 5% script-gen. Suite: `tests/verify_render.py` — timeout
  now pins QUEUED + midpoint + spent-budget FAILED; v956/v962 shapes plus four
  isolated-signature pins (a dropped token cannot hide behind the multi-match
  strings). `issues.TRANSIENT_SIGNATURES` updated in lockstep (its own comment
  requires the mirror) so the digest flags the new shapes as `transient`.
  Suite 89 → 107 checks. Review fixes: poll-before-timeout so a just-finished
  COMPLETE/FAILED is not abandoned as a hang; bare `"timed out"` dropped so
  CLI TimeoutExpired stays terminal; `_submit_new` pin so retry is not a
  status-only flip.
- **why (observed 2026-08-12..14):** overnight HyperFrames renders died permanently
  on LLM/provider blips and on the 40-min loop cap, burning the next day's bench.
  The existing transient list only caught 529/503/rate-limit.
- **caution:** normal (render_loop, not the money-path files). Isolated commit +
  regression tests. Did **not** raise `render_timeout_seconds` — retrying a hang
  is the fix; a longer cap is a separate policy change.
- **acceptance:** a timed-out render with retry_count<2 returns to QUEUED (handle
  / progress / error cleared); retry_count=2 still FAILs with `"render timed out"`;
  v956/v962 error strings re-queue; ffmpeg-class errors still fail immediately.

### 17. ✅ DONE (code shipped to main 2026-08-04) Audit JobRuns for the review-gate transitions (reject / requeue / retry / approve) — normal
- **resolution (2026-08-04):** each of the four endpoints logs one `quota.log` JobRun riding the
  same commit as the status flip — kind = the transition, `status="success"`, video/channel ids,
  detail tagged `via API` and carrying the PRE-transition status (`"rejected via API: review ->
  rejected; reason='…'"`); reject embeds the reason `!r`, retry names its branch (`re-publish` on
  the has-artifact path, `re-render` otherwise). Refused calls (404s, approve's 409) write nothing
  — every raise precedes the log, and `get_session` never commits a dirty session. New kinds
  verified inert everywhere: quota.py counters filter on exact kinds, issues.py only groups
  `status=="error"` rows, the Dashboard renders `r.kind` as plain text. Response shapes unchanged
  (pinned per endpoint). Suite: `tests/verify_lifecycle_audit.py` (60 checks) — refusal-writes-
  nothing per endpoint, exact per-kind final counts, plus adversarial-review hardening: a second
  channel exercises all five log sites cross-channel (a hardcoded `channel_id=1` mutant survived
  the single-channel first cut) and both retry details pin the pre-transition status ("failed" —
  the target-status substring was vacuously present in the template; both mutants re-run and
  killed from an isolated copy, bytecode caching off). E2E-verified against a real scratch
  uvicorn + scratch DB: all four transitions driven over HTTP, exactly 4 rows in `/api/runs`,
  refusals wrote none. Accepted residual (review finding): a reject reason longer than ~950 chars
  truncates in the JobRun detail (quota.log's 1000-char clamp); the full reason still lands on
  `video.rejected_reason`, though approve clears it and delete's JobRun doesn't carry it.
- **why:** the 2026-08-02 forensics concluded "every video lifecycle mutation writes a JobRun" —
  that was overbroad. Produce (`4e673ae`), delete (07-27), and the loop transitions are covered,
  but `jobrun` contains ZERO rows of kind reject/requeue/retry/approve ever (verified 2026-08-03:
  `SELECT kind, count(*) FROM jobrun GROUP BY kind`). Reject at least leaves `rejected_reason` on
  the video row; requeue/retry/approve leave no trail at all, so the next operator-vs-agent
  forensic on those paths starts blind again (both fires this fortnight were unlogged manual actions).
- **approach:** mirror `4e673ae`: in the four endpoint handlers write one JobRun per video
  (kind = the transition, detail tagged `via API`, video/channel ids), no-op calls (refused
  transitions) write nothing. Extend `tests/verify_produce_audit.py`'s pattern into a
  `verify_lifecycle_audit.py` covering all four + the refusal cases.
- **caution:** endpoint files only, no loop changes; keep the response shape unchanged.
- **acceptance:** each of the four transitions on a real row writes exactly one JobRun with the
  right kind/ids/tag; refused calls write none; suite green; live probe shows the row in /api/runs.
