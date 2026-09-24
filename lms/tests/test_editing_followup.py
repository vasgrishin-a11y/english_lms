import base64
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from lms.forms import AssignmentQuickForm
from lms.models import Assignment, AssignmentAttachment, Block, Topic

from .base import LMSCase

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)


class BulkTopicCopyTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.target = Block.objects.create(name="Target", slug="target")
        self.second = Topic.objects.create(block=self.block, title="Second", slug="second")
        self.copy_url = reverse("teacher_block_edit", args=[self.target.pk])

    def test_copies_multiple_topics_once_preserving_sources(self):
        response = self.teacher_client.post(
            self.copy_url,
            {"copy_topics": "1", "copy_topic_ids": [self.topic.pk, self.second.pk, self.topic.pk]},
        )
        self.assertRedirects(response, self.copy_url)
        self.assertEqual(self.target.topics.count(), 2)
        copy = Assignment.objects.get(topic__block=self.target)
        self.assertEqual(copy.status, "draft")
        self.assertEqual(copy.title, self.assignment.title)
        self.assertTrue(Topic.objects.filter(pk=self.topic.pk, block=self.block).exists())
        self.assertContains(self.teacher_client.get(self.copy_url), 'name="copy_topic_ids"')

    def test_empty_or_invalid_selection_copies_nothing(self):
        for ids in ([], [self.topic.pk, "bad"], [self.topic.pk, "999999"]):
            self.teacher_client.post(self.copy_url, {"copy_topics": "1", "copy_topic_ids": ids})
            self.assertEqual(self.target.topics.count(), 0)

    def test_bulk_copy_rolls_back_on_failure(self):
        from lms.views_teacher import _clone_topic_to_block

        def clone(source, block):
            if source.pk == self.second.pk:
                raise RuntimeError("Simulated failure")
            return _clone_topic_to_block(source, block)

        with patch("lms.views_teacher._clone_topic_to_block", side_effect=clone):
            with self.assertRaises(RuntimeError):
                self.teacher_client.post(
                    self.copy_url,
                    {"copy_topics": "1", "copy_topic_ids": [self.topic.pk, self.second.pk]},
                )
        self.assertEqual(self.target.topics.count(), 0)

    def test_student_cannot_copy(self):
        response = self.student_client.post(
            self.copy_url, {"copy_topics": "1", "copy_topic_ids": [self.topic.pk]}
        )
        self.assertEqual(response.status_code, 403)


class MaterialClearTests(LMSCase):
    def test_clear_ids_are_unique_and_saving_removes_only_requested_material(self):
        self.assignment.material_file.save(
            "main.txt", SimpleUploadedFile("main.txt", b"Saved text")
        )
        other = Assignment.objects.create(
            topic=self.topic,
            title="Other",
            description="Other",
            material_file=self.assignment.material_file.name,
        )
        first = AssignmentQuickForm(instance=self.assignment, auto_id="first-%s")
        second = AssignmentQuickForm(instance=other, auto_id="second-%s")
        self.assertIn('id="first-material_file-clear"', str(first["material_file"]))
        self.assertIn('id="second-material_file-clear"', str(second["material_file"]))
        form = AssignmentQuickForm(
            {
                "title": "Updated",
                "description": "Updated",
                "status": "published",
                "assignment_type": "text",
                "order": 0,
                "max_points": 100,
                "max_tries": 3,
                "material_file-clear": "on",
            },
            instance=self.assignment,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assignment.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(self.assignment.material_file)
        self.assertTrue(other.material_file)


class ExtractTextTests(LMSCase):
    @property
    def extract_url(self):
        return reverse("teacher_ai_extract")

    def test_text_upload_offline_and_role_restriction(self):
        response = self.teacher_client.post(
            self.extract_url, {"file": SimpleUploadedFile("text.txt", b"Read this text")}
        )
        self.assertEqual(response.json(), {"ok": True, "text": "Read this text"})
        self.assertEqual(self.student_client.post(self.extract_url).status_code, 403)

    @override_settings(
        LMS_AI_ENABLED=True,
        LMS_AI_LOCAL=True,
        LMS_AI_PROVIDER="ollama",
        LMS_AI_MODEL="qwen3-vl:8b",
        LMS_AI_ENDPOINT="http://localhost:11434/v1",
    )
    @patch("lms.ai._http_json")
    def test_image_ocr_returns_plain_text_not_generated_material(self, transport):
        transport.return_value = {
            "choices": [{"message": {"content": "Original sentence.\nSecond line."}}]
        }
        response = self.teacher_client.post(
            self.extract_url, {"file": SimpleUploadedFile("page.png", PNG, "image/png")}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["text"], "Original sentence.\nSecond line.")
        request = transport.call_args.args[0]
        body = json.loads(request.data)
        self.assertNotIn("response_format", body)
        self.assertEqual(
            body["messages"][0]["content"][1]["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(PNG).decode(),
        )

    @override_settings(LMS_AI_ENABLED=False)
    def test_image_without_vision_gives_actionable_error(self):
        response = self.teacher_client.post(
            self.extract_url, {"file": SimpleUploadedFile("page.png", PNG, "image/png")}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("поддержкой изображений", response.json()["error"])

    def test_existing_attachment_and_missing_file(self):
        attachment = AssignmentAttachment.objects.create(
            assignment=self.assignment, file=SimpleUploadedFile("saved.txt", b"Already saved")
        )
        response = self.teacher_client.post(self.extract_url, {"attachment_id": attachment.pk})
        self.assertEqual(response.json()["text"], "Already saved")
        attachment.file.storage.delete(attachment.file.name)
        self.assertEqual(
            self.teacher_client.post(
                self.extract_url, {"attachment_id": attachment.pk}
            ).status_code,
            400,
        )

    @patch("lms.ai._provider_material")
    def test_invalid_image_never_sent_to_provider(self, provider):
        response = self.teacher_client.post(
            self.extract_url, {"file": SimpleUploadedFile("fake.png", b"not png")}
        )
        self.assertEqual(response.status_code, 400)
        provider.assert_not_called()

    @override_settings(LMS_AI_MAX_FILE_BYTES=8)
    def test_oversized_file_is_rejected(self):
        response = self.teacher_client.post(
            self.extract_url, {"file": SimpleUploadedFile("text.txt", b"x" * 9)}
        )
        self.assertEqual(response.status_code, 400)
