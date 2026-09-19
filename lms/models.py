import os
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


ALLOWED_FILE_EXTENSIONS = [
    "pdf", "doc", "docx", "txt", "rtf", "odt",
    "xls", "xlsx", "ppt", "pptx",
    "jpg", "jpeg", "png", "gif", "webp",
    "mp3", "wav", "m4a", "ogg", "aac",
    "zip", "rar", "7z",
]

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


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        role = (
            Profile.Role.TEACHER
            if instance.is_superuser or instance.is_staff
            else Profile.Role.STUDENT
        )
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
        blank=True,
        validators=[file_validator],
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


class Submission(models.Model):
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
        on_delete=models.CASCADE,
        related_name="submissions",
        verbose_name="Задание",
    )
    text_answer = models.TextField(blank=True, verbose_name="Текстовый ответ")
    file_answer = models.FileField(
        upload_to=submission_upload_to,
        blank=True,
        validators=[file_validator],
        verbose_name="Файл ответа",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        verbose_name="Статус",
    )
    submitted_at = models.DateTimeField(auto_now_add=True, verbose_name="Отправлено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        verbose_name = "Сдача работы"
        verbose_name_plural = "Сдачи работ"
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "assignment"],
                name="unique_submission_per_student_assignment",
            )
        ]

    def __str__(self):
        return f"{self.student} → {self.assignment}"

    @property
    def is_late(self):
        return bool(self.assignment.deadline and self.submitted_at > self.assignment.deadline)


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
    grade = models.PositiveIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1000)],
        verbose_name="Балл",
    )
    comment = models.TextField(blank=True, verbose_name="Комментарий")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Проверка"
        verbose_name_plural = "Проверки"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Проверка: {self.submission}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.submission.status != Submission.Status.CHECKED:
            self.submission.status = Submission.Status.CHECKED
            self.submission.save(update_fields=["status", "updated_at"])