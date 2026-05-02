import django_filters
from django.db.models import Avg, Count, Q
from .models import TravelPackage


class DurationFilter(django_filters.CharFilter):
    """
    Maps a duration bucket to a day range.

    Accepted values:
        1d    → exactly 1 day
        2-3d  → 2–3 days
        4-7d  → 4–7 days
        1-2w  → 7–14 days
        2w+   → 15+ days
    """
    BUCKETS = {
        "1d":   (1, 1),
        "2-3d": (2, 3),
        "4-7d": (4, 7),
        "1-2w": (7, 14),
        "2w+":  (15, None),
    }

    def filter(self, qs, value):
        if not value or value not in self.BUCKETS:
            return qs
        min_days, max_days = self.BUCKETS[value]
        qs = qs.filter(duration_days__gte=min_days)
        if max_days is not None:
            qs = qs.filter(duration_days__lte=max_days)
        return qs


class TravelPackageFilter(django_filters.FilterSet):
    # ── Category / Event Type ────────────────
    category = django_filters.MultipleChoiceFilter(
        choices=TravelPackage.Category.choices,
        help_text="One or more categories, e.g. ?category=luxury_travel&category=nightlife",
    )

    # ── Price range ──────────────────────────
    price_min = django_filters.NumberFilter(
        field_name="price_shared",
        lookup_expr="gte",
        help_text="Minimum shared price",
    )
    price_max = django_filters.NumberFilter(
        field_name="price_shared",
        lookup_expr="lte",
        help_text="Maximum shared price",
    )

    # ── Duration bucket ──────────────────────
    duration = DurationFilter(
        help_text="Duration bucket: 1d | 2-3d | 4-7d | 1-2w | 2w+",
    )

    # ── Travel date window ───────────────────
    # Packages with no available_from / available_to are treated as always open.
    # The overlap check: package window must overlap the requested travel window.
    travel_from = django_filters.DateFilter(
        method="filter_travel_from",
        input_formats=["%d/%m/%Y", "%Y-%m-%d"],
        help_text="Travel start date (dd/mm/yyyy)",
    )
    travel_to = django_filters.DateFilter(
        method="filter_travel_to",
        input_formats=["%d/%m/%Y", "%Y-%m-%d"],
        help_text="Travel end date (dd/mm/yyyy)",
    )

    def filter_travel_from(self, qs, name, value):
        # Exclude packages whose availability window closes before the requested start
        return qs.filter(Q(available_to__isnull=True) | Q(available_to__gte=value))

    def filter_travel_to(self, qs, name, value):
        # Exclude packages whose availability window opens after the requested end
        return qs.filter(Q(available_from__isnull=True) | Q(available_from__lte=value))

    # ── Sort ─────────────────────────────────
    sort = django_filters.ChoiceFilter(
        choices=[
            ("featured",      "Featured"),
            ("price_asc",     "Price: Low to High"),
            ("price_desc",    "Price: High to Low"),
            ("top_rated",     "Top Rated"),
            ("most_reviewed", "Most Reviewed"),
        ],
        method="filter_sort",
        label="Sort by",
        help_text="featured | price_asc | price_desc | top_rated | most_reviewed",
    )

    def filter_sort(self, qs, name, value):
        if value == "featured":
            return qs.order_by("-is_featured", "-created_at")
        if value == "price_asc":
            return qs.order_by("price_shared")
        if value == "price_desc":
            return qs.order_by("-price_shared")
        if value == "top_rated":
            return (
                qs.annotate(avg_rating=Avg("reviews__rating", filter=Q(reviews__is_published=True)))
                .order_by("-avg_rating")
            )
        if value == "most_reviewed":
            return (
                qs.annotate(review_count=Count("reviews", filter=Q(reviews__is_published=True)))
                .order_by("-review_count")
            )
        return qs

    class Meta:
        model = TravelPackage
        fields = ["category", "price_min", "price_max", "duration", "travel_from", "travel_to", "sort"]
