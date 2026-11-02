from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import RelatedDropdownFilter
from .models import Review


@admin.register(Review)
class ReviewAdmin(ModelAdmin):
    list_display = ["reviewer_name", "reviewer_email", "package", "rating_display", "is_visible", "created_at"]
    list_filter = ["is_visible", "rating", ("package", RelatedDropdownFilter)]
    actions = ["show_reviews", "hide_reviews"]
    search_fields = ["reviewer_name", "reviewer_email", "title", "body"]
    readonly_fields = ["id", "created_at"]
    ordering = ["-created_at"]
    fieldsets = (
        ("Reviewer", {
            "fields": ("id", "user", "reviewer_name", "reviewer_email"),
        }),
        ("Review", {
            "fields": ("package", "booking", "rating", "title", "body", "is_visible"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
        }),
    )

    @admin.action(description="Show on the site", permissions=["change"])
    def show_reviews(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_visible=True)} review(s) shown.")

    @admin.action(description="Hide from the site", permissions=["change"])
    def hide_reviews(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_visible=False)} review(s) hidden.")

    @admin.display(description="Rating", ordering="rating")
    def rating_display(self, obj):
        return "★" * obj.rating + "☆" * (5 - obj.rating)
