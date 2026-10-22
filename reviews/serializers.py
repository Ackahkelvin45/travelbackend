from django.utils.html import strip_tags
from rest_framework import serializers
from .models import Review


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = [
            "id",
            "reviewer_name",
            "reviewer_email",
            "rating",
            "title",
            "body",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {
            "reviewer_email": {"write_only": True},
            "body": {"max_length": 5000},
        }

    def validate(self, attrs):
        # Stored as plain text: markup never reaches the database, whatever
        # renders it later.
        for f in ("reviewer_name", "title", "body"):
            if attrs.get(f):
                attrs[f] = strip_tags(attrs[f]).strip()
        if not attrs.get("body"):
            raise serializers.ValidationError({"body": "Please write a few words."})
        return attrs

    def create(self, validated_data):
        package = self.context["package"]
        request = self.context["request"]
        user = request.user if request.user.is_authenticated else None
        return Review.objects.create(package=package, user=user, **validated_data)
