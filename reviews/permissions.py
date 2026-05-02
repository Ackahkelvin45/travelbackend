from rest_framework.permissions import BasePermission


class IsReviewOwnerOrAdmin(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True
        return obj.user is not None and obj.user == request.user
