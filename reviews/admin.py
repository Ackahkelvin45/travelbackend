from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import Review


@admin.register(Review)
class ReviewAdmin(ModelAdmin):
    list_display = ["reviewer_name", "reviewer_email", "package", "rating", "created_at"]
    list_filter = ["rating"]
    search_fields = ["reviewer_name", "reviewer_email", "title", "body"]
    readonly_fields = ["id", "created_at"]
    ordering = ["-created_at"]
    fieldsets = (
        ("Reviewer", {
            "fields": ("id", "user", "reviewer_name", "reviewer_email"),
        }),
        ("Review", {
            "fields": ("package", "booking", "rating", "title", "body"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
        }),
    )
