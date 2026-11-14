# LLM hard guard: no external-workspace Google data in an LLM prompt

Status: CTO/CoS, 2026-11-13. Applies until CM moves off the consumer `grok login`
session to an xAI Enterprise API key.

## Why

Every CM LLM call goes through the Grok CLI logged in with a consumer account
(`grok login`). Its policy allows training on prompts. Google's API Services User
Data Policy (Limited Use) forbids sending Google user data (YouTube Data API,
YouTube Analytics, channel and comment content pulled with the buyer's OAuth
token, the growth agent's analytics) to a model that may train on it.

So **only Owera's own workspaces may reach the LLM**. Everything else fails closed.

## Allowlist

| Env | Format | Default |
|---|---|---|
| `MANAGER_LLM_INTERNAL_WORKSPACE_IDS` | comma-separated workspace ids, e.g. `1` | `1` |
| `MANAGER_AGENT_RUN_WORKSPACE_IDS` | comma-separated workspace ids the headless agents may observe and act on | `1` |

* `1` is workspace slug `owera` ("Owera (legacy default workspace)"), which holds
  ch1 Owera Software and ch2 Rodrigo Recio. Read-only on the claw0 DB, 2026-11-13.
* `2` (`tenant-b`, ch3 O4 Dogfood and ch4 elen3x) is the O3/O4 external-tenant
  test and is **not** internal by default.
* Empty or malformed value → no workspace allowed (everything blocked). It never widens.
* Stripe-provisioned buyer workspaces are never on the list.
* `MANAGER_AGENT_RUN_WORKSPACE_IDS` scopes growth / code / run-check. Default `1`.
  It does not widen the LLM allowlist: an id that is not also on
  `MANAGER_LLM_INTERNAL_WORKSPACE_IDS` is dropped (logged
  `llm_blocked_external_google_data reason=dropped_from_agent_run`) and never
  enters the agent context. If the effective set is empty, the run is refused
  (fail closed). Empty or malformed value → nothing allowed.

## Choke point

`app/services/llm_guard.require(call_type)` is the only gate. It runs before:

| Call site | What it does | call_type |
|---|---|---|
| `app/services/llm.py` `complete()` | the only `grok -p` spawner in `app/` | from the scope |
| `app/services/mpt_client.py` `submit()` | MoneyPrinterTurbo render; MPT runs its own LLM on the subject/script | `mpt_render` |
| `app/services/mpt_client.py` `social_metadata()` | MPT title/caption LLM | `mpt_social_metadata` |
| `run/growth-agent.sh`, `run/code-agent.sh`, `run/run-check.sh` | headless `grok` agents, via `run/llm-guard.sh` → `python -m app.services.llm_guard agent-preflight` | `growth_agent`, `code_agent`, `run_check` |

The workspace comes from a scope set at each entry point:
`with llm_guard.scope(workspace_id, call_type):`

| Entry point | Scope |
|---|---|
| `render_loop._submit_new` | pre-check `_llm_guard_block` (row → FAILED, no submit), then `gate_b_preflight` and `render_submit` scopes; `params["workspace_id"]` |
| `engines/worker.run_job` (render thread) | `render`: script, storyboard compose, legacy composition |
| `render_loop` after render | `metadata` |
| `routers/videos.regenerate_metadata` | `metadata` (403 when blocked) |
| `script_compose.compose_into` | `script_compose` (403 when blocked) |
| `routers/topics.generate_videos`, `routers/trends.adopt_trend`, `autofill_loop._refill_topic` | `ideas` (403 / skipped) |
| `publish_loop` thumbnail | `thumbnail` (falls back to the spoken claim, no LLM) |
| `run/rubric_review.py` | `rubric_review`, workspace read from the channel row |

No scope, a `None` workspace, an unknown id, or an id outside the allowlist →
`LLMBlocked` (a `GrokCLIError`, so existing grok-failure handling fails hard
and no retry loop starts) plus one log line:

```
llm_blocked_external_google_data workspace_id=2 call_type=ideas
```

The log line never contains prompt content.

## Headless agents

The growth agent, code agent and run check run `grok` with `bypassPermissions`,
the repo as cwd and the shared Basic password. The Basic username is the
workspace slug, so with that password they *could* read any workspace over the
API, or `manager.db` directly. To keep Google Limited Use, their runs are
**scoped to `MANAGER_AGENT_RUN_WORKSPACE_IDS`** (default `1` = owera):

* `llm agent-exec` prepends a workspace-scope preamble to `--prompt-file` listing
  only those workspaces and channels (id, slug, name — no tokens, emails, or
  analytics) and sets `MANAGER_AGENT_RUN_WORKSPACE_IDS` in the child env to the
  effective ids. The playbooks tell the agent to use only those slugs as the
  Basic username (or `{slug}+channels` / `{slug}+vm`) and to discard any other
  workspace's data.
* `agent_preflight` no longer blocks the global run just because another
  workspace (e.g. `tenant-b` id 2) owns a channel. That workspace is simply
  excluded from the context.
* The LLM allowlist is still a hard defense. If the agent-run env includes an
  id that is not on `MANAGER_LLM_INTERNAL_WORKSPACE_IDS`, that workspace is
  dropped and logged (`llm_blocked_external_google_data
  reason=dropped_from_agent_run`). If nothing remains, the run is refused
  (fail closed).
* A missing venv, an unreadable DB, an empty / malformed agent-run list, or an
  empty LLM allowlist still blocks the run (exit 0 from the shell wrapper,
  alert, event in the agent log).

With the default `1`, growth / code / run-check can run on claw0 while
`tenant-b` exists; they only see workspace 1. After this lands, the CTO can
set `MANAGER_LLM_INTERNAL_WORKSPACE_IDS` back to `1` (it is temporarily `1,2`
so the unscoped agents can start). Direct DB access remains an ops gap — the
injected preamble and playbook are the control; they do not physically revoke
`bypassPermissions`.

## No exemption for "pure provided script" calls

A provided script still travels with title, subject and topic text that may
come from a YouTube import, and render prompts carry channel context. Proving
a call Google-free per call isn't reliable today, so every LLM call needs an
allowlisted workspace. Revisit with the Enterprise key.

## Tests

`tests/verify_llm_guard.py`: allowlist parsing, external blocked, internal
allowed, unknown/no scope blocked, no content logged, `complete`/MPT blocked
before spawn/HTTP, `run_job` and `render_loop` behavior, agent preflight and
scoped context (ws2 excluded; configured `MANAGER_AGENT_RUN_WORKSPACE_IDS`;
subprocess-mocked `agent-exec` for growth/code/run-check), entry points scoped.
It also includes a **static grep** that fails on a new process spawn,
grok argv, HTTP LLM client, MPT LLM endpoint or `llm.complete` importer outside
the known sites, and on a `run/*.sh` that runs `grok` without the preflight.

## S8: workspace scope, field allowlist, kill switch, external release (2026-11-14)

PO CM cut S8; LegalOps LEGAL-DECISION-LLM-YT-DATA.md (L2/L3/L5/L7); L6 line
CM-CONNECT-AI-LINE.md (sha 845c22005853). Tests: `tests/verify_llm_guard_s8.py`.

| Rule | Where | Always on? |
|---|---|---|
| L2 data only from the bound workspace | `llm_guard.assert_data_workspace` / `note_data`; `llm_context.youtube_context` (the sanctioned reader); `require()` refuses when a noted workspace ≠ bound one (`reason=cross_workspace_data`) | yes |
| L3 field allowlist | `llm_guard.YT_FIELDS` = video_title, video_description, video_tags, channel_stats (raw subscriber/view/video counts) + `CM_FIELDS` (customer/CM-authored: topic, theme_prompt, niche, steer_notes, language, script, storyboard, craft_rules). Callers declare with `llm_guard.declare(...)` or `complete(fields=...)`; a name outside → `LLMFieldRejected`. `llm_guard.minimize` strips anything else (names logged, never values) | yes |
| L3 per-call log | `llm_call workspace_id=… call_type=… fields=… backend=… input_tokens=… output_tokens=… tokens=reported|estimated` (CLI has no usage: ~4 chars/token) | yes |
| L4 | no derived stat key passes `minimize` (avg_*, ctr, deltas, pct) | yes |
| L5 Gate B | a non-allowlisted workspace never skip-gate auto-approves (`render_loop._effective_skip_gate`) | yes (only reachable with the release flag) |
| L7 no raw dumps | `llm_guard.check_prompt`: a prompt with a YouTube/Analytics API payload (`"kind": "youtube#…"`, `etag`, `columnHeaders`) or an OAuth token field → `LLMRawDump`; excerpt caps `MAX_*` | yes |
| Kill switch | `Workspace.llm_disabled`; ops `python -m app.llm_kill_switch status|disable|enable --workspace ID`; effective next call, no restart; internal workspaces too | yes |
| External release | `MANAGER_LLM_EXTERNAL_RELEASE` (default **OFF** = #217 allowlist only) | flag |

Adding a Google-sourced field to `YT_FIELDS` changes the public L6 line (back to CMO + LegalOps).

Release check (flag ON, non-internal workspace), all required, first failing reason logged:
`backend_may_train` (needs `LLM_BACKEND=api` + `XAI_DATA_RETENTION_ACK`; the CLI consumer
session may train) → `purpose_not_released` (call type ∉ ideas, script_compose, metadata,
thumbnail, gate_b_preflight, render, render_submit, mpt_render, mpt_social_metadata) →
`fields_undeclared` → `workspace_unreadable` / `workspace_unknown` → `kill_switch` →
`not_paid_or_test` (paid = `paid_at` (S1) or `subscription_status == "active"`; or `is_test`)
→ `terms_privacy_not_accepted` (`terms_accepted_at` and `privacy_accepted_at`, written by the
S2 onboarding wizard) → `terms_pt_not_live` (the acceptance was under the PT sentence while
`MANAGER_TERMS_PT_LIVE` is false: not consent until /terms/pt/ is live; asked again in EN)
→ `ai_data_not_acknowledged` / `ai_data_ack_stale` (Google Limited Use
consent: `ai_data_ack_at` + `ai_data_ack_version` = sha256 of the exact LegalOps text shown, EN or
PT; a sha not in `google_limited_use.KNOWN_SHAS` is stale and asked again; banner step "AI data")
→ `workspace_monthly_cap` (ledger cost of this workspace this UTC month ≥
`MANAGER_LLM_WORKSPACE_MONTHLY_USD_CAP`, default 5; unreadable ledger or cap ≤ 0 = refused;
banner "Monthly draft limit reached.").
**The policy is not decided** (CTO/LegalOps/CoS/PO, due Wed 18); the flag stays OFF until it is
and L1 (no training at the provider) is proven. Headless agents are unchanged (internal only).

## S2: derived data and per-channel exclusion (PO CM / LegalOps 2026-11-14)

`app/services/llm_derived.py`.

- **L3 allowlist pinned**: `tests/verify_google_limited_use.py` pins `ALLOWED_FIELDS` to
  exactly 12 names: the 4 Google fields (`video_title`, `video_description`, `video_tags`,
  `channel_stats`) and the 8 CM-authored ones (`topic`, `theme_prompt`, `niche`,
  `steer_notes`, `language`, `script`, `storyboard`, `craft_rules`). Any new field fails.
- **Per-channel exclusion**: `channel_data_allowed(ch)` = CONNECTED and no `access_lost_at`.
  `llm_context.youtube_context` returns `{}` for any other channel (log
  `llm_channel_excluded`); for an external workspace the ideas "existing" list drops
  YouTube-sourced titles (`yt_video_id` set) of a disconnected channel.
- **Same draft + workspace**: `draft_scope(video_id)` is bound by `worker.run_job` (render
  params carry `video_id`), the render-loop metadata step and `script_compose`.
  `llm.complete` → `llm_derived.check_prompt`: when `script` / `storyboard` are declared, the
  normalized first 60 chars of the most recent 2000 stored scripts (other drafts, any
  workspace) are searched in the prompt. External workspace → `LLMCrossDraft`
  (`foreign_draft_data`; `foreign_draft_unverifiable` if the check itself fails, fail
  closed). Internal → warning `llm_foreign_draft_data` only (OS/RR never blocked).
- **No channel numbers**: `external_system(system)` appends `NO_CHANNEL_NUMBERS_RULE` to the
  system prompt of `worker._llm` / `_llm_compose` only when the bound workspace is external.
  Chosen external-only so OS/RR craft and Gate B prompts stay byte-identical (their
  quality gates were tuned on them); external workspaces get the rule on top of the same
  unchanged craft prompt.
- **Stats echo**: `flag_stats_echo(text, kind=script|storyboard, …)` after the script
  (worker + `script_compose`) and after the composition (visible HTML text) logs
  `llm_stats_echo workspace_id channel_id video_id kind stats=…` (WARNING) when a channel
  statistic ≥ 100 (latest `ChannelMetric`) appears literally (plain, `12,345`, `12.345`,
  `12 345`). Never raises, never blocks.
- **#214 scope**: `youtube_purge.purge_channel` also runs `purge_derived` (scripts,
  `creation_config`, job text files) when the reason is `deletion_requested` or
  `workspace_closed`. A plain disconnect keeps the draft.
- **Render media (PO CM round 3)**: for the same two reasons, `youtube_purge.purge_media`
  deletes the render artifacts of NOT-published drafts (status not PUBLISHED, no
  `yt_video_id`, no `published_at`): everything in `<storage_dir>/videos/<video_id>/`
  (video.mp4, thumb.jpg, provided thumbnail, other files) and in the render job dir
  `<hyperframes_storage_dir>/<handle>/` (final.mp4, narration.mp3, …). Safe paths only:
  the name must be one plain segment, the resolved dir a direct child of the configured
  root, symlinked dirs are skipped and symlinks are unlinked without following them, a job
  dir shared with another video is kept. The plan and every file are logged first
  (`youtube_purge_media plan … / delete …`); `MANAGER_PURGE_MEDIA_DRY_RUN=true` logs
  `would_delete` and deletes nothing. Published videos and YouTube are never touched; other
  workspaces' and internal OS/RR media only go on that workspace's own deletion request.
- **MPT task folders (PO CM round 4)**: the deletion-request / closure media purge also
  covers `<mpt_storage_dir>/<task_id>/` of unpublished drafts (same guards).
  Separately, `app/services/mpt_retention.py` deletes an EXTERNAL workspace's MPT task
  folder 7 days after its job finished (ok or failed; finish time = newest file mtime in
  the folder), even without a deletion request. Never: internal ws 1 / 2 (hard floor) or
  any workspace in `MANAGER_LLM_INTERNAL_WORKSPACE_IDS`, a draft / queued / rendering job,
  a folder shared by two videos or with no video row, symlinks / traversal. Same plan
  logging and `MANAGER_PURGE_MEDIA_DRY_RUN`. Published videos' MPT folders are included
  (scratch: publishing uses the Manager's own copy in `storage/videos/<id>/`). Runs as its
  own scheduler job `mpt_task_retention` every 6 h (same cycle as S7 `token_refresh_age`,
  not a step inside it: S7's tick stays DB-only and the two fail independently).
  Nothing after the render reads the MPT scratch (audit + `tests/verify_mpt_scratch_independence.py`):
  `render_loop._finalize` copies `final-1.mp4` → `storage/videos/<id>/video.mp4` (and `index.html`,
  if MPT wrote one, for long-form chapters); publish, thumbnails, chapters, Gate B / craft checks,
  vm_pass, re-render and the review player all use `storage/videos/<id>/`.
- **Follow-up (after 27/11)**: bind script/storyboard to video_id+workspace in DB instead of
  text prefix. Until then the text-prefix check stays fail-closed for external workspaces.
