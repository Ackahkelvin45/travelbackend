from django.urls import path

from .views import (
    BookingStatusView,
    CurrentPoliciesView,
    CheckoutView,
    ClaimBookingView,
    CreateBookingView,
    MyBookingsView,
)

app_name = "bookings"

urlpatterns = [
    # POST — create a new booking (guest or authenticated, legacy tier flow)
    path("", CreateBookingView.as_view(), name="create"),

    # POST — option-based checkout (flagship hotel/occupancy flow)
    path("checkout/", CheckoutView.as_view(), name="checkout"),

    # GET — currently-published policy documents (accepted at checkout)
    path("policies/", CurrentPoliciesView.as_view(), name="policies"),

    # GET — authenticated dashboard listing
    path("mine/", MyBookingsView.as_view(), name="mine"),

    # POST — attach a guest booking via emailed claim token
    path("claim/", ClaimBookingView.as_view(), name="claim"),

    # GET — check booking + payment status by booking reference
    path("<str:reference>/", BookingStatusView.as_view(), name="status"),
]
