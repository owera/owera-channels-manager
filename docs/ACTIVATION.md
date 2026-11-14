# Activation instrumentation (S1) and test workspaces (S5)

## Columns (additive, `_add_missing_columns`, no data migration)

| Column | Set by | Meaning |
|---|---|---|
| `workspace.paid_at` | `stripe_provision.handle_checkout_completed` (and `handle_invoice_paid` for an `is_test` workspace without one) | Stripe `event.created` of the first CM checkout (fallback: session `created`, then now). Never overwritten. Never set on ws 1. |
| `workspace.first_draft_at` | `render_loop._finalize` (render success) | First successful render of any video in the workspace. Never overwritten. Pre-S1 workspaces: derived from the oldest `jobrun` render success (read side only). |
| `workspace.is_test` | `python -m app.provision_test_workspace` | E2E workspace created without Stripe (internal `name+test@owera.com` only). **Cleared** by the first real payment (`checkout.session.completed` or `invoice.paid` with `amount_paid > 0`): a paying customer never stays marked as test. |
| `video.approved_by` | approve / retry-republish (`review_guard.actor_of`), render-loop skip-gate (`auto:skip_gate`) | Who approved. NULL when the request had no identity and whenever `approved_at` is cleared. |

Render-failed signal (A5) is derived, not stored: `FAILED` videos (and how many are
older than 24 h) plus `jobrun` render errors in the last 24 h, per workspace
(`activation.render_failed_signal`).

## A0–A6 (`app/services/activation.py`)

| Step | done when | at |
|---|---|---|
| A0 Provisioned | workspace exists | `created_at` (+ `paid_at`, `provision_lag_seconds`) |
| A1 YouTube connected | ≥ 1 channel `connected` | oldest platform credential write (#214 will give `oauth_status_changed_at`) |
| A2 Niche + topics | a channel niche and ≥ 3 active topics | 3rd active topic `created_at` |
| A3 Drafts | ≥ 5 videos rendered | 5th render success (+ `first_draft_at`) |
| A4 Buyer decision | ≥ 1 approval with `approved_by` not `auto:*` | first such `approved_at` |
| A5 No blocking failure | 0 videos `FAILED` for > 24 h **and** no active S8 LLM guard refusal (`A5.llm_blocked`) | `llm_blocked.at` while refused, else — (state) |
| A6 Published | ≥ 1 published | first `published_at` |

## Ops CLIs (not exposed over HTTP)

    PYTHONPATH=. uv run python -m app.provision_test_workspace --email <google account> [--name …]
    PYTHONPATH=. uv run python -m app.activation_report [--workspace slug|id] [--exclude-test] [--include-owera]

`provision_test_workspace` runs `init_db()` (same additive migration as startup),
writes one row (`owner_email`, `is_test=1`), no Stripe call, no email, no
`paid_at`; idempotent (returns the existing row, never flips a paid workspace to
test); refuses ws 1, the owera SPA allowlist email (even if listed below) and
every other email (exit 2). Accepted:

- `name+test@owera.com` (optional suffix: `name+test2`, `name+test-osi`;
  case-insensitive), for non-browser tests. A plain `name@owera.com` is refused.
- an email in **`MANAGER_TEST_WORKSPACE_EMAILS`** (comma-separated,
  case-insensitive, trimmed, default empty), for browser E2E through SPA Google
  login. PO CM 14/11 (option b): 1–2 free dedicated Gmail accounts, added as
  consent-screen test users, created by CTO and kept in the vault. No new
  Google Workspace seat.

### SPA Google login on test workspaces

`spa_google_login.workspace_for_email` (used by the OAuth callback and by every
session check) resolves an `is_test` workspace only for its provisioned
`owner_email` or an email in `MANAGER_TEST_WORKSPACE_EMAILS`, and never for an
owera allowlist email (that one always maps to `owera`). An email that owns more
than one workspace resolves to none, so one account never maps to two
workspaces. That case is an ops alert, not a generic error: the OAuth callback
redirects to `/?login_error=login_ambiguous_owner` (and `/api/auth/me` returns
401 `login_ambiguous_owner`), the SPA shows "We found more than one workspace for
this account. Email contact@owera.com and we'll fix it within 1 business day.",
the `manager.spa_google_login` logger writes one WARNING
`login_ambiguous_owner workspace_ids=[…] count=N` (no email), and each affected
workspace gets `login_ambiguous_owner_count` +1 / `login_ambiguous_owner_last_at`
(additive, nullable). `activation_report` lists it under `ops_alerts` (top level
and per workspace row). The `+test` alias is deliberately **not** mapped from the base
address: that would let `rodrigo@owera.com` into a test workspace (the
cross-workspace leak S4 closes).

### Test → paid transition

A real payment on an `is_test` workspace (`checkout.session.completed` for a CM
checkout, or `invoice.paid` with `amount_paid > 0` matched by subscription or
customer id) runs `stripe_provision.mark_paid_not_test`: `is_test` → false,
`paid_at` written only if still NULL, and one audit line on the
`manager.stripe_provision` logger (ids only, no PII):

    workspace_test_to_paid workspace_id=<id> event=<stripe type> event_id=<evt_…>

Idempotent: replays find `is_test` already false and log nothing. Ws 1 is never
touched. (The repo has no audit table; the structured line is the audit record.)

`activation_report` is read-only and never migrates: on a DB without the S1
columns it exits 2.

`GET /api/onboarding/status` adds the `first_draft` step (milestone; the
`onboarding_complete` rule still uses the first five steps), `at` per step,
a `timestamps` block, `is_test` and `render_failed`.
