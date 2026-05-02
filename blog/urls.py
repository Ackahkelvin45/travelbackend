from django.urls import path
from .views import (
    BlogListCreateView,
    BlogDetailView,
    BlogImageListCreateView,
    BlogImageDetailView,
    BlogSidebarView,
)

urlpatterns = [
    # Posts
    path("", BlogListCreateView.as_view(), name="blog-list-create"),
    path("sidebar/", BlogSidebarView.as_view(), name="blog-sidebar"),
    path("<uuid:id>/", BlogDetailView.as_view(), name="blog-detail"),

    # Images (nested under post)
    path("<uuid:blog_id>/images/", BlogImageListCreateView.as_view(), name="blog-image-list-create"),
    path("<uuid:blog_id>/images/<uuid:id>/", BlogImageDetailView.as_view(), name="blog-image-detail"),
]
