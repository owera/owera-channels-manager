# Activation instrumentation (S1) and test workspaces (S5)

## Columns (additive, `_add_missing_columns`, no data migration)

| Column | Set by | Meaning |
|---|---|---|
| `workspace.paid_at` | `stripe_provision.handle_checkout_completed` | Stripe `event.created` of the first CM checkout (fallback: session `created`, then now). Never overwritten. Never set on ws 1. |
| `workspace.first_draft_at` | `render_loop._finalize` (render success) | First successful render of any video in the workspace. Never overwritten. Pre-S1 workspaces: derived from the oldest `jobrun` render success (read side only). |
| `workspace.is_test` | `python -m app.provision_test_workspace` | E2E workspace created without Stripe. Kept even if a real checkout later lands on the same `owner_email`. |
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
| A5 No blocking failure | 0 videos `FAILED` for > 24 h | — (state) |
| A6 Published | ≥ 1 published | first `published_at` |

## Ops CLIs (not exposed over HTTP)

    PYTHONPATH=. uv run python -m app.provision_test_workspace --email <google account> [--name …]
    PYTHONPATH=. uv run python -m app.activation_report [--workspace slug|id] [--exclude-test] [--include-owera]

`provision_test_workspace` runs `init_db()` (same additive migration as startup),
writes one row (`owner_email`, `is_test=1`), no Stripe call, no email, no
`paid_at`; idempotent (returns the existing row, never flips a paid workspace to
test); refuses ws 1 and the owera SPA allowlist email.

`activation_report` is read-only and never migrates: on a DB without the S1
columns it exits 2.

`GET /api/onboarding/status` adds the `first_draft` step (milestone; the
`onboarding_complete` rule still uses the first five steps), `at` per step,
a `timestamps` block, `is_test` and `render_failed`.
