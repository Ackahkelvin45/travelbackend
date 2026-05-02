import json
from datetime import timedelta, date

from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone


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

    revenue_agg = Payment.objects.filter(status="success").aggregate(total=Sum("amount"))
    total_revenue = revenue_agg["total"] or 0

    total_users = User.objects.count()
    published_reviews = Review.objects.filter(is_published=True).count()
    avg_rating_agg = Review.objects.filter(is_published=True).aggregate(avg=Avg("rating"))
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
    cat_palette = ["#BD8F3A", "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#6B7280"]
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
        }],
    })

    # ── Chart: Bookings by Status (doughnut) ───────────────────────────────────
    status_qs = Booking.objects.values("status").annotate(count=Count("id"))
    status_display = dict(Booking.Status.choices)
    status_palette = {
        "pending": "#F59E0B",
        "confirmed": "#10B981",
        "cancelled": "#EF4444",
        "completed": "#3B82F6",
    }
    status_labels = [status_display.get(r["status"], r["status"]) for r in status_qs]
    status_counts = [r["count"] for r in status_qs]
    status_colors = [status_palette.get(r["status"], "#6B7280") for r in status_qs]
    status_chart = json.dumps({
        "labels": status_labels,
        "datasets": [{
            "data": status_counts,
            "backgroundColor": status_colors,
            "borderWidth": 0,
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
            "borderColor": "#BD8F3A",
            "backgroundColor": "rgba(189,143,58,0.12)",
            "fill": True,
            "tension": 0.4,
            "pointRadius": 3,
            "pointHoverRadius": 5,
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
            status="success",
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
            "label": "Revenue (GHS)",
            "data": rev_amounts,
            "backgroundColor": "#BD8F3A",
            "borderColor": "#BD8F3A",
            "borderWidth": 0,
            "borderRadius": 6,
        }],
    })

    # ── Chart: Packages by Price Option (doughnut) ────────────────────────────
    active_qs = TravelPackage.objects.filter(is_active=True)
    tier_chart = json.dumps({
        "labels": ["Shared / Couples", "Private (Solo)", "VIP"],
        "datasets": [{
            "data": [
                active_qs.count(),
                active_qs.filter(price_private__isnull=False).count(),
                active_qs.filter(price_vip__isnull=False).count(),
            ],
            "backgroundColor": ["#10B981", "#3B82F6", "#BD8F3A"],
            "borderWidth": 0,
        }],
    })

    # ── Recent Bookings ────────────────────────────────────────────────────────
    recent_bookings = list(
        Booking.objects.select_related("package").order_by("-created_at")[:8]
    )

    context.update({
        # KPI
        "kpi_active_packages": active_packages,
        "kpi_total_bookings": total_bookings,
        "kpi_pending_bookings": pending_bookings,
        "kpi_confirmed_bookings": confirmed_bookings,
        "kpi_total_revenue": f"GHS {total_revenue:,.2f}",
        "kpi_total_users": total_users,
        "kpi_published_reviews": published_reviews,
        "kpi_avg_rating": avg_rating,
        "kpi_bookings_this_month": bookings_this_month,
        # Charts
        "category_chart": category_chart,
        "status_chart": status_chart,
        "daily_chart": daily_chart,
        "revenue_chart": revenue_chart,
        "tier_chart": tier_chart,
        # Table
        "recent_bookings": recent_bookings,
    })
    return context
