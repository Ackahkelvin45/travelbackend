# 💳 Payment Integration Guide

Full guide for integrating the Paystack-powered booking and payment API into your Next.js frontend.

---

## API Base URL

```
http://localhost:8000/api          # local dev
https://yourdomain.com/api         # production
```

Interactive docs: `GET /api/docs/` (Swagger UI) · `GET /api/redoc/`

---

## Full Payment Flow

```
Step 1  POST /api/bookings/                  → Create booking (PENDING)
Step 2  POST /api/payments/initialize/       → Get Paystack checkout URL
Step 3  Redirect user → authorization_url   → User pays on Paystack
Step 4  Paystack redirects → your callback  → URL contains ?reference=AZT-PAY-...
Step 5  GET  /api/payments/verify/<ref>/     → Confirm payment + booking
Step 6  GET  /api/bookings/<reference>/      → Poll full status (optional)
```

---

## Step 1 — Create a Booking

**`POST /api/bookings/`**

No auth token required. If a JWT `Authorization: Bearer <token>` header is present, the booking is linked to the user's account automatically.

### Request body

```json
{
  "package_id":       "d9f1e2b3-4c5a-6789-abcd-ef1234567890",
  "price_tier":       "shared",
  "first_name":       "Kwame",
  "last_name":        "Mensah",
  "email":            "kwame@example.com",
  "phone":            "+233201234567",
  "country":          "Ghana",
  "num_guests":       2,
  "travel_date":      "2025-07-15",
  "special_requests": "Vegetarian meals please"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `package_id` | UUID string | ✅ | From the packages list endpoint |
| `price_tier` | `"shared"` \| `"private"` \| `"vip"` | ✅ | Price resolved server-side |
| `first_name` | string | ✅ | |
| `last_name` | string | ✅ | |
| `email` | string | ✅ | Used by Paystack for receipt |
| `phone` | string | | |
| `country` | string | | |
| `num_guests` | integer | | Default `1`. Total = unit_price × num_guests |
| `travel_date` | `YYYY-MM-DD` | | Optional. If omitted, uses package's available_from date or today. |
| `special_requests` | string | | |

### Response `201`

```json
{
  "id":           "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "reference":    "AZT-AB12CD34",
  "total_amount": "1200.00",
  "currency":     "GHS",
  "status":       "pending",
  "message":      "Booking created. Proceed to payment."
}
```

> **Save `id` (UUID) and `reference`** — you need both in the next steps.

### Next.js example

```typescript
// lib/api/bookings.ts
export async function createBooking(data: BookingPayload) {
  const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/bookings/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw await res.json();
  return res.json(); // { id, reference, total_amount, currency, status }
}
```

---

## Step 2 — Initialize Payment

**`POST /api/payments/initialize/`**

### Request body

```json
{ "booking_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890" }
```

### Response `200`

```json
{
  "authorization_url": "https://checkout.paystack.com/3ni8kdavz62431k",
  "access_code":       "3ni8kdavz62431k",
  "reference":         "AZT-PAY-AZT-AB12CD34",
  "amount":            "1200.00",
  "currency":          "GHS",
  "booking_reference": "AZT-AB12CD34"
}
```

> **Save `reference`** — you need it in Step 4 to verify the payment.

---

## Step 3a — Redirect Flow (simplest)

Redirect the user's browser to `authorization_url`. Paystack handles the payment page and redirects them back to your callback URL with:

```
https://yoursite.com/payment/callback?reference=AZT-PAY-AZT-AB12CD34&trxref=AZT-PAY-AZT-AB12CD34
```

### Next.js callback page

```typescript
// app/payment/callback/page.tsx
"use client";
import { useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";

export default function PaymentCallback() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const reference = searchParams.get("reference");
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    if (!reference) return;
    fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/payments/verify/${reference}/`)
      .then((r) => r.json())
      .then((data) => {
        setResult(data);
        if (data.status === "success") {
          router.push(`/booking/${data.booking_reference}/confirmed`);
        }
      });
  }, [reference]);

  if (!result) return <p>Verifying payment…</p>;
  return <p>Payment {result.status}</p>;
}
```

---

## Step 3b — Popup Flow (inline)

Install the Paystack JS package:
```bash
npm install @paystack/inline-js
```

```typescript
"use client";
import PaystackPop from "@paystack/inline-js";

function PayButton({ bookingId }: { bookingId: string }) {
  const handlePay = async () => {
    // 1. Initialize on backend
    const res = await fetch("/api/payments/initialize/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ booking_id: bookingId }),
    });
    const { access_code, reference } = await res.json();

    // 2. Open popup
    const popup = new PaystackPop();
    popup.resumeTransaction(access_code, {
      onSuccess: async () => {
        // 3. Verify on backend — NEVER trust client-side alone
        const verify = await fetch(`/api/payments/verify/${reference}/`);
        const data = await verify.json();
        if (data.status === "success") {
          window.location.href = `/booking/${data.booking_reference}/confirmed`;
        }
      },
      onCancel: () => console.log("Payment cancelled"),
    });
  };

  return <button onClick={handlePay}>Pay Now</button>;
}
```

---

## Step 4 — Verify Payment

**`GET /api/payments/verify/<reference>/`**

### Response `200`

```json
{
  "status":            "success",
  "booking_reference": "AZT-AB12CD34",
  "booking_status":    "confirmed",
  "amount":            "1200.00",
  "currency":          "GHS",
  "paid_at":           "2025-07-02T16:00:00Z"
}
```

| `status` value | Meaning |
|---|---|
| `success` | ✅ Payment confirmed — show success screen |
| `failed` | ❌ Payment failed — ask user to retry |
| `abandoned` | ⚠️ User closed Paystack — offer retry |
| `pending` | ⏳ Not yet paid |

---

## Step 5 — Poll Booking Status

**`GET /api/bookings/<reference>/`**

Use the `reference` from Step 1 (`AZT-AB12CD34`, not the payment reference).

### Response `200`

```json
{
  "id":               "uuid",
  "reference":        "AZT-AB12CD34",
  "first_name":       "Kwame",
  "last_name":        "Mensah",
  "email":            "kwame@example.com",
  "package_title":    "Accra City & Culture Tour",
  "num_guests":       2,
  "travel_date":      "2025-07-15",
  "unit_price":       "600.00",
  "total_amount":     "1200.00",
  "currency":         "GHS",
  "status":           "confirmed",
  "payment_status":   "success",
  "payment_reference":"AZT-PAY-AZT-AB12CD34",
  "created_at":       "2025-07-01T12:00:00Z"
}
```

> Pass `reference` as `bookingReference` to the `<LeaveReply />` review component.

---

## Webhook Setup (required for production)

Paystack sends a `charge.success` event to your server after every payment. This is a safety net — if the user closes the browser before Step 4 runs, the booking still gets confirmed.

1. In your **Paystack Dashboard → Settings → API Keys & Webhooks**, add:
   ```
   https://yourdomain.com/api/payments/webhook/
   ```
2. Every event is validated with HMAC-SHA512 using `PAYSTACK_SECRET_KEY`.

> ⚠️ Do **not** call this endpoint from the frontend.

---

## Environment Variables

```env
# .env.local (Next.js frontend)
NEXT_PUBLIC_API_URL=http://localhost:8000

# .env (Django backend)
PAYSTACK_SECRET_KEY=sk_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## Error Handling

All error responses follow this shape:

```json
{ "detail": "Human-readable error message." }
```

| HTTP Code | Meaning |
|---|---|
| `400` | Bad request / validation error |
| `404` | Booking or payment not found |
| `502` | Paystack gateway unreachable — retry |

---

## Full RTK Query Example (Redux Toolkit)

```typescript
// lib/api/paymentApi.ts
import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

export const paymentApi = createApi({
  reducerPath: "paymentApi",
  baseQuery: fetchBaseQuery({ baseUrl: process.env.NEXT_PUBLIC_API_URL }),
  endpoints: (builder) => ({
    createBooking: builder.mutation({
      query: (body) => ({ url: "/api/bookings/", method: "POST", body }),
    }),
    initializePayment: builder.mutation({
      query: (body) => ({ url: "/api/payments/initialize/", method: "POST", body }),
    }),
    verifyPayment: builder.query({
      query: (reference: string) => `/api/payments/verify/${reference}/`,
    }),
    getBookingStatus: builder.query({
      query: (reference: string) => `/api/bookings/${reference}/`,
    }),
  }),
});

export const {
  useCreateBookingMutation,
  useInitializePaymentMutation,
  useVerifyPaymentQuery,
  useGetBookingStatusQuery,
} = paymentApi;
```
