from django.urls import path

from .views import BookingStatusView, CreateBookingView

app_name = "bookings"

urlpatterns = [
    # POST — create a new booking (guest or authenticated)
    path("", CreateBookingView.as_view(), name="create"),

    # GET — check booking + payment status by booking reference
    path("<str:reference>/", BookingStatusView.as_view(), name="status"),
]
