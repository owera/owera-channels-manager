# WS5 — Early access multi-tenant QA matrix

Draft QA for **shared claw0** Channels Manager with **Workspace** isolation.  
Run against a staging manager — not production claw0 unless operator approves.  
Keep `MANAGER_GOOGLE_WEB_OAUTH=0` and `MANAGER_MULTI_WORKSPACE=0` unless the row says otherwise.

## Preconditions

| ID | Check |
|----|--------|
| P0 | `main` includes WS4 C–E (workspace, token store, platform OAuth code paths). |
| P1 | Automated: `verify_workspace`, `verify_token_store`, `verify_platform_oauth`, `verify_features`, `verify_workspace_quota`, `verify_onboarding`. |
| P2 | No real GCP secrets in repo; `.env` from `.env.example` placeholders only. |

## Workspace isolation (`MANAGER_MULTI_WORKSPACE=1`, dev)

| ID | Scenario | Expected |
|----|----------|----------|
| W1 | Two workspaces seeded; list channels with `X-Workspace-Slug` (dev header on) | Each list shows only its channels. |
| W2 | GET channel id from workspace B using workspace A slug | 404. |
| W3 | OAuth pending state from channel in A consumed with B workspace context | Rejected / no token write. |

## Platform OAuth (`MANAGER_GOOGLE_WEB_OAUTH=1`, staging credentials)

| ID | Scenario | Expected |
|----|----------|----------|
| O1 | Flag off: `POST /api/oauth/google/start` | 404. |
| O2 | Flag on: UI shows **Connect YouTube**, no client_secret upload | Start returns `auth_url`. |
| O3 | Callback with valid consent (test account) | `credential_source=platform_oauth`, encrypted row, CONNECTED. |
| O4 | Same YT channel id bound in workspace B | Grant rejected; workspace A unchanged. |

## Quota (PR-G)

| ID | Scenario | Expected |
|----|----------|----------|
| Q1 | Two channels same workspace; JobRuns sum over cap | Publish loop skips further uploads until Pacific reset. |
| Q2 | Dashboard `quota_spent_today` / `quota_cap` | Reflect workspace aggregate, not single channel only. |

## Onboarding (WS3)

| ID | Scenario | Expected |
|----|----------|----------|
| N1 | Fresh workspace | `/api/onboarding/status` incomplete; dashboard banner. |
| N2 | Channel + OAuth + topic + budgets + ideas | `onboarding_complete` true; banner hidden. |

## Regression (legacy ch1/ch2)

| ID | Scenario | Expected |
|----|----------|----------|
| L1 | `credential_source=legacy_file`, token.json on disk | Publish/metrics unchanged; no DB oauth row required. |
| L2 | Per-channel OAuth start/callback | Still works when platform flag off. |

## Out of scope (held)

| ID | Item |
|----|------|
| X1 | Video **#1458** / Gate B **#160** production changes — held until explicit operator yes. |
| X2 | claw0 tip deploy, live Stripe, DNS, `MANAGER_GOOGLE_WEB_OAUTH=1` on claw0. |
