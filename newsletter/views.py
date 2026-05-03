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

