from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from lms.models import Assignment, Block, Submission, Topic

from .base import LMSCase


class QueryAndMarkupTests(LMSCase):
    def test_catalog_query_budget_is_independent_of_topic_count(self):
        for number in range(30):
            block = Block.objects.create(name=f"Block {number}", slug=f"block-{number}")
            topic = Topic.objects.create(block=block, title=f"Topic {number}", slug="topic")
            Assignment.objects.create(topic=topic, title=f"Task {number}", description="Task")
        with CaptureQueriesContext(connection) as queries:
            response = self.student_client.get("/assignments/")
        self.assertEqual(response.status_code, 200)
        # 7 запросов: сессия, пользователь, роль, счётчик, страница, сводка
        # и квизлеты тем страницы (один запрос на все темы — бюджет по-прежнему
        # не зависит от размера курса).
        self.assertLessEqual(len(queries), 7)
        self.assertEqual(len(response.context["page_obj"]), 25)

    def test_teacher_queue_only_contains_latest_attempts(self):
        old = self.submit()
        new = self.submit(version=1)
        response = self.teacher_client.get("/teacher/submissions/")
        self.assertEqual([attempt.pk for attempt in response.context["submissions"]], [new.pk])
        self.assertNotEqual(old.pk, new.pk)

    def test_checked_archive_and_revision_filter(self):
        attempt = self.submit()
        self.review(attempt)
        self.assertEqual(
            len(self.teacher_client.get("/teacher/submissions/").context["submissions"]), 0
        )
        self.assertEqual(
            len(
                self.teacher_client.get("/teacher/submissions/?status=checked").context[
                    "submissions"
                ]
            ),
            1,
        )
        attempt.refresh_from_db()
        self.review(attempt, grade=None, decision="needs_revision")
        self.assertEqual(
            len(
                self.teacher_client.get("/teacher/submissions/?status=revision").context[
                    "submissions"
                ]
            ),
            1,
        )

    @override_settings(LMS_PAGE_SIZE=5)
    def test_queue_is_paginated_and_keeps_filters(self):
        tasks = Assignment.objects.bulk_create(
            [
                Assignment(topic=self.topic, title=f"Search task {i}", description="Task")
                for i in range(12)
            ]
        )
        Submission.objects.bulk_create(
            [
                Submission(
                    student=self.student, assignment=task, text_answer="Answer", status="submitted"
                )
                for task in tasks
            ]
        )
        with CaptureQueriesContext(connection) as queries:
            response = self.teacher_client.get("/teacher/submissions/?q=Search&status=all")
        self.assertLessEqual(len(queries), 6)
        self.assertEqual(len(response.context["submissions"]), 5)
        self.assertContains(response, "page=2")
        self.assertContains(response, "status=all")

    @override_settings(LMS_PAGE_SIZE=2)
    def test_attempt_history_is_paginated(self):
        for version in range(4):
            self.submit(version=version)
        response = self.student_client.get(self.url)
        self.assertEqual(len(response.context["page_obj"]), 2)
        self.assertEqual(response.context["page_obj"].paginator.count, 4)

    def test_help_text_has_aria_target_and_hidden_tokens_are_not_labels(self):
        attempt = self.submit()
        response = self.teacher_client.get(f"/teacher/submissions/{attempt.pk}/")
        self.assertContains(response, 'id="id_grade_helptext"')
        self.assertContains(response, 'aria-describedby="id_grade_helptext"')
        self.assertNotContains(response, '<label for="id_expected_version">')

    def test_catalog_does_not_show_old_grade_as_new_attempt_grade(self):
        old = self.submit()
        self.review(old)
        self.submit(version=1)
        response = self.student_client.get("/assignments/")
        assignment = list(response.context["page_obj"])[0]
        self.assertEqual(assignment.latest_status, "submitted")
        self.assertIsNone(assignment.latest_grade)

    def test_login_has_h1_and_table_has_accessible_scroll_region(self):
        self.assertContains(self.client.get("/accounts/login/"), "<h1>Вход в систему</h1>")
        response = self.teacher_client.get("/teacher/submissions/")
        self.assertContains(response, 'class="table-scroll" role="region"')
        self.assertContains(response, 'scope="col"')
        self.assertContains(response, "<caption>")
