from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import NewsletterSubscriber


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(ModelAdmin):
    list_display = ["email", "is_active", "created_at", "updated_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["email"]
    list_editable = ["is_active"]
    ordering = ["-created_at"]

