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
