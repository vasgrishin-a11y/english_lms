from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from lms.models import Assignment, Block, Profile, Submission, Topic
from lms.services import ConflictError, review_submission, submit_assignment


@skipUnless(
    connection.vendor == "postgresql", "Real row-lock concurrency requires PostgreSQL (CI job)."
)
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.student = User.objects.create_user("parallel_student")
        self.teacher = User.objects.create_user("parallel_teacher")
        self.teacher.profile.role = Profile.Role.TEACHER
        self.teacher.profile.save()
        block = Block.objects.create(name="Block", slug="block")
        topic = Topic.objects.create(block=block, title="Topic", slug="topic")
        self.assignment = Assignment.objects.create(topic=topic, title="Task", description="Task")

    def parallel(self, operations):
        barrier = Barrier(len(operations), timeout=10)

        def run(operation):
            close_old_connections()
            try:
                barrier.wait()
                operation()
                return "saved"
            except ConflictError:
                return "conflict"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            return list(pool.map(run, operations))

    def submit(self):
        return submit_assignment(
            student=self.student,
            assignment_id=self.assignment.pk,
            expected_version=0,
            text_answer="Answer",
        )

    def test_parallel_first_posts_are_serialized(self):
        results = self.parallel([self.submit, self.submit])
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(Submission.objects.count(), 1)

    def test_parallel_reviews_are_not_lost(self):
        attempt = self.submit()

        def review():
            review_submission(
                teacher=get_user_model().objects.get(pk=self.teacher.pk),
                submission_id=attempt.pk,
                expected_version=1,
                expected_review_revision=0,
                grade=50,
                comment="Review",
                decision="checked",
            )

        self.assertCountEqual(self.parallel([review, review]), ["saved", "conflict"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.review_revision, 1)
