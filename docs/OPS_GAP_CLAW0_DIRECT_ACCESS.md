# Ops gap: direct DB and file access on claw0 (not fixed here)

Status: documented for CTO/CoS (V3, 2026-11-13). This document doesn't propose a code change.

The V3 audit log and per-actor tokens only cover access **through the CM HTTP
API**. Everything below reaches workspace data without passing through it, so it
writes no audit line and no actor scope applies:

| Path | Who can use it today | What it exposes |
|---|---|---|
| `~/src/owera-channels-manager/manager.db` (SQLite, plain) | any process as user `rodrigorecio`: headless grok agents (`bypassPermissions`, cwd = repo), ssh sessions, other launchd jobs on claw0 | every workspace's channels, videos, metrics (`channelmetric`, `videometric`, `traffic_json`), OAuth credential rows (tokens are AES-GCM encrypted at rest, and the key sits in `.env` next to the DB), Stripe ids, buyer emails |
| `.env` | same | `MANAGER_APP_PASSWORD`, `MANAGER_TOKEN_ENC_KEY` (decrypts the OAuth tokens), Google client secret, Resend / Stripe webhook secrets, and once (M) ships, `XAI_API_KEY` |
| `credentials/` | same | legacy per-channel `client_secret.json` / token files, and once V3 ships, `actor-tokens.json` (hashes only) |
| `storage/videos/*`, `storage/grok-scratch/` | same | rendered media, prompts' working dirs |
| `audit/access.jsonl` | same (it can be edited unless `chflags uappnd` is set) | the audit trail itself |
| `~/Library/Logs/owera-*.log` | same | agent transcripts (may quote API responses) |

Why it matters: the headless agents (growth agent, code agent, run check) run
with `--permission-mode bypassPermissions` in the repo. Their playbooks tell them
not to touch `manager.db` or `.env`, but nothing enforces that. Guard (B) blocks
those agents while any external workspace owns a channel, for exactly this reason.

Options for later (CTO/CoS to choose; none applied):
1. Run the agents as a separate macOS user with no read access to the repo's
   `manager.db`, `.env`, `credentials/`, `audit/` (chmod 600/700 to the service user).
2. Run the manager as a dedicated service user, and keep the DB and `.env` readable only by it.
3. `chflags uappnd` on the audit file, and ship it off-box (e.g. a daily copy to Drive/R2).
4. FileVault + no shared ssh keys on claw0. Rotate `MANAGER_TOKEN_ENC_KEY` if `.env` is ever exposed.
5. Move the agents to API-only access with V3 scoped tokens, and stop giving them a repo cwd.
