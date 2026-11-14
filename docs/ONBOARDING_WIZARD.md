# S2 — guided onboarding to the first draft

PO CM NEXT-CUT S2 + condition (ii) (14/11). Code: `app/services/onboarding_wizard.py`,
`app/services/topic_presets.py`, `app/routers/onboarding.py` (`/api/onboarding/wizard*`),
`frontend/src/pages/Wizard.tsx`. Tests: `tests/verify_onboarding_wizard.py`.

## Flow

After login, `AuthGate` asks `GET /api/onboarding/wizard`. A workspace with **0 channels**
(or one that started the wizard and hasn't finished) is sent to `/welcome`. Workspaces that
already had channels (`wizard_started_at` null) never see it. Trends and Settings are hidden
while `first_login` is true (wizard active, or done but the first draft hasn't rendered).

| Step | API | Writes |
|---|---|---|
| channel | `POST /wizard/channel {name}` → `channels.create_channel` | Channel (app default budgets), `wizard_started_at` |
| youtube | existing `POST /api/oauth/google/start` or `/api/channels/{id}/oauth/start` | (OAuth callback) `oauth_status=connected` |
| topics | `POST /wizard/topics {channel_id, niche, preset_id?, topics[3]}` → `topics.create_topic` | `Channel.niche`, 3 topics (replaced while no idea exists) |
| budgets | `POST /wizard/budgets {channel_id, daily_render_budget?, daily_publish_budget?}` | channel budgets (1–50), `wizard_budgets_at` |
| consents | `POST /wizard/consents {terms, privacy, ai_data, ai_data_line_version}` | `terms_accepted_at`, `privacy_accepted_at`, `ai_data_ack_at`, `ai_data_ack_version` (first acceptance kept); `llm_guard.clear_block(ws)` |
| ideas + first draft | `POST /wizard/first-draft {channel_id}` → `topics.generate_videos` (3 ideas, first topic) + `videos.produce` | drafts, one QUEUED, `first_draft_video_id`, `wizard_completed_at` |

Steps are strictly ordered server-side (409 `step_out_of_order`). Every channel id goes
through `workspace.require_channel` (S4): a foreign or missing id is the same 404.
The autofill flag (`topic_autogen_enabled`, global) is never touched.

## AI data step (S8 release check)

`llm_guard.release_block_reason` (flag `MANAGER_LLM_EXTERNAL_RELEASE` ON, non-internal
workspace) now also requires `ai_data_ack_at`; missing → `ai_data_not_acknowledged`,
shown by S8's banner as `Drafts paused: finish setup step "AI data".` The SPA shows the
L6 line verbatim (EN/PT by locale, `frontend/src/l6ConnectAiLine.ts`, sha `845c22005853`)
and sends that sha with the ack; a different sha is refused (409 `ai_data_line_changed`).
Flag OFF: unchanged (#217 allowlist).

## First-draft priority

`render_loop._queued_candidates` ends with `_first_draft_first`: the video in
`Workspace.first_draft_video_id` (only if it is on a channel of that workspace) moves to
the front of the candidate list until S1 stamps `first_draft_at` (first successful render,
`activation.stamp_first_draft` in `_finalize`). Only the order changes: `render_concurrency`,
each channel's `daily_render_budget`, holds, pauses, the LLM guard and the per-channel
long/short mix (`_auto_produce`, `_rebalance_queued_mix`, i.e. the OS/RR limits) are
untouched, and an in-flight render is never preempted; the first draft takes the next slot
that frees up.

## Presets

No catalog exists (Generics v1: niche is free text). `topic_presets.NICHE_PRESETS` is a small
structure (niche line + 3 short topics per niche), neutral English placeholders,
**copy pending CMO review**. No Owera topics, no OS/RR series. The user can type free text.
