# S2 onboarding wizard — screenshots (EN + PT)

Taken locally on 2026-11-14 from this branch's build: temp SQLite, scheduler and
render loop off, Google OAuth stubbed (the start route returns a local URL; the
"connected" state is written straight to the channel row), LLM stubbed
(`llm._complete_api` returns fixed titles and is slowed for the "generating" shot;
the CLI raises). Workspace 1 plays an external Stripe buyer
(`MANAGER_LLM_INTERNAL_WORKSPACE_IDS=99`). No network, no `.env`, no Stripe, no paid
LLM call. Playwright Chromium, locales `en-US` and `pt-BR`, 1280×900.

Copy: CMO CM-WIZARD-COPY.md (sha df45bd814374). Verbatim LegalOps texts: the L6 line
and the Google Limited Use checkbox (EN / PT by browser locale).

| File | Step |
|---|---|
| `01-channel-{en,pt}.png` | Create channel (Trends / Settings hidden in the nav) |
| `02-youtube-{en,pt}.png` | Connect YouTube: help line, L6 line, #215 consent note |
| `03-topics-{en,pt}.png` | Niche + 3 topics, first CMO preset picked (PT presets in PT) |
| `04-budgets-{en,pt}.png` | Daily budgets: buyer week-1 defaults 2 / 2 |
| `05-consents-{en,pt}.png` | Terms + AI data: every box unchecked by default; Google Limited Use text + privacy link |
| `06-consents-checked-{en,pt}.png` | Same, ticked |
| `07-ideas-{en,pt}.png` | Generating ideas (1 per topic × 3) |
| `08-first-draft-queued-{en,pt}.png` | First draft queued at top render priority |
| `09-first-draft-ready-{en,pt}.png` | First draft ready (`first_draft_at` stamped for the shot) |
| `10-banner-ai-data-{en,pt}.png` | S8 banner, reason `ai_data_not_acknowledged` (state forced in the DB) |
| `11-banner-monthly-limit-{en,pt}.png` | Overview with the `workspace_monthly_cap` banner "Monthly draft limit reached" (state forced) |
| `12-ai-data-page-{en,pt}.png` | Ack-only page `/ai-data` after a withdraw (AuthGate redirect), box unchecked |
| `13-ai-data-stale-{en,pt}.png` | Ack-only page for a consent given to an older text sha |
| `14-settings-consent-{en,pt}.png` | Settings: Google Limited Use consent status + Withdraw |

The ack-only page and the Settings control use Channels Dev placeholder copy
(`ai_data.*`, `settings.glu.*` in `frontend/src/wizardI18n.ts`) until CMO copy lands. The
Terms / Privacy checkbox lines, the app nav and status chips stay English.
