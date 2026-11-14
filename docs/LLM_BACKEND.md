# LLM backend: Grok CLI (consumer) → xAI API for `complete()`

Status: **STAGED.** The default stays `cli`, so nothing changes until the CTO sets
the flag on claw0. The key is Rodrigo's personal xAI account (approved by Rodrigo on
2026-11-13), the CTO handles the .env, and no API key was created or used for this PR.

## One client

`app/services/llm.py` is the only LLM client module.
`complete()` → `llm_guard.require()` (guard B) → backend:

| `LLM_BACKEND` | `complete()` path |
|---|---|
| `cli` (default; any value other than `api`) | `grok -p` on the `grok login` session. Unchanged. |
| `api` | `POST https://api.x.ai/v1/responses` (xAI Responses API, [docs](https://docs.x.ai/docs/api-reference), read 2026-11-13) |

### Agent runs stay on the CLI (both modes)

The headless agents (growth agent — the analytics path —, code agent, run-check)
go through the same module:
`run/*.sh` → `python -m app.services.llm agent-exec <call_type> -- grok …`.

In **both** `cli` and `api`: guard preflight, then the `grok` command on the
consumer `grok login` session, with a workspace-scope preamble prepended to
`--prompt-file`. Agents are scoped to `MANAGER_AGENT_RUN_WORKSPACE_IDS`
(default `1`; see [`docs/LLM_GUARD.md`](LLM_GUARD.md)) so a tenant workspace
does not block the global run. `LLM_BACKEND=api` moves **only `complete()`** to the
API (CTO, 2026-11-13); agents are never refused because of the backend. The agent
child env has `XAI_API_KEY` / `GROK_API_KEY` / `ANTHROPIC_API_KEY` stripped, so the
CLI cannot bill the API key outside the cap and ledger, and carries the effective
`MANAGER_AGENT_RUN_WORKSPACE_IDS`. Agent runs write no
`llm-usage.jsonl` row and are not charged against `LLM_MONTHLY_USD_CAP`.

MoneyPrinterTurbo runs its own LLM provider (its own config). This PR doesn't
change it, and guard B blocks it for external workspaces.

## Model and effort mapping

| Today (CLI) | API |
|---|---|
| `MANAGER_GROK_MODEL=grok-4.7` (live claw0 .env) → `grok -m grok-4.7` | `model: "grok-4.7"` |
| `MANAGER_GROK_MODEL` empty → CLI default | `MANAGER_LLM_API_DEFAULT_MODEL` (default `grok-4.7`) |
| `--reasoning-effort <e>` (compose `MANAGER_GROK_COMPOSE_REASONING_EFFORT`=medium; light idea/autofill/metadata `MANAGER_GROK_EFFORT_IDEAS`=low, #224) | `reasoning: {"effort": <mapped>}` |
| no effort flag | no `reasoning` field (API default `high`) |
| system + prompt flattened into `-p` | `instructions` = system, `input` = prompt |
| — | `store: false` |

`--reasoning-effort` is a CLI flag only; it is never put on an API request. The API
gets `reasoning.effort` instead, mapped from the CLI vocabulary
(`none|minimal|low|medium|high|xhigh|max`) to the API's
(`low|medium|high|xhigh` on grok-4.5/4.6/4.7 —
[reasoning guide](https://docs.x.ai/docs/guides/reasoning); reasoning cannot be disabled):

| CLI | API |
|---|---|
| none, minimal, low | low |
| medium | medium |
| high | high |
| xhigh, max | xhigh |
| anything else | not sent (warning logged; API default) |

The ledger row keeps both `effort` (sent) and `effort_requested` (caller's value).

A model missing from the price table → `LLMConfigError` (fail closed).

## Behavior kept from the CLI

- One attempt per call, with no retry in the client. The CLI doesn't retry either, and `render_loop._TRANSIENT` owns retries.
- Timeout is the caller's (`grok_timeout_seconds` 600 / `_light`), and it fails as `LLMError("grok.Timeout: …")` (the `GrokCLIError` name is kept as a backward-compatible alias), the same transient token.
- HTTP 402 → `LLMInsufficientCredit` with reason `insufficient_credit`. One warning line logs `workspace_id`, `route` (call type), and `insufficient_credit` — no prompt or body. It is not transient: the client does not retry, `render_loop` marks the video failed with reason `insufficient_credit` (craft_review stays pending), and the issues digest / video list classifies it as an external credit failure (`hold`, not auto-requeue).
- Other HTTP ≥400 → `LLMError("xai api HTTP <status>: <error code>")`. A 503 or 529 is transient, as a CLI stderr carrying those codes would be. 401, 403 and 429 are not.
- Missing key → `LLMConfigError`, with a clear message and **no fallback to the CLI**.
- Guard B applies in both modes.

## Data retention acknowledgement (`XAI_DATA_RETENTION_ACK`)

Replaces the old `XAI_ZDR_CONFIRMED` gate (removed; it is no longer read). The
operator states, explicitly, what the xAI account behind `XAI_API_KEY` retains:

| value | meaning |
|---|---|
| `zdr` | Zero Data Retention is enabled on the xAI org |
| `30d_no_training` | standard API retention (30 days), prompts not used for training — the account approved on 2026-11-13 |

- **Boot check:** with `LLM_BACKEND=api` and the value empty or anything else, the
  manager **refuses to start** (`LLMRetentionNotAcknowledged` from the lifespan).
  Every api call checks it again (fail closed).
- **Logged:** boot log `llm backend=xai-api data_retention=<value> …`, every api call
  log line, and the `data_retention` field on every `llm-usage.jsonl` row.
- Never set `zdr` unless ZDR is actually on for the org.
- Note (xAI reasoning guide): on the Responses API, grok-4.7 always returns
  `reasoning.encrypted_content`, and that default does not stop xAI from storing the
  thinking trace server-side; `store:false` governs only whether the response is
  kept for `previous_response_id`. This is part of what the acknowledgement covers.

## Stateless requests

The body is restricted to `model`, `input` (string), `instructions`, `reasoning` and
`store`, and `store` is always explicitly `false`. Anything else is refused before
HTTP, including `previous_response_id`, conversation state, `background`,
`store:true`, and an `input` that isn't a string (a message array). Tests check the
mocked payload.

**Per-call log:** `llm_usage backend=xai-api model=… response_model=… call_type=… workspace_id=…
input_tokens=… cached_input_tokens=… output_tokens=… reasoning_tokens=… cost_usd=…`
(no content), plus the JSONL row below.

## Usage ledger (JSONL)

Default path: `storage/llm-usage.jsonl` (`MANAGER_LLM_USAGE_PATH` overrides it).
One line per api `complete()` call, with **no prompt, output or key**:

```json
{"backend":"api","cached_input_tokens":200,"call_type":"storyboard_compose","cost_usd":0.0047,"cost_usd_estimated":0.0047,"cost_usd_reported":null,"data_retention":"30d_no_training","duration_s":41.2,"effort":"medium","effort_requested":"medium","input_tokens":1000,"model":"grok-4.7","output_tokens":100,"reasoning_tokens":400,"status":"completed","ts":"2026-11-16T12:00:00+00:00","workspace_id":1}
```

- `cost_usd` is xAI's `usage.cost_in_usd_ticks` (1e10 ticks = $1) when the API returns it. Otherwise it's the estimate from the price table.
- Reasoning tokens are billed at the output rate.

## Budget guard (fail closed)

`LLM_MONTHLY_USD_CAP`, summed per UTC calendar month from the ledger, is checked before every api call:
- cap unset or 0 → blocked (`llm_budget_blocked reason=cap_not_set`)
- month-to-date ≥ cap → blocked (`reason=cap_reached`)
- ledger unreadable → blocked

`LLMBudgetExceeded` is not transient, so it doesn't start a render retry loop.

Reference cap: **150** USD/month (the xAI account limit is USD 250, shared with
Grok Bot and Cursor; the ledger only sees CM's api calls).

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
LLM_BACKEND=api                          # or MANAGER_LLM_BACKEND; unset/cli = today's grok login
XAI_API_KEY=xai-…                        # Rodrigo's xAI key; never commit / log / paste
XAI_DATA_RETENTION_ACK=30d_no_training   # zdr | 30d_no_training; empty/other = refuse boot
LLM_MONTHLY_USD_CAP=150                  # USD per UTC month; unset/0 in api mode = everything blocked
MANAGER_LLM_USAGE_PATH=/Users/rodrigorecio/src/owera-channels-manager/storage/llm-usage.jsonl
# optional:
# MANAGER_LLM_API_DEFAULT_MODEL=grok-4.7
# MANAGER_LLM_PRICE_TABLE_JSON={"grok-4.7":{"input":2,"cached_input":0.5,"output":6}}
```

Removed: `XAI_ZDR_CONFIRMED`, `MANAGER_LLM_AGENT_API_STATELESS_VERIFIED`,
`MANAGER_LLM_AGENT_RUN_USD_ESTIMATE` (agents no longer use the API).
`MANAGER_GROK_MODEL=grok-4.7` stays as it is, and it selects the API model too.

## Cutover / rollback (CTO)

1. Merge, tip and restart the manager with no env change. Behavior is identical (cli).
2. Add the env above, restart, check the boot log line `data_retention=…`, then smoke
   **one** cheap internal call (e.g. `POST /api/videos/{id}/regenerate-metadata` on an
   owera draft) and check the new ledger row. The next growth run should log
   `llm agent-exec … backend=grok-cli`.
3. Rollback: `LLM_BACKEND=cli` (or remove it) and restart.

## Known gaps / risks

- **The api backend has not been tested against the live API.** No key was used and the HTTP transport is mocked. The first real call is the step 2 smoke.
- Agents stay on the consumer `grok login` session, so the training/Limited-Use
  reasoning behind guard B still applies to them; guard preflight is unchanged.
- Moving `complete()` to the API does not by itself lift guard B. Relaxing `MANAGER_LLM_INTERNAL_WORKSPACE_IDS` is a separate CTO/CoS decision, along with the Limited Use disclosure.
