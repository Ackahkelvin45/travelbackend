from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from .models import User

# Re-register Group under unfold so it doesn't render with the default
# (unstyled) admin.
admin.site.unregister(Group)


def _granted(user) -> set:
    """Permissions held, read from the tables: Django reports none for an
    inactive account, which would make it look harmless to manage."""
    rows = Permission.objects.filter(Q(user=user) | Q(group__user=user))
    return set(rows.values_list("content_type__app_label", "codename"))


def _own(request) -> set:
    return _granted(request.user)


@admin.register(Group)
class GroupAdmin(ModelAdmin):
    """Groups define what staff can do, so only superusers change them."""
    search_fields = ["name"]
    ordering = ["name"]

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(User)
class UserAdmin(ModelAdmin, BaseUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    list_display = ("email", "first_name", "last_name", "is_staff", "is_active", "date_joined")
    list_filter = ("is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("-date_joined",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "phone_number", "country")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("date_joined", "updated_at")}),
    )
    readonly_fields = ("date_joined", "updated_at")
    actions = ["activate_users", "deactivate_users"]
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "password1", "password2"),
        }),
    )

    def _manageable(self, request, queryset):
        return queryset.filter(pk__in=[u.pk for u in queryset if self._outranks(request, u)])

    @admin.action(description="Activate", permissions=["change"])
    def activate_users(self, request, queryset):
        self.message_user(request, f"{self._manageable(request, queryset).update(is_active=True)} account(s) activated.")

    @admin.action(description="Deactivate", permissions=["change"])
    def deactivate_users(self, request, queryset):
        done = self._manageable(request, queryset).exclude(pk=request.user.pk).update(is_active=False)
        self.message_user(request, f"{done} account(s) deactivated. They can no longer sign in.")

    # No one gains access through this page. Only a superuser touches superuser
    # status or individual permissions; other staff can manage only accounts
    # holding no more than they hold, and hand out only groups they could use
    # themselves. So no one can take over a stronger account by resetting it.
    def _outranks(self, request, obj) -> bool:
        return request.user.is_superuser or not (obj.is_superuser or _granted(obj) - _own(request))

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if request.user.is_superuser:
            return fields
        # The email is the sign-in, so changing it would take the account over.
        return (*fields, "is_superuser", "user_permissions", *(("email",) if obj else ()))

    def user_change_password(self, request, id, form_url=""):
        # Same reason: setting someone's password takes the account over.
        if not request.user.is_superuser and str(request.user.pk) != str(id):
            raise PermissionDenied
        return super().user_change_password(request, id, form_url)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "groups" and not request.user.is_superuser:
            mine = _own(request)
            kwargs["queryset"] = Group.objects.filter(pk__in=[
                g.pk for g in Group.objects.prefetch_related("permissions__content_type")
                if {(p.content_type.app_label, p.codename) for p in g.permissions.all()} <= mine
            ])
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or self._outranks(request, obj))

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or self._outranks(request, obj))
