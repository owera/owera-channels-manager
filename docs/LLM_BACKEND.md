# LLM backend: Grok CLI (consumer) → xAI API (Enterprise ToS)

Status: **STAGED.** The default stays `cli`, so nothing changes until the CTO sets
the flag on claw0. The key is Rodrigo's, the CTO handles the .env, and no API key
was created or used for this PR.

## One client

`app/services/llm.py` is the only LLM client module.
`complete()` → `llm_guard.require()` (guard B) → backend:

| `LLM_BACKEND` | Path |
|---|---|
| `cli` (default; any value other than `api`) | `grok -p` on the `grok login` session. Unchanged. |
| `api` | `POST https://api.x.ai/v1/responses` (xAI API, OpenAI-compatible Responses API, [docs](https://docs.x.ai/docs/api-reference), read 2026-11-13) |

The headless agents (growth agent, which is the analytics path, plus code agent and
run-check) go through the same module:
`run/*.sh` → `python -m app.services.llm agent-exec <call_type> -- grok …`.
- In `cli`: guard preflight, then the same `grok` command.
- In `api`: guard preflight, key and budget checks, one usage row per run, then
  `grok` with an isolated `GROK_HOME=storage/grok-api-home`. Its `config.toml`
  binds the model to `https://api.x.ai/v1` with `env_key = "XAI_API_KEY"`, and a
  per-model key takes precedence over the session token
  ([Grok Build auth](https://docs.x.ai/build/enterprise)).

MoneyPrinterTurbo runs its own LLM provider (its own config). This PR doesn't
change it, and guard B blocks it for external workspaces.

## Model mapping

| Today (CLI) | API |
|---|---|
| `MANAGER_GROK_MODEL=grok-4.7` (live claw0 .env) → `grok -m grok-4.7` | `model: "grok-4.7"` |
| `MANAGER_GROK_MODEL` empty → CLI default | `MANAGER_LLM_API_DEFAULT_MODEL` (default `grok-4.7`) |
| compose `--reasoning-effort medium` (`MANAGER_GROK_COMPOSE_REASONING_EFFORT`) | `reasoning: {"effort": "medium"}` |
| no effort flag | no `reasoning` field (model default) |
| system + prompt flattened into `-p` | `instructions` = system, `input` = prompt |
| — | `store: false` (xAI doesn't keep the response) |

Agents: the CLI default model today (grok 1.0.77 on claw0; `models_cache.json`
lists grok-4.7 / 4.6 / 4.5). In `api` mode it's `grok-4.7`, or `MANAGER_GROK_MODEL` when that's set.

A model missing from the price table → `LLMConfigError` (fail closed).

## Behavior kept from the CLI

- One attempt per call, with no retry in the client. The CLI doesn't retry either, and `render_loop._TRANSIENT` owns retries.
- Timeout is the caller's (`grok_timeout_seconds` 600 / `_light` 180), and it fails as `GrokCLIError("grok.Timeout: …")`, the same transient token.
- HTTP ≥400 → `GrokCLIError("xai api HTTP <status>: <error code>")`. A 503 or 529 is transient, as a CLI stderr carrying those codes would be. 401, 403 and 429 are not.
- Missing key → `LLMConfigError`, with a clear message and **no fallback to the CLI**.
- Guard B applies in both modes.

## ZDR (Enterprise DPA)

- **Boot check:** with `LLM_BACKEND=api` and without `XAI_ZDR_CONFIRMED=1`, the manager
  **refuses to start** (`LLMZDRNotConfirmed` from the lifespan, with a clear message).
  Each api call and agent run checks it again.
- **Stateless requests only:** the body is restricted to `model`, `input` (string),
  `instructions`, `reasoning` and `store`, and `store` is always explicitly `false`.
  Anything else is refused before HTTP, including `previous_response_id`, conversation
  state, `background`, `store:true`, and an `input` that isn't a string (a message
  array). Tests check the mocked payload.
- **Agents on api:** the Grok CLI builds its own requests, and we haven't verified
  whether those set `store` or `previous_response_id`. So `agent-exec` in api mode is refused until
  the CTO verifies it and sets `MANAGER_LLM_AGENT_API_STATELESS_VERIFIED=1`. Until
  then the growth agent, code agent and run check do **not** run in api mode.
- **Per-call log:** `llm_usage backend=xai-api model=… response_model=… call_type=… workspace_id=…
  input_tokens=… cached_input_tokens=… output_tokens=… reasoning_tokens=… cost_usd=…`
  (no content), plus the JSONL row below.

## Usage ledger (JSONL)

Default path: `storage/llm-usage.jsonl` (`MANAGER_LLM_USAGE_PATH` overrides it).
One line per api call or agent run, with **no prompt, output or key**:

```json
{"backend":"api","cached_input_tokens":200,"call_type":"storyboard_compose","cost_usd":0.0047,"cost_usd_estimated":0.0047,"cost_usd_reported":null,"duration_s":41.2,"effort":"medium","input_tokens":1000,"model":"grok-4.7","output_tokens":100,"reasoning_tokens":400,"status":"completed","ts":"2026-11-16T12:00:00+00:00","workspace_id":1}
```

- `cost_usd` is xAI's `usage.cost_in_usd_ticks` (1e10 ticks = $1) when the API returns it. Otherwise it's the estimate from the price table.
- Reasoning tokens are billed at the output rate.
- Agent runs: per-run token usage isn't available to us, so each run is charged `MANAGER_LLM_AGENT_RUN_USD_ESTIMATE` (default $1.00). The xAI console remains the source of truth for agent spend.

## Budget guard (fail closed)

`LLM_MONTHLY_USD_CAP`, summed per UTC calendar month from the ledger, is checked before every api call and agent run:
- cap unset or 0 → blocked (`llm_budget_blocked reason=cap_not_set`)
- month-to-date ≥ cap → blocked (`reason=cap_reached`)
- ledger unreadable → blocked

`LLMBudgetExceeded` is not transient, so it doesn't start a render retry loop.

## Price table (USD / 1M tokens)

Source: [docs.x.ai/docs/models "Text API Pricing"](https://docs.x.ai/docs/models), read 2026-11-13.

| model | input | cached input | output | prompt ≥200k tokens (all tokens) |
|---|---|---|---|---|
| grok-4.7 | 2.00 | 0.50 | 6.00 | 4.00 / 1.00 / 12.00 |
| grok-4.6 | 2.00 | 0.50 | 6.00 | 4.00 / 1.00 / 12.00 |
| grok-4.5 | 2.00 | 0.30 | 6.00 | 4.00 / 0.60 / 12.00 |

Override: `MANAGER_LLM_PRICE_TABLE_JSON` (same keys: `input`, `cached_input`,
`output`, `long_threshold`, `long_input`, `long_cached_input`, `long_output`).

## Env vars (claw0: `~/src/owera-channels-manager/.env`, the manager's own .env at the repo root)

```dotenv
# --- LLM backend (docs/LLM_BACKEND.md) — CTO edits, Rodrigo's named yes ---
LLM_BACKEND=api                     # or MANAGER_LLM_BACKEND; unset/cli = today's grok login
XAI_API_KEY=xai-…                   # Rodrigo's xAI Enterprise key; never commit / log / paste
XAI_ZDR_CONFIRMED=1                 # only after ZDR is confirmed ON for the xAI org (Enterprise DPA)
LLM_MONTHLY_USD_CAP=50              # USD per UTC month; unset/0 in api mode = everything blocked
MANAGER_LLM_USAGE_PATH=/Users/rodrigorecio/src/owera-channels-manager/storage/llm-usage.jsonl
# optional:
# MANAGER_LLM_API_DEFAULT_MODEL=grok-4.7
# MANAGER_LLM_AGENT_RUN_USD_ESTIMATE=1.00
# MANAGER_LLM_AGENT_API_STATELESS_VERIFIED=1   # agents on api: only after the CTO verifies the CLI is stateless
# MANAGER_LLM_PRICE_TABLE_JSON={"grok-4.7":{"input":2,"cached_input":0.5,"output":6}}
```

`MANAGER_GROK_MODEL=grok-4.7` stays as it is, and it selects the API model too. The cap
of 50 above is only an example. The value is Rodrigo's call (spend).

## Cutover / rollback (CTO)

1. Merge, tip and restart the manager with no env change. Behavior is identical (cli).
2. Add the env above, restart, then smoke **one** cheap internal call
   (e.g. `POST /api/videos/{id}/regenerate-metadata` on an owera draft). Check the
   new ledger row, then check the agent's first `agent-exec` row in the log.
3. Rollback: `LLM_BACKEND=cli` (or remove it) and restart.

## Known gaps / risks

- **The api backend has not been tested against the live API.** No key was used and the HTTP transport is mocked. The first real call is the step 2 smoke.
- The Grok CLI per-model `env_key` config for the agents follows the public docs, but it hasn't been exercised. If it doesn't route to `api.x.ai`, the agent fails; it doesn't fall back to the consumer session, because `GROK_HOME` is isolated.
- In api mode the agents' child env carries `XAI_API_KEY`, and they run with `bypassPermissions`. An agent could read the key from its own env.
- Moving to the Enterprise ToS does not by itself lift guard B. Relaxing `MANAGER_LLM_INTERNAL_WORKSPACE_IDS` is a separate CTO/CoS decision, along with the Limited Use disclosure.
