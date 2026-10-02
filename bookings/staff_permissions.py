"""Staff API permission: the user must be staff AND hold the view's
`required_perm`. The permission names are the same Django permissions the
admin checks and `setup_staff_groups` grants — one table, two front doors."""

from rest_framework.permissions import BasePermission


class StaffPermission(BasePermission):
    message = "You don't have permission to do that."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        perm = getattr(view, "required_perm", None)
        return user.has_perm(perm) if perm else True
