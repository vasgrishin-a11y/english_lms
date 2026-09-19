from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import Assignment, Block, Feedback, Profile, Submission, Topic


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = "Профиль"
    fk_name = "user"


class CustomUserAdmin(UserAdmin):
    inlines = (ProfileInline,)
    list_display = ("username", "email", "first_name", "last_name", "role", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "profile__telegram")

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


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "order", "is_active", "topics_count")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug", "description")
    list_editable = ("order", "is_active")

    @admin.display(description="Тем")
    def topics_count(self, obj):
        return obj.topics.count()


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("title", "block", "slug", "order", "is_active", "assignments_count")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("block",)
    search_fields = ("title", "description", "block__name")
    list_filter = ("block", "is_active")
    list_editable = ("order", "is_active")

    @admin.display(description="Заданий")
    def assignments_count(self, obj):
        return obj.assignments.count()


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

    @admin.display(description="Сдач")
    def submissions_count(self, obj):
        return obj.submissions.count()


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("student", "assignment", "status", "submitted_at", "is_late", "grade")
    autocomplete_fields = ("student", "assignment")
    search_fields = (
        "student__username",
        "student__first_name",
        "student__last_name",
        "assignment__title",
    )
    list_filter = ("status", "assignment__topic__block", "assignment__topic")
    readonly_fields = (
        "student",
        "assignment",
        "text_answer",
        "file_answer",
        "submitted_at",
        "updated_at",
    )

    @admin.display(boolean=True, description="Просрочено")
    def is_late(self, obj):
        return obj.is_late

    @admin.display(description="Балл")
    def grade(self, obj):
        feedback = getattr(obj, "feedback", None)
        return feedback.grade if feedback else "—"


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("submission", "teacher", "grade", "updated_at")
    autocomplete_fields = ("submission", "teacher")
    search_fields = ("submission__student__username", "submission__assignment__title", "comment")
    list_filter = ("teacher",)
    readonly_fields = ("created_at", "updated_at")
