import uuid

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from . import ai
from .models import (
    TEACHER_FEEDBACK_RECORDING_LIMIT_SECONDS,
    Assignment,
    Block,
    Chapter,
    Choice,
    CommentSnippet,
    Feedback,
    Flashcard,
    Group,
    Profile,
    Question,
    Skill,
    Submission,
    Topic,
)
from .skills import apply_default_skills
from .validators import (
    ALLOWED_FILE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    validate_answer,
    validate_recording_limit,
    validate_upload,
)

User = get_user_model()


class SubmissionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    text_answer = forms.CharField(
        required=False,
        max_length=20000,
        label="Текстовый ответ",
        widget=forms.Textarea(attrs={"rows": 8, "placeholder": "Введите ваш ответ здесь..."}),
    )
    file_answer = forms.FileField(required=False, label="Файл ответа", validators=[validate_upload])

    def __init__(self, *args, assignment, submission=None, draft=None, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.update(
            {
                "expected_version": submission.version if submission else 0,
                "text_answer": submission.text_answer if submission else "",
                "file_answer": submission.file_answer if submission else None,
            }
        )
        if submission is None and draft is not None:
            # Восстанавливаем несохранённую попытку: черновик важнее пустой формы.
            initial["text_answer"] = draft.text
        self.assignment = assignment
        super().__init__(*args, **kwargs)
        if assignment.assignment_type == Assignment.Type.TEXT:
            del self.fields["file_answer"]
        else:
            audio = assignment.assignment_type == Assignment.Type.AUDIO
            extensions = AUDIO_EXTENSIONS if audio else ALLOWED_FILE_EXTENSIONS
            self.fields["file_answer"].widget.attrs["accept"] = ",".join(
                f".{ext}" for ext in sorted(extensions)
            )
            self.fields["file_answer"].label = "Аудиофайл" if audio else "Файл ответа"
            self.fields["file_answer"].widget.attrs.update(
                {
                    "data-dropzone": "1",
                    "data-max-mb": str(settings.LMS_MAX_FILE_BYTES // (1024 * 1024)),
                    "data-dropzone-hint": (
                        "Перетащите аудио сюда" if audio else "Перетащите файл сюда"
                    ),
                }
            )
            limit = (
                f" Аудио — не длиннее {assignment.recording_limit_display}."
                if assignment.recording_limit_seconds
                else ""
            )
            self.fields["file_answer"].help_text = (
                f"До {settings.LMS_MAX_FILE_BYTES // (1024 * 1024)} MiB.{limit} "
                "Отправка создаёт новую попытку; старый ответ сохранится в истории."
            )
            if assignment.assignment_type != Assignment.Type.MIXED:
                self.fields["text_answer"].label = "Комментарий (необязательно)"

    def clean(self):
        data = super().clean()
        try:
            validate_answer(
                self.assignment.assignment_type,
                data.get("text_answer", ""),
                data.get("file_answer"),
            )
        except ValidationError as exc:
            for field, errors in exc.message_dict.items():
                self.add_error(field if field in self.fields else None, errors)
        upload = data.get("file_answer")
        if upload and hasattr(upload, "size") and "file_answer" in self.fields:
            try:
                validate_recording_limit(upload, self.assignment.recording_limit_seconds)
            except ValidationError as exc:
                self.add_error("file_answer", exc)
        return data


class ReviewForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_review_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    grade = forms.IntegerField(required=False, min_value=0, label="Балл")
    comment = forms.CharField(
        required=False,
        max_length=10000,
        label="Комментарий преподавателя",
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": "Напишите, что получилось хорошо и что улучшить…",
            }
        ),
    )
    audio_comment = forms.FileField(
        required=False,
        label="Голосовой комментарий",
        validators=[validate_upload],
        widget=forms.ClearableFileInput(
            attrs={
                "accept": ".mp3,.wav,.m4a,.ogg,.aac,audio/*",
                "data-max-mb": str(settings.LMS_MAX_FILE_BYTES // (1024 * 1024)),
                "data-dropzone": "1",
                "data-dropzone-hint": "Перетащите запись сюда или вставьте из буфера Ctrl+V",
            }
        ),
        help_text="Новая запись заменит предыдущую.",
    )
    remove_audio = forms.BooleanField(
        required=False,
        label="Удалить опубликованный голосовой комментарий",
        widget=forms.CheckboxInput(),
    )
    decision = forms.ChoiceField(
        choices=Feedback._meta.get_field("decision").choices, label="Решение"
    )

    def __init__(self, *args, submission, feedback=None, manual_responses=(), **kwargs):
        super().__init__(*args, **kwargs)
        maximum = submission.max_points_snapshot
        self.item_fields = []
        for response in manual_responses:
            name = f"item_{response.pk}"
            self.fields[name] = forms.IntegerField(
                required=False,
                min_value=0,
                max_value=response.question.points,
                label=f"Пункт {getattr(response, 'number', '')}: баллы",
                help_text=f"из {response.question.points}",
                initial=response.teacher_points,
                # Поле стоит рядом с ответом ученика, но отправляется общей формой решения.
                widget=forms.NumberInput(
                    attrs={"form": "review-form", "data-item-points": response.question.points}
                ),
            )
            self.item_fields.append((name, response.pk))
        # Build the field with its validator; assigning .max_value later is insufficient.
        self.fields["grade"] = forms.IntegerField(
            required=False,
            min_value=0,
            max_value=maximum,
            label="Балл",
            help_text=f"Максимум для этой попытки: {maximum}",
        )
        self.initial.update(
            {
                "expected_version": submission.version,
                "expected_review_revision": submission.review_revision,
                "decision": feedback.decision if feedback else Submission.Status.CHECKED,
                "grade": feedback.grade if feedback else None,
                "comment": feedback.comment if feedback else "",
            }
        )

    def clean_audio_comment(self):
        audio = self.cleaned_data.get("audio_comment")
        if audio:
            validate_recording_limit(audio, TEACHER_FEEDBACK_RECORDING_LIMIT_SECONDS)
        return audio

    def clean(self):
        data = super().clean()
        item_points = {pk: data.pop(name, None) for name, pk in self.item_fields}
        data["item_points"] = item_points
        items_complete = bool(item_points) and all(v is not None for v in item_points.values())
        if (
            data.get("decision") == Submission.Status.CHECKED
            and data.get("grade") is None
            and not items_complete
        ):
            self.add_error(
                "grade",
                "Для завершения проверки укажите балл"
                + (" или оцените все пункты со свободным ответом." if item_points else "."),
            )
        return data

    def visible_fields(self):
        """Поля пунктов рисуются рядом с ответами, а не в общей колонке решения."""
        item_names = {name for name, _ in self.item_fields}
        return [field for field in super().visible_fields() if field.name not in item_names]

    def item_field(self, response_pk):
        return self[f"item_{response_pk}"]


# ── Консоль преподавателя: курс, тесты, карточки, банк комментариев ──────────

DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"]


def _datetime_widget():
    return forms.DateTimeInput(
        attrs={"type": "datetime-local", "class": "datetime"}, format="%Y-%m-%dT%H:%M"
    )


def unique_slug(model, value, field="slug", instance=None, parent=None):
    """Кириллический slug с allow_unicode; при пустом результате — случайный хвост."""
    base = slugify(value or "", allow_unicode=True)[:48].strip("-") or uuid.uuid4().hex[:8]
    candidate = base
    suffix = 2
    while True:
        queryset = model.objects.filter(**{field: candidate})
        if parent is not None:
            queryset = queryset.filter(**parent)
        if instance is not None:
            queryset = queryset.exclude(pk=instance.pk)
        if not queryset.exists():
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


class SluglessModelForm(forms.ModelForm):
    """Служебное поле slug заполняется само: преподаватель о нём не думает."""

    slug_source = "name"

    def save(self, commit=True):
        instance = self.instance
        source = self.cleaned_data.get(self.slug_source) or getattr(instance, self.slug_source, "")
        parent = self.slug_parent()
        if not instance.slug:
            instance.slug = unique_slug(
                self._meta.model, source, instance=instance or None, parent=parent
            )
        return super().save(commit=commit)

    def slug_parent(self):
        return None


class AudienceFormMixin(forms.Form):
    """Поля «Кому доступно» — одинаковые у класса, главы, темы и задания.

    Пустые поля значат «как у родителя»: назначения по иерархии складываются,
    а если нигде ничего не выбрано — материал видят все ученики.
    """

    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="Группы",
        help_text="Назначить целиком группам. Пусто — как у родителя.",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "audience-list"}),
    )
    students = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
        label="Ученики персонально",
        help_text="Открыть отдельным ученикам помимо групп.",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "audience-list"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = Group.objects.filter(is_active=True).order_by("name")
        self.fields["students"].queryset = User.objects.filter(
            profile__role=Profile.Role.STUDENT, is_active=True
        ).order_by("last_name", "first_name", "username")
        self.fields["students"].label_from_instance = lambda user: (
            user.get_full_name() or user.username
        )


class BlockForm(AudienceFormMixin, SluglessModelForm):
    slug_source = "name"

    class Meta:
        model = Block
        fields = ["name", "cefr_level", "description", "order", "is_active", "groups", "students"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Например: A2 — Базовый курс"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }


class ChapterForm(AudienceFormMixin, SluglessModelForm):
    """Глава: тот же подход, что у темы, только на уровень выше."""

    slug_source = "title"

    class Meta:
        model = Chapter
        fields = ["block", "title", "description", "order", "is_active", "groups", "students"]
        labels = {"block": "Класс"}
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Например: Хобби"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["block"].queryset = Block.objects.order_by("order", "name")
        self.fields["block"].empty_label = "— выберите класс —"

    def slug_parent(self):
        block = self.cleaned_data.get("block")
        return {"block": block} if block else None


class ChapterSelect(forms.Select):
    """Select глав: каждая option знает свой класс, чтобы lms.js фильтровал список."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-block"] = str(instance.block_id)
        return option


class TopicForm(AudienceFormMixin, SluglessModelForm):
    slug_source = "title"

    class Meta:
        model = Topic
        fields = [
            "block",
            "chapter",
            "title",
            "description",
            "order",
            "is_active",
            "groups",
            "students",
        ]
        labels = {"block": "Класс", "chapter": "Глава"}
        widgets = {
            "title": forms.TextInput(
                attrs={"placeholder": "Например: Present Simple vs Continuous"}
            ),
            "description": forms.Textarea(attrs={"rows": 3}),
            "chapter": ChapterSelect,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["block"].queryset = Block.objects.order_by("order", "name")
        self.fields["block"].empty_label = "— выберите класс —"
        chapter = self.fields["chapter"]
        chapter.required = False
        chapter.queryset = Chapter.objects.select_related("block").order_by(
            "block__order", "block__name", "order", "title"
        )
        chapter.empty_label = "— без главы: попадёт в «Общее» —"
        chapter.label_from_instance = lambda item: f"{item.block.name} → {item.title}"
        chapter.help_text = (
            "Список фильтруется по выбранному классу. Без главы тема попадёт в «Общее»."
        )
        # data-атрибуты для фильтрации списка глав по классу на клиенте
        chapter.widget.attrs["data-chapter-select"] = "1"
        self.fields["block"].widget.attrs["data-block-select"] = "1"

    def clean(self):
        cleaned = super().clean()
        block, chapter = cleaned.get("block"), cleaned.get("chapter")
        if block and chapter and chapter.block_id != block.pk:
            self.add_error(
                "chapter", "Глава относится к другому классу. Выберите главу этого класса."
            )
        return cleaned

    def slug_parent(self):
        block = self.cleaned_data.get("block")
        return {"block": block} if block else None


class DurationLimitWidget(forms.MultiWidget):
    """Число + «сек / мин» в одной строке."""

    template_name = "lms/widgets/duration_limit.html"

    def __init__(self, attrs=None):
        number = forms.NumberInput(
            attrs={
                "min": 1,
                "max": 600,
                "step": 1,
                "inputmode": "numeric",
                "class": "duration-value",
            }
        )
        unit = forms.Select(
            choices=[("sec", "секунд"), ("min", "минут")], attrs={"class": "duration-unit"}
        )
        super().__init__([number, unit], attrs)

    def decompress(self, value):
        if not value:
            return [None, "min"]
        value = int(value)
        if value % 60 == 0:
            return [value // 60, "min"]
        return [value, "sec"]


class DurationLimitField(forms.MultiValueField):
    """Лимит записи: преподаватель вводит секунды или минуты, в БД — секунды."""

    widget = DurationLimitWidget

    def __init__(self, *, min_seconds=10, max_seconds=600, **kwargs):
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        fields = (
            forms.IntegerField(min_value=1, max_value=max_seconds, required=False),
            forms.ChoiceField(choices=[("sec", "секунд"), ("min", "минут")], required=False),
        )
        kwargs.setdefault("require_all_fields", False)
        super().__init__(fields, **kwargs)

    def compress(self, data_list):
        if not data_list or data_list[0] in (None, ""):
            return None
        value, unit = data_list[0], data_list[1] or "sec"
        seconds = int(value) * (60 if unit == "min" else 1)
        if seconds < self.min_seconds:
            raise forms.ValidationError(f"Минимальный лимит — {self.min_seconds} секунд.")
        if seconds > self.max_seconds:
            raise forms.ValidationError(
                f"Максимальный лимит — {self.max_seconds // 60} минут "
                "(больше не помещается в лимит размера файла)."
            )
        return seconds


class MultipleFileInput(forms.FileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Поле, которое действительно принимает несколько файлов сразу.

    Обычный FileField с multiple-виджетом получает из запроса список и падает
    на нём с «Ни одного файла не было отправлено»: выбрать два вложения разом
    было нельзя, хотя подсказка это обещала. Проверяем каждый файл отдельно.
    """

    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if isinstance(data, (list, tuple)):
            return [super(MultipleFileField, self).clean(item, initial) for item in data]
        return super().clean(data, initial)


PASTE_HINT = "Перетащите файл сюда или вставьте скриншот Ctrl+V"


def enable_paste_uploads(form, *, description=None, upload=None, hint=PASTE_HINT):
    """Разрешить перетаскивание и вставку из буфера в полях формы.

    Атрибут data-dropzone превращает поле в зону загрузки, data-paste-target —
    отправляет картинку из Ctrl+V в файловое поле той же формы. Раньше это было
    прописано только в полной форме задания, поэтому в быстром редактировании
    вставка скриншота не работала.
    """
    max_mb = settings.LMS_MAX_FILE_BYTES // (1024 * 1024)
    if upload and upload in form.fields:
        attrs = form.fields[upload].widget.attrs
        attrs.setdefault("accept", ",".join(f".{ext}" for ext in sorted(ALLOWED_FILE_EXTENSIONS)))
        attrs["data-dropzone"] = "1"
        attrs["data-max-mb"] = str(max_mb)
        attrs.setdefault("data-dropzone-hint", hint)
    if description and description in form.fields:
        form.fields[description].widget.attrs["data-paste-target"] = "1"


class MaterialFileInput(forms.ClearableFileInput):
    clear_checkbox_label = "Удалить основной файл после сохранения"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["checkbox_id"] = context["widget"]["attrs"].get("id", name) + "-clear"
        return context


class AssignmentForm(forms.ModelForm):
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="Группы",
        help_text=(
            "Назначить задание целиком группам. Пусто — как у темы/главы/класса; "
            "если нигде ничего не выбрано, задание видят все ученики."
        ),
        widget=forms.CheckboxSelectMultiple(attrs={"class": "audience-list"}),
    )
    assigned_students = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label="Ученики персонально",
        help_text="Открыть задание отдельным ученикам помимо групп",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "audience-list"}),
    )
    new_attachments = MultipleFileField(
        required=False,
        label="Дополнительные файлы",
        widget=MultipleFileInput(
            attrs={
                "data-dropzone": "1",
                "data-dropzone-hint": "Перетащите файлы сюда или вставьте скриншот Ctrl+V",
            }
        ),
        help_text="Можно выбрать несколько файлов, перетащить или вставить картинку из буфера (Ctrl+V).",
    )

    class Meta:
        model = Assignment
        fields = [
            "topic",
            "groups",
            "assigned_students",
            "title",
            "description",
            "assignment_type",
            "skills",
            "max_points",
            "max_tries",
            "allow_retake",
            "exam_mode",
            "recording_limit_seconds",
            "deadline",
            "publish_at",
            "status",
            "order",
            "material_file",
            "is_active",
        ]
        widgets = {
            "material_file": MaterialFileInput(),
            "title": forms.TextInput(
                attrs={"placeholder": "Например: Опишите свою обычную субботу"}
            ),
            "description": forms.Textarea(
                attrs={
                    "rows": 8,
                    "placeholder": "Что нужно сделать, объём, критерии, пример ответа",
                    "data-description-editor": "1",
                }
            ),
            "deadline": _datetime_widget(),
            "publish_at": _datetime_widget(),
            "assignment_type": forms.RadioSelect(attrs={"class": "type-picker"}),
            "skills": forms.CheckboxSelectMultiple(attrs={"class": "skill-chips"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }

    recording_limit_seconds = DurationLimitField(
        required=False,
        label="Лимит времени на запись",
        help_text="От 10 секунд до 10 минут. Пусто — без лимита. Запись остановится сама.",
    )

    def clean_max_tries(self):
        value = self.cleaned_data.get("max_tries")
        if value is None:
            return self.instance.max_tries or 3
        return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["max_tries"].widget.attrs.update({"min": 1, "max": 5})
        self.fields["max_tries"].required = False
        self.fields["max_tries"].help_text = (
            "Для пунктов с правильным ответом: после каждого «Принять» ученик видит ✓ или ✗. "
            "Для выбора из двух вариантов разумно ставить 1."
        )
        self.fields["deadline"].input_formats = DATETIME_FORMATS
        self.fields["publish_at"].input_formats = DATETIME_FORMATS
        self.fields["skills"].queryset = Skill.objects.all().order_by("order", "name")
        self.fields["skills"].required = False
        self.fields[
            "skills"
        ].help_text = "Подставляются по типу задания. Нажмите чип, чтобы заменить набор."
        self.fields["assignment_type"].label = "Тип задания"
        self.fields[
            "max_points"
        ].help_text = "Для теста максимум считается автоматически как сумма баллов вопросов."
        self.fields[
            "publish_at"
        ].help_text = "Оставьте пустым, чтобы опубликовать сразу. Черновик ученикам не виден."

        # Улучшаем отображение поля статуса
        self.fields["status"].label = "Статус публикации"
        self.fields[
            "status"
        ].help_text = "Черновик виден только вам. Опубликованное задание доступно ученикам."
        self.fields["status"].widget = forms.Select(
            choices=[
                (Assignment.Publication.DRAFT, "📝 Черновик — скрыто от учеников"),
                (Assignment.Publication.PUBLISHED, "✅ Опубликовано — видно ученикам"),
            ]
        )

        # Кому доступно: группы и персональные ученики
        self.fields["groups"].queryset = Group.objects.filter(is_active=True).order_by("name")
        self.fields["assigned_students"].queryset = User.objects.filter(
            profile__role=Profile.Role.STUDENT, is_active=True
        ).order_by("last_name", "first_name", "username")
        self.fields["assigned_students"].label_from_instance = lambda user: (
            user.get_full_name() or user.username
        )

        # Материалы можно прикрепить к заданию любого типа
        max_mb = settings.LMS_MAX_FILE_BYTES // (1024 * 1024)
        enable_paste_uploads(self, description="description", upload="material_file")
        enable_paste_uploads(self, upload="new_attachments")
        self.fields["material_file"].help_text = (
            f"Файл до {max_mb} MiB: документ, картинка, аудио или архив. "
            "Можно перетащить или вставить из буфера (Ctrl+V). Ученик увидит его на странице задания."
        )
        self.fields["description"].widget.attrs["placeholder"] = (
            "Что нужно сделать, объём, критерии, пример ответа. Можно вставить картинку Ctrl+V — она добавится как вложение."
        )

        # Улучшаем выпадающий список тем: показываем Класс · Глава — Тема
        self.fields["topic"].queryset = (
            Topic.objects.select_related("block", "chapter")
            .filter(is_active=True)
            .order_by("block__order", "block__name", "chapter__order", "order", "title")
        )
        self.fields["topic"].label_from_instance = lambda obj: (
            f"[{obj.block.name} · {obj.chapter.title}] {obj.title}"
        )
        self.fields["topic"].empty_label = "Выберите тему курса"

        # Скрываем is_active из формы, так как это техническое поле
        self.fields["is_active"].widget = forms.HiddenInput()

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            posted = self.cleaned_data.get("skills")
            has_skills = posted is not None and posted.exists()
            if not has_skills and self.data.get("skills_manual") != "1":
                apply_default_skills(instance, override=True)
        return instance


class AssignmentQuickForm(forms.ModelForm):
    # То же поле, что и в полной форме: вложения обрабатывает представление
    # через request.FILES.getlist, а форма отвечает за вид и подсказки.
    new_attachments = MultipleFileField(
        required=False,
        label="Добавить вложения",
        widget=MultipleFileInput(
            attrs={"data-dropzone-hint": "Перетащите файлы сюда или вставьте скриншот Ctrl+V"}
        ),
        help_text="Можно выбрать несколько файлов, перетащить или вставить картинку (Ctrl+V).",
    )

    class Meta:
        model = Assignment
        fields = [
            "title",
            "description",
            "assignment_type",
            "status",
            "publish_at",
            "exam_mode",
            "recording_limit_seconds",
            "order",
            "skills",
            "max_points",
            "deadline",
            "max_tries",
            "allow_retake",
            "material_file",
        ]
        widgets = {
            "material_file": MaterialFileInput(),
            "description": forms.Textarea(attrs={"rows": 4}),
            "deadline": _datetime_widget(),
            "publish_at": _datetime_widget(),
            "skills": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Быстрое редактирование должно принимать файлы так же, как полная форма:
        # перетаскиванием и Ctrl+V, иначе скриншот некуда вставить прямо на доске.
        enable_paste_uploads(self, description="description", upload="material_file")
        enable_paste_uploads(self, upload="new_attachments")


class QuestionForm(forms.ModelForm):
    choices_text = forms.CharField(
        label="Правильный ответ и варианты",
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text=(
            "По одному варианту на строку. Правильный ответ отметьте звёздочкой в начале строки: "
            "*London. Для «вписать ответ» — все допустимые варианты, по одному на строку "
            "(don't / do not). Для «соответствия» и «сортировки» пишите пары через вертикальную "
            "черту: to book | бронировать (для сортировки справа — название колонки). "
            "Для «предложения из слов» перечислите слова в правильном порядке, по одному на строку. "
            "Для свободного и голосового ответа поле не нужно — такие пункты проверяете вы."
        ),
    )
    recording_limit_seconds = DurationLimitField(
        required=False,
        label="Лимит времени на ответ голосом",
        help_text=(
            "Только для голосового ответа. От 10 секунд до 10 минут; "
            f"по умолчанию — {Question.DEFAULT_VOICE_LIMIT} секунд."
        ),
    )

    class Meta:
        model = Question
        fields = [
            "kind",
            "text",
            "choices_text",
            "recording_limit_seconds",
            "points",
            "explanation",
            "order",
        ]
        widgets = {
            "text": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Вопрос или предложение с пропуском"}
            ),
            "explanation": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        question = self.instance
        if question.pk and "choices_text" not in self.data:
            self.initial["choices_text"] = self.serialize_choices(question)

    @staticmethod
    def serialize_choices(question):
        lines = []
        for choice in question.choices.all():
            if question.kind in (Question.Kind.MATCH, Question.Kind.SORT):
                lines.append(f"{choice.text} | {choice.match_text}")
            elif question.kind in (Question.Kind.GAP, Question.Kind.SPELL):
                lines.append(choice.text)
            else:
                lines.append(("*" if choice.is_correct else "") + choice.text)
        return "\n".join(lines)

    def clean_kind(self):
        kind = self.cleaned_data["kind"]
        if self.instance.pk and kind != self.instance.kind:
            if (
                self.instance.responses.exists()
                or self.instance.assignment.submissions.filter(quiz_attempt__isnull=False).exists()
            ):
                raise forms.ValidationError(
                    "У пункта уже есть ответы. Для другого типа создайте новый пункт."
                )
        return kind

    def clean_choices_text(self):
        raw = self.cleaned_data.get("choices_text", "")
        kind = self.data.get(self.add_prefix("kind")) or self.instance.kind
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        parsed = []
        if kind in Question.MANUAL_KINDS:
            return []
        if not lines:
            raise forms.ValidationError(
                "Укажите правильный ответ — без него пункт нельзя проверить автоматически."
            )
        if kind in (Question.Kind.MATCH, Question.Kind.SORT):
            label = "соответствия" if kind == Question.Kind.MATCH else "сортировки"
            for line in lines:
                if "|" not in line:
                    raise forms.ValidationError(
                        f"Для {label} каждая строка должна содержать «элемент | колонка»."
                    )
                term, _, definition = line.partition("|")
                if not term.strip() or not definition.strip():
                    raise forms.ValidationError(f"Пустая половина пары в {label}.")
                parsed.append(
                    {
                        "text": term.strip()[:500],
                        "match_text": definition.strip()[:500],
                        "correct": True,
                    }
                )
            if kind == Question.Kind.SORT and len({item["match_text"] for item in parsed}) < 2:
                raise forms.ValidationError("Для сортировки нужно минимум две колонки.")
        elif kind == Question.Kind.ORDER:
            for line in lines:
                parsed.append(
                    {"text": line.lstrip("*").strip()[:500], "match_text": "", "correct": False}
                )
            if len(parsed) < 2:
                raise forms.ValidationError(
                    "Для «предложения из слов» нужно минимум два слова — по одному на строку."
                )
        elif kind in (Question.Kind.GAP, Question.Kind.SPELL):
            for line in lines:
                for variant in line.split(" / ") if kind == Question.Kind.GAP else [line]:
                    if variant.strip().lstrip("*").strip():
                        parsed.append(
                            {
                                "text": variant.lstrip("*").strip()[:500],
                                "match_text": "",
                                "correct": True,
                            }
                        )
        else:
            for line in lines:
                correct = line.startswith("*")
                parsed.append(
                    {"text": line.lstrip("*").strip()[:500], "match_text": "", "correct": correct}
                )
            if len(parsed) < 2:
                raise forms.ValidationError("Нужно минимум два варианта ответа.")
            if not any(item["correct"] for item in parsed):
                raise forms.ValidationError("Отметьте хотя бы один правильный ответ звёздочкой *.")
        if len(parsed) > 40:
            raise forms.ValidationError("Слишком много вариантов: максимум 40.")
        return parsed

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if kind != Question.Kind.VOICE:
            data["recording_limit_seconds"] = None
        return data

    def save_choices(self, question):
        # Keep choice IDs stable: student answers reference them. Match unchanged
        # text first (including reordered options), then reuse the edited row.
        existing = list(question.choices.all())
        parsed = self.cleaned_data["choices_text"]
        matched = {}
        for index, item in enumerate(parsed):
            choice = next((c for c in existing if c.text == item["text"]), None)
            if choice:
                existing.remove(choice)
                matched[index] = choice
        for index, item in enumerate(parsed):
            choice = matched.get(index)
            if choice is None:
                choice = existing.pop(0) if existing else Choice(question=question)
            choice.text = item["text"]
            choice.match_text = item["match_text"]
            choice.is_correct = item["correct"]
            choice.order = index
            choice.save()
        question.choices.filter(pk__in=[c.pk for c in existing]).delete()


class CommentSnippetForm(forms.ModelForm):
    class Meta:
        model = CommentSnippet
        fields = ["title", "code", "text", "is_shared"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Например: окончание -s"}),
            "code": forms.TextInput(attrs={"placeholder": "-s"}),
            "text": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip()
        if code and " " in code:
            raise forms.ValidationError("Код не должен содержать пробелы.")
        return code[:24]


class DictionaryWordForm(forms.Form):
    """Слово в личный словарь ученика: слово и перевод со страницы задания."""

    term = forms.CharField(
        label="Слово или фраза",
        max_length=300,
        widget=forms.TextInput(attrs={"placeholder": "например, to book"}),
    )
    translation = forms.CharField(
        label="Перевод",
        max_length=300,
        widget=forms.TextInput(attrs={"placeholder": "например, бронировать"}),
    )
    example = forms.CharField(
        label="Пример употребления (необязательно)",
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"placeholder": "I'd like to book a table for two."}),
    )


class FlashcardForm(forms.ModelForm):
    class Meta:
        model = Flashcard
        fields = ["front", "back", "example", "order"]
        widgets = {
            "front": forms.TextInput(attrs={"placeholder": "to book a flight"}),
            "back": forms.TextInput(attrs={"placeholder": "забронировать рейс"}),
            "example": forms.TextInput(attrs={"placeholder": "I need to book a flight to Berlin."}),
        }


class FlashcardBulkForm(forms.Form):
    """Массовое добавление карточек: по одной на строку, «лицо | оборот | пример»."""

    cards_text = forms.CharField(
        label="Список карточек",
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "placeholder": "to book a flight | забронировать рейс | I need to book a flight.\n"
                "departure | отправление",
            }
        ),
    )
    replace = forms.BooleanField(
        required=False, label="Очистить набор перед импортом", initial=False
    )

    def clean_cards_text(self):
        cards = []
        for number, line in enumerate(self.cleaned_data["cards_text"].splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise forms.ValidationError(
                    f"Строка {number}: нужен формат «лицо | оборот» (пример можно третьим полем)."
                )
            if len(parts) > 3:
                raise forms.ValidationError(f"Строка {number}: слишком много частей, максимум 3.")
            cards.append(
                {
                    "front": parts[0][:300],
                    "back": parts[1][:300],
                    "example": (parts[2][:500] if len(parts) == 3 else ""),
                }
            )
        if not cards:
            raise forms.ValidationError("Добавьте хотя бы одну карточку.")
        if len(cards) > 500:
            raise forms.ValidationError("Максимум 500 карточек за один импорт.")
        return cards


class TeacherProfileForm(forms.Form):
    """Профиль преподавателя в консоли: имя, контакт, заметка. Без системных прав."""

    first_name = forms.CharField(max_length=150, required=False, label="Имя")
    last_name = forms.CharField(max_length=150, required=False, label="Фамилия")
    email = forms.EmailField(required=False, label="Электронная почта")
    telegram = forms.CharField(max_length=100, required=False, label="Telegram")
    comment = forms.CharField(
        required=False,
        label="Заметка о себе",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Например: уровни и направления занятий, часы доступности для проверки.",
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        profile = getattr(user, "profile", None)
        kwargs.setdefault(
            "initial",
            {
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "telegram": profile.telegram if profile else "",
                "comment": profile.comment if profile else "",
            },
        )
        super().__init__(*args, **kwargs)

    def save(self):
        data = self.cleaned_data
        user = self.user
        user.first_name = data["first_name"][:150]
        user.last_name = data["last_name"][:150]
        user.email = data["email"][:254]
        user.save(update_fields=["first_name", "last_name", "email"])
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.telegram = data["telegram"][:100]
        profile.comment = data["comment"]
        profile.save(update_fields=["telegram", "comment", "updated_at"])
        return user


# ── Управление группами и учениками ───────────────────────────────────────────


class GroupForm(forms.ModelForm):
    """Форма создания/редактирования учебной группы."""

    student_ids = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label="Ученики в группе",
        widget=forms.CheckboxSelectMultiple,
        help_text="Выберите учеников для добавления в группу",
    )

    class Meta:
        model = Group
        fields = ["name", "slug", "description", "cefr_level", "teacher", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Например: Группа A2-1, 2024"}),
            "slug": forms.TextInput(attrs={"placeholder": "a2-1-2024"}),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Описание группы"}),
            "cefr_level": forms.Select(),
            "teacher": forms.Select(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Показываем только учеников с понятным лейблом
        self.fields["student_ids"].queryset = User.objects.filter(
            profile__role=Profile.Role.STUDENT, is_active=True
        ).order_by("last_name", "first_name", "username")
        self.fields["student_ids"].label_from_instance = lambda u: (
            f"{u.get_full_name() or u.username} (@{u.username})"
        )

        # Показываем только учителей
        self.fields["teacher"].queryset = User.objects.filter(
            profile__role=Profile.Role.TEACHER, is_active=True
        ).order_by("last_name", "first_name", "username")
        self.fields["teacher"].label_from_instance = lambda u: (
            f"{u.get_full_name() or u.username} (@{u.username})"
        )
        self.fields["teacher"].empty_label = "Не назначен (общая группа)"

        # Если редактируем существующую группу, устанавливаем текущих студентов
        if self.instance.pk:
            self.fields["student_ids"].initial = self.instance.students.all()

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            instance.students.set(self.cleaned_data["student_ids"])
        return instance

    def clean_slug(self):
        slug = self.cleaned_data.get("slug")
        if not slug:
            from django.utils.text import slugify

            name = self.cleaned_data.get("name", "")
            slug = slugify(name, allow_unicode=True) or uuid.uuid4().hex[:8]
        return slug


class StudentCreateForm(forms.ModelForm):
    """Форма создания нового ученика с генерацией пароля или ручным вводом."""

    first_name = forms.CharField(
        max_length=150,
        required=False,
        label="Имя",
        help_text="Необязательно. Без имени ученик видит у себя только логин.",
    )
    last_name = forms.CharField(max_length=150, required=False, label="Фамилия")
    email = forms.EmailField(required=False, label="Email")
    username = forms.CharField(max_length=150, required=True, label="Логин")

    # Поле пароля: автосгенерированное значение по умолчанию, которое учитель может изменить вручную
    password = forms.CharField(
        max_length=128,
        required=True,
        widget=forms.TextInput(attrs={"class": "password-display"}),
        label="Пароль для входа",
        help_text="Сгенерирован автоматически, но вы можете изменить его на свой вариант",
    )

    group_ids = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label="Группы",
        widget=forms.CheckboxSelectMultiple,
        help_text="Выберите группы, куда добавить ученика",
    )

    telegram = forms.CharField(max_length=100, required=False, label="Telegram")
    comment = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Комментарий"
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "password"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Показываем только активные группы с уровнем
        self.fields["group_ids"].queryset = Group.objects.filter(is_active=True).order_by("name")
        self.fields["group_ids"].label_from_instance = lambda g: (
            f"{g.name} ({g.get_cefr_level_display()})" if g.cefr_level else g.name
        )
        if "password" not in self.initial and not self.data:
            import secrets

            self.initial["password"] = secrets.token_urlsafe(10)

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Пользователь с таким логином уже существует")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email and User.objects.filter(email=email).exists():
            raise forms.ValidationError("Пользователь с таким email уже существует")
        return email

    def save(self, commit=True):
        raw_password = self.cleaned_data["password"]
        self.saved_password = raw_password

        user = User(
            username=self.cleaned_data["username"],
            first_name=(self.cleaned_data.get("first_name") or "").strip(),
            last_name=(self.cleaned_data.get("last_name") or "").strip(),
            email=self.cleaned_data.get("email", ""),
        )
        user.set_password(raw_password)
        if commit:
            user.save()
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = Profile.Role.STUDENT
            profile.telegram = self.cleaned_data.get("telegram", "")
            profile.comment = self.cleaned_data.get("comment", "")
            # Учитель сможет увидеть выданный пароль снова на странице ученика.
            profile.remember_password(raw_password, save=False)
            profile.save()

            if self.cleaned_data.get("group_ids"):
                user.student_groups.set(self.cleaned_data["group_ids"])

        return user


class StudentEditForm(forms.ModelForm):
    """Форма редактирования ученика."""

    first_name = forms.CharField(
        max_length=150,
        required=False,
        label="Имя",
        help_text="Необязательно. Без имени ученик видит у себя только логин.",
    )
    last_name = forms.CharField(max_length=150, required=False, label="Фамилия")
    email = forms.EmailField(required=False, label="Email")

    group_ids = forms.ModelMultipleChoiceField(
        queryset=None,
        required=False,
        label="Группы",
        widget=forms.CheckboxSelectMultiple,
        help_text="Выберите группы, куда добавить ученика",
    )

    telegram = forms.CharField(max_length=100, required=False, label="Telegram")
    comment = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Комментарий"
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Показываем только активные группы с уровнем
        self.fields["group_ids"].queryset = Group.objects.filter(is_active=True).order_by("name")
        self.fields["group_ids"].label_from_instance = lambda g: (
            f"{g.name} ({g.get_cefr_level_display()})" if g.cefr_level else g.name
        )

        # Если редактируем, устанавливаем текущие группы
        if self.instance.pk and hasattr(self.instance, "student_groups"):
            self.fields["group_ids"].initial = self.instance.student_groups.all()

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email and User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Пользователь с таким email уже существует")
        return email

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.telegram = self.cleaned_data.get("telegram", "")
            profile.comment = self.cleaned_data.get("comment", "")
            profile.save()

            # Обновляем группы
            if self.cleaned_data.get("group_ids") is not None:
                user.student_groups.set(self.cleaned_data["group_ids"])

        return user


class AIMaterialForm(forms.Form):
    """ИИ-помощник: файл (фото, Word, Excel, PDF, видео) или текст + пожелания.

    Конкретный ученик заранее неизвестен: помощник создаёт универсальный материал
    по явно указанной теме, уровню и цели, без профиля и истории попыток ученика.
    Материал всегда превращается в черновики курса: помощник ничего не публикует.
    """

    target = forms.ChoiceField(
        label="Что собрать",
        choices=ai.TARGETS,
        initial="mixed",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    prompt = forms.CharField(
        label="Пожелания к материалу",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": (
                    "Например: сделай класс B1 по теме Travel, 2 главы (Airport, Hotel), "
                    "в каждой по 2 темы, тест на Present Perfect и набор карточек"
                ),
            }
        ),
        help_text=(
            "Можно указать главы: «раздели на главы Airport / Hotel / Restaurant». "
            "Профиль конкретного ученика заранее не передаётся — опишите общий уровень, тему, навык и цель."
        ),
    )
    text = forms.CharField(
        label="Или вставьте текст материала",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "placeholder": (
                    "# Класс A2 — Travel [B1]\n"
                    "## Глава 1 — At the Airport [глава]\n"
                    "### Тема: Check-in\n"
                    "#### Задание: Слова [карточки]\n"
                    "- check-in | регистрация\n"
                    "#### Задание: Диалог [quiz]\n"
                    "? Где выход на посадку?\n"
                    "* Where is the gate?\n"
                    "- Where is gate?\n"
                    "## Глава 2 — At the Hotel\n"
                    "### Тема: Booking\n"
                    "#### Задание: Бронирование [material]\n"
                    "Текст задания..."
                ),
            }
        ),
        help_text=(
            "Иерархия: # Класс → ## Глава → ### Тема → #### Задание. "
            "Если глав нет — используйте # Блок / ## Тема / ### Задание, темы попадут в «Общее». "
            "Тип задания в []: [quiz], [карточки], [material]."
        ),
    )
    upload = forms.FileField(
        label="Файл материала",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={
                "accept": ",".join(sorted(ai.UPLOAD_EXTENSIONS)),
                "data-ai-upload": "1",
                "data-dropzone": "1",
                "data-max-mb": str(ai.max_upload_bytes() // (1024 * 1024)),
                "data-dropzone-hint": "Перетащите фото или документ сюда",
            }
        ),
        help_text=(
            "Фото страницы, Word, Excel, PDF, видео или текстовый файл — до {mb} МБ.".format(
                mb=ai.max_upload_bytes() // (1024 * 1024)
            )
        ),
    )
    target_topic = forms.ModelChoiceField(
        queryset=Topic.objects.none(),
        required=False,
        label="Добавить в существующую тему",
        empty_label="Нет — создать новые классы и темы",
        help_text="Если выбрано, задания и карточки попадут прямо в эту тему.",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_topic"].queryset = (
            Topic.objects.select_related("block", "chapter")
            .filter(is_active=True, chapter__is_active=True, block__is_active=True)
            .order_by("block__order", "block__name", "chapter__order", "order", "title")
        )
        self.fields["target_topic"].label_from_instance = lambda obj: (
            f"[{obj.block.name} → {obj.chapter.title}] {obj.title}"
        )

    def clean_upload(self):
        upload = self.cleaned_data.get("upload")
        if not upload:
            return None
        extension = ai.extension_of(upload.name)
        if extension not in ai.UPLOAD_EXTENSIONS:
            raise forms.ValidationError(
                "Такой формат не поддерживается: загрузите фото, Word, Excel, PDF, видео "
                "или текстовый файл."
            )
        limit = ai.max_upload_bytes()
        if upload.size > limit:
            raise forms.ValidationError(
                "Файл больше {mb} МБ — разделите материал на части.".format(
                    mb=limit // (1024 * 1024)
                )
            )
        return upload

    def clean(self):
        cleaned = super().clean()
        has_input = any(
            (
                cleaned.get("upload"),
                (cleaned.get("text") or "").strip(),
                (cleaned.get("prompt") or "").strip(),
            )
        )
        if not has_input:
            raise forms.ValidationError(
                "Приложите файл, вставьте текст или опишите задачу словами."
            )
        return cleaned
