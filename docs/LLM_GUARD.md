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

* `1` is workspace slug `owera` ("Owera (legacy default workspace)"), which holds
  ch1 Owera Software and ch2 Rodrigo Recio. Read-only on the claw0 DB, 2026-11-13.
* `2` (`tenant-b`, ch3 O4 Dogfood and ch4 elen3x) is the O3/O4 external-tenant
  test and is **not** internal by default.
* Empty or malformed value → no workspace allowed (everything blocked). It never widens.
* Stripe-provisioned buyer workspaces are never on the list.

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
workspace slug, so with that password they can read **any** workspace over the
API, or `manager.db` directly. Their effective scope is "every workspace".
They may start only when **no workspace outside the allowlist owns a channel**.
A buyer workspace with no channel yet holds no Google data and does not block.
A missing venv, an unreadable DB or any error blocks the run (exit 0, alert,
event in the agent log).

Effect on claw0 today: `tenant-b` (id 2) owns ch3/ch4, so **run-check and
code-agent are skipped** until the CTO does one of these:
(a) adds `2` to the allowlist, if tenant-b is Owera-internal test data;
(b) removes or purges tenant-b's channel data;
(c) ships V3, so agent tokens are scoped to `owera`. Direct DB access is still an ops gap after that.

## No exemption for "pure provided script" calls

A provided script still travels with title, subject and topic text that may
come from a YouTube import, and render prompts carry channel context. Proving
a call Google-free per call isn't reliable today, so every LLM call needs an
allowlisted workspace. Revisit with the Enterprise key.

## Tests

`tests/verify_llm_guard.py`: allowlist parsing, external blocked, internal
allowed, unknown/no scope blocked, no content logged, `complete`/MPT blocked
before spawn/HTTP, `run_job` and `render_loop` behavior, agent preflight, entry
points scoped. It also includes a **static grep** that fails on a new process spawn,
grok argv, HTTP LLM client, MPT LLM endpoint or `llm.complete` importer outside
the known sites, and on a `run/*.sh` that runs `grok` without the preflight.
