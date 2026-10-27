from django.urls import path

from .views import (
    BookingStatusView,
    CurrentPoliciesView,
    CheckoutView,
    ConfigurableCheckoutView,
    AvailableAddonsView,
    CancellationQuoteView,
    CancellationRequestView,
    ClaimBookingView,
    ComponentCheckoutView,
    ComponentQuoteView,
    HoldView,
    TravellerShareView,
    TravelInfoView,
    BookingPreferencesView,
    CreateBookingView,
    MyBookingsView,
    GuestInviteClaimView,
    GuestInviteView,
    MyTripsView,
    TravellerInviteView,
)

app_name = "bookings"

urlpatterns = [
    # POST — create a new booking (guest or authenticated, legacy tier flow)
    path("", CreateBookingView.as_view(), name="create"),

    # POST — option-based checkout (flagship hotel/occupancy flow)
    path("checkout/", CheckoutView.as_view(), name="checkout"),

    # POST — core + add-ons checkout (Michael Blackson style)
    path("checkout/configurable/", ConfigurableCheckoutView.as_view(), name="checkout-configurable"),

    # Component pricing (Change Request v2): quote → hold → checkout
    path("quote/", ComponentQuoteView.as_view(), name="quote"),
    path("holds/", HoldView.as_view(), name="holds"),
    path("holds/<str:token>/", HoldView.as_view(), name="hold-release"),
    path("checkout/component/", ComponentCheckoutView.as_view(), name="checkout-component"),
    path("share/<uuid:traveller_id>/", TravellerShareView.as_view(), name="traveller-share"),

    # GET — currently-published policy documents (accepted at checkout)
    path("policies/", CurrentPoliciesView.as_view(), name="policies"),

    # GET — authenticated dashboard listing
    path("mine/", MyBookingsView.as_view(), name="mine"),
    # GET — trips the user joined as an invited guest (their own part only)
    path("trips/", MyTripsView.as_view(), name="trips"),
    # Guest invites: the lead sends one; the guest completes it and links an account
    path("invite/<str:token>/", GuestInviteView.as_view(), name="guest-invite"),
    path("invite/<str:token>/claim/", GuestInviteClaimView.as_view(), name="guest-invite-claim"),

    # POST — attach a guest booking via emailed claim token
    path("claim/", ClaimBookingView.as_view(), name="claim"),

    # Owner-only cancellation: preview the refund, then lodge the request
    path("<str:reference>/addons/available/", AvailableAddonsView.as_view(), name="addons-available"),
    path("<str:reference>/travellers/<uuid:traveller_id>/invite/", TravellerInviteView.as_view(), name="traveller-invite"),
    path("<str:reference>/travel/", TravelInfoView.as_view(), name="travel-info"),
    path("<str:reference>/preferences/", BookingPreferencesView.as_view(), name="preferences"),
    path("<str:reference>/cancellation-quote/", CancellationQuoteView.as_view(), name="cancellation-quote"),
    path("<str:reference>/cancellation-request/", CancellationRequestView.as_view(), name="cancellation-request"),

    # GET — check booking + payment status by booking reference
    path("<str:reference>/", BookingStatusView.as_view(), name="status"),
]
