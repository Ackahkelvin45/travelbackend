from django.urls import path
from reviews.views import PackageReviewListCreateView, PackageReviewDestroyView
from .views import (
    GalleryImagesView,
    DestinationListCreateView,
    DestinationDetailView,
    DestinationImageListCreateView,
    DestinationImageDetailView,
    TravelPackageListCreateView,
    TravelPackageDetailView,
    TrendingPackagesView,
    PastPackagesView,
    ItineraryListCreateView,
    ItineraryDetailView,
    PackageImageListCreateView,
    PackageImageDetailView,
    PackageFAQListCreateView,
    PackageFAQDetailView,
)

urlpatterns = [
    # Gallery
    path("gallery/", GalleryImagesView.as_view(), name="gallery-images"),

    # Destinations
    path("destinations/", DestinationListCreateView.as_view(), name="destination-list-create"),
    path("destinations/<uuid:id>/", DestinationDetailView.as_view(), name="destination-detail"),
    path("destinations/<uuid:destination_id>/images/", DestinationImageListCreateView.as_view(), name="destination-image-list-create"),
    path("destinations/<uuid:destination_id>/images/<uuid:id>/", DestinationImageDetailView.as_view(), name="destination-image-detail"),

    # Packages
    path("", TravelPackageListCreateView.as_view(), name="package-list-create"),
    path("trending/", TrendingPackagesView.as_view(), name="package-trending"),
    path("past/", PastPackagesView.as_view(), name="package-past"),
    path("<uuid:id>/", TravelPackageDetailView.as_view(), name="package-detail"),

    # Itineraries (nested under package)
    path("<uuid:package_id>/itineraries/", ItineraryListCreateView.as_view(), name="itinerary-list-create"),
    path("<uuid:package_id>/itineraries/<uuid:id>/", ItineraryDetailView.as_view(), name="itinerary-detail"),

    # Images (nested under package)
    path("<uuid:package_id>/images/", PackageImageListCreateView.as_view(), name="package-image-list-create"),
    path("<uuid:package_id>/images/<uuid:id>/", PackageImageDetailView.as_view(), name="package-image-detail"),

    # FAQs (nested under package)
    path("<uuid:package_id>/faqs/", PackageFAQListCreateView.as_view(), name="package-faq-list-create"),
    path("<uuid:package_id>/faqs/<uuid:id>/", PackageFAQDetailView.as_view(), name="package-faq-detail"),

    # Reviews (nested under package)
    path("<uuid:package_id>/reviews/", PackageReviewListCreateView.as_view(), name="review-list-create"),
    path("<uuid:package_id>/reviews/<uuid:id>/", PackageReviewDestroyView.as_view(), name="review-destroy"),
]
