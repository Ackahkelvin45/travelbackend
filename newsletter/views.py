from rest_framework import generics, permissions, status
from rest_framework.response import Response

from .models import NewsletterSubscriber
from .serializers import NewsletterSubscriberSerializer


class NewsletterSubscribeView(generics.CreateAPIView):
    """
    POST /api/newsletter/subscribe/
    Subscribe an email to package announcements.
    """

    serializer_class = NewsletterSubscriberSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        subscriber, created = NewsletterSubscriber.objects.get_or_create(
            email=email,
            defaults={"is_active": True},
        )

        if not created and not subscriber.is_active:
            subscriber.is_active = True
            subscriber.save(update_fields=["is_active", "updated_at"])

        output_serializer = self.get_serializer(subscriber)
        response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(output_serializer.data, status=response_status)




class NewsletterUnsubscribeView(generics.GenericAPIView):
    """GET /api/newsletter/unsubscribe/?token=… — one click from the email
    footer. Idempotent; shows a plain confirmation page (no login, no app)."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.core import signing
        from django.http import HttpResponse

        from .services import UNSUBSCRIBE_SALT

        try:
            email = signing.loads(request.query_params.get("token", ""), salt=UNSUBSCRIBE_SALT)["e"]
        except (signing.BadSignature, KeyError, TypeError):
            return HttpResponse(_page("This unsubscribe link isn't valid.", "Please use the link from a recent email, or contact hello@azuratravels.live."), status=400)
        NewsletterSubscriber.objects.filter(email__iexact=email).update(is_active=False)
        return HttpResponse(_page("You've been unsubscribed.", "You won't receive travel updates from Azura Travels any more. Changed your mind? Subscribe again on our website any time."))


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head><body style="margin:0;background:#f0ece4;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:520px;margin:60px auto;background:#fff;border-radius:16px;padding:40px;text-align:center;">
<h1 style="color:#d4a843;font-size:14px;letter-spacing:3px;text-transform:uppercase;margin:0 0 20px;">Azura Travels</h1>
<p style="font-size:20px;font-weight:800;color:#1a1a2e;margin:0 0 10px;">{title}</p>
<p style="font-size:14px;color:#555;line-height:1.6;margin:0;">{body}</p></div></body></html>"""
