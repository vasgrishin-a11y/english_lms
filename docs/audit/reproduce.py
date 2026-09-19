"""Characterization probes for english_lms, not a passing security regression suite.

Run from the repository root with its requirements installed. Uses an in-memory
SQLite test database and temporary media/static directories, never the real DB.
The assertions deliberately prove current defects as well as working controls.
Optional AUDIT_ARTIFACTS points to a directory for JSON and synthetic HTML.
"""
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
from datetime import timedelta
from unittest.mock import patch

REPO = Path(os.environ.get("AUDIT_REPO", Path.cwd())).resolve()
if not (REPO / "manage.py").is_file():
    raise SystemExit("Run from the english_lms repository root or set AUDIT_REPO.")
if hashlib.sha256((REPO / "lms/forms.py").read_bytes()).hexdigest() != "d4979fcee7c4c2dfa476d285acde2ba91e1a5565db5e2881bd5c3e25693ff961":
    raise SystemExit("Historical audit only: export commit d669968 and set AUDIT_REPO to it. For the fixed version run manage.py test --settings=core.test_settings.")
sys.path.insert(0, str(REPO))
os.environ["DJANGO_SETTINGS_MODULE"] = "core.settings"
# Prevent dotenv/database settings from pointing these probes at a deployed DB.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(64)
os.environ["DJANGO_DEBUG"] = "True"

import django
from django.conf import settings

TEMP = tempfile.TemporaryDirectory(prefix="english-lms-audit-")
settings.DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
}
settings.DATABASE_ROUTERS = []
settings.ALLOWED_HOSTS = ["testserver"]
settings.MEDIA_ROOT = Path(TEMP.name) / "media"
settings.STATIC_ROOT = Path(TEMP.name) / "static"
settings.STATIC_ROOT.mkdir()
settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
django.setup()

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.runner import DiscoverRunner
from django.test.utils import CaptureQueriesContext
from django.urls import clear_url_caches
from django.utils import timezone

from lms.forms import ReviewForm, SubmissionForm
from lms.models import Assignment, Block, Feedback, Profile, Submission, Topic

User = get_user_model()
OBSERVATIONS = {}
ARTIFACTS = os.environ.get("AUDIT_ARTIFACTS")
logging.getLogger("django.request").setLevel(logging.CRITICAL)
logging.getLogger("django.security.csrf").setLevel(logging.CRITICAL)


def observe(key, **values):
    OBSERVATIONS[key] = values
    print("OBSERVATION", key, json.dumps(values, ensure_ascii=False), flush=True)


class AuditProbes(TestCase):
    def setUp(self):
        self.student = User.objects.create_user("audit_student", password="temporary")
        self.teacher = User.objects.create_user("audit_teacher", password="temporary")
        self.teacher.profile.role = Profile.Role.TEACHER
        self.teacher.profile.save()
        self.admin = User.objects.create_superuser(
            "audit_admin", "audit@example.invalid", "temporary"
        )
        self.block = Block.objects.create(name="English", slug="english")
        self.topic = Topic.objects.create(
            block=self.block, title="Grammar", slug="grammar"
        )
        self.assignment = Assignment.objects.create(
            topic=self.topic, title="Present perfect", description="Write an answer.",
            max_points=100,
        )
        self.student_client = Client()
        self.student_client.force_login(self.student)
        self.teacher_client = Client()
        self.teacher_client.force_login(self.teacher)
        self.admin_client = Client()
        self.admin_client.force_login(self.admin)

    def url(self, assignment=None):
        return f"/assignments/{(assignment or self.assignment).pk}/"

    def review_url(self, submission):
        return f"/teacher/submissions/{submission.pk}/"

    def submission(self, **kwargs):
        values = dict(
            student=self.student, assignment=self.assignment,
            text_answer="Original answer", status=Submission.Status.SUBMITTED,
        )
        values.update(kwargs)
        return Submission.objects.create(**values)

    def make_file_submission(self):
        self.assignment.assignment_type = Assignment.Type.FILE
        self.assignment.save()
        return self.submission(file_answer=SimpleUploadedFile("answer.txt", b"private answer"))

    def test_01_grade_above_assignment_and_model_max_is_saved(self):
        submission = self.submission()
        form = ReviewForm(
            {"grade": "1001", "comment": "review", "decision": "checked"},
            assignment=self.assignment,
        )
        self.assertTrue(form.is_valid(), form.errors)
        response = self.teacher_client.post(
            self.review_url(submission),
            {"grade": "1001", "comment": "review", "decision": "checked"},
        )
        self.assertEqual(response.status_code, 302)
        feedback = Feedback.objects.get(submission=submission)
        self.assertEqual(feedback.grade, 1001)
        observe("grade_limit", http=response.status_code, assignment_max=100,
                submitted_grade=1001, saved_grade=feedback.grade,
                validators=[type(v).__name__ for v in form.fields["grade"].validators])

    def test_02_required_existing_file_can_be_cleared(self):
        submission = self.make_file_submission()
        response = self.student_client.post(self.url(), {"file_answer-clear": "on"})
        submission.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(submission.file_answer)
        self.assertEqual(submission.status, Submission.Status.SUBMITTED)
        observe("required_file_clear", http=response.status_code,
                saved_file=str(submission.file_answer), status=submission.status)

    def test_03_audio_assignment_accepts_non_audio_file(self):
        self.assignment.assignment_type = Assignment.Type.AUDIO
        self.assignment.save()
        response = self.student_client.post(
            self.url(), {"file_answer": SimpleUploadedFile("answer.pdf", b"not audio", "application/pdf")}
        )
        self.assertEqual(response.status_code, 302)
        saved = Submission.objects.get(assignment=self.assignment, student=self.student)
        self.assertTrue(saved.file_answer.name.endswith(".pdf"))
        observe("audio_type", http=response.status_code, accepted_extension="pdf")

    def test_04_file_content_is_not_validated(self):
        self.assignment.assignment_type = Assignment.Type.AUDIO
        form = SubmissionForm(
            {}, {"file_answer": SimpleUploadedFile("answer.mp3", b"not an mp3 file", "text/plain")},
            assignment=self.assignment,
        )
        self.assertTrue(form.is_valid(), form.errors)
        observe("file_content", accepted=True, filename="answer.mp3", content_type="text/plain")

    def test_05_large_file_has_no_application_size_limit(self):
        self.assignment.assignment_type = Assignment.Type.FILE
        self.assignment.save()
        length = 16 * 1024 * 1024
        response = self.student_client.post(
            self.url(), {"file_answer": SimpleUploadedFile("large.txt", b"x" * length)}
        )
        self.assertEqual(response.status_code, 302)
        saved = Submission.objects.get(assignment=self.assignment, student=self.student)
        self.assertEqual(saved.file_answer.size, length)
        observe("file_size", http=response.status_code, bytes_saved=length,
                memory_threshold=settings.FILE_UPLOAD_MAX_MEMORY_SIZE)

    def test_06_media_is_readable_without_authentication_in_debug(self):
        submission = self.make_file_submission()
        response = Client().get(submission.file_answer.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"private answer")
        response.close()
        other = User.objects.create_user("other_student")
        other_client = Client()
        other_client.force_login(other)
        other_response = other_client.get(submission.file_answer.url)
        self.assertEqual(other_response.status_code, 200)
        other_response.close()
        observe("media_acl", debug=True, anonymous_http=200, other_student_http=200)

    def test_07_media_is_not_served_by_production_urlconf(self):
        import core.urls
        submission = self.make_file_submission()
        try:
            with override_settings(DEBUG=False):
                importlib.reload(core.urls)
                clear_url_caches()
                response = self.student_client.get(submission.file_answer.url)
                self.assertEqual(response.status_code, 404)
                observe("production_media", debug=False, file_exists=True,
                        authenticated_http=response.status_code)
        finally:
            importlib.reload(core.urls)
            clear_url_caches()

    def test_08_resubmission_keeps_feedback_for_replaced_answer(self):
        submission = self.submission()
        Feedback.objects.create(submission=submission, teacher=self.teacher, grade=90,
                                comment="This feedback belongs to the original answer.")
        response = self.student_client.post(self.url(), {"text_answer": "Completely different answer"})
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.SUBMITTED)
        self.assertEqual(submission.feedback.grade, 90)
        detail = self.student_client.get(self.url()).content.decode()
        self.assertIn("This feedback belongs to the original answer.", detail)
        self.assertIn("Completely different answer", detail)
        observe("stale_feedback", new_status=submission.status, old_grade_visible=True,
                new_answer_saved=True, feedback_records=Feedback.objects.count())

    def test_09_resubmission_keeps_original_time_and_is_late_is_false(self):
        self.assignment.deadline = timezone.now() - timedelta(days=1)
        self.assignment.save()
        submission = self.submission()
        initial_time = self.assignment.deadline - timedelta(days=1)
        Submission.objects.filter(pk=submission.pk).update(submitted_at=initial_time)
        response = self.student_client.post(self.url(), {"text_answer": "Answer after deadline"})
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.submitted_at, initial_time)
        self.assertFalse(submission.is_late)
        observe("resubmission_time", resubmitted_after_deadline=True,
                submitted_at_unchanged=True, is_late=submission.is_late)

    def test_10_teacher_can_mark_an_unseen_new_version_checked(self):
        submission = self.submission(text_answer="Version seen by teacher")
        review_page = self.teacher_client.get(self.review_url(submission))
        self.assertContains(review_page, "Version seen by teacher")
        self.student_client.post(self.url(), {"text_answer": "Version teacher has never seen"})
        response = self.teacher_client.post(
            self.review_url(submission), {"grade": "80", "comment": "For old version", "decision": "checked"}
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.text_answer, "Version teacher has never seen")
        self.assertEqual(submission.status, Submission.Status.CHECKED)
        observe("stale_review", sequence="teacher GET -> student POST -> teacher POST",
                unseen_answer_status=submission.status, saved_grade=submission.feedback.grade)

    def test_11_revision_update_is_not_atomic_under_injected_failure(self):
        submission = self.submission()
        original_save = Submission.save

        def fail_revision(instance, *args, **kwargs):
            if instance.status == Submission.Status.NEEDS_REVISION:
                raise RuntimeError("injected final state write failure")
            return original_save(instance, *args, **kwargs)

        with patch.object(Submission, "save", fail_revision):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.teacher_client.post(
                    self.review_url(submission),
                    {"grade": "10", "comment": "Please revise", "decision": "needs_revision"},
                )
        submission.refresh_from_db()
        self.assertTrue(Feedback.objects.filter(submission=submission).exists())
        self.assertEqual(submission.status, Submission.Status.CHECKED)
        observe("review_atomicity", fault_injection=True,
                requested_status="needs_revision", persisted_status=submission.status,
                feedback_persisted=True)

    def test_12_admin_creation_with_profile_data_raises_integrity_error(self):
        data = {
            "username": "new_user_with_profile",
            "password1": "TemporaryAuditPassword!427",
            "password2": "TemporaryAuditPassword!427",
            "usable_password": "true",
            "profile-TOTAL_FORMS": "1", "profile-INITIAL_FORMS": "0",
            "profile-MIN_NUM_FORMS": "0", "profile-MAX_NUM_FORMS": "1",
            "profile-0-id": "", "profile-0-user": "",
            "profile-0-role": "teacher", "profile-0-telegram": "@audit_example",
            "profile-0-comment": "Created through the only user-management interface",
            "_save": "Save",
        }
        with self.assertRaises(IntegrityError) as raised:
            with transaction.atomic():
                response = self.admin_client.post("/admin/auth/user/add/", data)
                if response.status_code == 200:
                    print("ADMIN_FORM_ERRORS", response.context["adminform"].form.errors)
                    for fs in response.context["inline_admin_formsets"]:
                        print("INLINE_ERRORS", fs.formset.errors, fs.formset.non_form_errors())
        self.assertIn("lms_profile.user_id", str(raised.exception))
        self.assertFalse(User.objects.filter(username="new_user_with_profile").exists())
        observe("admin_user_profile_collision", exception="IntegrityError",
                unique_constraint="lms_profile.user_id", user_creation_rolled_back=True)

    def test_13_admin_add_submission_lacks_required_foreign_keys(self):
        response = self.admin_client.get("/admin/lms/submission/add/")
        self.assertEqual(response.status_code, 200)
        fields = list(response.context["adminform"].form.fields)
        self.assertNotIn("student", fields)
        self.assertNotIn("assignment", fields)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.admin_client.post("/admin/lms/submission/add/", {"status": "submitted", "_save": "Save"})
        observe("admin_add_submission", editable_fields=fields,
                exception="IntegrityError: required FK missing")

    def test_14_replaced_and_deleted_files_remain_in_storage(self):
        submission = self.make_file_submission()
        first_path = Path(submission.file_answer.path)
        response = self.student_client.post(
            self.url(), {"file_answer": SimpleUploadedFile("replacement.txt", b"new private answer")}
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        second_path = Path(submission.file_answer.path)
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.exists())
        submission.delete()
        self.assertTrue(second_path.exists())
        observe("orphan_files", old_file_after_replace=True, latest_file_after_delete=True)

    def test_15_assignment_catalog_query_count_scales_with_topics(self):
        with CaptureQueriesContext(connection) as baseline:
            response = self.student_client.get("/assignments/")
            self.assertEqual(response.status_code, 200)
        Block.objects.all().delete()
        for b in range(6):
            block = Block.objects.create(name=f"Block {b}", slug=f"block-{b}")
            for t in range(4):
                topic = Topic.objects.create(block=block, title=f"Topic {t}", slug=f"topic-{t}")
                Assignment.objects.create(topic=topic, title=f"Assignment {b}-{t}", description="Test")
        with CaptureQueriesContext(connection) as scaled:
            response = self.student_client.get("/assignments/")
            self.assertEqual(response.status_code, 200)
        self.assertGreater(len(scaled), len(baseline) + 40)
        observe("catalog_queries", small={"blocks": 1, "topics": 1, "queries": len(baseline)},
                larger={"blocks": 6, "topics": 24, "queries": len(scaled)})

    def test_16_teacher_queue_is_unpaginated(self):
        assignments = Assignment.objects.bulk_create([
            Assignment(topic=self.topic, title=f"Task {i}", description="Test") for i in range(205)
        ])
        Submission.objects.bulk_create([
            Submission(student=self.student, assignment=a, status="submitted", text_answer="Answer")
            for a in assignments
        ])
        response = self.teacher_client.get("/teacher/submissions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["submissions"]), 205)
        self.assertNotIn("LIMIT", str(response.context["submissions"].query).upper())
        observe("teacher_pagination", rows_in_one_response=205, response_bytes=len(response.content))

    def test_17_second_teacher_can_see_all_submissions(self):
        submission = self.submission()
        teacher2 = User.objects.create_user("second_teacher")
        teacher2.profile.role = Profile.Role.TEACHER
        teacher2.profile.save()
        client = Client()
        client.force_login(teacher2)
        response = client.get(self.review_url(submission))
        self.assertEqual(response.status_code, 200)
        observe("teacher_scope", other_teacher_http=200,
                note="Architectural observation; no per-class ownership exists in the data model")

    def test_18_standard_authorization_controls_work(self):
        submission = self.submission()
        anonymous = Client()
        self.assertEqual(anonymous.get(self.url()).status_code, 302)
        forbidden = self.student_client.post(
            self.review_url(submission), {"grade": "100", "decision": "checked"}
        )
        self.assertEqual(forbidden.status_code, 302)
        self.assertFalse(Feedback.objects.filter(submission=submission).exists())
        self.assertRedirects(self.teacher_client.get(self.url()), "/teacher/submissions/", fetch_redirect_response=False)
        observe("authorization_controls", anonymous_redirect=True,
                student_cannot_review=True, teacher_redirect_from_student_view=True)

    def test_19_student_html_does_not_leak_another_students_answer(self):
        other = User.objects.create_user("other_student")
        self.submission(student=other, text_answer="OTHER_STUDENT_SECRET")
        response = self.student_client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "OTHER_STUDENT_SECRET")
        observe("student_html_isolation", protected=True)

    def test_20_inactive_assignment_hierarchy_is_hidden(self):
        for obj in [self.assignment, self.topic, self.block]:
            obj.is_active = False
            obj.save()
            self.assertEqual(self.student_client.get(self.url()).status_code, 404)
            obj.is_active = True
            obj.save()
        observe("inactive_hierarchy", assignment_404=True, topic_404=True, block_404=True)

    def test_21_csrf_controls_work(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        self.assertEqual(client.post(self.url(), {"text_answer": "No CSRF token"}).status_code, 403)
        self.assertEqual(client.post("/accounts/login/", {"username": "x", "password": "x"}).status_code, 403)
        self.assertEqual(client.get("/accounts/logout/").status_code, 405)
        observe("csrf", submission_post_without_token=403, login_without_token=403, logout_get=405)

    def test_22_template_output_is_escaped(self):
        payload = "<script>alert('audit')</script>"
        submission = self.submission(text_answer=payload)
        self.assignment.description = payload
        self.assignment.save()
        Feedback.objects.create(submission=submission, teacher=self.teacher, grade=1, comment=payload)
        response = self.student_client.get(self.url())
        self.assertNotIn(payload, response.content.decode())
        self.assertContains(response, "&lt;script&gt;")
        observe("template_escaping", text_answer=True, description=True, feedback_comment=True)

    def test_23_disallowed_extension_is_rejected(self):
        self.assignment.assignment_type = Assignment.Type.FILE
        form = SubmissionForm({}, {"file_answer": SimpleUploadedFile("answer.exe", b"not executable")},
                              assignment=self.assignment)
        self.assertFalse(form.is_valid())
        self.assertIn("file_answer", form.errors)
        observe("extension_allowlist", exe_rejected=True)

    def test_24_negative_grade_is_rejected(self):
        form = ReviewForm({"grade": "-1", "decision": "checked"}, assignment=self.assignment)
        self.assertFalse(form.is_valid())
        self.assertIn("grade", form.errors)
        observe("minimum_grade", negative_rejected=True)

    def test_25_external_login_redirect_is_rejected(self):
        response = Client().post("/accounts/login/", {
            "username": "audit_student", "password": "temporary", "next": "https://example.invalid/"
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        observe("login_redirect", external_next_rejected=True)

    def test_26_feedback_admin_allows_more_than_assignment_max(self):
        submission = self.submission()
        from django.contrib import admin
        from django.test import RequestFactory
        request = RequestFactory().get("/admin/lms/feedback/add/")
        request.user = self.admin
        model_admin = admin.site._registry[Feedback]
        form_class = model_admin.get_form(request)
        form = form_class({"submission": submission.pk, "teacher": self.teacher.pk,
                           "grade": 101, "comment": "Over assignment limit"})
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.grade, 101)
        observe("admin_grade_limit", assignment_max=100, admin_accepts_grade=101)

    def test_27_first_submission_race_is_not_handled(self):
        # Deterministic stale-read injection, not a real concurrent load test.
        existing = self.submission()
        from django.db.models.query import QuerySet
        original_first = QuerySet.first

        def stale_first(qs):
            if qs.model is Submission:
                return None
            return original_first(qs)

        with patch.object(QuerySet, "first", stale_first):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    self.student_client.post(self.url(), {"text_answer": "Concurrent first answer"})
        existing.refresh_from_db()
        observe("first_submission_race", stale_read_injection=True,
                duplicate_insert="unhandled IntegrityError", original_row_preserved=True)

    def test_28_baseline_admin_student_creation_without_changed_inline_succeeds(self):
        response = self.admin_client.post("/admin/auth/user/add/", {
            "username": "new_plain_student", "password1": "TemporaryAuditPassword!427",
            "password2": "TemporaryAuditPassword!427", "usable_password": "true",
            "profile-TOTAL_FORMS": "1", "profile-INITIAL_FORMS": "0",
            "profile-MIN_NUM_FORMS": "0", "profile-MAX_NUM_FORMS": "1",
            "profile-0-id": "", "profile-0-user": "", "profile-0-role": "student",
            "profile-0-telegram": "", "profile-0-comment": "", "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username="new_plain_student")
        self.assertEqual(created.profile.role, Profile.Role.STUDENT)
        observe("admin_plain_student_creation", http=response.status_code, succeeds=True)

    def test_29_a11y_markup_and_status_styles(self):
        submission = self.submission(status="needs_revision")
        response = self.teacher_client.get(self.review_url(submission))
        html = response.content.decode()
        self.assertIn('aria-describedby="id_grade_helptext"', html)
        self.assertNotIn('id="id_grade_helptext"', html)
        self.assertIn("badge-needs_revision", html)
        self.assertNotIn(".badge-needs_revision", html)
        observe("markup", grade_helptext_id_missing=True, revision_badge_css_mismatch=True)

    def test_30_capture_synthetic_pages_for_optional_browser_audit(self):
        submission = self.submission(status="needs_revision")
        pages = {
            "login": Client().get("/accounts/login/"),
            "student_catalog": self.student_client.get("/assignments/"),
            "assignment": self.student_client.get(self.url()),
            "teacher_queue": self.teacher_client.get("/teacher/submissions/"),
            "teacher_review": self.teacher_client.get(self.review_url(submission)),
        }
        for name, response in pages.items():
            self.assertEqual(response.status_code, 200)
            if ARTIFACTS:
                destination = Path(ARTIFACTS) / "html"
                destination.mkdir(parents=True, exist_ok=True)
                (destination / f"{name}.html").write_bytes(response.content)
        observe("rendering", all_five_application_pages_http=200)

    def test_31_admin_feedback_edit_silently_approves_revision(self):
        submission = self.submission()
        feedback = Feedback.objects.create(submission=submission, teacher=self.teacher,
                                           grade=10, comment="Please revise")
        Submission.objects.filter(pk=submission.pk).update(status=Submission.Status.NEEDS_REVISION)
        response = self.admin_client.post(f"/admin/lms/feedback/{feedback.pk}/change/", {
            "submission": submission.pk, "teacher": self.teacher.pk,
            "grade": 10, "comment": "Corrected typo in revision instructions", "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.CHECKED)
        observe("admin_revision_state", old_status="needs_revision",
                action="edit feedback comment", resulting_status=submission.status)

    def test_32_login_attempts_have_no_in_application_throttle(self):
        client = Client()
        statuses = [client.post("/accounts/login/", {
            "username": "audit_student", "password": "wrong_password"
        }).status_code for _ in range(30)]
        self.assertEqual(set(statuses), {200})
        success = client.post("/accounts/login/", {"username": "audit_student", "password": "temporary"})
        self.assertEqual(success.status_code, 302)
        observe("login_throttling", failed_attempts=30, http_codes=sorted(set(statuses)),
                next_valid_login_http=success.status_code, external_proxy_not_tested=True)


if __name__ == "__main__":
    class ReportingRunner(DiscoverRunner):
        def suite_result(self, suite, result, **kwargs):
            self.audit_result = result
            return super().suite_result(suite, result, **kwargs)

    try:
        runner = ReportingRunner(verbosity=2, interactive=False, debug_mode=True)
        failures = runner.run_tests(["__main__"])
        result = runner.audit_result
        report = {
            "python": sys.version.split()[0], "django": django.get_version(),
            "tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "observations": OBSERVATIONS,
            "warning": "Passing characterization probes confirm existing behavior, including defects; they do not certify application security.",
        }
        if ARTIFACTS:
            Path(ARTIFACTS).mkdir(parents=True, exist_ok=True)
            (Path(ARTIFACTS) / "probes.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "observations"}, ensure_ascii=False))
    finally:
        TEMP.cleanup()
    raise SystemExit(bool(failures))
