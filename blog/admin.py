import json

from django import forms
from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline

from .models import Blog, BlogImage, PendingBlog


# ---------------------------------------------------------------------------
# Custom widget: renders a JSON list of tags as comma-separated plain text
# ---------------------------------------------------------------------------

class TagsWidget(forms.TextInput):
    """Display a JSONField list as a simple comma-separated text input."""

    def format_value(self, value):
        if isinstance(value, list):
            return ", ".join(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return ", ".join(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        return value or ""


class CKEditor5Widget(forms.Textarea):
    """CKEditor 5 Classic editor loaded from CDN — no extra package required."""

    class Media:
        css = {"all": ["https://cdn.ckeditor.com/ckeditor5/43.3.1/ckeditor5.css"]}
        js = ["https://cdn.ckeditor.com/ckeditor5/43.3.1/ckeditor5.umd.js"]

    def render(self, name, value, attrs=None, renderer=None):
        textarea_html = super().render(name, value, attrs, renderer)
        widget_id = (attrs or {}).get("id") or f"id_{name}"
        init = (
            f"<script>"
            f"(function(){{"
            f"  function init(){{"
            f"    var el=document.getElementById('{widget_id}');"
            f"    if(!el||!window.CKEDITOR)return;"
            f"    var C=window.CKEDITOR;"
            f"    C.ClassicEditor.create(el,{{"
            f"      plugins:["
            f"        C.Essentials,C.Bold,C.Italic,C.Underline,C.Strikethrough,"
            f"        C.Link,C.Paragraph,C.Heading,"
            f"        C.List,C.ListProperties,C.BlockQuote,"
            f"        C.Table,C.TableToolbar,C.TableProperties,C.TableCellProperties,"
            f"        C.HorizontalLine,C.Indent,C.IndentBlock"
            f"      ],"
            f"      toolbar:{{"
            f"        items:['heading','|','bold','italic','underline','strikethrough','|',"
            f"               'link','|','bulletedList','numberedList','|','outdent','indent','|',"
            f"               'blockQuote','insertTable','horizontalLine'],"
            f"        shouldNotGroupWhenFull:true"
            f"      }},"
            f"      table:{{"
            f"        contentToolbar:['tableColumn','tableRow','mergeTableCells']"
            f"      }}"
            f"    }}).catch(console.error);"
            f"  }}"
            f"  if(document.readyState==='loading'){{"
            f"    document.addEventListener('DOMContentLoaded',init);"
            f"  }}else{{"
            f"    init();"
            f"  }}"
            f"}})();"
            f"</script>"
        )
        return mark_safe(str(textarea_html) + init)


PREDEFINED_TAGS = [
    ("safari", "Safari"),
    ("luxury", "Luxury"),
    ("culture", "Culture"),
    ("food", "Food"),
    ("beach", "Beach"),
    ("wildlife", "Wildlife"),
    ("heritage", "Heritage"),
    ("adventure", "Adventure"),
    ("diaspora", "Diaspora"),
    ("festivals", "Festivals"),
    ("architecture", "Architecture"),
    ("markets", "Markets"),
]


class BlogAdminForm(forms.ModelForm):
    """ModelForm that replaces tags with a checkbox list and body with CKEditor."""

    tags = forms.MultipleChoiceField(
        choices=PREDEFINED_TAGS,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Select all tags that apply.",
    )

    body = forms.CharField(
        required=False,
        widget=CKEditor5Widget(attrs={"rows": 30}),
    )

    class Meta:
        model = Blog
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance and isinstance(instance.tags, list):
            self.initial["tags"] = instance.tags

    def clean_tags(self):
        return self.cleaned_data.get("tags", [])


# ---------------------------------------------------------------------------
# Inline: images with live preview thumbnail
# ---------------------------------------------------------------------------

class BlogImageInline(TabularInline):
    model = BlogImage
    extra = 1
    fields = ["image_preview", "image", "caption", "is_cover", "order"]
    readonly_fields = ["image_preview"]

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height:90px; border-radius:6px; '
                'box-shadow:0 1px 4px rgba(0,0,0,.25);" />',
                obj.image.url,
            )
        return "—"

    image_preview.short_description = "Preview"


# ---------------------------------------------------------------------------
# Main Blog admin
# ---------------------------------------------------------------------------

@admin.register(Blog)
class BlogAdmin(ModelAdmin):
    form = BlogAdminForm
    inlines = [BlogImageInline]

    list_display = [
        "title",
        "author_display",
        "category",
        "status",
        "published_at",
        "created_at",
    ]
    list_filter = ["status", "category"]
    search_fields = [
        "title",
        "excerpt",
        "body",
        "author_first_name",
        "author_last_name",
    ]
    prepopulated_fields = {"slug": ("title",)}
    ordering = ["-created_at"]
    actions = ["approve_selected"]

    fieldsets = (
        (
            "Post Details",
            {
                "fields": ("title", "slug", "category", "status"),
            },
        ),
        (
            "Author",
            {
                "description": "Fill in the first and last name of the person publishing this post.",
                "fields": ("author", "author_first_name", "author_last_name"),
            },
        ),
        (
            "Content",
            {
                "fields": ("excerpt", "body", "tags"),
            },
        ),
        (
            "Publishing",
            {
                "fields": ("published_at",),
            },
        ),
    )

    # ------------------------------------------------------------------
    # List display helpers
    # ------------------------------------------------------------------

    @admin.display(description="Author")
    def author_display(self, obj):
        name = obj.author_full_name
        return name if name != "Unknown" else (str(obj.author) if obj.author else "—")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @admin.action(description="✅  Approve selected blogs (publish immediately)")
    def approve_selected(self, request, queryset):
        updated = queryset.filter(status=Blog.Status.DRAFT).update(
            status=Blog.Status.PUBLISHED
        )
        self.message_user(request, f"{updated} blog(s) approved and published.")


# ---------------------------------------------------------------------------
# Approval admin — separate sidebar entry showing only DRAFT blogs
# ---------------------------------------------------------------------------

class PendingBlogImageInline(TabularInline):
    """Read-only image gallery shown on the approval change-form."""

    model = BlogImage
    extra = 0
    can_delete = False
    fields = ["image_preview_large", "caption", "is_cover"]
    readonly_fields = ["image_preview_large", "caption", "is_cover"]

    def image_preview_large(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-width:320px; border-radius:8px; '
                'box-shadow:0 2px 8px rgba(0,0,0,.3);" />',
                obj.image.url,
            )
        return "No image"

    image_preview_large.short_description = "Image"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PendingBlog)
class PendingBlogAdmin(ModelAdmin):
    """
    Read-and-approve workflow.

    Open a draft blog, read the excerpt and body, browse its images,
    then hit the 'Approve' action — or change status directly in the
    Status field and save.
    """

    inlines = [PendingBlogImageInline]
    actions = ["approve_selected"]

    # ------------------------------------------------------------------
    # List view — show enough to decide without opening the post
    # ------------------------------------------------------------------
    list_display = [
        "title",
        "author_display",
        "category",
        "cover_thumbnail",
        "created_at",
    ]
    list_filter = ["category"]
    search_fields = ["title", "excerpt", "author_first_name", "author_last_name"]
    ordering = ["-created_at"]

    # ------------------------------------------------------------------
    # Change form — read-only content + editable status only
    # ------------------------------------------------------------------
    readonly_fields = [
        "title",
        "slug",
        "author_display",
        "category",
        "excerpt_display",
        "body_display",
        "tags_display",
        "created_at",
        "published_at",
    ]

    fieldsets = (
        (
            "Review",
            {
                "description": (
                    "Read the post below, check the images, then change "
                    "Status to Published and save — or use the Approve action "
                    "from the list view."
                ),
                "fields": ("title", "author_display", "category", "status"),
            },
        ),
        (
            "Content Preview",
            {
                "fields": ("excerpt_display", "body_display", "tags_display"),
            },
        ),
        (
            "Meta",
            {
                "fields": ("created_at", "published_at"),
            },
        ),
    )

    # ------------------------------------------------------------------
    # Queryset: only drafts
    # ------------------------------------------------------------------

    def get_queryset(self, request):
        return super().get_queryset(request).filter(status=Blog.Status.DRAFT)

    # ------------------------------------------------------------------
    # Custom readonly display fields
    # ------------------------------------------------------------------

    @admin.display(description="Author")
    def author_display(self, obj):
        name = obj.author_full_name
        return name if name != "Unknown" else (str(obj.author) if obj.author else "—")

    @admin.display(description="Cover")
    def cover_thumbnail(self, obj):
        cover = obj.images.filter(is_cover=True).first() or obj.images.first()
        if cover and cover.image:
            return format_html(
                '<img src="{}" style="height:48px; border-radius:4px; '
                'box-shadow:0 1px 3px rgba(0,0,0,.2);" />',
                cover.image.url,
            )
        return "—"

    @admin.display(description="Excerpt")
    def excerpt_display(self, obj):
        return format_html(
            '<p style="font-size:1rem; line-height:1.6; max-width:720px;">{}</p>',
            obj.excerpt,
        )

    @admin.display(description="Body")
    def body_display(self, obj):
        return format_html(
            '<div style="font-size:.95rem; line-height:1.7; max-width:720px;">{}</div>',
            mark_safe(obj.body),
        )

    @admin.display(description="Tags")
    def tags_display(self, obj):
        if not obj.tags:
            return "—"
        badges = "".join(
            f'<span style="display:inline-block; margin:2px 4px; padding:2px 10px; '
            f'border-radius:999px; background:#e0f2fe; color:#0369a1; font-size:.8rem;">'
            f"{tag}</span>"
            for tag in obj.tags
        )
        return mark_safe(badges)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @admin.action(description="✅  Approve selected blogs (publish immediately)")
    def approve_selected(self, request, queryset):
        from django.utils import timezone

        updated = 0
        for blog in queryset:
            blog.status = Blog.Status.PUBLISHED
            if not blog.published_at:
                blog.published_at = timezone.now()
            blog.save(update_fields=["status", "published_at"])
            updated += 1
        self.message_user(request, f"{updated} blog(s) approved and published.")

    # Prevent creating new posts from the approval section
    def has_add_permission(self, request):
        return False
