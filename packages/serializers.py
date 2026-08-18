from django.db.models import Avg
from rest_framework import serializers
from drf_yasg.utils import swagger_serializer_method
from .models import (
    TravelPackage,
    Itinerary,
    PackageImage,
    PackageFAQ,
    Destination,
    DestinationImage,
    PackageOption,
    TourDeparture,
)


class DestinationImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = DestinationImage
        fields = ["id", "image", "caption", "is_cover", "order", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]


class DestinationSerializer(serializers.ModelSerializer):
    images = DestinationImageSerializer(many=True, read_only=True)
    package_count = serializers.SerializerMethodField()

    class Meta:
        model = Destination
        fields = [
            "id",
            "name",
            "description",
            "map_url",
            "latitude",
            "longitude",
            "package_count",
            "images",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    @swagger_serializer_method(serializer_or_field=serializers.IntegerField())
    def get_package_count(self, obj):
        if hasattr(obj, "package_count"):
            return obj.package_count
        return obj.packages.count()


class PackageImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PackageImage
        fields = ["id", "image", "caption", "is_cover", "order", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]


class GalleryPackageImageSerializer(serializers.ModelSerializer):
    package_id = serializers.UUIDField(source="package.id", read_only=True)
    package_title = serializers.CharField(source="package.title", read_only=True)
    image_type = serializers.SerializerMethodField()

    class Meta:
        model = PackageImage
        fields = [
            "id",
            "image",
            "caption",
            "is_cover",
            "order",
            "uploaded_at",
            "image_type",
            "package_id",
            "package_title",
        ]

    @swagger_serializer_method(serializer_or_field=serializers.CharField())
    def get_image_type(self, obj):
        return "package"


class GalleryDestinationImageSerializer(serializers.ModelSerializer):
    destination_id = serializers.UUIDField(source="destination.id", read_only=True)
    destination_name = serializers.CharField(source="destination.name", read_only=True)
    image_type = serializers.SerializerMethodField()

    class Meta:
        model = DestinationImage
        fields = [
            "id",
            "image",
            "caption",
            "is_cover",
            "order",
            "uploaded_at",
            "image_type",
            "destination_id",
            "destination_name",
        ]

    @swagger_serializer_method(serializer_or_field=serializers.CharField())
    def get_image_type(self, obj):
        return "destination"


class GalleryResponseSerializer(serializers.Serializer):
    packages = GalleryPackageImageSerializer(many=True)
    destinations = GalleryDestinationImageSerializer(many=True)
    total_packages = serializers.IntegerField()
    total_destinations = serializers.IntegerField()
    total_images = serializers.IntegerField()


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


class PackageOptionSerializer(serializers.ModelSerializer):
    """A bookable hotel × occupancy variant with its current effective price."""

    occupancy_display = serializers.CharField(source="get_occupancy_display", read_only=True)
    guests_per_booking = serializers.IntegerField(read_only=True)

    class Meta:
        model = PackageOption
        fields = [
            "id",
            "hotel_name",
            "star_rating",
            "hotel_image",
            "occupancy",
            "occupancy_display",
            "guests_per_booking",
            "price_per_person",
            "early_bird_price_per_person",
            "is_active",
            "order",
        ]
        read_only_fields = ["id"]


class TourDepartureSerializer(serializers.ModelSerializer):
    """A single scheduled date a day tour runs, with remaining availability."""
    seats_left = serializers.IntegerField(read_only=True)
    is_full = serializers.BooleanField(read_only=True)

    class Meta:
        model = TourDeparture
        fields = ["id", "date", "capacity", "seats_left", "is_full", "note"]
        read_only_fields = fields


class TripUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        from .models import TripUpdate
        model = TripUpdate
        fields = ["id", "title", "body", "published_at"]
        read_only_fields = fields


class TravelPackageListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views — includes cover image and rating summary."""
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    destination = serializers.SerializerMethodField()
    destinations = DestinationSerializer(many=True, read_only=True)
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
            "destinations",
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
            "has_options",
            "from_price",
            "is_day_tour",
            "price_usd_estimate",
            "next_departure",
        ]

    has_options = serializers.BooleanField(read_only=True)
    from_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    next_departure = serializers.SerializerMethodField()

    def get_next_departure(self, obj):
        if not obj.is_day_tour:
            return None
        upcoming = obj.upcoming_departures()
        return upcoming[0].date if upcoming else None

    def get_cover_image(self, obj):
        cover = obj.images.filter(is_cover=True).first() or obj.images.first()
        if cover:
            return PackageImageSerializer(cover, context=self.context).data
        return None

    def get_destination(self, obj):
        return ", ".join(destination.name for destination in obj.destinations.all()) or None

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
    destination = serializers.SerializerMethodField()
    destinations = DestinationSerializer(many=True, read_only=True)
    destination_ids = serializers.PrimaryKeyRelatedField(
        source="destinations",
        many=True,
        queryset=Destination.objects.all(),
        write_only=True,
        required=False,
    )
    images = PackageImageSerializer(many=True, read_only=True)
    itineraries = ItinerarySerializer(many=True, read_only=True)
    faqs = PackageFAQSerializer(many=True, read_only=True)
    options = PackageOptionSerializer(many=True, read_only=True)
    departures = serializers.SerializerMethodField()
    avg_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    has_options = serializers.BooleanField(read_only=True)
    from_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    early_bird_active = serializers.BooleanField(read_only=True)

    @swagger_serializer_method(serializer_or_field=TourDepartureSerializer(many=True))
    def get_departures(self, obj):
        """Upcoming, bookable departures (day tours only)."""
        if not obj.is_day_tour:
            return []
        return TourDepartureSerializer(obj.upcoming_departures(), many=True, context=self.context).data

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
            "whats_excluded",
            "destination",
            "destinations",
            "destination_ids",
            "duration_days",
            "max_guests",
            "price_shared",
            "price_private",
            "price_vip",
            "from_price",
            "currency",
            "available_from",
            "available_to",
            "has_options",
            "options",
            "is_day_tour",
            "price_usd_estimate",
            "departures",
            "early_bird_deadline",
            "early_bird_active",
            "allow_installments",
            "deposit_minimum",
            "final_payment_deadline",
            "visa_addon_enabled",
            "visa_fee",
            "visa_info",
            "refund_tiers",
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

    def get_destination(self, obj):
        return ", ".join(destination.name for destination in obj.destinations.all()) or None

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
