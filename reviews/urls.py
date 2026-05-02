from django.urls import path
from .views import PackageReviewListCreateView, PackageReviewDestroyView

urlpatterns = [
    path("", PackageReviewListCreateView.as_view(), name="review-list-create"),
    path("<uuid:id>/", PackageReviewDestroyView.as_view(), name="review-destroy"),
]
