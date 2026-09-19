from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.utils import timezone

from lms.forms import ReviewForm
from lms.models import Feedback, Submission, SubmissionEvent
from lms.services import ConflictError, RateLimitError

from .base import LMSCase


class WorkflowTests(LMSCase):
    def test_initial_submission_is_versioned(self):
        response = self.student_client.post(
            self.url, {"text_answer": "First", "expected_version": 0}
        )
        self.assertEqual(response.status_code, 302)
        attempt = Submission.objects.get()
        self.assertEqual(
            (attempt.version, attempt.status, attempt.max_points_snapshot), (1, "submitted", 100)
        )
        self.assertEqual(attempt.events.get().action, "submitted")

    def test_empty_text_rejected(self):
        response = self.student_client.post(self.url, {"text_answer": "  ", "expected_version": 0})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Submission.objects.exists())

    def test_missing_version_token_rejected(self):
        response = self.student_client.post(self.url, {"text_answer": "Answer"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Submission.objects.exists())

    def test_resubmission_preserves_old_answer_and_grade(self):
        first = self.submit()
        self.review(first)
        second = self.submit(version=1, text_answer="New answer")
        first.refresh_from_db()
        self.assertEqual(first.text_answer, "A real answer")
        self.assertEqual(first.feedback.grade, 80)
        self.assertEqual(first.status, "checked")
        self.assertEqual(second.version, 2)
        self.assertFalse(Feedback.objects.filter(submission=second).exists())
        self.assertEqual(second.status, "submitted")
        self.assertContains(self.student_client.get(self.url), "Попытка 2")

    def test_resubmission_has_real_time_and_lateness(self):
        first = self.submit()
        Submission.objects.filter(pk=first.pk).update(
            submitted_at=timezone.now() - timedelta(days=2)
        )
        self.assignment.deadline = timezone.now() - timedelta(days=1)
        self.assignment.save()
        second = self.submit(version=1)
        self.assertTrue(second.is_late)
        self.assertGreater(second.submitted_at, self.assignment.deadline)
        first.refresh_from_db()
        self.assertFalse(first.is_late)

    def test_maximum_and_deadline_are_snapshots(self):
        first = self.submit()
        self.assignment.max_points = 10
        self.assignment.deadline = timezone.now() - timedelta(days=1)
        self.assignment.save()
        self.review(first, grade=80)
        first.refresh_from_db()
        self.assertEqual(first.feedback.grade, 80)
        self.assertFalse(first.is_late)
        second = self.submit(version=1)
        self.assertEqual(second.max_points_snapshot, 10)
        self.assertTrue(second.is_late)

    def test_posted_grade_above_limit_is_never_saved(self):
        attempt = self.submit()
        for grade in (-1, 101, 1001):
            with self.subTest(grade=grade):
                response = self.review_post(attempt, grade=grade)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Feedback.objects.exists())

    def test_grade_field_has_server_validator_and_html_max(self):
        attempt = self.submit()
        form = ReviewForm(submission=attempt)
        self.assertEqual(form.fields["grade"].widget.attrs["max"], 100)
        self.assertIn(
            "MaxValueValidator", [type(v).__name__ for v in form.fields["grade"].validators]
        )

    def test_zero_and_maximum_grades_are_valid(self):
        attempt = self.submit()
        for grade in (0, 100):
            attempt.refresh_from_db()
            self.assertEqual(self.review_post(attempt, grade=grade).status_code, 302)
            self.assertEqual(Feedback.objects.get(submission=attempt).grade, grade)

    def test_empty_checked_grade_is_not_silently_zero(self):
        attempt = self.submit()
        response = self.review_post(attempt, grade="")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Feedback.objects.exists())

    def test_revision_can_have_no_grade(self):
        attempt = self.submit()
        self.assertEqual(
            self.review_post(attempt, decision="needs_revision", grade="").status_code, 302
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "needs_revision")
        self.assertIsNone(attempt.feedback.grade)

    def test_model_save_enforces_grade_limit_too(self):
        attempt = self.submit()
        with self.assertRaises(ValidationError):
            Feedback.objects.create(submission=attempt, teacher=self.teacher, grade=101)
        self.assertFalse(Feedback.objects.exists())

    def test_saving_comment_never_changes_status(self):
        attempt = self.submit()
        feedback = self.review(attempt, decision="needs_revision", grade=None)
        feedback.comment = "Typo corrected"
        feedback.save()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "needs_revision")

    def test_attempt_answer_cannot_be_overwritten_via_save(self):
        attempt = self.submit()
        attempt.text_answer = "Silently replace answer"
        with self.assertRaises(ValidationError):
            attempt.save()
        attempt.refresh_from_db()
        self.assertEqual(attempt.text_answer, "A real answer")

    def test_stale_student_form_conflicts_instead_of_overwriting(self):
        self.submit()
        response = self.student_client.post(
            self.url, {"expected_version": 0, "text_answer": "Stale"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Submission.objects.count(), 1)

    def test_double_service_submission_conflicts(self):
        self.submit()
        with self.assertRaises(ConflictError):
            self.submit()
        self.assertEqual(Submission.objects.count(), 1)

    def test_teacher_cannot_review_unseen_new_attempt(self):
        old = self.submit()
        self.teacher_client.get(f"/teacher/submissions/{old.pk}/")
        latest = self.submit(version=1)
        response = self.review_post(old)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Feedback.objects.exists())
        latest.refresh_from_db()
        self.assertEqual(latest.status, "submitted")

    def test_two_teachers_cannot_silently_overwrite_a_review(self):
        attempt = self.submit()
        self.review(attempt)
        response = self.review_post(attempt, grade=1, comment="Stale form")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Feedback.objects.get().grade, 80)

    def test_review_is_atomic_on_final_state_failure(self):
        attempt = self.submit()
        with patch.object(Submission, "save", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(RuntimeError):
                self.review(attempt, decision="needs_revision")
        self.assertFalse(Feedback.objects.exists())
        self.assertEqual(SubmissionEvent.objects.count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitted")

    def test_submission_and_event_are_atomic(self):
        with patch.object(
            SubmissionEvent.objects, "create", side_effect=RuntimeError("simulated event failure")
        ):
            with self.assertRaises(RuntimeError):
                self.submit()
        self.assertFalse(Submission.objects.exists())

    def test_review_changes_are_recorded(self):
        attempt = self.submit()
        self.review(attempt, grade=30, comment="First decision")
        attempt.refresh_from_db()
        self.review(attempt, grade=40, comment="Correction")
        self.assertEqual(attempt.events.filter(action="reviewed").count(), 2)
        self.assertEqual(
            list(attempt.events.filter(action="reviewed").values_list("grade", flat=True)), [40, 30]
        )

    def test_service_requires_teacher(self):
        attempt = self.submit()
        with self.assertRaises(PermissionDenied):
            self.review(attempt, teacher=self.student)

    @override_settings(LMS_SUBMISSIONS_PER_HOUR=1)
    def test_submission_rate_limit(self):
        self.submit()
        with self.assertRaises(RateLimitError):
            self.submit(version=1)
        response = self.student_client.post(
            self.url, {"text_answer": "Another", "expected_version": 1}
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def test_catalog_deletion_cannot_cascade_into_learning_records(self):
        self.submit()
        with self.assertRaises(ProtectedError):
            self.assignment.delete()
