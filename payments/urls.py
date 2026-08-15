from django.urls import path

from .views import (
    InitializePaymentView,
    PaymentChannelsView,
    PaymentStatusView,
    PaystackWebhookView,
    VerifyPaymentView,
)

app_name = "payments"

urlpatterns = [
    # GET — payment methods the checkout offers (server-controlled routing)
    path("channels/", PaymentChannelsView.as_view(), name="channels"),

    # POST — exchange a booking_id for a Paystack charge (fresh attempt each call)
    path("initialize/", InitializePaymentView.as_view(), name="initialize"),

    # GET — one-shot verify after redirect / popup onSuccess (hits Paystack once)
    path("verify/<str:reference>/", VerifyPaymentView.as_view(), name="verify"),

    # GET — DB-only poll target for the callback page
    path("status/<str:reference>/", PaymentStatusView.as_view(), name="status"),

    # POST — Paystack server-to-server webhook (async source of truth)
    path("webhook/", PaystackWebhookView.as_view(), name="webhook"),
]
