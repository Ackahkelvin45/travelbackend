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
        }

    def create(self, validated_data):
        package = self.context["package"]
        request = self.context["request"]
        user = request.user if request.user.is_authenticated else None
        return Review.objects.create(package=package, user=user, **validated_data)
