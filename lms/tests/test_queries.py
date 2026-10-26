from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from lms.models import Assignment, Block, Group, Submission, Topic

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
        # 8 запросов: сессия, пользователь, роль, членство в группах для точного
        # расчёта исключений аудитории, счётчик, страница, сводка и карточки заданий.
        # Бюджет по-прежнему не зависит от размера курса.
        self.assertLessEqual(len(queries), 8)
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
            response = self.teacher_client.get(
                f"/teacher/submissions/?status=pending&sort=assignment&block={self.block.pk}"
            )
        # Три запроса сверх страницы — варианты для селектов «Ученик», «Класс»,
        # «Задание»; бюджет по-прежнему не зависит от размера очереди.
        self.assertLessEqual(len(queries), 9)
        self.assertEqual(len(response.context["submissions"]), 5)
        self.assertContains(response, "page=2")
        self.assertContains(response, "sort=assignment")

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


class TeacherStudentsPerformanceTests(LMSCase):
    """Регрессия стоимости пути «Ученики → карточка → задание»."""

    def test_students_list_is_paginated_and_has_fixed_query_budget(self):
        User = get_user_model()
        group = Group.objects.create(name="Perf group", slug="perf-group")
        for number in range(18):
            student = User.objects.create_user(f"perf-student-{number}")
            group.students.add(student)
        # Первый запрос заполняет минутный кэш счётчика очереди в сессии.
        self.teacher_client.get("/teacher/students/")
        with override_settings(LMS_PAGE_SIZE=5):
            with CaptureQueriesContext(connection) as queries:
                response = self.teacher_client.get("/teacher/students/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.context["page_obj"].object_list), 5)
            self.assertEqual(response.context["page_obj"].paginator.count, 20)
            # Сессия/пользователь/роль, страница, prefetch групп, статистика,
            # количество заданий. Число запросов не растёт вместе со списком.
            self.assertLessEqual(len(queries), 8)

    def test_student_detail_does_not_load_all_answers(self):
        for number in range(25):
            assignment = Assignment.objects.create(
                topic=self.topic,
                title=f"Perf task {number}",
                description="x",
                order=number + 1,
            )
            Submission.objects.create(
                student=self.student,
                assignment=assignment,
                version=1,
                text_answer="A real answer " * 30,
                max_points_snapshot=assignment.max_points,
                status=Submission.Status.CHECKED,
            )
        self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        with CaptureQueriesContext(connection) as queries:
            response = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "A real answer")
        self.assertContains(response, "Показать ответ и разбор")
        self.assertNotContains(response, 'class="review-items assignment-items"')
        self.assertNotContains(response, "Последние 30 попыток")
        self.assertNotContains(response, "Навыки</h2>")
        # Остались дерево курса, состояния, счётчик карточек, пароль и сессия;
        # второстепенные блоки и их SQL больше не загружаются.
        self.assertLessEqual(len(queries), 11)
        # 25 заданий: компактное дерево без ответов, в gzip укладывается в
        # десятки килобайт (отдельная проверка ниже).
        self.assertLess(len(response.content), 130_000)

    def test_dynamic_html_is_gzipped(self):
        response = self.teacher_client.get(
            f"/teacher/students/{self.student.pk}/", HTTP_ACCEPT_ENCODING="gzip"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", response["Vary"])
        self.assertLess(len(response.content), 20_000)
        self.assertLessEqual(settings.LMS_PAGE_SIZE, 25)


class CurriculumPathBudgetTests(LMSCase):
    """Регрессия стоимости пути «Классы → тема → задание» и счётчика в навигации."""

    def setUp(self):
        super().setUp()
        # Прогрев: первый запрос в процессе может стоить одного лишнего
        # SELECT (сессия, ContentType) — сравниваем прогретые состояния.
        self.teacher_client.get("/teacher/curriculum/")

    def add_tasks(self, count, **kwargs):
        return Assignment.objects.bulk_create(
            [
                Assignment(
                    topic=self.topic, title=f"Board task {number}", description="Task", **kwargs
                )
                for number in range(count)
            ]
        )

    def query_count(self, client, url):
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return len(queries), response

    def test_topic_board_loads_quick_editors_on_demand(self):
        self.add_tasks(11)
        url = f"/teacher/curriculum/topics/{self.topic.pk}/board/"
        lazy_queries, lazy = self.query_count(self.teacher_client, url)
        html = lazy.content.decode()
        # Форма быстрых правок едет только открытому редактору: двенадцать
        # инлайновых форм стоили больше 150 КБ разметки на теме.
        self.assertNotIn('data-dropzone="1"', html)
        self.assertIn("/quick-editor/", html)
        self.assertIn("/quick-editor/", html)
        self.assertLess(len(lazy.content) // 12, 9000)
        opened_queries, opened = self.query_count(
            self.teacher_client, f"{url}?edit={self.assignment.pk}"
        )
        self.assertIn('data-dropzone="1"', opened.content.decode())
        # Открытый редактор добавляет пару запросов на выбранные значения полей
        # и около 12 КБ — вместо формы на каждое задание темы.
        self.assertLessEqual(opened_queries - lazy_queries, 4)
        self.assertLess(len(opened.content) - len(lazy.content), 40_000)

        fragment_queries, fragment = self.query_count(
            self.teacher_client,
            f"/teacher/curriculum/assignments/{self.assignment.pk}/quick-editor/",
        )
        self.assertIn('name="new_attachments"', fragment.content.decode())
        self.assertLess(len(fragment.content), 40_000)
        self.assertLessEqual(fragment_queries, 12)

    def test_map_query_budget_is_independent_of_assignment_count(self):
        """Карта курса не считает аудиторию каждого задания: бюджет плоский."""
        before, small = self.query_count(self.teacher_client, "/teacher/curriculum/")
        self.add_tasks(20)
        after, big = self.query_count(self.teacher_client, "/teacher/curriculum/")
        self.assertIn("21 задание", big.content.decode())
        self.assertEqual(before, after)
        # Бейдж «кому доступно» у строки задания на карте не рисуется, поэтому
        # и правила самих заданий не загружаются.
        self.assertNotIn("Board task 0", big.content.decode())
        self.assertLess(len(big.content) - len(small.content), 30_000)

    def test_htmx_fragments_never_compute_the_queue_counter(self):
        """HTMX-фрагмент не перечитывает очередь и не пишет сессию."""
        from django.core.cache import cache

        from lms import curriculum
        from lms.curriculum import QUEUE_COUNTS_CACHE_KEY

        Submission.objects.create(
            student=self.student,
            assignment=self.assignment,
            text_answer="A",
            status=Submission.Status.SUBMITTED,
        )
        preview_url = (
            f"/teacher/students/{self.student.pk}/assignments/{self.assignment.pk}/preview/"
        )
        progress_url = (
            f"/teacher/students/{self.student.pk}/assignments/{self.assignment.pk}/progress/"
        )
        calls = []
        original = curriculum.queue_counts

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        self.teacher_client.get("/teacher/groups/")  # прогрев кэша счётчика
        cache.get(QUEUE_COUNTS_CACHE_KEY)
        curriculum.queue_counts = counting
        try:
            htmx = self.client_for(self.teacher, headers={"HX-Request": "true"})
            for url in (preview_url, progress_url):
                response = htmx.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertNotIn("nav-badge-count", response.content.decode())
                self.assertIsNone(response.cookies.get("sessionid"))
            # Обычная страница преподавателя берёт счётчик из кэша.
            self.assertEqual(self.teacher_client.get("/teacher/groups/").status_code, 200)
            self.assertEqual(calls, [])
            cache.delete(QUEUE_COUNTS_CACHE_KEY)
            self.teacher_client.get("/teacher/groups/")
            self.assertEqual(calls, [1])
        finally:
            curriculum.queue_counts = original

    @override_settings(LMS_PAGE_SIZE=5)
    def test_gradebook_rows_are_paginated_but_export_stays_full(self):
        """Журнал отдаёт страницу учеников, а не всю матрицу курса."""
        from lms import curriculum

        for number in range(12):
            get_user_model().objects.create_user(f"grade-student-{number}")
        total = curriculum.gradebook_students().count()
        response = self.teacher_client.get("/teacher/analytics/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 5)
        self.assertEqual(response.context["students_total"], total)
        self.assertContains(response, "page=2")
        second = self.teacher_client.get("/teacher/analytics/?page=2")
        self.assertEqual(len(second.context["rows"]), 5)
        self.assertEqual(
            {row["student"].pk for row in response.context["rows"]}.isdisjoint(
                {row["student"].pk for row in second.context["rows"]}
            ),
            True,
        )
        # Выгрузка XLSX строится по всем ученикам: пагинация касается только HTML.
        self.assertEqual(len(curriculum.gradebook()["rows"]), total)
        # Компактные клетки: у пустой клетки нет ни span, ни title — только класс.
        restricted = Assignment.objects.create(
            topic=self.topic, title="Only for the group", description="x"
        )
        group = Group.objects.create(name="Gradebook group", slug="gradebook-group")
        group.students.add(self.student)
        restricted.groups.add(group)
        marked = self.teacher_client.get("/teacher/analytics/")
        html = marked.content.decode()
        self.assertIn('class="grade-col is-not-assigned"', html)
        self.assertNotIn("Задание не назначено этому ученику", html)
        self.assertNotIn("Рейтинг класса", html)

    def test_queue_counter_is_cached_for_a_minute(self):
        from django.core.cache import cache

        from lms.curriculum import QUEUE_COUNTS_CACHE_KEY, cached_queue_counts

        cache.delete(QUEUE_COUNTS_CACHE_KEY)
        cold, _ = self.query_count(self.teacher_client, "/teacher/")
        with CaptureQueriesContext(connection) as queries:
            cached_queue_counts()
        warm_queries = len(queries)
        self.assertEqual(warm_queries, 0)
        self.assertGreater(cold, warm_queries)
