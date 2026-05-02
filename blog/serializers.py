from rest_framework import serializers
from .models import Blog, BlogImage


class BlogImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogImage
        fields = ["id", "image", "caption", "is_cover", "order", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]


class BlogListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for listing — cover image and author name only."""
    author_name = serializers.SerializerMethodField()
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    cover_image = serializers.SerializerMethodField()

    class Meta:
        model = Blog
        fields = [
            "id",
            "title",
            "slug",
            "author_name",
            "category",
            "category_display",
            "excerpt",
            "tags",
            "status",
            "published_at",
            "cover_image",
        ]

    def get_author_name(self, obj):
        return obj.author.full_name if obj.author else None

    def get_cover_image(self, obj):
        cover = obj.images.filter(is_cover=True).first() or obj.images.first()
        if cover:
            return BlogImageSerializer(cover, context=self.context).data
        return None


class BlogDetailSerializer(serializers.ModelSerializer):
    """Full serializer with body and all images."""
    author_name = serializers.SerializerMethodField()
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    images = BlogImageSerializer(many=True, read_only=True)

    class Meta:
        model = Blog
        fields = [
            "id",
            "title",
            "slug",
            "author_name",
            "category",
            "category_display",
            "excerpt",
            "body",
            "tags",
            "status",
            "published_at",
            "images",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "published_at", "created_at", "updated_at"]

    def get_author_name(self, obj):
        return obj.author.full_name if obj.author else None
