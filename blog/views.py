from collections import Counter
from datetime import timedelta

import django_filters
from django.db.models import Count
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import generics, permissions, filters
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Blog, BlogImage
from .serializers import BlogListSerializer, BlogDetailSerializer, BlogImageSerializer


class IsAdminOrReadOnly(permissions.BasePermission):
    """Allow anyone to read published content; only admins can write."""
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user and request.user.is_staff


# ──────────────────────────────────────────
# FILTERS
# ──────────────────────────────────────────

class BlogFilter(django_filters.FilterSet):
    """
    ?category=travel_guides   — exact match on category value
    ?tag=safari               — post's tags list contains this tag
    ?search=...               — handled separately by SearchFilter
    """
    tag = django_filters.CharFilter(method="filter_tag", label="Tag")

    class Meta:
        model = Blog
        fields = ["category", "status"]

    def filter_tag(self, queryset, name, value):
        # Search for the quoted tag within the JSON array text, e.g. `"safari"`.
        # This avoids partial matches ("food" won't match "seafood") and works on SQLite.
        tag = value.lower().strip()
        return queryset.filter(tags__icontains=f'"{tag}"')


# ──────────────────────────────────────────
# BLOG POSTS
# ──────────────────────────────────────────

class BlogListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/blog/         — list published posts (admins see all)
    POST /api/blog/         — create a post (admin only)

    Filters:  ?category=travel_guides  ?tag=safari  ?search=accra
    Ordering: ?ordering=published_at | -published_at | title | created_at
    """
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = BlogFilter
    search_fields = ["title", "excerpt", "body"]
    ordering_fields = ["published_at", "title", "created_at"]

    def get_queryset(self):
        qs = Blog.objects.select_related("author").prefetch_related("images")
        if not (self.request.user and self.request.user.is_staff):
            qs = qs.filter(status=Blog.Status.PUBLISHED)
        return qs

    def get_serializer_class(self):
        if self.request.method == "POST":
            return BlogDetailSerializer
        return BlogListSerializer

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)


class BlogDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/blog/{id}/  — retrieve a full post
    PATCH  /api/blog/{id}/  — partial update (admin only)
    PUT    /api/blog/{id}/  — full update (admin only)
    DELETE /api/blog/{id}/  — delete (admin only)
    """
    serializer_class = BlogDetailSerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "id"

    def get_queryset(self):
        qs = Blog.objects.select_related("author").prefetch_related("images")
        if not (self.request.user and self.request.user.is_staff):
            qs = qs.filter(status=Blog.Status.PUBLISHED)
        return qs


# ──────────────────────────────────────────
# BLOG IMAGES  (nested under a post)
# ──────────────────────────────────────────

class BlogImageListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/blog/{blog_id}/images/   — list all images for a post
    POST /api/blog/{blog_id}/images/   — upload an image (admin only)
    """
    serializer_class = BlogImageSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        return BlogImage.objects.filter(blog_id=self.kwargs["blog_id"])

    def perform_create(self, serializer):
        blog = generics.get_object_or_404(Blog, id=self.kwargs["blog_id"])
        serializer.save(blog=blog)


class BlogImageDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/blog/{blog_id}/images/{id}/  — retrieve an image
    PATCH  /api/blog/{blog_id}/images/{id}/  — update caption/order/cover (admin only)
    DELETE /api/blog/{blog_id}/images/{id}/  — delete (admin only)
    """
    serializer_class = BlogImageSerializer
    permission_classes = [IsAdminOrReadOnly]
    lookup_field = "id"

    def get_queryset(self):
        return BlogImage.objects.filter(blog_id=self.kwargs["blog_id"])


# ──────────────────────────────────────────
# SIDEBAR DATA  (categories · tags · recent)
# ──────────────────────────────────────────

# Fixed ordered lists — order matches what the frontend sidebar renders
SIDEBAR_CATEGORIES = [
    (value, label)
    for value, label in Blog.Category.choices
]

SIDEBAR_TAGS = [
    "safari", "luxury", "culture", "food", "beach", "wildlife",
    "heritage", "adventure", "diaspora", "festivals", "architecture", "markets",
]


class BlogSidebarView(APIView):
    """
    GET /api/blog/sidebar/

    Returns three blocks consumed by the frontend sidebar:

    • categories  — all defined categories with live post counts (0 if none published)
    • popular_tags — fixed tag list with live post counts, sorted by count descending
    • recent_posts — published posts from the last 24 hours (newest first, max 10)
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        published = Blog.objects.filter(status=Blog.Status.PUBLISHED)

        # ── Categories — fixed order, live counts ────────────────────────────
        count_by_category = {
            row["category"]: row["count"]
            for row in published.values("category").annotate(count=Count("id"))
        }
        categories = [
            {
                "value": value,
                "label": label,
                "count": count_by_category.get(value, 0),
            }
            for value, label in SIDEBAR_CATEGORIES
        ]

        # ── Popular tags — fixed list, live counts, sorted by frequency ──────
        all_tags = []
        for tags in published.values_list("tags", flat=True):
            if isinstance(tags, list):
                all_tags.extend(tags)
        tag_counter = Counter(all_tags)
        popular_tags = sorted(
            [{"tag": tag, "count": tag_counter.get(tag, 0)} for tag in SIDEBAR_TAGS],
            key=lambda x: -x["count"],
        )

        # ── Recent posts (last 24 hours) ─────────────────────────────────────
        since = timezone.now() - timedelta(hours=24)
        recent_qs = (
            published
            .filter(published_at__gte=since)
            .select_related("author")
            .prefetch_related("images")
            .order_by("-published_at")[:10]
        )
        recent_posts = BlogListSerializer(
            recent_qs, many=True, context={"request": request}
        ).data

        return Response({
            "categories": categories,
            "popular_tags": popular_tags,
            "recent_posts": recent_posts,
        })
