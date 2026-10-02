from django.urls import path

from .staff_views import (
    StaffCancellationListView,
    StaffCancellationResolveView,
    StaffRefundListView,
    StaffRefundResolveView,
    StaffBookingDetailView,
    StaffBookingEmailsView,
    StaffCancelBookingView,
    StaffBookingListView,
    StaffExtendDeadlineView,
    StaffMarkArrivedView,
    StaffTravellerChangeView,
    StaffRemoveAddonView,
    StaffMeView,
    StaffOfflinePaymentView,
)

app_name = "staff"

urlpatterns = [
    path("me/", StaffMeView.as_view(), name="me"),
    path("bookings/", StaffBookingListView.as_view(), name="bookings"),
    path("bookings/<str:reference>/", StaffBookingDetailView.as_view(), name="booking"),
    path("bookings/<str:reference>/offline-payment/", StaffOfflinePaymentView.as_view(), name="offline-payment"),
    path("bookings/<str:reference>/extend-deadline/", StaffExtendDeadlineView.as_view(), name="extend-deadline"),
    path("bookings/<str:reference>/mark-arrived/", StaffMarkArrivedView.as_view(), name="mark-arrived"),
    path("bookings/<str:reference>/emails/", StaffBookingEmailsView.as_view(), name="booking-emails"),
    path("bookings/<str:reference>/traveller/", StaffTravellerChangeView.as_view(), name="booking-traveller"),
    path("bookings/<str:reference>/remove-addon/", StaffRemoveAddonView.as_view(), name="booking-remove-addon"),
    path("bookings/<str:reference>/cancel/", StaffCancelBookingView.as_view(), name="booking-cancel"),
    path("cancellations/", StaffCancellationListView.as_view(), name="cancellations"),
    path("cancellations/<uuid:pk>/<str:decision>/", StaffCancellationResolveView.as_view(), name="cancellation-resolve"),
    path("refunds/", StaffRefundListView.as_view(), name="refunds"),
    path("refunds/<uuid:pk>/<str:decision>/", StaffRefundResolveView.as_view(), name="refund-resolve"),
]
