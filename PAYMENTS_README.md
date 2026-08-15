# 💳 Booking & Payments Integration Guide

The API surface for the Azura booking + installment-payment platform.
Interactive docs: `GET /api/docs/` (Swagger UI) · `GET /api/redoc/`

Two booking flows coexist:

| Flow | Used by | Create endpoint |
|---|---|---|
| **Option-based** (flagship tour: hotel × occupancy, early bird, visa, installments) | packages that have `PackageOption` rows | `POST /api/bookings/checkout/` |
| **Legacy tier** (old catalog: shared/private/vip) | packages without options | `POST /api/bookings/` |

`package.has_options` on the package detail response tells the frontend which
flow to render.

---

## Option-based flow (flagship)

```
Step 1  GET  /api/packages/<id>/pricing/     → full pricing matrix (see below)
Step 2  GET  /api/bookings/policies/         → current policy documents to accept
Step 3  POST /api/bookings/checkout/         → create booking (snapshot + acceptances)
Step 4  POST /api/payments/initialize/       → fresh Paystack attempt (GHS charge)
Step 5  redirect → authorization_url         → customer pays on Paystack
Step 6  GET  /api/payments/verify/<ref>/     → ONE gateway-backed verify on return
Step 7  GET  /api/payments/status/<ref>/     → DB-only polling (webhook is truth)
```

### 1. Pricing matrix — `GET /api/packages/<id>/pricing/`

Everything the booking UI needs in ONE response — the frontend does lookups,
never money math:

- `options[]` — each hotel/occupancy with `standard_*`, `early_bird_*`,
  `effective_*` prices and `saving_total`
- `visa` — `enabled`, `fee_per_guest`, `info` (non-refundable)
- `installments` — `enabled`, `deposit_minimum`, `final_payment_deadline`
- `early_bird` — `active`, `deadline`
- `charge` — `currency` ("GHS") + `exchange_rate` used at charge time for
  non-GHS packages (display "Charged as GHS X")
- `server_now` — drive countdowns from this, not the browser clock

### 2. Checkout — `POST /api/bookings/checkout/`

```json
{
  "option_id": "<uuid>", "visa": true, "payment_plan": "installment",
  "first_name": "...", "last_name": "...", "email": "...",
  "accepted_policies": ["terms", "installment", "refund", "privacy"],
  "expected_total": "4700.00"
}
```

- Prices recomputed server-side; early-bird eligibility locks at creation.
- Every currently-published policy type must appear in `accepted_policies`;
  acceptances are recorded transactionally (`400` lists `missing_policies`).
- **`409`** = the price changed (early bird expired mid-session); response
  contains a fresh `quote` — re-confirm with the customer, never silently
  charge the new total.
- Response includes `amount_due_today` (deposit for installment plans).
- Unpaid bookings expire after `PENDING_BOOKING_TTL_HOURS` (24h default).

### 3. Initialize — `POST /api/payments/initialize/`

```json
{ "booking_id": "<uuid>", "intent": "deposit" }
```

- `intent`: `balance` (default — everything outstanding) · `deposit`
  (outstanding deposit portion) · `custom` (+`amount`, server-clamped to the
  balance). Amounts are ALWAYS server-computed.
- Every call mints a **fresh single-use reference** — safe to retry after a
  declined card. Prior pending attempts are auto-abandoned.
- **Currency**: the ledger is the booking currency (USD); the gateway charge
  is GHS at the live FX rate (+margin), returned as `charged_amount` /
  `charged_currency` / `exchange_rate`.
- `503` = no trustworthy FX rate available (alerts ops automatically).

### 4. Verify / status / webhook

- `GET /api/payments/verify/<ref>/` — call ONCE on the Paystack return
  redirect (one gateway call).
- `GET /api/payments/status/<ref>/` — poll this afterwards (DB-only, cheap);
  returns payment + booking status, `amount_paid`, `balance`.
- `POST /api/payments/webhook/` — Paystack server-to-server, HMAC-SHA512
  verified. Handles `charge.success` / `charge.failed`. Idempotent; verifies
  gateway amount+currency against the attempt before crediting; returns 5xx
  on processing errors so Paystack retries.
- A booking is **confirmed** when the deposit (installment plan) or full
  amount is received. Payments are an append-only ledger; booking
  `amount_paid`/`balance` are cached projections (audit:
  `manage.py reconcile_bookings`).

---

## Accounts & dashboard

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/auth/register/` · `POST /api/auth/token/` · `POST /api/auth/token/refresh/` · `GET /api/auth/me/` | — / JWT | simplejwt auth (`{access, refresh}`) |
| `GET /api/bookings/mine/` | JWT | Full booking detail incl. payment ledger, balance, deadline |
| `POST /api/bookings/claim/` `{token}` | JWT | Attach a guest booking via the signed link from the confirmation email |
| `GET /api/bookings/<reference>/` | none (throttled) | Status by reference (legacy surface) |
| `GET /api/packages/<id>/updates/` | none | Published trip announcements |
| `GET /api/bookings/policies/` | none | Current policy documents |

## Refunds (operator workflow)

1. Booking admin → **"Cancel booking + compute refund"** — computes from the
   booking's SNAPSHOTTED tiers (days before departure × amount paid, minus
   non-refundable components e.g. visa), decomposed into per-payment legs.
2. Each pending Refund states the exact **GHS amount to refund on which
   Paystack transaction** (`Execute on gateway` column).
3. Execute in the Paystack dashboard / bank, then Refund admin →
   **"Mark processed"** (updates cached totals; double-refund guarded).

## Ops

- **Scheduling is self-managing — no crontab required.** An in-app scheduler
  (Admin → Payments → *Scheduled Tasks*) runs `refresh_fx_rates` hourly and
  `send_payment_reminders` daily, triggered by normal traffic, claimed under a
  row lock so multiple workers never double-run. Tasks can be paused there and
  show their last run/result. `entrypoint.sh` also seeds FX rates at every
  container start. (Running the commands manually or via a real cron remains
  safe — everything is idempotent.)
- **Alert email**: set in Admin → Payments → *Operational Settings* (falls
  back to the `ADMIN_ALERT_EMAIL` env var). Receives gateway mismatches,
  overpayments and FX-outage alerts.
- **Secrets stay in env** (`.env.prod`): Paystack keys, Resend key — never in
  the database. See `.env.prod.example` for FX tuning knobs.
- **Offline payments** (USD bank wires): Booking admin → add a Payment inline
  row (method = bank transfer) — routed through the same service as gateway
  payments (same confirmation, receipts, totals).
