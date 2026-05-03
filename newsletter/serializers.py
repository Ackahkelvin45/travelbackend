from rest_framework import serializers

from .models import NewsletterSubscriber


class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()

    class Meta:
        model = NewsletterSubscriber
        fields = ["id", "email", "is_active", "created_at"]
        read_only_fields = ["id", "is_active", "created_at"]
        extra_kwargs = {
            "email": {"validators": []},
        }

    def validate_email(self, value):
        return value.strip().lower()
