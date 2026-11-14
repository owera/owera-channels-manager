# S2 — guided onboarding to the first draft

PO CM NEXT-CUT S2 + condition (ii) (14/11). Code: `app/services/onboarding_wizard.py`,
`app/services/topic_presets.py`, `app/routers/onboarding.py` (`/api/onboarding/wizard*`),
`app/services/google_limited_use.py`, `app/services/buyer_budget.py`,
`frontend/src/pages/Wizard.tsx`, `frontend/src/pages/AiData.tsx`, `frontend/src/wizardI18n.ts`.
Tests: `tests/verify_onboarding_wizard.py`, `tests/verify_google_limited_use.py`.

## Flow

After login, `AuthGate` asks `GET /api/onboarding/wizard`. A workspace with **0 channels**
(or one that started the wizard and hasn't finished) is sent to `/welcome`. Workspaces that
already had channels (`wizard_started_at` null) never see it. Trends and Settings are hidden
while `first_login` is true (wizard active, or done but the first draft hasn't rendered).

| Step | API | Writes |
|---|---|---|
| channel | `POST /wizard/channel {name}` → `channels.create_channel` | Channel (buyer week-1 budgets 2 / 2), `wizard_started_at` |
| youtube | existing `POST /api/oauth/google/start` or `/api/channels/{id}/oauth/start` | (OAuth callback) `oauth_status=connected` |
| topics | `POST /wizard/topics {channel_id, niche, preset_id?, topics[3]}` → `topics.create_topic` | `Channel.niche`, 3 topics (replaced while no idea exists) |
| budgets | `POST /wizard/budgets {channel_id, daily_render_budget?, daily_publish_budget?}` | channel budgets (1–50), `wizard_budgets_at` |
| consents | `POST /wizard/consents {terms, privacy, ai_data, ai_data_text_sha, ai_data_locale}` | `terms_accepted_at`, `privacy_accepted_at` (first acceptance kept); Google Limited Use consent (below); `llm_guard.clear_block(ws)` |
| ideas + first draft | `POST /wizard/first-draft {channel_id}` → `topics.generate_videos` (1 idea per topic × 3 topics) + `videos.produce` | 3 drafts, the first topic's one QUEUED, `first_draft_video_id`, `wizard_completed_at` |

Every `/api/onboarding/*` route takes `?locale=` (the SPA sends the L6 detection result;
fallback `Accept-Language`) and returns presets and banners in EN or PT.

Steps are strictly ordered server-side (409 `step_out_of_order`). Every channel id goes
through `workspace.require_channel` (S4): a foreign or missing id is the same 404.
The autofill flag (`topic_autogen_enabled`, global) is never touched.

## Google Limited Use consent (S8 release check)

LegalOps text (14/11), verbatim, in `app/services/google_limited_use.py` and
`frontend/src/googleLimitedUse.ts`:

- EN: "I agree that Channels Manager sends my videos' titles, descriptions, tags and channel
  stats to an AI provider in the US, only to create my own drafts." + link "Privacy policy"
- PT: "Concordo que o Channels Manager envie os títulos, descrições e tags dos meus vídeos e
  as estatísticas do canal a um provedor de IA nos EUA, só para criar os meus próprios
  rascunhos." + link "Política de privacidade"

The checkbox is unchecked by default; the link opens the Google section of the privacy
policy in a new tab, per locale (PO CM 14/11): EN `https://owera.com/privacy/#cm-google`, PT
`https://owera.com/privacy/pt/#cm-google`. Flag ON (`MANAGER_LLM_EXTERNAL_RELEASE`) also
depends on the Mon 16 privacy deploy (AI paragraph + `#cm-google` anchors) being live. The SPA sends the sha256 of the exact string it displayed and its
locale; the server accepts only the shas of these two strings (`KNOWN_SHAS`) and stores
`ai_data_ack_at`, `ai_data_ack_version` (= text sha), `ai_data_ack_locale`,
`ai_data_ack_user` (SPA Google e-mail, else the Basic Auth actor) on the workspace, plus an
append-only `GoogleLimitedUseConsentEvent` row (ack / withdraw). The L6 line stays displayed
above the box (informational).

`llm_guard.release_block_reason` (flag ON, non-internal workspace) refuses with
`ai_data_not_acknowledged` (none) or `ai_data_ack_stale` (sha of a text no longer shown),
banner step "AI data". Flag OFF: unchanged (#217 allowlist).

- **Ack-only page** `/ai-data` (`GET /api/onboarding/ai-data`, `POST …/ack`): AuthGate sends
  any external workspace there while `required` (flag ON, not in
  `MANAGER_LLM_INTERNAL_WORKSPACE_IDS`, no current consent), including workspaces that
  accepted the terms outside the wizard. "Not now" (per tab) keeps the app usable: connect
  and view work; only the LLM is refused.
- **Manual withdraw** (PO CM 14/11): an always-visible "Withdraw AI consent" / "Retirar
  consentimento de IA" link in the sidebar account area (and the mobile footer), shown
  whenever the workspace has a current consent (also before the first draft), plus the
  Settings → "AI data" button. Both open the same 1-step dialog (CMO v2: "Withdraw AI data
  agreement?" Withdraw / Keep) → `POST /api/onboarding/ai-data/withdraw`. It cuts the LLM for
  the WHOLE workspace at once, even with channels still connected; only a new ack brings it
  back.
- **Automatic withdraw by disconnect**: `POST /api/channels/{id}/disconnect` and
  `/request-deletion` withdraw the workspace consent only when NO connected channel remains.
  The disconnected channel itself leaves every prompt at once
  (`llm_derived.channel_data_allowed`: `llm_context.youtube_context` returns nothing, the
  ideas "existing" list drops its YouTube titles), even while the other channels keep
  consent. Data deletion is NOT duplicated: it stays on the #214 clock those routes already
  start (`notify.mark_operator_disconnect` / `mark_deletion_requested` → `youtube_purge`, 7 d;
  dead token 30 d).
- **Script / storyboard (derived from Google data)**: used only for the same draft and
  workspace (`llm_derived.draft_scope` + the check in `llm.complete`; another draft's or
  workspace's script in an external prompt → `LLMCrossDraft` `foreign_draft_data`; internal
  OS/RR: warning log only). In #214 scope: a deletion request or workspace closure purges
  `Video.script`, `Video.creation_config` and the job's `index.html` / `narration_words.json`
  / `status.json` script within 7 days (`youtube_purge.purge_channel`); a plain disconnect
  keeps the draft. External workspaces' script / storyboard prompts get
  `llm_derived.NO_CHANNEL_NUMBERS_RULE` appended to the system prompt (OS/RR prompts
  byte-identical); `llm_derived.flag_stats_echo` logs a `llm_stats_echo` warning (never
  blocks) when a raw channel statistic appears literally in the script or storyboard text.
- Naming: "Google Limited Use consent" in code and logs (`google_limited_use_consent …`).

## Week-1 budget and monthly subcap (PO CM 14/11)

- Buyer workspace = not internal AND created via the wizard (`wizard_started_at`) or Stripe
  (`paid_at` / `stripe_customer_id`). Its new channels default to 2 renders / 2 publishes
  a day (explicit values win). Day-1 burst: for 24 h after the channel was created the
  render loop allows up to 3 renders (`buyer_budget.effective_render_budget`, used by
  `_submit_new` and `_auto_produce` only). Internal ws1/ws2 and their channels: unchanged.
- `MANAGER_LLM_WORKSPACE_MONTHLY_USD_CAP` (default 5): ledger cost of one external workspace in
  the current UTC month; ≥ cap → `workspace_monthly_cap`, banner "Monthly draft limit
  reached. …" (EN/PT), persisted via S8 `llm_blocked`, counted in A5 (`A5.monthly_cap`). Cap ≤ 0
  or an unreadable ledger → refused. Internal workspaces exempt. The global
  `MANAGER_LLM_MONTHLY_USD_CAP` still applies.

## First-draft priority

`render_loop._queued_candidates` ends with `_first_draft_first`: the video in
`Workspace.first_draft_video_id` (only if it is on a channel of that workspace) moves to
the front of the candidate list until S1 stamps `first_draft_at` (first successful render,
`activation.stamp_first_draft` in `_finalize`). Only the order changes: `render_concurrency`,
each channel's `daily_render_budget`, holds, pauses, the LLM guard and the per-channel
long/short mix (`_auto_produce`, `_rebalance_queued_mix`, i.e. the OS/RR limits) are
untouched, and an in-flight render is never preempted; the first draft takes the next slot
that frees up.

## Copy

CMO `CM-WIZARD-COPY.md` v2 (sha a8b4aa958efd; v1 df45bd814374): presets (5 niches × 3 topics, EN + PT, same ids:
`cooking`, `fitness`, `personal_finance`, `tech_tips`, `local_business`;
`topic_presets.public_presets(locale)` / `get(id, locale)`, default EN), step titles and help
lines, banners (`llm_guard.BLOCK_USER_TEXT`, keyed by reason + locale, server-side) and the
wizard chrome (`frontend/src/wizardI18n.ts`, CMO keys). CMO v2 also covers the ack-only page
(`ai_data.*`), the Settings "AI data" panel and Withdraw dialog (`settings.glu.*`), the status
chips (`status.*`: `STATUS_META`, held / paused, OAuth labels, Trend Radar; the wizard's
first-draft chip and Trends now use the label map, not the raw enum) and the nav (`nav.*`,
"Owera Channels Manager" unchanged). Buyer UI says "AI data"; "Google Limited Use" stays in
code and logs. The dashboard funnel cell uses CMO's fallback "Em render" (PT "Renderizando"
clipped at 70px).
No Owera topics, no OS/RR series. The user can type free text.
