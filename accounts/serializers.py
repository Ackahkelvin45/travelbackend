from django.contrib.auth import get_user_model
from rest_framework import serializers

from .password_validation import PasswordStrengthValidator

User = get_user_model()


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = [
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "country",
            "password",
            "password_confirm",
        ]

    def validate_password(self, value):
        # Same five rules as the frontend checklist; raises with readable
        # messages so the UI can surface them if client checks were skipped.
        PasswordStrengthValidator().validate(value)
        return value

    def validate(self, data):
        if data["password"] != data.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        return data

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class UserProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone_number",
            "country",
            "email_verified",
            "is_staff",
            "date_joined",
            "updated_at",
        ]
        read_only_fields = ["id", "email", "email_verified", "is_staff", "date_joined", "updated_at"]
