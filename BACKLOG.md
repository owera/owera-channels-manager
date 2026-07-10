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

### 2. Self-healing OAuth alert on token expiry — HIGH
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

### 4. Bake the loopback reconnect helper into the app — normal
- **why:** reconnect currently needs an ad-hoc external script; make it first-class.
- **approach:** add an endpoint/CLI that runs the localhost-loopback consent flow and writes the token,
  bypassing the portal-Host and basic-auth-callback issues. Reuse `youtube.build_flow` / `finish_flow`.
- **caution:** touches oauth (HIGH) — isolated PR.
- **acceptance:** documented one-command reconnect; `/verify` shows a channel going connected.

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

### 7. Test coverage for least-covered service modules — normal
- **why:** broaden the safety net so future auto-changes are safer (compounds the loop's own gate).
- **approach:** pick the least-covered `app/services/*` module each cycle; add meaningful branch tests.
- **caution:** normal.
- **acceptance:** new tests pass and exercise real branches (not smoke).

### 8. Remove the basic-auth-on-callback smell + document reconnect — normal
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

### 11. R7 spoken-CTA experiment (SUBSCRIBER OFFENSIVE) — normal (high value)
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

### 12. Publish windows — audience-peak drip (SUBSCRIBER OFFENSIVE) — HIGH
- **why:** publishing is drip-whenever; small channels get their best algorithmic test in the first
  hours, so publishing at audience-dead hours wastes it.
- **approach:** per-channel allowed publish windows (ch2 ≈ 12:00 & 19:00 BRT; ch1 ≈ 9:00–12:00 ET) as
  channel fields checked in `publish_loop.tick` alongside `_drip_ok`; native `publishAt` scheduling
  later.
- **caution:** touches `publish_loop.py` (HIGH) — isolated commit + regression test in
  `tests/verify_publish.py`.
- **acceptance:** videos only publish inside the window; test proves the gate; drip otherwise unchanged.

### 13. Long-form chapters in descriptions (SUBSCRIBER OFFENSIVE) — normal
- **why:** chapters lift long-form retention and search; beat timings already exist in the storyboard.
- **approach:** derive `MM:SS <beat headline>` lines from storyboard beat starts at metadata/publish
  time for `content_format=long`; append to description before the CTA block.
- **caution:** normal.
- **acceptance:** a long video's description carries valid ascending chapters; YouTube renders them.

### 14. ch2 back-catalog backfill tool (SUBSCRIBER OFFENSIVE) — normal
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
