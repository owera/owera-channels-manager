# S2 onboarding wizard — screenshots (EN + PT)

Taken locally on 2026-11-14 from this branch's build: temp SQLite, scheduler and
render loop off, Google OAuth stubbed (the start route returns a local URL; the
"connected" state is written straight to the channel row), LLM stubbed
(`llm._complete_api` returns fixed titles and is slowed for the "generating" shot;
the CLI raises). Workspace 1 plays an external Stripe buyer
(`MANAGER_LLM_INTERNAL_WORKSPACE_IDS=99`). No network, no `.env`, no Stripe, no paid
LLM call. Playwright Chromium, locales `en-US` and `pt-BR`, 1280×900.

Copy: CMO CM-WIZARD-COPY.md (sha 6a389cd4d636: v3 + v3.1 single Terms box with LegalOps FIX4 wording). Verbatim LegalOps texts: the L6 line
and the Google Limited Use checkbox (EN / PT by browser locale).

| File | Step |
|---|---|
| `01-channel-{en,pt}.png` | Create channel (Trends / Settings hidden in the nav) |
| `02-youtube-{en,pt}.png` | Connect YouTube: help line, L6 line, #215 consent note |
| `03-topics-{en,pt}.png` | Niche + 3 topics, first CMO preset picked (PT presets in PT) |
| `04-budgets-{en,pt}.png` | Daily budgets: buyer week-1 defaults 2 / 2 |
| `05-consents-{en,pt}.png` | ONE Terms + Privacy box + the AI-data box, both unchecked by default (PT shows the EN Terms sentence while `TERMS_PT_LIVE=false`) |
| `06-consents-checked-{en,pt}.png` | Same, ticked |
| `07-ideas-{en,pt}.png` | Generating ideas (1 per topic × 3) |
| `08-first-draft-queued-{en,pt}.png` | First draft queued at top render priority |
| `09-first-draft-ready-{en,pt}.png` | First draft ready (`first_draft_at` stamped for the shot) |
| `10-banner-ai-data-{en,pt}.png` | S8 banner, reason `ai_data_not_acknowledged` (state forced in the DB) |
| `11-banner-monthly-limit-{en,pt}.png` | Overview with the `workspace_monthly_cap` banner "Monthly draft limit reached" (state forced) |
| `12-ai-data-page-{en,pt}.png` | Ack-only page `/ai-data` after a withdraw (AuthGate redirect), box unchecked |
| `13-ai-data-stale-{en,pt}.png` | Ack-only page for a consent given to an older text sha |
| `14-settings-consent-{en,pt}.png` | Settings: "AI data" status + Withdraw |
| `15-withdraw-link-{en,pt}.png` | Overview: always-visible "Withdraw AI consent" / "Retirar consentimento de IA" link in the sidebar account area |
| `16-withdraw-dialog-{en,pt}.png` | The link's 1-step dialog (Withdraw / Keep); Withdraw then cuts the whole workspace (checked: `valid=false`) |
| `17-settings-withdraw-dialog-{en,pt}.png` | Settings Withdraw: same dialog |

All 17 states × EN/PT were re-taken (01–14 with CMO v2 copy: ideas help, ack page,
Settings "AI data", PT nav and status chips; first-draft chip shows the mapped label, e.g.
"Na fila" / "Revisão"). Layout check (scrollWidth vs width, every heading, help line,
button, nav item, chip and dialog element): no overflow. "Renderizando" clipped in the
70px dashboard funnel cell, so that cell uses CMO's fallback "Em render". Privacy link:
EN `https://owera.com/privacy/#cm-google`, PT `https://owera.com/privacy/pt/#cm-google`.
Re-taken again with CMO v3/v3.1 (footer links + aria "Jurídico", Dashboard and Settings
bodies in PT, single Terms box): no overflow. Still English in PT, by design: the Terms
sentence (PT gate closed), the #215 Connect consent note, and server activity kind/detail
on the Dashboard.
