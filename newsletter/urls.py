from django.urls import path

from .views import NewsletterSubscribeView, NewsletterUnsubscribeView

urlpatterns = [
    path("subscribe/", NewsletterSubscribeView.as_view(), name="newsletter-subscribe"),
    path("unsubscribe/", NewsletterUnsubscribeView.as_view(), name="newsletter-unsubscribe"),
]

