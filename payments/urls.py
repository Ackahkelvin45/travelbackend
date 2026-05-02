from django.urls import path

from .views import InitializePaymentView, VerifyPaymentView, PaystackWebhookView

app_name = "payments"

urlpatterns = [
    # POST — exchange a booking_id for a Paystack checkout URL
    path("initialize/", InitializePaymentView.as_view(), name="initialize"),

    # GET — verify after redirect / popup onSuccess
    path("verify/<str:reference>/", VerifyPaymentView.as_view(), name="verify"),

    # POST — Paystack server-to-server webhook (async safety net)
    path("webhook/", PaystackWebhookView.as_view(), name="webhook"),
]
