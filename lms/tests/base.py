import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from lms.models import Assignment, Block, Profile, Topic
from lms.services import review_submission, submit_assignment


class LMSCase(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lms-regression-")
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name)
        media.enable()
        self.addCleanup(media.disable)
        User = get_user_model()
        self.password = "TemporaryTestingPassword!842"
        self.student = User.objects.create_user("student", password=self.password)
        self.other = User.objects.create_user("other", password=self.password)
        self.teacher = User.objects.create_user("teacher", password=self.password)
        self.teacher.profile.role = Profile.Role.TEACHER
        self.teacher.profile.save()
        self.admin = User.objects.create_superuser("owner", "owner@example.invalid", self.password)
        self.block = Block.objects.create(name="English", slug="english")
        self.topic = Topic.objects.create(block=self.block, title="Grammar", slug="grammar")
        self.assignment = Assignment.objects.create(
            topic=self.topic,
            title="Past tense",
            description="Write an answer.",
            max_points=100,
            deadline=timezone.now() + timedelta(days=1),
        )
        self.student_client = self.client_for(self.student)
        self.teacher_client = self.client_for(self.teacher)
        self.admin_client = self.client_for(self.admin)

    def client_for(self, user, **kwargs):
        client = Client(**kwargs)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    @property
    def url(self):
        return f"/assignments/{self.assignment.pk}/"

    def submit(self, version=0, **kwargs):
        values = {
            "student": self.student,
            "assignment_id": self.assignment.pk,
            "expected_version": version,
            "text_answer": "A real answer",
        }
        values.update(kwargs)
        return submit_assignment(**values)

    def review(self, attempt, **kwargs):
        values = {
            "teacher": self.teacher,
            "submission_id": attempt.pk,
            "expected_version": attempt.version,
            "expected_review_revision": attempt.review_revision,
            "grade": 80,
            "comment": "Good work",
            "decision": "checked",
        }
        values.update(kwargs)
        return review_submission(**values)

    def review_post(self, attempt, **kwargs):
        data = {
            "expected_version": attempt.version,
            "expected_review_revision": attempt.review_revision,
            "grade": 80,
            "comment": "Review",
            "decision": "checked",
        }
        data.update(kwargs)
        return self.teacher_client.post(f"/teacher/submissions/{attempt.pk}/", data)
