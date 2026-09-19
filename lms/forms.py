from django import forms

from .models import Assignment, Submission


class SubmissionForm(forms.ModelForm):
    class Meta:
        model = Submission
        fields = ["text_answer", "file_answer"]
        widgets = {
            "text_answer": forms.Textarea(
                attrs={
                    "rows": 8,
                    "placeholder": "Введите ваш ответ здесь...",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.assignment = kwargs.pop("assignment", None)
        super().__init__(*args, **kwargs)

        if self.assignment:
            if self.assignment.assignment_type == Assignment.Type.TEXT:
                self.fields["text_answer"].label = "Текстовый ответ"
                self.fields["file_answer"].label = "Файл (не требуется)"
            elif self.assignment.assignment_type == Assignment.Type.FILE:
                self.fields["text_answer"].label = "Комментарий (не требуется)"
                self.fields["file_answer"].label = "Файл ответа"
            elif self.assignment.assignment_type == Assignment.Type.AUDIO:
                self.fields["text_answer"].label = "Комментарий (не требуется)"
                self.fields["file_answer"].label = "Аудиофайл"
            elif self.assignment.assignment_type == Assignment.Type.MIXED:
                self.fields["text_answer"].label = "Текстовый ответ"
                self.fields["file_answer"].label = "Файл или аудио"

    def clean(self):
        cleaned_data = super().clean()

        if not self.assignment:
            return cleaned_data

        assignment_type = self.assignment.assignment_type

        text_answer = cleaned_data.get("text_answer", "")
        file_answer = cleaned_data.get("file_answer")

        has_existing_file = False
        if self.instance and self.instance.pk:
            has_existing_file = bool(self.instance.file_answer)

        if assignment_type == Assignment.Type.TEXT:
            if not text_answer.strip():
                self.add_error("text_answer", "Введите текстовый ответ.")

        elif assignment_type in [
            Assignment.Type.FILE,
            Assignment.Type.AUDIO,
        ]:
            if not file_answer and not has_existing_file:
                self.add_error("file_answer", "Загрузите файл.")

        elif assignment_type == Assignment.Type.MIXED:
            if not text_answer.strip():
                self.add_error("text_answer", "Введите текстовый ответ.")
            if not file_answer and not has_existing_file:
                self.add_error("file_answer", "Загрузите файл или аудио.")

        return cleaned_data


class ReviewForm(forms.Form):
    DECISION_CHECKED = "checked"
    DECISION_NEEDS_REVISION = "needs_revision"

    DECISION_CHOICES = [
        (DECISION_CHECKED, "Проверено"),
        (DECISION_NEEDS_REVISION, "Отправить на доработку"),
    ]

    grade = forms.IntegerField(
        required=False,
        min_value=0,
        label="Балл",
        widget=forms.NumberInput(attrs={"placeholder": "Например, 85"}),
    )

    comment = forms.CharField(
        required=False,
        label="Комментарий преподавателя",
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": "Напишите комментарий ученику...",
            }
        ),
    )

    decision = forms.ChoiceField(
        choices=DECISION_CHOICES,
        label="Решение",
    )

    def __init__(self, *args, assignment=None, feedback=None, submission=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.assignment = assignment

        if assignment:
            self.fields["grade"].max_value = assignment.max_points
            self.fields["grade"].help_text = (
                f"Максимум баллов: {assignment.max_points}"
            )

        if feedback:
            self.initial["grade"] = feedback.grade
            self.initial["comment"] = feedback.comment

        if submission:
            if submission.status == Submission.Status.NEEDS_REVISION:
                self.initial["decision"] = self.DECISION_NEEDS_REVISION
            else:
                self.initial["decision"] = self.DECISION_CHECKED
        else:
            self.initial.setdefault("decision", self.DECISION_CHECKED)