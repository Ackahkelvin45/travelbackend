from django.db.models import Avg
from rest_framework import serializers
from .models import TravelPackage, Itinerary, PackageImage, PackageFAQ


class PackageImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageImage
        fields = ["id", "image", "caption", "is_cover", "order", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]


class PackageFAQSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageFAQ
        fields = ["id", "question", "answer", "order"]
        read_only_fields = ["id"]


class ItinerarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Itinerary
        fields = [
            "id",
            "day",
            "title",
            "description",
            "activities",
            "notes",
        ]


class TravelPackageListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views — includes cover image and rating summary."""
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    cover_image = serializers.SerializerMethodField()
    avg_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()

    class Meta:
        model = TravelPackage
        fields = [
            "id",
            "title",
            "slug",
            "category",
            "category_display",
            "destination",
            "duration_days",
            "price_shared",
            "price_private",
            "price_vip",
            "currency",
            "is_featured",
            "is_active",
            "available_from",
            "available_to",
            "cover_image",
            "avg_rating",
            "review_count",
        ]

    def get_cover_image(self, obj):
        cover = obj.images.filter(is_cover=True).first() or obj.images.first()
        if cover:
            return PackageImageSerializer(cover, context=self.context).data
        return None

    def get_avg_rating(self, obj):
        if hasattr(obj, "avg_rating"):
            val = obj.avg_rating
            return round(val, 1) if val is not None else None
        result = obj.reviews.aggregate(avg=Avg("rating"))
        val = result["avg"]
        return round(val, 1) if val is not None else None

    def get_review_count(self, obj):
        if hasattr(obj, "review_count"):
            return obj.review_count
        return obj.reviews.count()


class TravelPackageDetailSerializer(serializers.ModelSerializer):
    """Full serializer with nested images, itineraries, and review stats."""
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    images = PackageImageSerializer(many=True, read_only=True)
    itineraries = ItinerarySerializer(many=True, read_only=True)
    faqs = PackageFAQSerializer(many=True, read_only=True)
    avg_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()

    class Meta:
        model = TravelPackage
        fields = [
            "id",
            "title",
            "slug",
            "category",
            "category_display",
            "description",
            "highlights",
            "whats_included",
            "destination",
            "map_url",
            "latitude",
            "longitude",
            "duration_days",
            "max_guests",
            "price_shared",
            "price_private",
            "price_vip",
            "currency",
            "available_from",
            "available_to",
            "is_active",
            "is_featured",
            "avg_rating",
            "review_count",
            "images",
            "itineraries",
            "faqs",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_avg_rating(self, obj):
        if hasattr(obj, "avg_rating"):
            val = obj.avg_rating
            return round(val, 1) if val is not None else None
        result = obj.reviews.aggregate(avg=Avg("rating"))
        val = result["avg"]
        return round(val, 1) if val is not None else None

    def get_review_count(self, obj):
        if hasattr(obj, "review_count"):
            return obj.review_count
        return obj.reviews.count()
