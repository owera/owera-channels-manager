# S2 onboarding wizard — screenshots (EN + PT)

Taken locally on 2026-11-14 from this branch's build: temp SQLite, scheduler and
render loop off, Google OAuth stubbed (the start route returns a local URL; the
"connected" state is written straight to the channel row), LLM stubbed
(`llm._complete_api` returns three fixed titles, the CLI raises). No network, no
`.env`, no Stripe, no paid LLM call. Playwright Chromium, locales `en-US` and
`pt-BR`, 1280×900.

| File | Step |
|---|---|
| `01-channel-{en,pt}.png` | Create channel (Trends / Settings hidden in the nav) |
| `02-youtube-{en,pt}.png` | Connect YouTube: L6 line (EN / PT by locale, `lang="pt"`), button `aria-describedby` = the line, #215 consent note |
| `03-topics-{en,pt}.png` | Niche + 3 topics, first preset picked (presets = copy pending CMO review) |
| `04-budgets-{en,pt}.png` | Default daily budgets (app defaults, 6 / 6) |
| `05-consents-{en,pt}.png` | Terms of Service + Privacy Policy + AI-data acknowledgement (L6 line) |
| `06-first-draft-{en,pt}.png` | Ideas generated, first draft queued at top render priority |
| `07-banner-ai-data-{en,pt}.png` | S8 banner for the `ai_data_not_acknowledged` refusal (state forced in the DB for the shot) |

Only the L6 line changes with the locale; the rest of the wizard is English (the SPA
has no other i18n), pending CMO copy.
