import os
import random
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

    # Геймификация (мягкая)
    streak_days = models.PositiveIntegerField(default=0, verbose_name="Серия дней")
    streak_last_date = models.DateField(
        null=True, blank=True, verbose_name="Последний день активности"
    )
    xp_total = models.PositiveIntegerField(default=0, verbose_name="Всего XP")
    badges = models.JSONField(default=list, blank=True, verbose_name="Бейджи")

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


class Group(models.Model):
    """Учебная группа/класс для разделения учеников."""

    name = models.CharField(max_length=100, verbose_name="Название группы")
    slug = models.SlugField(unique=True, verbose_name="URL")
    description = models.TextField(blank=True, verbose_name="Описание")
    cefr_level = models.CharField(
        max_length=2,
        choices=[
            ("A1", "A1 — Начальный"),
            ("A2", "A2 — Элементарный"),
            ("B1", "B1 — Средний"),
            ("B2", "B2 — Выше среднего"),
            ("C1", "C1 — Продвинутый"),
            ("C2", "C2 — В совершенстве"),
        ],
        blank=True,
        verbose_name="Уровень CEFR",
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="taught_groups",
        verbose_name="Преподаватель",
        limit_choices_to={"profile__role": "teacher"},
    )
    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="student_groups",
        verbose_name="Ученики",
        limit_choices_to={"profile__role": "student"},
    )
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Группа"
        verbose_name_plural = "Группы"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CefrLevel(models.TextChoices):
    """Общеевропейские уровни — общая шкала для блоков и аналитики."""

    A1 = "A1", "A1 — Начальный"
    A2 = "A2", "A2 — Элементарный"
    B1 = "B1", "B1 — Средний"
    B2 = "B2", "B2 — Выше среднего"
    C1 = "C1", "C1 — Продвинутый"
    C2 = "C2", "C2 — В совершенстве"


class Block(models.Model):
    name = models.CharField(max_length=150, verbose_name="Название блока")
    slug = models.SlugField(unique=True, verbose_name="URL")
    description = models.TextField(blank=True, verbose_name="Описание")
    cefr_level = models.CharField(
        max_length=2,
        choices=CefrLevel.choices,
        blank=True,
        verbose_name="Уровень CEFR",
    )
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


class AssignmentQuerySet(models.QuerySet):
    def visible(self, user=None, at=None):
        """Всё, что реально видит ученик: активно, опубликовано, срок публикации наступил.
        Если передан user, фильтруем по группе или персональному назначению."""
        moment = at or timezone.now()
        qs = self.filter(
            is_active=True,
            status=self.model.Publication.PUBLISHED,
            topic__is_active=True,
            topic__block__is_active=True,
        ).filter(Q(publish_at__isnull=True) | Q(publish_at__lte=moment))

        # Если пользователь указан, показываем:
        # 1. Задания без ограничений (нет группы и нет персональных учеников)
        # 2. Задания для его групп
        # 3. Задания, назначенные ему лично
        if user and user.is_authenticated:
            user_groups = user.student_groups.all() if hasattr(user, "student_groups") else []
            condition = (
                (Q(group__isnull=True) & Q(assigned_students__isnull=True))
                | Q(group__in=user_groups)
                | Q(assigned_students=user)
            )
            qs = qs.filter(condition).distinct()

        return qs


class Skill(models.Model):
    """Навык (третья ось иерархии): Grammar, Vocabulary, Listening…"""

    class Kind(models.TextChoices):
        GRAMMAR = "grammar", "Грамматика"
        VOCABULARY = "vocabulary", "Лексика"
        LISTENING = "listening", "Аудирование"
        SPEAKING = "speaking", "Говорение"
        WRITING = "writing", "Письмо"
        READING = "reading", "Чтение"

    name = models.CharField(max_length=80, verbose_name="Навык")
    slug = models.SlugField(unique=True, verbose_name="URL")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.GRAMMAR)
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")

    class Meta:
        verbose_name = "Навык"
        verbose_name_plural = "Навыки"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Assignment(models.Model):
    class Type(models.TextChoices):
        TEXT = "text", "Текстовый ответ"
        FILE = "file", "Файл"
        AUDIO = "audio", "Аудио"
        MIXED = "mixed", "Текст + файл/аудио"
        QUIZ = "quiz", "Тест с автопроверкой"

    class Publication(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PUBLISHED = "published", "Опубликовано"

    objects = AssignmentQuerySet.as_manager()

    topic = models.ForeignKey(
        Topic,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name="Тема",
    )
    group = models.ForeignKey(
        "Group",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name="Группа",
        help_text="Если указано, задание доступно только ученикам этой группы",
    )
    title = models.CharField(max_length=200, verbose_name="Название задания")
    description = models.TextField(verbose_name="Условия задания")
    assignment_type = models.CharField(
        max_length=20,
        choices=Type.choices,
        default=Type.TEXT,
        verbose_name="Тип ответа",
    )
    status = models.CharField(
        max_length=20,
        choices=Publication.choices,
        default=Publication.PUBLISHED,
        verbose_name="Публикация",
    )
    publish_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Отложить публикацию до",
    )
    skills = models.ManyToManyField(
        Skill,
        blank=True,
        related_name="assignments",
        verbose_name="Навыки",
    )
    assigned_students = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_assignments",
        verbose_name="Индивидуально для учеников",
        help_text="Если выбраны ученики, задание также будет доступно им персонально",
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

    @property
    def is_quiz(self):
        return self.assignment_type == Assignment.Type.QUIZ

    @property
    def is_visible(self):
        """Опубликовано и доступно ученикам прямо сейчас."""
        return bool(
            self.is_active
            and self.status == Assignment.Publication.PUBLISHED
            and (self.publish_at is None or timezone.now() >= self.publish_at)
        )

    @property
    def total_question_points(self):
        return sum(question.points for question in self.questions.all())


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


class CommentSnippet(models.Model):
    """Банк комментариев преподавателя: типовые формулировки вместо ручного ввода."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="comment_snippets",
        verbose_name="Автор",
    )
    title = models.CharField(max_length=120, verbose_name="Название")
    code = models.CharField(
        max_length=24,
        blank=True,
        verbose_name="Короткий код",
        help_text="Например -s: вставка в комментарий по коду.",
    )
    text = models.TextField(max_length=2000, verbose_name="Текст комментария")
    is_shared = models.BooleanField(default=True, verbose_name="Общий для всех преподавателей")
    usage_count = models.PositiveIntegerField(default=0, verbose_name="Использований")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Шаблон комментария"
        verbose_name_plural = "Банк комментариев"
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=["author", "code"],
                condition=~Q(code=""),
                name="unique_snippet_code_per_author",
            )
        ]

    def __str__(self):
        return self.title


class Question(models.Model):
    """Вопрос теста с автоматической проверкой."""

    class Kind(models.TextChoices):
        MCQ = "mcq", "Один правильный ответ"
        MULTI = "multi", "Несколько правильных ответов"
        GAP = "gap", "Вписать ответ"
        MATCH = "match", "Установить соответствие"
        ORDER = "order", "Предложение из слов"
        SORT = "sort", "Сортировка по колонкам"
        SPELL = "spell", "Слово из букв"

    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.CASCADE,
        related_name="questions",
        verbose_name="Задание",
    )
    kind = models.CharField(
        max_length=10, choices=Kind.choices, default=Kind.MCQ, verbose_name="Тип вопроса"
    )
    text = models.TextField(verbose_name="Вопрос")
    explanation = models.TextField(blank=True, verbose_name="Пояснение к ответу")
    points = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        verbose_name="Баллы",
    )
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Вопрос теста"
        verbose_name_plural = "Вопросы тестов"
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.assignment_id}: {self.text[:60]}"

    @property
    def correct_choices(self):
        return [choice for choice in self.choices.all() if choice.is_correct]

    @property
    def pairs(self):
        """Пары соответствия «термин → определение» в порядке вариантов."""
        return [
            {"pk": choice.pk, "term": choice.text, "definition": choice.match_text}
            for choice in self.choices.all()
            if choice.match_text
        ]

    @property
    def gaps(self):
        """Принимаемые ответы для пропуска."""
        return [choice.text for choice in self.choices.all() if choice.is_correct]

    @property
    def sequence(self):
        """Эталонный порядок слов для «предложение из слов»."""
        return [choice.text for choice in self.choices.all()]

    @property
    def columns(self):
        """Колонки сортировки: уникальные значения второй половины пар."""
        seen = []
        for choice in self.choices.all():
            if choice.match_text and choice.match_text not in seen:
                seen.append(choice.match_text)
        return seen

    @property
    def scrambled(self):
        """Перемешанные буквы для анаграммы: детерминированно и не равно ответу."""
        word = next((choice.text for choice in self.choices.all() if choice.is_correct), "")
        letters = list(word.replace(" ", "").lower())
        if not letters:
            return []
        rng = random.Random(self.pk or 0)
        for _ in range(8):
            rng.shuffle(letters)
            if "".join(letters) != word.replace(" ", "").lower():
                break
        return letters


class Choice(models.Model):
    """Вариант ответа, принятый ответ для пропуска или пара для соответствия."""

    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="choices", verbose_name="Вопрос"
    )
    text = models.CharField(max_length=500, verbose_name="Текст")
    match_text = models.CharField(
        max_length=500, blank=True, verbose_name="Вторая половина пары (для соответствия)"
    )
    is_correct = models.BooleanField(default=False, verbose_name="Правильный")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")

    class Meta:
        verbose_name = "Вариант ответа"
        verbose_name_plural = "Варианты ответов"
        ordering = ["order", "pk"]

    def __str__(self):
        return self.text[:60]


class QuizAttempt(models.Model):
    """Результат автопроверки теста, привязанный к неизменяемой попытке."""

    submission = models.OneToOneField(
        Submission, on_delete=models.CASCADE, related_name="quiz_attempt", verbose_name="Попытка"
    )
    score = models.PositiveIntegerField(default=0, verbose_name="Набрано")
    max_score = models.PositiveIntegerField(default=0, verbose_name="Максимум")
    correct_count = models.PositiveIntegerField(default=0, verbose_name="Верных ответов")
    total_count = models.PositiveIntegerField(default=0, verbose_name="Всего вопросов")
    answers = models.JSONField(default=dict, blank=True, verbose_name="Ответы по вопросам")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Результат теста"
        verbose_name_plural = "Результаты тестов"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.score}/{self.max_score} — {self.submission_id}"


class AnswerDraft(models.Model):
    """Черновик ответа до отправки. Не изменяет неизменяемые попытки."""

    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="answer_drafts",
        verbose_name="Ученик",
    )
    assignment = models.ForeignKey(
        Assignment,
        on_delete=models.CASCADE,
        related_name="answer_drafts",
        verbose_name="Задание",
    )
    text = models.TextField(blank=True, max_length=20000, verbose_name="Черновик")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Сохранено")

    class Meta:
        verbose_name = "Черновик ответа"
        verbose_name_plural = "Черновики ответов"
        constraints = [
            models.UniqueConstraint(
                fields=["student", "assignment"], name="unique_answer_draft_per_student"
            )
        ]

    def __str__(self):
        return f"Черновик {self.student_id} → {self.assignment_id}"


class FlashcardDeck(models.Model):
    """Квизлет: набор карточек внутри темы или личный словарь ученика."""

    topic = models.ForeignKey(
        Topic,
        on_delete=models.CASCADE,
        related_name="decks",
        verbose_name="Тема",
        null=True,
        blank=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="own_decks",
        verbose_name="Владелец личного словаря",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=150, verbose_name="Название")
    description = models.TextField(blank=True, verbose_name="Описание")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Набор карточек"
        verbose_name_plural = "Наборы карточек (квизлеты)"
        ordering = ["topic", "order", "title"]

    @property
    def is_personal(self):
        """Личный словарь ученика: без темы курса, с владельцем."""
        return self.topic_id is None and self.owner_id is not None

    def __str__(self):
        return self.title


class Flashcard(models.Model):
    deck = models.ForeignKey(
        FlashcardDeck, on_delete=models.CASCADE, related_name="cards", verbose_name="Набор"
    )
    front = models.CharField(max_length=300, verbose_name="Лицевая сторона")
    back = models.CharField(max_length=300, verbose_name="Оборотная сторона")
    example = models.CharField(max_length=500, blank=True, verbose_name="Пример")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Карточка"
        verbose_name_plural = "Карточки"
        ordering = ["order", "pk"]

    def __str__(self):
        return self.front[:60]


class CardReview(models.Model):
    """Состояние интервального повторения SM-2 для пары ученик × карточка."""

    card = models.ForeignKey(
        Flashcard, on_delete=models.CASCADE, related_name="reviews", verbose_name="Карточка"
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="card_reviews",
        verbose_name="Ученик",
    )
    ease = models.FloatField(
        default=2.5, validators=[MinValueValidator(1.3), MaxValueValidator(3.5)]
    )
    interval_days = models.PositiveIntegerField(default=0)
    repetitions = models.PositiveIntegerField(default=0)
    lapses = models.PositiveIntegerField(default=0)
    due_at = models.DateTimeField(default=timezone.now, verbose_name="Повторить")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Повторение карточки"
        verbose_name_plural = "Повторения карточек"
        constraints = [
            models.UniqueConstraint(fields=["card", "student"], name="unique_card_review")
        ]
        indexes = [models.Index(fields=["student", "due_at"], name="card_review_due_idx")]

    def __str__(self):
        return f"{self.card_id} × {self.student_id}"
