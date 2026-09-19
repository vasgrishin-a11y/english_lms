import io
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from lms.models import Feedback

from .base import LMSCase
from .test_files import text_file


class MaintenanceTests(LMSCase):
    def test_admin_bootstrap_requires_complete_valid_credentials(self):
        for env in (
            {},
            {"DJANGO_SUPERUSER_USERNAME": "newadmin"},
            {"DJANGO_SUPERUSER_USERNAME": "newadmin", "DJANGO_SUPERUSER_PASSWORD": "123"},
        ):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(CommandError):
                call_command("bootstrap_admin", stdout=io.StringIO())

    def test_admin_bootstrap_is_idempotent_and_never_promotes_existing_user(self):
        env = {"DJANGO_SUPERUSER_USERNAME": "newadmin", "DJANGO_SUPERUSER_PASSWORD": self.password}
        with patch.dict(os.environ, env):
            call_command("bootstrap_admin", stdout=io.StringIO())
            user = get_user_model().objects.get(username="newadmin")
            password = user.password
            call_command("bootstrap_admin", stdout=io.StringIO())
            user.refresh_from_db()
            self.assertTrue(user.is_superuser)
            self.assertEqual(user.password, password)
        with (
            patch.dict(os.environ, {"DJANGO_SUPERUSER_USERNAME": "student"}),
            self.assertRaises(CommandError),
        ):
            call_command("bootstrap_admin", stdout=io.StringIO())

    def test_orphan_cleanup_defaults_to_dry_run_and_respects_grace_period(self):
        root = Path(settings.MEDIA_ROOT) / "submissions"
        root.mkdir(parents=True)
        old, recent = root / "old.txt", root / "recent.txt"
        old.write_text("Old unreferenced content")
        recent.write_text("In-flight upload")
        os.utime(old, (1, 1))
        call_command("cleanup_orphan_files", stdout=io.StringIO())
        self.assertTrue(old.exists())
        call_command("cleanup_orphan_files", delete=True, stdout=io.StringIO())
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        with self.assertRaises(CommandError):
            call_command("cleanup_orphan_files", min_age_hours=0)

    def test_orphan_cleanup_never_deletes_referenced_or_outside_files(self):
        self.assignment.assignment_type = "file"
        self.assignment.save()
        attempt = self.submit(file_answer=text_file())
        path = Path(attempt.file_answer.path)
        os.utime(path, (1, 1))
        outside = Path(settings.MEDIA_ROOT).parent / "outside.txt"
        outside.write_text("Outside")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = Path(settings.MEDIA_ROOT) / "submissions" / "link.txt"
        link.symlink_to(outside)
        call_command("cleanup_orphan_files", delete=True, stdout=io.StringIO())
        self.assertTrue(path.exists())
        self.assertTrue(outside.exists())

    def test_student_erasure_requires_explicit_confirmation(self):
        self.assignment.assignment_type = "file"
        self.assignment.save()
        attempt = self.submit(file_answer=text_file())
        path = Path(attempt.file_answer.path)
        call_command("purge_student", "student", stdout=io.StringIO())
        self.assertTrue(get_user_model().objects.filter(pk=self.student.pk).exists())
        with self.captureOnCommitCallbacks(execute=True):
            call_command(
                "purge_student", "student", confirm_username="student", stdout=io.StringIO()
            )
        self.assertFalse(get_user_model().objects.filter(pk=self.student.pk).exists())
        self.assertFalse(path.exists())

    def test_teacher_cannot_be_erased_by_student_command(self):
        with self.assertRaises(CommandError):
            call_command(
                "purge_student", "teacher", confirm_username="teacher", stdout=io.StringIO()
            )

    def test_legacy_audit_is_read_only_and_rejects_invalid_grade(self):
        attempt = self.submit()
        self.review(attempt)
        call_command("audit_legacy_data", stdout=io.StringIO())
        Feedback.objects.filter(submission=attempt).update(grade=101)
        with self.assertRaises(CommandError):
            call_command("audit_legacy_data", stdout=io.StringIO())
        self.assertEqual(Feedback.objects.get().grade, 101)
