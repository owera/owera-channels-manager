# Manage billing (Stripe Customer Portal)

Settings shows a **Manage billing** panel for a workspace that was provisioned
by Stripe checkout (`Workspace.stripe_customer_id = cus_…`, never the owera
workspace id 1) when the portal key is configured.

| Route | Auth | Does |
|---|---|---|
| `GET /api/billing` | workspace (Basic / SPA session) | `{subscription_status, has_billing, portal_enabled}` |
| `POST /api/billing/portal` | workspace | `POST https://api.stripe.com/v1/billing_portal/sessions` with `customer` + `return_url`, returns `{url}`; the SPA redirects there |

- Only a portal **session** is created. The portal **configuration**
  (`bpc_…`, features, policy links) is never read or changed; it stays the
  account default managed in the Stripe Dashboard.
- Errors: `503` key unset, `404` no customer on this workspace, `502` Stripe
  error / unreachable / unexpected URL (logged with the Stripe error code, never the key).

## Environment

| Variable | Purpose |
|---|---|
| `MANAGER_STRIPE_SECRET_KEY` | Stripe key used only for portal sessions. Use a **restricted key** (`rk_live_…`) with just *Customer portal: Write*. Unset = feature off (panel hidden, `503`). |
| `MANAGER_STRIPE_PORTAL_RETURN_URL` | Optional. Default `https://channels.owera.com/settings`. |

Adding the key to the claw0 `.env` is a live-Stripe change that needs Rodrigo's yes.
