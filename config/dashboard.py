import json
from datetime import timedelta, date

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from config.unfold_theme import (
    BOOKING_STATUS_BADGE,
    BOOKING_STATUS_CHART_COLORS,
    CHART_GOLD,
    CHART_GOLD_SOFT,
    CHART_PALETTE,
    CHART_SURFACE,
    SEMANTIC,
)

# ---------------------------------------------------------------------------
# Aurora chart language for Chart.js. Unfold replaces its own chart defaults
# entirely when a canvas carries data-options, so each builder returns the
# COMPLETE option object: Plus Jakarta type, muted grey ticks (#77878F is
# legible on both surfaces), recessive grids (grid line color is re-applied
# per theme by unfold's own observer), dark Aurora tooltips, and legends only
# where identity needs them (doughnuts).
# ---------------------------------------------------------------------------

_CHART_FONT = {"family": "'Plus Jakarta Sans', 'Inter', sans-serif", "size": 11}
_TICK_COLOR = "#77878F"
_TOOLTIP = {
    "backgroundColor": "#1B2124",
    "titleColor": "#EBF2F5",
    "bodyColor": "#C3D3DB",
    "cornerRadius": 8,
    "padding": 10,
    "boxWidth": 8,
    "boxHeight": 8,
    "usePointStyle": True,
    "titleFont": {**_CHART_FONT, "weight": "bold"},
    "bodyFont": _CHART_FONT,
}
_AXES = {
    "x": {
        "grid": {"display": False},
        "border": {"display": False},
        "ticks": {"color": _TICK_COLOR, "font": _CHART_FONT},
    },
    "y": {
        "beginAtZero": True,
        "border": {"display": False},
        "grid": {"tickWidth": 0},
        "ticks": {"color": _TICK_COLOR, "font": _CHART_FONT, "maxTicksLimit": 5},
    },
}


def _bar_options():
    return json.dumps({
        "maintainAspectRatio": False,
        "plugins": {"legend": {"display": False}, "tooltip": _TOOLTIP},
        "scales": _AXES,
    })


def _line_options():
    # Dense daily series: cap x ticks and keep them horizontal
    axes = {
        "x": {
            **_AXES["x"],
            "ticks": {**_AXES["x"]["ticks"], "maxTicksLimit": 8, "maxRotation": 0},
        },
        "y": _AXES["y"],
    }
    return json.dumps({
        "maintainAspectRatio": False,
        "interaction": {"mode": "index", "intersect": False},
        "plugins": {"legend": {"display": False}, "tooltip": _TOOLTIP},
        "scales": axes,
    })


def _doughnut_options():
    return json.dumps({
        "maintainAspectRatio": False,
        "cutout": "68%",
        "plugins": {
            "legend": {
                "display": True,
                "position": "bottom",
                "labels": {
                    "usePointStyle": True,
                    "pointStyle": "circle",
                    "boxWidth": 6,
                    "boxHeight": 6,
                    "padding": 16,
                    "color": _TICK_COLOR,
                    "font": _CHART_FONT,
                },
            },
            "tooltip": _TOOLTIP,
        },
    })


def dashboard_callback(request, context):
    from accounts.models import User
    from bookings.models import Booking
    from packages.models import TravelPackage
    from payments.models import Payment
    from reviews.models import Review

    today = timezone.now().date()
    now = timezone.now()
    thirty_days_ago = today - timedelta(days=29)

    # ── KPI Cards ──────────────────────────────────────────────────────────────
    active_packages = TravelPackage.objects.filter(is_active=True).count()
    total_bookings = Booking.objects.count()
    pending_bookings = Booking.objects.filter(status=Booking.Status.PENDING).count()
    confirmed_bookings = Booking.objects.filter(status=Booking.Status.CONFIRMED).count()

    # Revenue per currency — a GHS legacy catalog and a USD flagship tour can
    # coexist, and summing across currencies would be meaningless.
    revenue_rows = (
        Payment.objects.filter(status=Payment.Status.SUCCESS)
        .values("currency")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )
    revenue_display = " · ".join(f"{r['currency']} {r['total']:,.2f}" for r in revenue_rows) or "0.00"
    total_revenue = revenue_rows[0]["total"] if revenue_rows else 0

    total_users = User.objects.count()
    published_reviews = Review.objects.count()
    avg_rating_agg = Review.objects.aggregate(avg=Avg("rating"))
    avg_rating = round(avg_rating_agg["avg"] or 0, 1)

    bookings_this_month = Booking.objects.filter(
        created_at__month=today.month, created_at__year=today.year
    ).count()

    # ── Chart: Packages by Category ────────────────────────────────────────────
    category_qs = (
        TravelPackage.objects.filter(is_active=True)
        .values("category")
        .annotate(count=Count("id"))
        .order_by("category")
    )
    cat_display = dict(TravelPackage.Category.choices)
    cat_palette = CHART_PALETTE
    cat_labels = [cat_display.get(r["category"], r["category"]) for r in category_qs]
    cat_counts = [r["count"] for r in category_qs]
    category_chart = json.dumps({
        "labels": cat_labels,
        "datasets": [{
            "label": "Packages",
            "data": cat_counts,
            "backgroundColor": cat_palette[: len(cat_labels)],
            "borderWidth": 0,
            "borderRadius": 6,
            "maxBarThickness": 28,
        }],
    })

    # ── Chart: Bookings by Status (doughnut) ───────────────────────────────────
    status_qs = Booking.objects.values("status").annotate(count=Count("id"))
    status_display = dict(Booking.Status.choices)
    status_labels = [status_display.get(r["status"], r["status"]) for r in status_qs]
    status_counts = [r["count"] for r in status_qs]
    status_colors = [
        BOOKING_STATUS_CHART_COLORS.get(r["status"], SEMANTIC["neutral"]) for r in status_qs
    ]
    status_chart = json.dumps({
        "labels": status_labels,
        "datasets": [{
            "data": status_counts,
            "backgroundColor": status_colors,
            # 2px surface ring between segments (Aurora/dataviz spacer)
            "borderWidth": 2,
            "borderColor": CHART_SURFACE,
            "hoverOffset": 4,
        }],
    })

    # ── Chart: Bookings — last 30 days (line) ──────────────────────────────────
    daily_qs = (
        Booking.objects.filter(created_at__date__gte=thirty_days_ago)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    day_map = {r["day"]: r["count"] for r in daily_qs}
    daily_labels, daily_counts = [], []
    for i in range(30):
        d = thirty_days_ago + timedelta(days=i)
        daily_labels.append(d.strftime("%b %d"))
        daily_counts.append(day_map.get(d, 0))
    daily_chart = json.dumps({
        "labels": daily_labels,
        "datasets": [{
            "label": "Bookings",
            "data": daily_counts,
            "borderColor": CHART_GOLD,
            "backgroundColor": CHART_GOLD_SOFT,
            "borderWidth": 2,
            "fill": True,
            "tension": 0.4,
            "pointRadius": 0,
            "pointHoverRadius": 4,
            "maxTicksXLimit": 10,
        }],
    })

    # ── Chart: Revenue — last 6 months (bar) ───────────────────────────────────
    first_of_month = today.replace(day=1)
    # go back 5 full months so we show 6 months total including current
    month_cursor = first_of_month
    for _ in range(5):
        if month_cursor.month == 1:
            month_cursor = month_cursor.replace(year=month_cursor.year - 1, month=12)
        else:
            month_cursor = month_cursor.replace(month=month_cursor.month - 1)
    six_months_start = month_cursor

    revenue_qs = (
        Payment.objects.filter(
            status=Payment.Status.SUCCESS,
            paid_at__isnull=False,
            paid_at__date__gte=six_months_start,
        )
        .annotate(month=TruncMonth("paid_at"))
        .values("month")
        .annotate(total=Sum("amount"))
        .order_by("month")
    )
    rev_map = {r["month"].strftime("%b %Y"): float(r["total"]) for r in revenue_qs}
    rev_labels, rev_amounts = [], []
    cur = six_months_start
    for _ in range(6):
        label = cur.strftime("%b %Y")
        rev_labels.append(label)
        rev_amounts.append(rev_map.get(label, 0))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    revenue_chart = json.dumps({
        "labels": rev_labels,
        "datasets": [{
            "label": "Revenue",
            "data": rev_amounts,
            "backgroundColor": CHART_GOLD,
            "borderColor": CHART_GOLD,
            "borderWidth": 0,
            "borderRadius": 6,
            "maxBarThickness": 28,
        }],
    })

    # ── Chart: Packages by Price Option (doughnut) ────────────────────────────
    active_qs = TravelPackage.objects.filter(is_active=True)
    tier_counts = [
        active_qs.filter(price_shared__isnull=False).count(),
        active_qs.filter(price_private__isnull=False).count(),
        active_qs.filter(price_vip__isnull=False).count(),
    ]
    # Option-based (flagship) packages have no legacy price tiers — hide the
    # chart instead of rendering an empty ring.
    tier_chart = None
    if any(tier_counts):
        tier_chart = json.dumps({
            "labels": ["Shared / Couples", "Private (Solo)", "VIP"],
            "datasets": [{
                "data": tier_counts,
                "backgroundColor": [CHART_PALETTE[2], CHART_PALETTE[1], CHART_PALETTE[0]],
                "borderWidth": 2,
                "borderColor": CHART_SURFACE,
                "hoverOffset": 4,
            }],
        })

    # ── Chart: Payments by Status (doughnut) ───────────────────────────────────
    # Answers "successful vs failed vs abandoned vs pending" directly off the
    # ledger — the payment states, not booking states.
    _PAYMENT_STATUS_HEX = {
        "success": SEMANTIC["success"],
        "pending": SEMANTIC["warning"],
        "failed": SEMANTIC["danger"],
        "abandoned": SEMANTIC["neutral"],
        "refunded": SEMANTIC["info"],
    }
    pay_status_qs = Payment.objects.values("status").annotate(count=Count("id"))
    pay_status_display = dict(Payment.Status.choices)
    pay_status_labels = [pay_status_display.get(r["status"], r["status"]) for r in pay_status_qs]
    pay_status_counts = [r["count"] for r in pay_status_qs]
    pay_status_colors = [_PAYMENT_STATUS_HEX.get(r["status"], SEMANTIC["neutral"]) for r in pay_status_qs]
    payment_status_chart = json.dumps({
        "labels": pay_status_labels,
        "datasets": [{
            "data": pay_status_counts,
            "backgroundColor": pay_status_colors,
            "borderWidth": 2,
            "borderColor": CHART_SURFACE,
            "hoverOffset": 4,
        }],
    }) if pay_status_counts else None

    # ── Revenue by service component (from confirmed bookings' snapshots) ──────
    # A booking's total decomposes into package (hotel option × guests) plus
    # add-ons (visa). We report BOOKED VALUE by component, per currency, so the
    # numbers always reconcile: package + add-ons == sum of booking totals.
    from decimal import Decimal
    service_by_currency = {}
    for b in Booking.objects.filter(status=Booking.Status.CONFIRMED).only(
        "currency", "unit_price", "num_guests", "total_amount", "early_bird_discount", "addons"
    ):
        acc = service_by_currency.setdefault(
            b.currency, {"package": Decimal("0"), "addons": Decimal("0"),
                         "discount": Decimal("0"), "total": Decimal("0")})
        base = Decimal(b.unit_price) * (b.num_guests or 1)
        addons = sum((Decimal(str(a.get("line_total", "0"))) for a in (b.addons or [])), Decimal("0"))
        acc["package"] += base
        acc["addons"] += addons
        acc["discount"] += Decimal(b.early_bird_discount or 0)
        acc["total"] += Decimal(b.total_amount)
    service_revenue = [
        {"currency": cur, "package": v["package"], "addons": v["addons"],
         "discount": v["discount"], "total": v["total"],
         "reconciles": (v["package"] + v["addons"]) == v["total"]}
        for cur, v in sorted(service_by_currency.items(), key=lambda kv: -kv[1]["total"])
    ]
    # Composition doughnut for the primary (highest-value) currency.
    service_chart = None
    if service_revenue and (service_revenue[0]["package"] + service_revenue[0]["addons"]) > 0:
        top = service_revenue[0]
        service_chart = json.dumps({
            "labels": ["Package / Tour", "Add-ons (Visa)"],
            "datasets": [{
                "data": [float(top["package"]), float(top["addons"])],
                "backgroundColor": [CHART_GOLD, CHART_PALETTE[5]],
                "borderWidth": 2,
                "borderColor": CHART_SURFACE,
                "hoverOffset": 4,
            }],
        })

    # ── Recent Bookings ────────────────────────────────────────────────────────
    recent_bookings = list(
        Booking.objects.select_related("package").order_by("-created_at")[:8]
    )
    # Badge type consumed by unfold/helpers/label.html — same colors as the
    # booking changelist badges.
    for booking in recent_bookings:
        booking.status_badge_type = BOOKING_STATUS_BADGE.get(booking.status, "")

    # ── KPI cards (rendered by templates/admin/partials/kpi_card.html) ────────
    kpi_cards = [
        {"icon": "luggage", "color": SEMANTIC["gold"], "value": active_packages,
         "label": "Active Packages"},
        {"icon": "book_online", "color": SEMANTIC["blue"], "value": total_bookings,
         "label": "Total Bookings", "sub": f"{confirmed_bookings} confirmed"},
        {"icon": "payments", "color": SEMANTIC["success"], "value": revenue_display,
         "label": "Total Revenue", "compact_value": True},
        {"icon": "pending_actions", "color": SEMANTIC["warning"], "value": pending_bookings,
         "label": "Pending Bookings"},
        {"icon": "people", "color": SEMANTIC["purple"], "value": total_users,
         "label": "Registered Users"},
        {"icon": "star", "color": SEMANTIC["warning"], "value": published_reviews,
         "label": "Published Reviews",
         "sub": f"{avg_rating}★ average" if avg_rating > 0 else None},
    ]

    context.update({
        "kpi_cards": kpi_cards,
        "kpi_bookings_this_month": bookings_this_month,
        # Charts
        "category_chart": category_chart,
        "status_chart": status_chart,
        "daily_chart": daily_chart,
        "revenue_chart": revenue_chart,
        "tier_chart": tier_chart,
        "payment_status_chart": payment_status_chart,
        "service_chart": service_chart,
        "service_revenue": service_revenue,
        # Complete Chart.js option objects (unfold does not merge defaults
        # into canvases that carry data-options)
        "bar_chart_options": _bar_options(),
        "line_chart_options": _line_options(),
        "doughnut_chart_options": _doughnut_options(),
        # Table
        "recent_bookings": recent_bookings,
    })
    return context
