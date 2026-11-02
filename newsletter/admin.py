from django.contrib import admin
from config.admin_tools import safe_csv
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import RangeDateFilter

from .models import NewsletterSubscriber


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(ModelAdmin):
    list_display = ["email", "is_active", "created_at", "updated_at"]
    list_filter = ["is_active", ("created_at", RangeDateFilter)]
    list_filter_submit = True
    search_fields = ["email"]
    ordering = ["-created_at"]
    actions = ["activate", "deactivate", "export_csv"]

    @admin.action(description="Activate", permissions=["change"])
    def activate(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=True)} subscriber(s) activated.")

    @admin.action(description="Deactivate", permissions=["change"])
    def deactivate(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=False)} subscriber(s) deactivated.")

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        from django.http import HttpResponse
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="azura-subscribers.csv"'
        w = safe_csv(response)
        w.writerow(["Email", "Active", "Joined"])
        w.writerows(queryset.values_list("email", "is_active", "created_at"))
        return response

