from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import Assignment, Feedback, Submission
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

    def __init__(self, *args, assignment, submission=None, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.update(
            {
                "expected_version": submission.version if submission else 0,
                "text_answer": submission.text_answer if submission else "",
                "file_answer": submission.file_answer if submission else None,
            }
        )
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
