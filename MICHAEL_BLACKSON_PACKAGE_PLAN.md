# Michael Blackson Ghana Experience — Model Upgrade Plan

Supports a **mandatory Core Tour + optional add-ons (single- and multi-select) +
non-stacking bundle discounts**, on top of the existing USD/GHS charging,
deposit/installments, refund-tier, terms-acceptance and itinerary machinery.

Guiding principle: **generalise the one thing the model hard-coded (add-ons =
visa-only, in code) into configurable data, and add a small rule-based discount
engine.** No new "package type" fork — existing flagship and day-tour flows are
untouched.

---

## New pricing model

`TravelPackage.pricing_model` routes pricing:

| value | base price from | used by |
|---|---|---|
| `option_based` | the chosen `PackageOption` (hotel × occupancy) | flagship |
| `flat` | `price_shared` | day tours / legacy tiers |
| `core_plus_addons` | **`base_price_per_person`** + `PackageAddon`s | Michael Blackson |

A data migration backfilled existing packages (option-bearing → `option_based`,
everything else → `flat`), so no behaviour changed.

## Schema (Phase 1 — DONE)

- **`TravelPackage`**: `pricing_model`, `base_price_per_person`.
- **`PackageAddonGroup`**: `selection` (`single`/`multi`), `required`, `order`.
  Hotels = single-select optional; Experiences = multi-select.
- **`PackageAddon`** (supersedes hard-coded visa): `code`, `name`, `price`,
  `unit` (`per_person`/`per_booking`), `group`, `is_default`, `refundable`,
  `is_active`. Mixed units solved: experiences per-person, hotels flat.
- **`PackageDiscountRule`**: `percent`, `required_addons` (M2M). Non-stacking.

### The discount engine (the elegant part)

"Apply only the single highest qualifying discount" = *find every rule whose
`required_addons` are all in the cart, apply the max `percent`.* No negative
conditions. This reproduces the guide's ladder exactly (validated to the cent by
`seed_michael_blackson`):

| Rule | required add-ons | % |
|---|---|---|
| Core + all extras | kozo, enzo, massage | 5 |
| Core + Mid | hotel_mid | 7 |
| Core + Mid + all extras | hotel_mid, kozo, enzo, massage | 10 |
| Core + Premium | hotel_premium | 12 |
| Core + Premium + all extras | hotel_premium, kozo, enzo, massage | 15 |

Discount applies to the **subtotal** (base×guests + add-ons).

## Reused unchanged
USD pricing + GHS settlement · deposit $1,000 + installments · refund tiers
(already 90/60/40/0) · terms acceptance · 8-day itinerary · "no visa" = don't
define a visa add-on.

## Design decisions (locked)
1. **"No Hotel"** = an explicit **$0 default add-on** in the hotel group (cleaner cart + rules).
2. **Discounts** for `core_plus_addons` come only from bundle rules (early-bird off).
3. **Refundability** is a per-add-on admin flag (`refundable`, default true) — operator decides.

---

## Phasing

- **Phase 1 — models + admin + seed (DONE).** Schema, admin inlines (groups /
  add-ons / discount rules / pricing fields), `seed_michael_blackson` command
  (self-validates against the guide totals). 133 tests green.
- **Phase 2 — pricing engine + checkout (DONE).**
  `bookings.pricing.compute_configurable_quote` (base + add-ons + highest-rule
  discount) and `build_configurable_matrix`; `POST /api/bookings/checkout/configurable/`
  (`ConfigurableCheckoutView`) with group validation (single-select, required,
  $0-default auto-apply), stale-cart 409 guard, and `create_configurable_booking`
  snapshotting add-ons + `Booking.discount_amount`/`discount_note`. The
  `/packages/<id>/pricing/` endpoint routes by `pricing_model`. Receipt +
  admin breakdown net off the bundle discount and reconcile. Option-based/flat
  paths untouched. 140 tests green (7 new).
- **Phase 3 — frontend cart (DONE).** `/experience?package=<id>` cart: Core
  locked, hotel single-select (radio), experiences multi-select (checkboxes),
  guest stepper, live subtotal/discount/total mirroring the backend rules,
  contact + policy acceptance, payment plan + method picker, then
  configurableCheckout → initialize → Paystack. The tour-detail page routes
  core_plus_addons packages to a `ConfigurableBookingPanel` ("Build your
  experience" → the cart). Verified: selecting Mid + all extras shows −$321.80
  (10%) → $2,896.20, matching the backend to the cent. tsc clean, prod build ok.
- **Phase 4 (optional).** Migrate the flagship's hard-coded visa into a
  `PackageAddon` and retire the `visa_*` fields.

## Try it
```bash
python manage.py seed_michael_blackson   # creates the package + validates pricing
```
Then Admin → Travel Packages → Michael Blackson Ghana Experience → the Add-on
Groups / Add-ons / Discount Rules inlines.
