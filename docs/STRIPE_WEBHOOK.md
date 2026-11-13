# Stripe provisioning webhook

Paid sign-ups via [Payment Link](https://buy.stripe.com/4gMeVc81u18Kg9GcIK5EY00) ($149/mo USD) provision a dedicated CM workspace when Stripe posts to the manager.

## Environment

| Variable | Purpose |
|----------|---------|
| `MANAGER_STRIPE_WEBHOOK_SECRET` | Webhook signing secret (`whsec_…`) from Stripe Dashboard → Developers → Webhooks |
| `MANAGER_STRIPE_CM_PAYMENT_LINKS` | Optional, comma-separated Payment Link ids that provision. Default is the CM link; leave unset |
| `MANAGER_RESEND_API_KEY` | Resend API key for the one-time welcome email (optional; logs a warning if unset) |

Load via the repo-root `.env` on claw0 (same as other `MANAGER_*` settings). Do not commit secrets.

## Endpoint

- **POST** `/api/stripe/webhook`
- **Public** (no Basic Auth, no SPA session) — same exemption class as `/api/auth/*` and OAuth callbacks.
- Verifies `Stripe-Signature` on the raw body (300s timestamp tolerance).
- Returns `503` if the secret is unset, `400` on bad signature, `200` for handled and ignored events.

## Handled events

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Find/create workspace for buyer email (`customer_details.email` or `customer_email`); set `subscription_status=active`; store Stripe customer/subscription ids; send welcome email once |
| `customer.subscription.created` / `.updated` | Sync plan status from Stripe (`active`/`trialing` → `active`; `past_due`/`unpaid`/`canceled`/`incomplete_expired` mapped accordingly) |
| `customer.subscription.deleted` | Set `subscription_status=canceled` (data retained) |
| `invoice.payment_failed` | Set `subscription_status=past_due` |
| Other types | Ignored (`200`) |

Idempotency (two layers, both race-safe):

1. `StripeEvent` (primary key = Stripe `event.id`). Replays and duplicate deliveries of the same event are no-ops.
2. `StripeCheckoutSession` (primary key = Checkout Session `cs_…`). A second `checkout.session.completed` for the same session under a **different** event id (a second live endpoint, or a resend) is a no-op. A partial unique index `ux_workspace_stripe_subscription` also allows only one workspace per `stripe_subscription_id`.

Under concurrent delivery on SQLite, a racing request either waits for the write lock and then sees the committed row, or gets `database is locked` and returns `500`; Stripe retries it and the retry is a no-op. The welcome email is sent **after** the provisioning commit, claimed by a conditional `UPDATE … WHERE welcome_email_sent = 0`, so it goes out at most once per workspace. A failed send releases the claim.

### Which checkouts provision

The live Stripe account is shared with OSI (one-time Payment Links), and every webhook endpoint receives every `checkout.session.completed`. Only sessions whose `payment_link` is in `MANAGER_STRIPE_CM_PAYMENT_LINKS` (default: the CM link `plink_1UL1ccADrWLjH4u9HD9GC4n4`) provision. A session with no `payment_link` provisions only when `mode=subscription`. Everything else is acknowledged with `200` and ignored.

## Google Sign-In mapping

Buyer email is stored on `Workspace.owner_email`. SPA Google login resolves `workspace_for_email()` to that workspace slug (in addition to the static v1 allowlist). The buyer must sign in with the **same Google account email** used at checkout.

## Plan-inactive gating (not wired here)

To block usage when `subscription_status` is not `active`, gate in:

- `app/workspace.py` — `current_workspace` / `resolve_workspace_slug` (HTTP 402/403), and/or
- `app/main.py` — `basic_auth` middleware for SPA sessions, and/or
- Feature routers (render/publish loops) if you want soft limits instead of hard auth denial.

## Test mode smoke

```bash
# Terminal 1 — forward Stripe CLI events to local manager
stripe listen --forward-to http://127.0.0.1:7070/api/stripe/webhook

# Terminal 2 — trigger sample events (copy whsec from listen output into .env)
export MANAGER_STRIPE_WEBHOOK_SECRET=whsec_...
stripe trigger checkout.session.completed
```

Regression suite:

```bash
PYTHONPATH=. uv run python tests/verify_stripe_webhook.py
```

## Rollback

1. Remove or unset `MANAGER_STRIPE_WEBHOOK_SECRET` (route returns `503`, Stripe retries stop once the endpoint is disabled in Dashboard).
2. Revert the deploy / merge commit. SQLite columns (`workspace.stripe_*`, `stripeevent`) are harmless if left in place; no data is deleted on subscription cancel events.
3. Manually adjust any mis-provisioned `workspace` rows if needed (`owner_email`, `stripe_subscription_id`).
