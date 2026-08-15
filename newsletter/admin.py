from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import RangeDateFilter

from .models import NewsletterSubscriber


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(ModelAdmin):
    list_display = ["email", "is_active", "created_at", "updated_at"]
    list_filter = ["is_active", ("created_at", RangeDateFilter)]
    list_filter_submit = True
    search_fields = ["email"]
    list_editable = ["is_active"]
    ordering = ["-created_at"]

