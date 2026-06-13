# Signal Desk — YouTube Channel Manager

[![License: MIT](https://img.shields.io/badge/license-MIT-c9f24e.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%E2%80%933.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-SPA-61DAFB?logo=react&logoColor=black)
![SQLite](https://img.shields.io/badge/SQLite-source%20of%20truth-003B57?logo=sqlite&logoColor=white)

A self-hosted manager that orchestrates the MoneyPrinterTurbo (MPT)
engine to run **multiple YouTube channels**: queue topics, render in batches behind
an optional **approval gate**, and publish into each topic's **playlist** — with full
control over how each video is generated via reusable **render profiles**.

```
React SPA (:7000 /)  ─►  Manager API (:7000 /api)  ─►  MPT engine (:8080 /api/v1)
                          SQLite manager.db                 storage/tasks/<id>/final-1.mp4
                          scheduler: render + publish loops
                          credentials/<channel>/ (per Google account)
                          storage/videos/<topic>/video.mp4
```

## Architecture

- **MPT engine** (existing) is driven over its HTTP API — never forked. One instance
  serves all channels; per-channel/topic differences ride in the `VideoParams` body.
- **SQLite** (`manager.db`) is the single source of truth — channels, render profiles,
  playlists, topics (with a status state machine), job runs, settings.
- **Scheduler** (in-process, threaded): a *render loop* (`queued → rendering → rendered
  → review|approved`) and a *publish loop* (`approved → publishing → published`,
  quota-aware, drip-spaced). The approval gate is just a state — `review` topics aren't
  eligible to publish until you approve them; `skip_gate` routes straight to `approved`.

## Prerequisites

- The MPT engine set up and working (this repo's `MoneyPrinterTurbo/`), with
  `config.toml` configured (pexels keys, `llm_provider=litellm`).
- `ANTHROPIC_API_KEY` available to **both** services. It lives in `channel/.env`
  (the systemd units and dev launcher load it). MPT's own process needs it for script
  generation — that's the #1 gotcha; the units handle it via `EnvironmentFile`.
- Per channel: a Google Cloud project with **YouTube Data API v3** enabled and an
  OAuth **Desktop** client. A separate project per channel is recommended so quotas
  don't share. You upload each `client_secret.json` in the UI.

## Run

**Dev (foreground-ish):**
```sh
manager/run/dev.sh          # builds SPA if needed, starts MPT :8080 + manager :7000
# open http://localhost:7000
```

Or manually:
```sh
# build the SPA once
cd manager/frontend && npm install && npm run build
# engine (needs the key)
cd MoneyPrinterTurbo && ANTHROPIC_API_KEY=… uv run uvicorn app.asgi:app --port 8080
# manager
cd manager && uv run uvicorn app.main:app --port 7000
```

Frontend hot-reload during development: `cd manager/frontend && npm run dev` (Vite on
:5173, proxies `/api` → :7000).

**Unattended (systemd --user):**
```sh
mkdir -p ~/.config/systemd/user
cp manager/run/mpt.service manager/run/ytmanager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mpt.service ytmanager.service
loginctl enable-linger "$USER"     # keep services running when logged out
```

## Migrate the existing channel

Imports `channel/output/*` (published vs rendered, by `.uploaded` marker) and the
remaining `topics.txt` queue into the DB, and copies the OAuth creds:
```sh
cd manager && uv run python -m app.migrate
```
The old token only has the `youtube.upload` scope; playlists need the broader
`youtube` scope, so the imported channel shows **expired** — click **Reconnect** in
the UI once to upgrade.

After migrating and confirming the manager runs the pipeline, retire the old cron:
```sh
crontab -e   # remove the line calling channel/daily_run.sh
```
(The manager's scheduler replaces `daily_run.sh`.)

## Workflow

1. **Channels** → add a channel → upload its `client_secret.json` → **Connect** (a
   browser opens for consent; the channel title is captured).
2. **Render Profiles** → create a profile (9:16, voice, subtitle styling, music, …);
   set it as the channel default. Blank fields inherit the engine defaults.
3. **Channels** → create/sync **playlists**; set defaults.
4. **Board** → bulk-add topics → watch them flow `queued → rendering → rendered`.
5. **Review** → preview the video, edit title/description/tags/playlist → **Approve**
   (or toggle the channel's *skip gate* to auto-approve).
6. The publish loop uploads within the daily budget/quota and adds the video to its
   playlist. Watch progress on the **Dashboard**.

## Notes

- YouTube quota ≈ 10k units/day ≈ 6 uploads/day per project; the per-channel publish
  budget + a safety cap guard it. Quota-exceeded leaves topics `approved` for the next day.
- OAuth apps in "Testing" expire refresh tokens ~weekly → channel shows `expired`;
  click Reconnect. Publish the consent screen to stop expiry.
- Background music defaults to `bgm_type="random"` over the user's own tracks in
  `MoneyPrinterTurbo/resource/songs/`. Don't point it at unlicensed music (Content ID).
