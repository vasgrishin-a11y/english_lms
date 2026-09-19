from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.db.models import Count
from django.forms.models import BaseInlineFormSet, construct_instance
from django.urls import reverse
from django.utils.html import format_html

from .models import Assignment, Block, Feedback, Profile, Submission, SubmissionEvent, Topic

User = get_user_model()


class ProfileInlineFormSet(BaseInlineFormSet):
    def save_new(self, form, commit=True):
        # post_save(User) may already have created this one-to-one row. Reuse it,
        # preserving created_at instead of inserting a duplicate inline profile.
        existing = Profile.objects.filter(user=self.instance).first()
        if existing:
            form.instance = construct_instance(
                form, existing, form._meta.fields, form._meta.exclude
            )
        return super().save_new(form, commit=commit)


class ProfileInline(admin.StackedInline):
    model = Profile
    formset = ProfileInlineFormSet
    can_delete = False
    verbose_name_plural = "Профиль"
    fk_name = "user"
    extra = 1
    max_num = 1


class CustomUserAdmin(UserAdmin):
    inlines = (ProfileInline,)
    list_display = ("username", "email", "first_name", "last_name", "role", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "profile__telegram")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("profile")

    @admin.display(description="Роль")
    def role(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.get_role_display() if profile else "—"


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "telegram", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__first_name", "user__last_name", "telegram")
    autocomplete_fields = ("user",)
    list_select_related = ("user",)


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "order", "is_active", "topics_count")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug", "description")
    list_editable = ("order", "is_active")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_topics_count=Count("topics"))

    @admin.display(description="Тем", ordering="_topics_count")
    def topics_count(self, obj):
        return obj._topics_count


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("title", "block", "slug", "order", "is_active", "assignments_count")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("block",)
    search_fields = ("title", "description", "block__name")
    list_filter = ("block", "is_active")
    list_editable = ("order", "is_active")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("block")
            .annotate(_assignments_count=Count("assignments"))
        )

    @admin.display(description="Заданий", ordering="_assignments_count")
    def assignments_count(self, obj):
        return obj._assignments_count


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "topic",
        "assignment_type",
        "deadline",
        "max_points",
        "is_active",
        "submissions_count",
    )
    autocomplete_fields = ("topic",)
    search_fields = ("title", "description", "topic__title", "topic__block__name")
    list_filter = ("assignment_type", "is_active", "topic__block", "topic")
    date_hierarchy = "deadline"
    list_editable = ("max_points", "is_active")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("topic__block")
            .annotate(_submissions_count=Count("submissions"))
        )

    @admin.display(description="Попыток", ordering="_submissions_count")
    def submissions_count(self, obj):
        return obj._submissions_count


class ReadOnlyRecordsAdmin(admin.ModelAdmin):
    """Changes go through versioned services; erasure is an explicit maintenance operation."""

    actions = None

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields) + (
            ("review_link",) if hasattr(self, "review_link") else ()
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Submission)
class SubmissionAdmin(ReadOnlyRecordsAdmin):
    list_display = (
        "student",
        "assignment",
        "version",
        "status",
        "submitted_at",
        "is_late",
        "grade",
        "review_link",
    )
    search_fields = (
        "student__username",
        "student__first_name",
        "student__last_name",
        "assignment__title",
    )
    list_filter = ("status", "assignment__topic__block", "assignment__topic")
    list_select_related = ("student", "assignment__topic__block", "feedback")

    @admin.display(boolean=True, description="Просрочено")
    def is_late(self, obj):
        return obj.is_late

    @admin.display(description="Балл")
    def grade(self, obj):
        feedback = getattr(obj, "feedback", None)
        return feedback.grade if feedback and feedback.grade is not None else "—"

    @admin.display(description="Проверка")
    def review_link(self, obj):
        return format_html(
            '<a href="{}">Открыть проверку</a>', reverse("teacher_submission_review", args=[obj.pk])
        )


@admin.register(Feedback)
class FeedbackAdmin(ReadOnlyRecordsAdmin):
    list_display = ("submission", "teacher", "decision", "grade", "updated_at", "review_link")
    search_fields = ("submission__student__username", "submission__assignment__title", "comment")
    list_filter = ("teacher", "decision")
    list_select_related = ("submission__student", "submission__assignment", "teacher")

    @admin.display(description="Изменение проверки")
    def review_link(self, obj):
        return format_html(
            '<a href="{}">Открыть проверку</a>',
            reverse("teacher_submission_review", args=[obj.submission_id]),
        )


@admin.register(SubmissionEvent)
class SubmissionEventAdmin(ReadOnlyRecordsAdmin):
    list_display = ("submission", "action", "actor", "decision", "grade", "created_at")
    list_filter = ("action", "decision")
    search_fields = ("submission__student__username", "submission__assignment__title")
    list_select_related = ("submission__student", "submission__assignment", "actor")
