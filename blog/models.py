from django.conf import settings
from django.db import models
from django.utils import timezone
import uuid


class Blog(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    class Category(models.TextChoices):
        TRAVEL_GUIDES = "travel_guides", "Travel Guides"
        CULTURAL_EXPERIENCES = "cultural_experiences", "Cultural Experiences"
        SAFARI_WILDLIFE = "safari_wildlife", "Safari & Wildlife"
        LUXURY_TRAVEL = "luxury", "Luxury Travel"
        FOOD_CUISINE = "food", "Food & Cuisine"
        DIASPORA = "diaspora", "Diaspora Stories"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=300)
    slug = models.SlugField(unique=True, max_length=320)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blogs",
    )
    author_first_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="First name of the person who published this post",
    )
    author_last_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Last name of the person who published this post",
    )
    category = models.CharField(max_length=30, choices=Category.choices)
    excerpt = models.TextField(max_length=500, help_text="Short summary shown on listing cards")
    body = models.TextField(help_text="Full blog content")
    tags = models.JSONField(default=list, help_text="List of tag strings, e.g. ['accra', 'safari']")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Stamp published_at the first time status becomes published
        if self.status == self.Status.PUBLISHED and not self.published_at:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    @property
    def is_published(self):
        return self.status == self.Status.PUBLISHED

    @property
    def author_full_name(self):
        """Return the manually entered author name, falling back to the linked user."""
        parts = [self.author_first_name, self.author_last_name]
        name = " ".join(p for p in parts if p).strip()
        if name:
            return name
        return str(self.author) if self.author else "Unknown"


class PendingBlog(Blog):
    """Proxy model used exclusively by the Blog Approval admin section.
    Shows only draft blogs awaiting review — no extra database table.
    """

    class Meta:
        proxy = True
        verbose_name = "Blog Pending Approval"
        verbose_name_plural = "Blog Approvals"


class BlogImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    blog = models.ForeignKey(Blog, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="blog/images/")
    caption = models.CharField(max_length=200, blank=True, null=True)
    is_cover = models.BooleanField(default=False, help_text="Mark as the hero/cover image")
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "uploaded_at"]

    def __str__(self):
        return f"Image for '{self.blog.title}' ({'cover' if self.is_cover else f'order {self.order}'})"

    def save(self, *args, **kwargs):
        # Ensure only one cover image per blog post
        if self.is_cover:
            BlogImage.objects.filter(blog=self.blog, is_cover=True).exclude(pk=self.pk).update(is_cover=False)
        super().save(*args, **kwargs)
