import io
import wave
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from lms.forms import SubmissionForm
from lms.models import Assignment, Submission, SubmissionEvent

from .base import LMSCase


def text_file(name="answer.txt", content=b"Actual text answer"):
    return SimpleUploadedFile(name, content, "text/plain")


def wave_file():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    return SimpleUploadedFile("answer.wav", stream.getvalue(), "audio/wav")


class FileTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.FILE
        self.assignment.save()

    def test_required_file_cannot_be_cleared(self):
        old = self.submit(file_answer=text_file())
        response = self.student_client.post(
            self.url, {"expected_version": 1, "file_answer-clear": "on"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Submission.objects.count(), 1)
        old.refresh_from_db()
        self.assertTrue(old.file_answer)

    def test_mixed_answer_requires_both_parts(self):
        self.assignment.assignment_type = "mixed"
        for data, files in [
            ({"expected_version": 0}, {"file_answer": text_file()}),
            ({"expected_version": 0, "text_answer": "Text"}, {}),
        ]:
            self.assertFalse(SubmissionForm(data, files, assignment=self.assignment).is_valid())

    def test_text_assignment_rejects_file_in_service(self):
        self.assignment.assignment_type = "text"
        self.assignment.save()
        with self.assertRaises(ValidationError):
            self.submit(file_answer=text_file())

    def test_pdf_is_not_audio(self):
        self.assignment.assignment_type = "audio"
        form = SubmissionForm(
            {"expected_version": 0},
            {"file_answer": SimpleUploadedFile("answer.pdf", b"%PDF-1.7\nexample")},
            assignment=self.assignment,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("file_answer", form.errors)

    def test_fake_audio_is_rejected(self):
        self.assignment.assignment_type = "audio"
        form = SubmissionForm(
            {"expected_version": 0},
            {"file_answer": text_file("answer.mp3")},
            assignment=self.assignment,
        )
        self.assertFalse(form.is_valid())

    def test_real_audio_is_accepted(self):
        self.assignment.assignment_type = "audio"
        self.assignment.save()
        response = self.student_client.post(
            self.url, {"expected_version": 0, "file_answer": wave_file()}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Submission.objects.get().file_answer.name.endswith(".wav"))

    def test_disallowed_extension_and_false_signature_rejected(self):
        for name in ("answer.exe", "answer.pdf", "answer.docx", "answer.png"):
            with self.subTest(name=name):
                form = SubmissionForm(
                    {"expected_version": 0},
                    {"file_answer": text_file(name)},
                    assignment=self.assignment,
                )
                self.assertFalse(form.is_valid())

    def test_empty_file_rejected(self):
        form = SubmissionForm(
            {"expected_version": 0},
            {"file_answer": text_file(content=b"")},
            assignment=self.assignment,
        )
        self.assertFalse(form.is_valid())

    @override_settings(LMS_MAX_FILE_BYTES=8)
    def test_size_limit_in_form(self):
        form = SubmissionForm(
            {"expected_version": 0},
            {"file_answer": text_file(content=b"x" * 9)},
            assignment=self.assignment,
        )
        self.assertFalse(form.is_valid())

    @override_settings(LMS_MAX_FILE_BYTES=8)
    def test_stream_limit_returns_413_without_saving(self):
        response = self.student_client.post(
            self.url, {"expected_version": 0, "file_answer": text_file(content=b"x" * 100)}
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(Submission.objects.exists())

    @override_settings(LMS_MAX_FILE_BYTES=8)
    def test_declared_body_limit_returns_413(self):
        response = self.student_client.generic(
            "POST",
            self.url,
            data=b"x",
            content_type="application/octet-stream",
            CONTENT_LENGTH=str(2 * 1024 * 1024),
        )
        self.assertEqual(response.status_code, 413)

    @override_settings(LMS_STUDENT_QUOTA_BYTES=10)
    def test_quota_and_shared_file_counting(self):
        first = self.submit(file_answer=text_file(content=b"12345678"))
        second = self.submit(version=1, file_answer=first.file_answer)
        self.assertEqual(first.file_answer.name, second.file_answer.name)
        with self.assertRaises(ValidationError):
            self.submit(version=2, file_answer=text_file(content=b"123"))
        self.assertEqual(Submission.objects.count(), 2)

    def test_private_file_authentication_and_owner_checks(self):
        attempt = self.submit(file_answer=text_file())
        self.assertEqual(Client().get(attempt.file_answer.url).status_code, 302)
        self.assertEqual(self.client_for(self.other).get(attempt.file_answer.url).status_code, 404)
        for client in (self.student_client, self.teacher_client, self.admin_client):
            response = client.get(attempt.file_answer.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"Actual text answer")
            self.assertIn("attachment", response["Content-Disposition"])
            self.assertIn("private", response["Cache-Control"])
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            # Consume ClientHandler's streaming wrapper once. Calling close()
            # again sends request_finished outside its signal guard and closes
            # the surrounding TestCase transaction on PostgreSQL.
            self.assertTrue(response.closed)

    def test_private_files_work_without_debug_and_raw_media_never_works(self):
        attempt = self.submit(file_answer=text_file())
        for debug in (True, False):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                response = self.student_client.get(attempt.file_answer.url)
                self.assertEqual(response.status_code, 200)
                b"".join(response.streaming_content)
                self.assertEqual(
                    self.student_client.get(
                        attempt.file_answer.url.replace("/files/", "/media/")
                    ).status_code,
                    404,
                )

    def test_materials_require_authentication_and_active_hierarchy(self):
        self.assignment.material_file = text_file("material.txt")
        self.assignment.save()
        self.assertEqual(Client().get(self.assignment.material_file.url).status_code, 302)
        response = self.student_client.get(self.assignment.material_file.url)
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)
        self.block.is_active = False
        self.block.save()
        self.assertEqual(
            self.student_client.get(self.assignment.material_file.url).status_code, 404
        )
        response = self.teacher_client.get(self.assignment.material_file.url)
        self.assertEqual(response.status_code, 200)
        b"".join(response.streaming_content)

    def test_unreferenced_paths_and_path_traversal_are_not_downloadable(self):
        (Path(settings.MEDIA_ROOT) / "secret.txt").write_text("Unreferenced")
        for path in ("/files/secret.txt", "/files/../../manage.py", "/files/no-such-file.txt"):
            self.assertEqual(self.student_client.get(path).status_code, 404)

    def test_deleted_attempt_file_removed_only_after_commit(self):
        attempt = self.submit(file_answer=text_file())
        path = Path(attempt.file_answer.path)
        with self.captureOnCommitCallbacks(execute=True):
            attempt.delete()
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())

    def test_shared_historical_file_not_deleted_while_still_referenced(self):
        first = self.submit(file_answer=text_file())
        second = self.submit(version=1, file_answer=first.file_answer)
        path = Path(first.file_answer.path)
        with self.captureOnCommitCallbacks(execute=True):
            first.delete()
        self.assertTrue(path.exists())
        with self.captureOnCommitCallbacks(execute=True):
            second.delete()
        self.assertFalse(path.exists())

    def test_replacing_answer_preserves_intentional_history(self):
        first = self.submit(file_answer=text_file())
        second = self.submit(version=1, file_answer=text_file(content=b"Revised"))
        self.assertNotEqual(first.file_answer.name, second.file_answer.name)
        self.assertTrue(Path(first.file_answer.path).exists())

    def test_replacing_material_removes_unreferenced_old_file(self):
        self.assignment.material_file = text_file()
        self.assignment.save()
        old = Path(self.assignment.material_file.path)
        with self.captureOnCommitCallbacks(execute=True):
            self.assignment.material_file = text_file(content=b"Updated material")
            self.assignment.save()
        self.assertFalse(old.exists())
        self.assertTrue(Path(self.assignment.material_file.path).exists())

    def test_failed_submission_does_not_leak_new_storage_file(self):
        with patch.object(
            SubmissionEvent.objects, "create", side_effect=RuntimeError("simulated failure")
        ):
            with self.assertRaises(RuntimeError):
                self.submit(file_answer=text_file())
        self.assertFalse(Submission.objects.exists())
        self.assertEqual([p for p in Path(settings.MEDIA_ROOT).rglob("*") if p.is_file()], [])

    def test_valid_docx_container_is_accepted(self):
        from zipfile import ZipFile

        data = io.BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("word/document.xml", "<document>Example</document>")
        form = SubmissionForm(
            {"expected_version": 0},
            {"file_answer": SimpleUploadedFile("answer.docx", data.getvalue())},
            assignment=self.assignment,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_unbounded_zip_directory_is_rejected_before_parsing(self):
        import struct
        from zipfile import ZipFile

        data = io.BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("file.txt", "Example")
        content = bytearray(data.getvalue())
        eocd = content.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", content, eocd + 8, 65535, 65535)
        form = SubmissionForm(
            {"expected_version": 0},
            {"file_answer": SimpleUploadedFile("answer.zip", bytes(content))},
            assignment=self.assignment,
        )
        self.assertFalse(form.is_valid())
