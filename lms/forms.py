import uuid

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from .models import (
    Assignment,
    Block,
    Choice,
    CommentSnippet,
    Feedback,
    Flashcard,
    FlashcardDeck,
    Profile,
    Question,
    Skill,
    Submission,
    Topic,
)
from .validators import ALLOWED_FILE_EXTENSIONS, AUDIO_EXTENSIONS, validate_answer, validate_upload


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
            self.fields[
                "file_answer"
            ].help_text = f"До {settings.LMS_MAX_FILE_BYTES // (1024 * 1024)} MiB. Отправка создаёт новую попытку; старый ответ сохранится в истории."
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
        return data


class ReviewForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_review_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    grade = forms.IntegerField(required=False, min_value=0, label="Балл")
    comment = forms.CharField(
        required=False,
        max_length=10000,
        label="Комментарий преподавателя",
        widget=forms.Textarea(attrs={"rows": 6}),
    )
    decision = forms.ChoiceField(
        choices=Feedback._meta.get_field("decision").choices, label="Решение"
    )

    def __init__(self, *args, submission, feedback=None, **kwargs):
        super().__init__(*args, **kwargs)
        maximum = submission.max_points_snapshot
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

    def clean(self):
        data = super().clean()
        if data.get("decision") == Submission.Status.CHECKED and data.get("grade") is None:
            self.add_error("grade", "Для завершения проверки укажите балл.")
        return data


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


class BlockForm(SluglessModelForm):
    slug_source = "name"

    class Meta:
        model = Block
        fields = ["name", "cefr_level", "description", "order", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Например: A2 — Базовый курс"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }


class TopicForm(SluglessModelForm):
    slug_source = "title"

    class Meta:
        model = Topic
        fields = ["block", "title", "description", "order", "is_active"]
        widgets = {
            "title": forms.TextInput(
                attrs={"placeholder": "Например: Present Simple vs Continuous"}
            ),
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def slug_parent(self):
        block = self.cleaned_data.get("block")
        return {"block": block} if block else None


class AssignmentForm(forms.ModelForm):
    class Meta:
        model = Assignment
        fields = [
            "topic",
            "group",
            "title",
            "description",
            "assignment_type",
            "skills",
            "max_points",
            "deadline",
            "publish_at",
            "status",
            "order",
            "material_file",
            "is_active",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={"placeholder": "Например: Опишите свою обычную субботу"}
            ),
            "description": forms.Textarea(
                attrs={
                    "rows": 8,
                    "placeholder": "Что нужно сделать, объём, критерии, пример ответа",
                }
            ),
            "deadline": _datetime_widget(),
            "publish_at": _datetime_widget(),
            "skills": forms.CheckboxSelectMultiple,
            "group": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["deadline"].input_formats = DATETIME_FORMATS
        self.fields["publish_at"].input_formats = DATETIME_FORMATS
        self.fields["skills"].queryset = Skill.objects.all()
        self.fields[
            "max_points"
        ].help_text = "Для теста максимум считается автоматически как сумма баллов вопросов."
        self.fields[
            "publish_at"
        ].help_text = "Оставьте пустым, чтобы опубликовать сразу. Черновик ученикам не виден."
        
        # Улучшаем отображение поля статуса
        self.fields["status"].label = "Статус публикации"
        self.fields["status"].help_text = "Черновик виден только вам. Опубликованное задание доступно ученикам."
        self.fields["status"].widget = forms.Select(
            choices=[
                (Assignment.Publication.DRAFT, "📝 Черновик — скрыто от учеников"),
                (Assignment.Publication.PUBLISHED, "✅ Опубликовано — видно ученикам"),
            ]
        )
        
        # Улучшаем отображение поля группы
        self.fields["group"].label = "Группа назначения"
        self.fields["group"].help_text = "Если не выбрано, задание доступно всем ученикам. Выберите группу для ограничения доступа."
        self.fields["group"].empty_label = "Все ученики (общее задание)"
        self.fields["group"].queryset = Group.objects.filter(is_active=True).order_by("name")
        
        # Скрываем is_active из формы, так как это техническое поле
        self.fields["is_active"].widget = forms.HiddenInput()


class QuestionForm(forms.ModelForm):
    choices_text = forms.CharField(
        label="Варианты ответа",
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text=(
            "По одному варианту на строку. Правильный ответ отметьте звёздочкой в начале строки: "
            "*London. Для типа «соответствие» пишите пары через вертикальную черту: "
            "to book | бронировать."
        ),
    )

    class Meta:
        model = Question
        fields = ["kind", "text", "choices_text", "points", "explanation", "order"]
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
            if question.kind == Question.Kind.MATCH:
                lines.append(f"{choice.text} | {choice.match_text}")
            elif question.kind == Question.Kind.GAP:
                lines.append(choice.text)
            else:
                lines.append(("*" if choice.is_correct else "") + choice.text)
        return "\n".join(lines)

    def clean_choices_text(self):
        raw = self.cleaned_data.get("choices_text", "")
        kind = self.data.get("kind") or self.instance.kind
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        parsed = []
        if kind == Question.Kind.MATCH:
            for line in lines:
                if "|" not in line:
                    raise forms.ValidationError(
                        "Для соответствия каждая строка должна содержать «термин | определение»."
                    )
                term, _, definition = line.partition("|")
                if not term.strip() or not definition.strip():
                    raise forms.ValidationError("Пустая половина пары в соответствии.")
                parsed.append(
                    {
                        "text": term.strip()[:500],
                        "match_text": definition.strip()[:500],
                        "correct": True,
                    }
                )
        elif kind == Question.Kind.GAP:
            for line in lines:
                parsed.append(
                    {"text": line.lstrip("*").strip()[:500], "match_text": "", "correct": True}
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

    def save_choices(self, question):
        question.choices.all().delete()
        Choice.objects.bulk_create(
            [
                Choice(
                    question=question,
                    text=item["text"],
                    match_text=item["match_text"],
                    is_correct=item["correct"],
                    order=index,
                )
                for index, item in enumerate(self.cleaned_data["choices_text"])
            ]
        )


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


class FlashcardDeckForm(forms.ModelForm):
    class Meta:
        model = FlashcardDeck
        fields = ["topic", "title", "description", "order", "is_active"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Например: Travel vocabulary"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }


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
