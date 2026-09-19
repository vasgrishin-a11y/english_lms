import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .validators import ALLOWED_FILE_EXTENSIONS, validate_answer, validate_upload

file_validator = FileExtensionValidator(allowed_extensions=ALLOWED_FILE_EXTENSIONS)


def _safe_extension(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return ext if ext else ".bin"


def assignment_upload_to(instance, filename):
    return f"assignments/{instance.topic_id}/{uuid.uuid4().hex}{_safe_extension(filename)}"


def submission_upload_to(instance, filename):
    return f"submissions/{instance.assignment_id}/user_{instance.student_id}/{uuid.uuid4().hex}{_safe_extension(filename)}"


class Profile(models.Model):
    class Role(models.TextChoices):
        STUDENT = "student", "Ученик"
        TEACHER = "teacher", "Преподаватель"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STUDENT,
    )
    telegram = models.CharField(max_length=100, blank=True, verbose_name="Telegram")
    comment = models.TextField(blank=True, verbose_name="Комментарий преподавателя")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Профиль"
        verbose_name_plural = "Профили"

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        return f"{name} — {self.get_role_display()}"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    if created and not kwargs.get("raw"):
        role = Profile.Role.TEACHER if instance.is_superuser else Profile.Role.STUDENT
        Profile.objects.get_or_create(user=instance, defaults={"role": role})


class Block(models.Model):
    name = models.CharField(max_length=150, verbose_name="Название блока")
    slug = models.SlugField(unique=True, verbose_name="URL")
    description = models.TextField(blank=True, verbose_name="Описание")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Блок"
        verbose_name_plural = "Блоки"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Topic(models.Model):
    block = models.ForeignKey(
        Block,
        on_delete=models.CASCADE,
        related_name="topics",
        verbose_name="Блок",
    )
    title = models.CharField(max_length=200, verbose_name="Тема")
    slug = models.SlugField(verbose_name="URL")
    description = models.TextField(blank=True, verbose_name="Описание")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Тема"
        verbose_name_plural = "Темы"
        ordering = ["block", "order", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["block", "slug"],
                name="unique_topic_slug_per_block",
            )
        ]

    def __str__(self):
        return f"{self.block.name}: {self.title}"


class Assignment(models.Model):
    class Type(models.TextChoices):
        TEXT = "text", "Текстовый ответ"
        FILE = "file", "Файл"
        AUDIO = "audio", "Аудио"
        MIXED = "mixed", "Текст + файл/аудио"

    topic = models.ForeignKey(
        Topic,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name="Тема",
    )
    title = models.CharField(max_length=200, verbose_name="Название задания")
    description = models.TextField(verbose_name="Условия задания")
    assignment_type = models.CharField(
        max_length=20,
        choices=Type.choices,
        default=Type.TEXT,
        verbose_name="Тип ответа",
    )
    material_file = models.FileField(
        upload_to=assignment_upload_to,
        db_index=True,
        blank=True,
        validators=[file_validator, validate_upload],
        verbose_name="Материалы задания",
    )
    deadline = models.DateTimeField(null=True, blank=True, verbose_name="Дедлайн")
    max_points = models.PositiveIntegerField(
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(1000)],
        verbose_name="Максимум баллов",
    )
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    is_active = models.BooleanField(default=True, verbose_name="Активно")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Задание"
        verbose_name_plural = "Задания"
        ordering = ["topic", "order", "created_at"]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        return bool(self.deadline and timezone.now() > self.deadline)


class SubmissionQuerySet(models.QuerySet):
    def latest_attempts(self):
        newer = self.model.objects.filter(
            student_id=OuterRef("student_id"),
            assignment_id=OuterRef("assignment_id"),
            version__gt=OuterRef("version"),
        )
        return self.filter(~Exists(newer))


class Submission(models.Model):
    """One immutable answer attempt; only review state may change."""

    objects = SubmissionQuerySet.as_manager()

    class Status(models.TextChoices):
        NEW = "new", "Новая"
        SUBMITTED = "submitted", "Сдана"
        IN_REVIEW = "in_review", "На проверке"
        NEEDS_REVISION = "needs_revision", "Нужно доработать"
        CHECKED = "checked", "Проверена"

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="submissions",
        verbose_name="Ученик",
    )
    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.PROTECT,
        related_name="submissions",
        verbose_name="Задание",
    )
    text_answer = models.TextField(blank=True, max_length=20000, verbose_name="Текстовый ответ")
    file_answer = models.FileField(
        upload_to=submission_upload_to,
        db_index=True,
        blank=True,
        validators=[file_validator, validate_upload],
        verbose_name="Файл ответа",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        verbose_name="Статус",
    )
    version = models.PositiveIntegerField(default=1, verbose_name="Попытка")
    review_revision = models.PositiveIntegerField(default=0, verbose_name="Версия проверки")
    max_points_snapshot = models.PositiveIntegerField(
        default=100, validators=[MaxValueValidator(1000)]
    )
    deadline_snapshot = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True, verbose_name="Отправлено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Сдача работы"
        verbose_name_plural = "Сдачи работ"
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "assignment", "version"],
                name="unique_submission_attempt",
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="submission_version_positive"),
            models.CheckConstraint(
                condition=Q(max_points_snapshot__lte=1000), name="submission_points_range"
            ),
        ]
        indexes = [models.Index(fields=["status", "-submitted_at"], name="submission_queue_idx")]

    def __str__(self):
        return f"{self.student} → {self.assignment} (попытка {self.version})"

    @property
    def is_late(self):
        return bool(
            self.deadline_snapshot
            and self.submitted_at
            and self.submitted_at > self.deadline_snapshot
        )

    def clean(self):
        super().clean()
        if self.assignment_id:
            validate_answer(self.assignment.assignment_type, self.text_answer, self.file_answer)

    def save(self, *args, **kwargs):
        immutable = (
            "student_id",
            "assignment_id",
            "version",
            "text_answer",
            "file_answer",
            "submitted_at",
            "max_points_snapshot",
            "deadline_snapshot",
        )
        previous = (
            type(self).objects.filter(pk=self.pk).values(*immutable).first() if self.pk else None
        )
        if previous:
            for field, value in previous.items():
                current = str(self.file_answer) if field == "file_answer" else getattr(self, field)
                if current != value:
                    raise ValidationError(
                        "Отправленный ответ нельзя изменять. Создайте новую попытку."
                    )
            self._meta.get_field("status").clean(self.status, self)
        else:
            self.max_points_snapshot = self.assignment.max_points
            self.deadline_snapshot = self.assignment.deadline
            self.full_clean(validate_unique=False, validate_constraints=False)
        super().save(*args, **kwargs)


class Feedback(models.Model):
    submission = models.OneToOneField(
        Submission,
        on_delete=models.CASCADE,
        related_name="feedback",
        verbose_name="Сдача",
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedbacks_given",
        verbose_name="Преподаватель",
    )
    decision = models.CharField(
        max_length=20,
        choices=[("checked", "Проверено"), ("needs_revision", "На доработку")],
        default="checked",
    )
    grade = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1000)],
        verbose_name="Балл",
    )
    comment = models.TextField(blank=True, max_length=10000, verbose_name="Комментарий")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Проверка"
        verbose_name_plural = "Проверки"
        ordering = ["-updated_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(grade__isnull=True) | Q(grade__lte=1000), name="feedback_grade_range"
            ),
            models.CheckConstraint(
                condition=Q(decision="needs_revision")
                | (Q(decision="checked") & Q(grade__isnull=False)),
                name="feedback_decision_grade",
            ),
        ]

    def __str__(self):
        return f"Проверка: {self.submission}"

    def clean(self):
        super().clean()
        if self.decision == "checked" and self.grade is None:
            raise ValidationError({"grade": "Для завершения проверки укажите балл."})
        if (
            self.submission_id
            and self.grade is not None
            and self.grade > self.submission.max_points_snapshot
        ):
            raise ValidationError(
                {"grade": f"Максимум для этой попытки: {self.submission.max_points_snapshot}."}
            )

    def save(self, *args, **kwargs):
        # Validation applies to ORM writes too; saving a comment never changes status.
        self.full_clean()
        super().save(*args, **kwargs)


class SubmissionEvent(models.Model):
    class Action(models.TextChoices):
        SUBMITTED = "submitted", "Отправлено"
        REVIEWED = "reviewed", "Проверка"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    decision = models.CharField(max_length=20, blank=True)
    grade = models.PositiveIntegerField(null=True, blank=True)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "Событие сдачи"
        verbose_name_plural = "История сдач и проверок"

    def __str__(self):
        return f"{self.get_action_display()} #{self.submission_id}"
