"""Вставка картинок и файлов (Ctrl+V) должна работать во всех редакторах заданий.

Раньше зона загрузки и обработчик вставки были прописаны только в полной форме
задания — своим inline-скриптом с жёстко зашитыми id. В быстром редактировании
на доске темы и в проверке работ ни перетаскивания, ни Ctrl+V не было. Теперь
атрибуты ставит общий помощник форм, а разбирает их общий скрипт lms.js.
"""

from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from lms.forms import AssignmentForm, AssignmentQuickForm, ReviewForm, SubmissionForm
from lms.models import Assignment

from .base import LMSCase

SCRIPT = Path("lms/static/lms/js/lms.js").read_text(encoding="utf-8")


def dropzone_fields(form):
    return {
        name: field
        for name, field in form.fields.items()
        if field.widget.attrs.get("data-dropzone") == "1"
    }


class PasteAttributeTests(LMSCase):
    """Каждая форма с файлом объявляет зону загрузки и цель для вставки."""

    def test_full_assignment_form_marks_both_file_fields(self):
        form = AssignmentForm(instance=self.assignment)
        self.assertEqual(sorted(dropzone_fields(form)), ["material_file", "new_attachments"])
        self.assertEqual(form.fields["description"].widget.attrs.get("data-paste-target"), "1")

    def test_quick_editor_marks_the_same_fields_as_the_full_form(self):
        form = AssignmentQuickForm(instance=self.assignment)
        self.assertEqual(sorted(dropzone_fields(form)), ["material_file", "new_attachments"])
        self.assertEqual(form.fields["description"].widget.attrs.get("data-paste-target"), "1")

    def test_quick_editor_states_the_size_limit_and_formats(self):
        attrs = AssignmentQuickForm(instance=self.assignment).fields["material_file"].widget.attrs
        self.assertIn(".png", attrs["accept"])
        self.assertTrue(attrs["data-max-mb"].isdigit())
        self.assertIn("Ctrl+V", attrs["data-dropzone-hint"])

    def test_review_audio_comment_is_a_dropzone(self):
        submission = self.submit(text_answer="Готово")
        attrs = ReviewForm(submission=submission).fields["audio_comment"].widget.attrs
        self.assertEqual(attrs.get("data-dropzone"), "1")
        self.assertIn("Ctrl+V", attrs["data-dropzone-hint"])

    def test_student_submission_stays_a_dropzone(self):
        self.assignment.assignment_type = Assignment.Type.FILE
        form = SubmissionForm(assignment=self.assignment)
        self.assertEqual(sorted(dropzone_fields(form)), ["file_answer"])


class PasteMarkupTests(LMSCase):
    """Атрибуты доезжают до HTML тех страниц, где учитель правит задание."""

    def test_topic_board_quick_editor_renders_a_dropzone(self):
        html = self.teacher_client.get(
            reverse("teacher_topic_board", args=[self.topic.pk])
        ).content.decode()
        self.assertIn('data-dropzone="1"', html)
        self.assertIn('data-paste-target="1"', html)
        self.assertIn("вставьте скриншот Ctrl+V", html)

    def test_topic_board_keeps_the_multiple_attachments_input(self):
        html = self.teacher_client.get(
            reverse("teacher_topic_board", args=[self.topic.pk])
        ).content.decode()
        self.assertIn('name="new_attachments"', html)
        self.assertIn("multiple", html)

    def test_full_editor_renders_a_dropzone_without_its_own_script(self):
        html = self.teacher_client.get(
            reverse("teacher_assignment_form", args=[self.assignment.pk])
        ).content.decode()
        self.assertIn('data-dropzone="1"', html)
        self.assertIn('data-paste-target="1"', html)
        # Вставку теперь ведёт общий скрипт, дубля в шаблоне быть не должно.
        self.assertNotIn("Paste handling for description", html)
        self.assertNotIn("Global paste for dropzones", html)

    def test_review_page_renders_a_dropzone_for_audio(self):
        submission = self.submit(text_answer="Готово")
        html = self.teacher_client.get(
            reverse("teacher_submission_review", args=[submission.pk])
        ).content.decode()
        self.assertIn('data-dropzone="1"', html)
        self.assertIn("вставьте из буфера Ctrl+V", html)


class PasteScriptTests(LMSCase):
    """Общий скрипт разбирает атрибуты, а не конкретные id полной формы."""

    def test_paste_is_handled_globally(self):
        self.assertIn("function pasteUploads()", SCRIPT)
        self.assertIn('document.addEventListener("paste"', SCRIPT)
        self.assertIn("pasteUploads();", SCRIPT)

    def test_paste_target_attribute_is_read(self):
        self.assertIn("[data-paste-target]", SCRIPT)

    def test_destination_is_limited_to_the_form_of_the_paste(self):
        # Иначе скриншот на доске тем улетел бы в соседнее задание.
        self.assertIn('origin.closest("form")', SCRIPT)

    def test_hidden_editors_are_skipped(self):
        self.assertIn("function usableUpload(", SCRIPT)

    def test_clipboard_screenshots_get_a_readable_name(self):
        self.assertIn("screenshot-", SCRIPT)

    def test_dropzone_exposes_its_validation_to_the_paste_handler(self):
        self.assertIn("input.lmsAcceptFiles = acceptFiles;", SCRIPT)


class MultipleAttachmentTests(LMSCase):
    """Несколько файлов разом — обещание подсказки, которое форма не держала."""

    def payload(self, **extra):
        data = {
            "title": self.assignment.title,
            "description": self.assignment.description,
            "assignment_type": self.assignment.assignment_type,
            "status": self.assignment.status,
            "order": self.assignment.order,
            "max_points": self.assignment.max_points,
            "max_tries": self.assignment.max_tries,
        }
        data.update(extra)
        return data

    def files(self):
        return [
            SimpleUploadedFile("one.txt", b"First attachment", "text/plain"),
            SimpleUploadedFile("two.txt", b"Second attachment", "text/plain"),
        ]

    def test_quick_editor_accepts_two_files_at_once(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]),
            self.payload(new_attachments=self.files()),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.assignment.attachments.count(), 2)

    def test_full_editor_accepts_two_files_at_once(self):
        from django.utils.datastructures import MultiValueDict

        form = AssignmentForm(
            self.payload(topic=self.topic.pk),
            MultiValueDict({"new_attachments": self.files()}),
            instance=self.assignment,
        )
        form.is_valid()
        self.assertNotIn("new_attachments", form.errors)

    def test_a_broken_file_is_still_rejected(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]),
            self.payload(
                new_attachments=SimpleUploadedFile("bad.png", b"not an image", "image/png")
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.assignment.attachments.count(), 0)


class PasteTextSafetyTests(LMSCase):
    """Текст из буфера остаётся текстом: Word и Excel кладут в буфер и картинку."""

    def test_text_paste_into_a_field_is_not_hijacked(self):
        self.assertIn('getData("text/plain")', SCRIPT)
        self.assertIn("if (typing && text) return;", SCRIPT)
