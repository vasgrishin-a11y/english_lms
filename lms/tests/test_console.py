"""Консоль преподавателя: проверка, курс, тесты, карточки, ученики, аналитика.

Консоль — единственный рабочий интерфейс преподавателя, поэтому здесь pokryты
и счастливые пути, и защитные сценарии: конфликт версий, защищённые удаления,
чужие шаблоны комментариев, отбрасывание мусорных параметров фильтра.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from lms.models import (
    AnswerDraft,
    Assignment,
    Block,
    CardReview,
    Choice,
    CommentSnippet,
    Flashcard,
    Group,
    Profile,
    Question,
    Skill,
    Submission,
    Topic,
)

from .base import LMSCase

User = get_user_model()


class ConsoleAccessTests(LMSCase):
    def test_student_is_redirected_out_of_console(self):
        for url in ["/teacher/", "/teacher/review/", "/teacher/curriculum/", "/teacher/analytics/"]:
            with self.subTest(url=url):
                response = self.student_client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/assignments/"))

    def test_student_cannot_write_to_console(self):
        response = self.student_client.post(
            "/teacher/curriculum/blocks/new/", {"name": "Hack", "order": 1}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Block.objects.filter(name="Hack").exists())

    def test_anonymous_needs_login(self):
        self.assertEqual(self.client.get("/teacher/review/").status_code, 302)

    def test_console_home_shows_queue_drafts_and_overview(self):
        draft = Assignment.objects.create(
            topic=self.topic,
            title="Draft task",
            description="Draft",
            status=Assignment.Publication.DRAFT,
        )
        attempt = self.submit()
        response = self.teacher_client.get("/teacher/")
        self.assertEqual(response.status_code, 200)
        overview = response.context["overview"]
        self.assertEqual(overview["queue"]["waiting"], 1)
        self.assertEqual(overview["curriculum"]["drafts"], 1)
        self.assertEqual(overview["students"], 1)
        self.assertIn(draft, response.context["drafts"])
        self.assertIn(attempt, response.context["waiting"])
        self.assertContains(response, "Draft task")


class ArchiveAndStudentViewTests(LMSCase):
    def test_archive_moves_a_whole_block_without_destroying_history(self):
        response = self.teacher_client.post(f"/teacher/archive/block/{self.block.pk}/")
        self.assertRedirects(response, "/teacher/curriculum/", fetch_redirect_response=False)
        self.block.refresh_from_db()
        self.topic.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertFalse(self.block.is_active)
        self.assertFalse(self.topic.is_active)
        self.assertFalse(self.assignment.is_active)
        self.assertNotContains(
            self.teacher_client.get("/teacher/curriculum/"), self.assignment.title
        )
        self.assertEqual(self.teacher_client.get("/teacher/archive/").status_code, 200)

        response = self.teacher_client.post(
            f"/teacher/archive/block/{self.block.pk}/",
            {"action": "restore", "restore_tree": "1"},
        )
        self.assertRedirects(response, "/teacher/archive/", fetch_redirect_response=False)
        self.block.refresh_from_db()
        self.topic.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertTrue(self.block.is_active)
        self.assertTrue(self.topic.is_active)
        self.assertTrue(self.assignment.is_active)

    def test_teacher_can_view_student_and_return_to_console(self):
        response = self.teacher_client.post(f"/teacher/students/{self.student.pk}/view-as/")
        self.assertRedirects(response, "/my/", fetch_redirect_response=False)
        self.assertEqual(response.wsgi_request.user.pk, self.student.pk)
        student_view = self.teacher_client.get("/my/")
        self.assertEqual(student_view.status_code, 200)
        self.assertTrue(student_view.context["is_impersonating"])
        response = self.teacher_client.post("/teacher/return/")
        self.assertRedirects(
            response,
            f"/teacher/students/{self.student.pk}/",
            fetch_redirect_response=False,
        )
        self.assertEqual(response.wsgi_request.user.pk, self.teacher.pk)
        self.assertEqual(self.teacher_client.get("/teacher/").status_code, 200)


class QueueFilterTests(LMSCase):
    def setUp(self):
        super().setUp()
        # В очереди только последние попытки, поэтому две работы — два задания.
        self.second_task = Assignment.objects.create(
            topic=self.topic, title="Second task", description="Task", order=2
        )
        self.old = self.submit()
        # Попытка неизменяема на уровне модели, поэтому дату сдвигаем обновлением.
        Submission.objects.filter(pk=self.old.pk).update(
            submitted_at=timezone.now() - timedelta(days=2)
        )
        self.new = self.submit(assignment_id=self.second_task.pk)

    def test_fifo_order_is_default(self):
        response = self.teacher_client.get("/teacher/review/")
        self.assertEqual(
            [item.pk for item in response.context["submissions"]], [self.old.pk, self.new.pk]
        )
        self.assertEqual(response.context["order"], "fifo")

    def test_newest_first_order(self):
        response = self.teacher_client.get("/teacher/review/?order=new")
        self.assertEqual(
            [item.pk for item in response.context["submissions"]], [self.new.pk, self.old.pk]
        )

    def test_unknown_order_and_status_fall_back(self):
        response = self.teacher_client.get("/teacher/review/?order=chaos&status=chaos")
        self.assertEqual(response.context["order"], "fifo")
        self.assertEqual(response.context["queue"], "pending")

    def test_status_filters(self):
        self.review(self.old)
        self.review(self.new, grade=None, decision="needs_revision")
        self.assertEqual(Submission.objects.latest_attempts().count(), 2)
        cases = {
            "pending": [],
            "checked": [self.old.pk],
            "revision": [self.new.pk],
            "all": [self.old.pk, self.new.pk],
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                response = self.teacher_client.get(f"/teacher/review/?status={status}")
                self.assertEqual([item.pk for item in response.context["submissions"]], expected)

    def test_search_by_student_assignment_topic_and_block(self):
        for query, expected in [
            ("student", 2),
            ("nobody-here", 0),
            ("Past tense", 1),
            ("Second task", 1),
            ("Grammar", 2),
            ("English", 2),
        ]:
            with self.subTest(query=query):
                response = self.teacher_client.get(f"/teacher/review/?status=all&q={query}")
                self.assertEqual(len(response.context["submissions"]), expected)

    def test_filters_by_student_and_assignment(self):
        response = self.teacher_client.get(f"/teacher/review/?status=all&student={self.student.pk}")
        self.assertEqual(len(response.context["submissions"]), 2)
        response = self.teacher_client.get(f"/teacher/review/?status=all&student={self.other.pk}")
        self.assertEqual(len(response.context["submissions"]), 0)
        response = self.teacher_client.get(
            f"/teacher/review/?status=all&assignment={self.assignment.pk}"
        )
        self.assertEqual(len(response.context["submissions"]), 1)
        # мусорные значения игнорируются, а не роняют страницу
        response = self.teacher_client.get("/teacher/review/?status=all&student=abc&assignment=x")
        self.assertEqual(len(response.context["submissions"]), 2)

    def test_counts_are_shared_with_navigation(self):
        response = self.teacher_client.get("/teacher/review/?status=all")
        counts = response.context["counts"]
        self.assertEqual(counts["waiting"], 2)
        self.assertEqual(counts["total"], 2)
        self.assertContains(response, 'class="table-scroll" role="region"')
        self.assertContains(response, "<caption>")


class ReviewDetailTests(LMSCase):
    def test_review_saves_decision_and_comment(self):
        attempt = self.submit()
        response = self.review_post(attempt, grade=91, comment="Отличная работа", next="")
        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.feedback.grade, 91)
        self.assertEqual(attempt.feedback.comment, "Отличная работа")
        self.assertEqual(attempt.status, Submission.Status.CHECKED)

    def test_save_and_go_next_moves_to_neighbour(self):
        second_task = Assignment.objects.create(
            topic=self.topic, title="Second", description="Task", order=2
        )
        first = self.submit()
        second = self.submit(assignment_id=second_task.pk)
        response = self.teacher_client.post(
            f"/teacher/review/{first.pk}/",
            {
                "expected_version": first.version,
                "expected_review_revision": first.review_revision,
                "grade": 80,
                "comment": "Review",
                "decision": "checked",
                "next": "1",
            },
        )
        self.assertRedirects(
            response, f"/teacher/review/{second.pk}/", fetch_redirect_response=False
        )

    def test_save_and_go_next_stays_when_queue_is_empty(self):
        attempt = self.submit()
        response = self.review_post(attempt, next="1")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(f"/teacher/review/{attempt.pk}/"))

    def test_snippet_usage_is_counted(self):
        snippet = CommentSnippet.objects.create(
            title="Окончание -s", code="-s", text="Проверьте окончание -s.", author=self.teacher
        )
        attempt = self.submit()
        self.review_post(attempt, snippet_used=str(snippet.pk))
        snippet.refresh_from_db()
        self.assertEqual(snippet.usage_count, 1)
        # мусорное значение не увеличивает счётчик и не роняет проверку
        self.review_post(attempt, grade=70, snippet_used="abc")
        snippet.refresh_from_db()
        self.assertEqual(snippet.usage_count, 1)

    def test_conflict_returns_409_and_keeps_data(self):
        attempt = self.submit()
        response = self.review_post(attempt, expected_review_revision=99)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Проверка не сохранена", status_code=409)
        self.assertFalse(hasattr(attempt, "feedback") and attempt.feedback is not None)

    def test_checked_without_grade_is_rejected(self):
        attempt = self.submit()
        response = self.review_post(attempt, grade="", decision="checked")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "укажите балл")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Submission.Status.SUBMITTED)

    def test_grade_above_snapshot_maximum_is_rejected(self):
        attempt = self.submit()
        response = self.review_post(attempt, grade=attempt.max_points_snapshot + 1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors.get("grade"))

    def test_historical_attempt_is_read_only(self):
        old = self.submit()
        self.review(old)
        new = self.submit(version=1)
        response = self.teacher_client.get(f"/teacher/review/{old.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_latest"])
        self.assertEqual(response.context["latest"].pk, new.pk)
        self.assertNotContains(response, "Сохранить проверку")
        self.assertContains(response, "историческая попытка")
        # запись закрыта: форма не отображается, решение не меняется
        post = self.review_post(old, grade=10)
        self.assertEqual(post.status_code, 409)
        self.assertContains(post, "Проверка не сохранена", status_code=409)
        old.refresh_from_db()
        self.assertEqual(old.feedback.grade, 80)

    def test_quiz_review_shows_automatic_breakdown(self):
        quiz = self.make_quiz()
        from lms.services import submit_quiz

        attempt = submit_quiz(
            student=self.student,
            assignment_id=quiz.pk,
            expected_version=0,
            answers={
                str(question.pk): str(question.choices.get(is_correct=True).pk)
                for question in [quiz.questions.first()]
            },
        )
        response = self.teacher_client.get(f"/teacher/review/{attempt.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["quiz"])
        self.assertContains(response, "Автопроверка теста")
        self.assertContains(response, "Правильный ответ")

    def test_legacy_alias_url_still_works(self):
        attempt = self.submit()
        self.assertEqual(
            self.teacher_client.get(f"/teacher/submissions/{attempt.pk}/").status_code, 200
        )
        self.assertEqual(self.teacher_client.get("/teacher/submissions/").status_code, 200)

    def make_quiz(self):
        quiz = Assignment.objects.create(
            topic=self.topic,
            title="Quiz",
            description="Choose",
            assignment_type=Assignment.Type.QUIZ,
            max_points=2,
        )
        question = Question.objects.create(
            assignment=quiz, kind=Question.Kind.MCQ, text="One?", points=2, order=1
        )
        Choice.objects.create(question=question, text="yes", is_correct=True, order=1)
        Choice.objects.create(question=question, text="no", order=2)
        return quiz


class CurriculumTreeTests(LMSCase):
    def test_tree_shows_blocks_topics_assignments_and_counters(self):
        draft = Assignment.objects.create(
            topic=self.topic,
            title="Hidden draft",
            description="x",
            status=Assignment.Publication.DRAFT,
        )
        self.submit()
        response = self.teacher_client.get("/teacher/curriculum/")
        self.assertEqual(response.status_code, 200)
        blocks = response.context["blocks_data"]
        self.assertEqual(blocks[0]["block"].pk, self.block.pk)
        entry = blocks[0]["topics"][0]["assignments"][0]
        self.assertEqual(entry["stats"]["waiting"], 1)
        self.assertEqual(response.context["totals"]["drafts"], 1)
        self.assertContains(response, draft.title)
        self.assertContains(response, self.assignment.title)

    def test_search_filters_tree(self):
        response = self.teacher_client.get("/teacher/curriculum/?q=Past tense")
        self.assertEqual(len(response.context["blocks_data"]), 1)
        response = self.teacher_client.get("/teacher/curriculum/?q=нет-такого")
        self.assertEqual(len(response.context["blocks_data"]), 0)

    def test_block_crud(self):
        response = self.teacher_client.post(
            "/teacher/curriculum/blocks/new/",
            {"name": "Speaking B2", "cefr_level": "B2", "description": "", "order": 5},
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        block = Block.objects.get(name="Speaking B2")
        self.assertTrue(block.slug)
        self.assertEqual(block.cefr_level, "B2")

        response = self.teacher_client.post(
            f"/teacher/curriculum/blocks/{block.pk}/",
            {"name": "Speaking B2+", "cefr_level": "C1", "description": "", "order": 6},
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        block.refresh_from_db()
        self.assertEqual(block.name, "Speaking B2+")
        self.assertEqual(block.cefr_level, "C1")

        response = self.teacher_client.post(f"/teacher/curriculum/blocks/{block.pk}/delete/")
        self.assertRedirects(response, "/teacher/curriculum/")
        self.assertFalse(Block.objects.filter(pk=block.pk).exists())

    def test_duplicate_slug_gets_suffix(self):
        self.teacher_client.post("/teacher/curriculum/blocks/new/", {"name": "English", "order": 2})
        self.assertEqual(Block.objects.filter(slug="english").count(), 1)
        second = Block.objects.exclude(pk=self.block.pk).first()
        self.assertNotEqual(second.slug, "english")
        self.assertTrue(second.slug.startswith("english"))

    def test_protected_block_delete_reports_reason(self):
        response = self.teacher_client.post(f"/teacher/curriculum/blocks/{self.block.pk}/delete/")
        self.assertRedirects(response, "/teacher/curriculum/")
        self.assertTrue(Block.objects.filter(pk=self.block.pk).exists())
        messages = list(response.wsgi_request._messages)
        self.assertIn("нельзя удалить", str(messages[0]))

    def test_block_move_reorders_and_reports_edges(self):
        second = Block.objects.create(name="Second", slug="second", order=0)
        response = self.teacher_client.post(
            f"/teacher/curriculum/blocks/{second.pk}/move/", {"direction": "up"}
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        second.refresh_from_db()
        self.block.refresh_from_db()
        self.assertLess(second.order, self.block.order)

        response = self.teacher_client.post(
            f"/teacher/curriculum/blocks/{second.pk}/move/", {"direction": "up"}
        )
        self.assertContains(self.teacher_client.get("/teacher/curriculum/"), second.name)
        self.assertEqual(response.status_code, 302)
        # неизвестное направление ничего не меняет
        before = second.order
        self.teacher_client.post(
            f"/teacher/curriculum/blocks/{second.pk}/move/", {"direction": "sideways"}
        )
        second.refresh_from_db()
        self.assertEqual(second.order, before)

    def test_topic_crud_and_move(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/new/?block={self.block.pk}",
            {"block": self.block.pk, "title": "Vocabulary", "description": "", "order": 2},
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        topic = Topic.objects.get(title="Vocabulary")
        self.assertEqual(topic.block, self.block)
        self.assertTrue(topic.slug)

        third = Topic.objects.create(block=self.block, title="Third", slug="third", order=3)
        self.teacher_client.post(
            f"/teacher/curriculum/topics/{third.pk}/move/", {"direction": "up"}
        )
        third.refresh_from_db()
        topic.refresh_from_db()
        self.assertLess(third.order, topic.order)

        response = self.teacher_client.post(f"/teacher/curriculum/topics/{third.pk}/delete/")
        self.assertRedirects(response, "/teacher/curriculum/")
        self.assertFalse(Topic.objects.filter(pk=third.pk).exists())

        response = self.teacher_client.post(f"/teacher/curriculum/topics/{self.topic.pk}/delete/")
        self.assertRedirects(response, "/teacher/curriculum/")
        self.assertTrue(Topic.objects.filter(pk=self.topic.pk).exists())
        self.assertTrue(Assignment.objects.filter(pk=self.assignment.pk).exists())

    def test_assignment_create_edit_and_publish(self):
        deadline = timezone.localtime(timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.teacher_client.post(
            "/teacher/curriculum/assignments/new/",
            {
                "topic": self.topic.pk,
                "title": "Essay",
                "description": "Write 200 words.",
                "assignment_type": "text",
                "max_points": 50,
                "deadline": deadline,
                "publish_at": "",
                "status": Assignment.Publication.DRAFT,
                "order": 3,
                "is_active": "on",
            },
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        essay = Assignment.objects.get(title="Essay")
        self.assertEqual(essay.status, Assignment.Publication.DRAFT)
        self.assertEqual(timezone.localtime(essay.deadline).strftime("%Y-%m-%dT%H:%M"), deadline)
        self.assertFalse(essay.is_visible)

        response = self.teacher_client.post(
            f"/teacher/curriculum/assignments/{essay.pk}/publish/", {"publish": "1"}
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        essay.refresh_from_db()
        self.assertTrue(essay.is_visible)

        response = self.teacher_client.post(
            f"/teacher/curriculum/assignments/{essay.pk}/publish/", {"publish": "0"}
        )
        essay.refresh_from_db()
        self.assertEqual(essay.status, Assignment.Publication.DRAFT)
        self.assertEqual(response.status_code, 302)

    def test_publish_honours_next_target(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/assignments/{self.assignment.pk}/publish/",
            {"publish": "1", "next": "teacher_home"},
        )
        self.assertRedirects(response, "/teacher/", fetch_redirect_response=False)

    def test_publish_clears_scheduled_date(self):
        self.assignment.publish_at = timezone.now() + timedelta(days=2)
        self.assignment.status = Assignment.Publication.DRAFT
        self.assignment.save()
        self.teacher_client.post(
            f"/teacher/curriculum/assignments/{self.assignment.pk}/publish/", {"publish": "1"}
        )
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.publish_at)
        self.assertTrue(self.assignment.is_visible)

    def test_save_and_continue_to_questions(self):
        response = self.teacher_client.post(
            "/teacher/curriculum/assignments/new/",
            {
                "topic": self.topic.pk,
                "title": "Quiz draft",
                "description": "Auto-check",
                "assignment_type": Assignment.Type.QUIZ,
                "max_points": 0,
                "status": Assignment.Publication.DRAFT,
                "order": 4,
                "_save_questions": "1",
            },
        )
        quiz = Assignment.objects.get(title="Quiz draft")
        self.assertRedirects(
            response,
            f"/teacher/curriculum/assignments/{quiz.pk}/questions/",
            fetch_redirect_response=False,
        )

    def test_duplicate_copies_questions_choices_and_skills(self):
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()
        skill = Skill.objects.create(name="Grammar", slug="grammar")
        self.assignment.skills.set([skill])
        question = Question.objects.create(
            assignment=self.assignment, kind=Question.Kind.MCQ, text="One?", points=3, order=1
        )
        Choice.objects.create(question=question, text="yes", is_correct=True, order=1)
        Choice.objects.create(question=question, text="no", order=2)

        response = self.teacher_client.post(
            f"/teacher/curriculum/assignments/{self.assignment.pk}/duplicate/"
        )
        copy = Assignment.objects.get(title="Past tense (копия)")
        self.assertRedirects(
            response,
            f"/teacher/curriculum/assignments/{copy.pk}/",
            fetch_redirect_response=False,
        )
        self.assertEqual(copy.status, Assignment.Publication.DRAFT)
        self.assertEqual(copy.order, self.assignment.order + 1)
        self.assertEqual(list(copy.skills.all()), [skill])
        self.assertEqual(copy.questions.count(), 1)
        self.assertEqual(copy.questions.first().choices.count(), 2)
        self.assertTrue(copy.questions.first().choices.filter(is_correct=True).exists())

    def test_delete_is_protected_by_submissions(self):
        self.submit()
        response = self.teacher_client.post(
            f"/teacher/curriculum/assignments/{self.assignment.pk}/delete/"
        )
        self.assertRedirects(response, "/teacher/curriculum/")
        self.assertTrue(Assignment.objects.filter(pk=self.assignment.pk).exists())

        draft = Assignment.objects.create(
            topic=self.topic, title="No attempts", description="x", order=9
        )
        response = self.teacher_client.post(f"/teacher/curriculum/assignments/{draft.pk}/delete/")
        self.assertFalse(Assignment.objects.filter(pk=draft.pk).exists())
        self.assertEqual(response.status_code, 302)

    def test_preview_hides_and_reveals_answers(self):
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()
        question = Question.objects.create(
            assignment=self.assignment, kind=Question.Kind.MCQ, text="Capital?", points=1, order=1
        )
        Choice.objects.create(question=question, text="London", is_correct=True, order=1)
        Choice.objects.create(question=question, text="Paris", order=2)
        url = f"/teacher/curriculum/assignments/{self.assignment.pk}/preview/"
        hidden = self.teacher_client.get(url)
        self.assertFalse(hidden.context["reveal"])
        self.assertNotContains(hidden, "верный")
        revealed = self.teacher_client.get(url + "?answers=1")
        self.assertTrue(revealed.context["reveal"])
        self.assertContains(revealed, "верный")


class QuestionEditorTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.max_points = 0
        self.assignment.save()
        self.questions_url = f"/teacher/curriculum/assignments/{self.assignment.pk}/questions/"

    def add(self, **overrides):
        data = {
            "kind": Question.Kind.MCQ,
            "text": "Capital of the UK?",
            "choices_text": "*London\nParis\nBerlin",
            "points": 2,
            "explanation": "",
            "order": 0,
        }
        data.update(overrides)
        return self.teacher_client.post(self.questions_url, data)

    def test_add_single_choice_question_and_sync_points(self):
        response = self.add()
        self.assertRedirects(response, self.questions_url)
        question = Question.objects.get()
        self.assertEqual(question.choices.count(), 3)
        self.assertEqual(question.order, 1)
        self.assertTrue(question.choices.get(text="London").is_correct)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.max_points, 2)

    def test_add_multiple_gap_and_match_questions(self):
        self.add(
            kind=Question.Kind.MULTI,
            choices_text="*yet\n*since\nyesterday",
            points=4,
        )
        self.add(
            kind=Question.Kind.GAP,
            text="She ___ here.",
            choices_text="has lived\nhave lived",
            points=2,
        )
        self.add(
            kind=Question.Kind.MATCH,
            text="Match",
            choices_text="look after | заботиться\ngive up | бросить",
            points=4,
        )
        self.assertEqual(Question.objects.count(), 3)
        gap = Question.objects.get(kind=Question.Kind.GAP)
        self.assertEqual(gap.gaps, ["has lived", "have lived"])
        match = Question.objects.get(kind=Question.Kind.MATCH)
        self.assertEqual(
            [(pair["term"], pair["definition"]) for pair in match.pairs],
            [("look after", "заботиться"), ("give up", "бросить")],
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.max_points, 10)

    def test_choice_validation(self):
        cases = {
            "one option": {"choices_text": "*London"},
            "no correct": {"choices_text": "London\nParis"},
            "match without pipe": {
                "kind": Question.Kind.MATCH,
                "choices_text": "look after заботиться",
            },
            "match with empty half": {"kind": Question.Kind.MATCH, "choices_text": "look after |"},
            "too many options": {"choices_text": "\n".join(f"opt{i}" for i in range(41))},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                response = self.add(**overrides)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors.get("choices_text"))
        self.assertEqual(Question.objects.count(), 0)

    def test_edit_question_replaces_choices(self):
        self.add()
        question = Question.objects.get()
        response = self.teacher_client.post(
            f"/teacher/curriculum/questions/{question.pk}/",
            {
                "kind": Question.Kind.MCQ,
                "text": "Capital of France?",
                "choices_text": "*Paris\nLondon",
                "points": 5,
                "explanation": "Paris — столица Франции.",
                "order": 1,
            },
        )
        self.assertRedirects(response, self.questions_url)
        question.refresh_from_db()
        self.assertEqual(question.text, "Capital of France?")
        self.assertEqual(question.points, 5)
        self.assertEqual(question.choices.count(), 2)
        self.assertTrue(question.choices.get(text="Paris").is_correct)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.max_points, 5)

    def test_edit_form_serializes_existing_choices(self):
        self.add(kind=Question.Kind.MATCH, choices_text="a | b\nc | d", text="Match")
        question = Question.objects.get()
        response = self.teacher_client.get(f"/teacher/curriculum/questions/{question.pk}/")
        self.assertEqual(response.context["form"].initial["choices_text"], "a | b\nc | d")

    def test_delete_question_resyncs_points(self):
        self.add()
        self.add(text="Second?", choices_text="*a\nb", points=3, order=2)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.max_points, 5)
        question = Question.objects.get(text="Second?")
        response = self.teacher_client.post(f"/teacher/curriculum/questions/{question.pk}/delete/")
        self.assertRedirects(response, self.questions_url)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.max_points, 2)


class AssignmentCardsTests(LMSCase):
    """Карточки живут внутри задания: страница карточек, импорт и защита прогресса."""

    def cards_url(self, assignment):
        return f"/teacher/curriculum/assignments/{assignment.pk}/cards/"

    def test_legacy_card_url_redirects_to_new_place(self):
        response = self.teacher_client.get("/teacher/curriculum/decks/new/")
        self.assertRedirects(response, "/teacher/curriculum/assignments/new/?type=flashcards")
        self.assertRedirects(
            self.teacher_client.get("/teacher/curriculum/decks/5/cards/"),
            "/teacher/curriculum/",
        )

    def test_flashcard_assignment_leads_to_cards_page(self):
        response = self.teacher_client.post(
            "/teacher/curriculum/assignments/new/",
            {
                "topic": self.topic.pk,
                "title": "Travel",
                "description": "Слова темы",
                "assignment_type": Assignment.Type.FLASHCARDS,
                "max_points": 0,
                "status": Assignment.Publication.DRAFT,
                "skills": [],
                "order": 0,
            },
        )
        assignment = Assignment.objects.get(title="Travel")
        self.assertEqual(assignment.assignment_type, Assignment.Type.FLASHCARDS)
        self.assertRedirects(response, self.cards_url(assignment), fetch_redirect_response=False)

    def test_add_single_card(self):
        assignment = self.card_assignment(title="Travel")
        url = self.cards_url(assignment)
        response = self.teacher_client.post(
            url,
            {
                "card-front": "departure",
                "card-back": "отправление",
                "card-example": "",
                "card-order": 0,
            },
        )
        self.assertRedirects(response, url)
        card = assignment.cards.get()
        self.assertEqual(card.front, "departure")
        self.assertEqual(card.order, 1)

    def test_bulk_import_appends_and_can_replace(self):
        assignment = self.card_assignment(title="Travel")
        Flashcard.objects.create(assignment=assignment, front="old", back="старое", order=1)
        url = self.cards_url(assignment)
        response = self.teacher_client.post(
            url,
            {
                "bulk-cards_text": (
                    "# комментарий пропускается\n"
                    "departure | отправление | The departure time changed.\n"
                    "luggage | багаж\n"
                )
            },
        )
        self.assertRedirects(response, url)
        self.assertEqual(assignment.cards.count(), 3)
        self.assertEqual(
            assignment.cards.get(front="departure").example, "The departure time changed."
        )

        response = self.teacher_client.post(
            url, {"bulk-cards_text": "gate | выход", "bulk-replace": "on"}
        )
        self.assertEqual(assignment.cards.count(), 1)
        self.assertEqual(assignment.cards.get().front, "gate")
        self.assertEqual(response.status_code, 302)

    def test_bulk_import_validates_lines(self):
        assignment = self.card_assignment(title="Travel")
        url = self.cards_url(assignment)
        for bad in ["only-front", "a | b | c | d", "| b", "a |"]:
            with self.subTest(line=bad):
                response = self.teacher_client.post(url, {"bulk-cards_text": bad})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["bulk_form"].errors)
        self.assertEqual(assignment.cards.count(), 0)

    def test_card_preset_can_be_added_directly(self):
        assignment = self.card_assignment(title="Travel")
        response = self.teacher_client.post(
            self.cards_url(assignment), {"preset_id": "travel-a2", "_preset": "1"}
        )
        self.assertRedirects(response, self.cards_url(assignment))
        self.assertGreater(assignment.cards.count(), 0)

    def test_delete_card_keeps_assignment(self):
        assignment = self.card_assignment(title="Travel")
        card = Flashcard.objects.create(assignment=assignment, front="a", back="b", order=1)
        self.teacher_client.post(f"/teacher/curriculum/cards/{card.pk}/delete/")
        self.assertEqual(assignment.cards.count(), 0)
        self.assertTrue(Assignment.objects.filter(pk=assignment.pk).exists())

    def test_assignment_with_learner_progress_is_not_silently_lost(self):
        assignment = self.card_assignment(title="Travel")
        card = Flashcard.objects.create(assignment=assignment, front="a", back="b", order=1)
        CardReview.objects.create(card=card, student=self.student)
        response = self.teacher_client.post(self.cards_url(assignment))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CardReview.objects.count(), 1)

    def test_inactive_card_assignment_is_hidden_from_students(self):
        assignment = self.card_assignment(title="Travel", is_active=False)
        Flashcard.objects.create(assignment=assignment, front="a", back="b", order=1)
        response = self.student_client.get("/trainer/")
        self.assertEqual(response.status_code, 200)
        course_sets = [item for item in response.context["card_sets"] if not item["is_personal"]]
        self.assertEqual(course_sets, [])
        self.assertEqual(self.student_client.get(f"/trainer/{assignment.pk}/").status_code, 404)


class StudentDirectoryTests(LMSCase):
    def test_lists_students_with_stats(self):
        self.submit()
        response = self.teacher_client.get("/teacher/students/")
        usernames = [student.username for student in response.context["students"]]
        self.assertIn("student", usernames)
        self.assertNotIn("teacher", usernames)
        row = next(item for item in response.context["students"] if item.username == "student")
        self.assertEqual(row.stats["attempts"], 1)
        self.assertEqual(row.stats["waiting"], 1)
        self.assertEqual(response.context["total_assignments"], 1)

    def test_search_by_name_and_telegram(self):
        self.student.first_name = "Anna"
        self.student.save()
        self.student.profile.telegram = "@anna_engl"
        self.student.profile.save()
        for query, expected in [("Anna", 1), ("anna_engl", 1), ("student", 1), ("max", 0)]:
            with self.subTest(query=query):
                response = self.teacher_client.get(f"/teacher/students/?q={query}")
                self.assertEqual(len(response.context["students"]), expected)

    def test_student_card_shows_progress_and_skills(self):
        skill = Skill.objects.create(name="Writing", slug="writing")
        self.assignment.skills.set([skill])
        attempt = self.submit()
        self.review(attempt, grade=90)
        response = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["done"], 1)
        self.assertEqual(response.context["skills"][0][0], "Writing")
        self.assertEqual(response.context["skills"][0][1]["percent"], 90)
        self.assertContains(response, "Past tense")

    def test_teacher_card_is_not_available(self):
        self.assertEqual(
            self.teacher_client.get(f"/teacher/students/{self.teacher.pk}/").status_code, 404
        )

    def test_teacher_can_create_student_with_password(self):
        response = self.teacher_client.post(
            "/teacher/students/create/",
            {
                "username": "new_student",
                "first_name": "New",
                "last_name": "Student",
                "email": "new@example.com",
                "password": "CustomSecretPassword123!",
                "telegram": "@new_student",
            },
        )
        self.assertRedirects(response, "/teacher/students/")
        new_user = User.objects.get(username="new_student")
        self.assertEqual(new_user.first_name, "New")
        self.assertTrue(new_user.check_password("CustomSecretPassword123!"))
        self.assertEqual(new_user.profile.role, Profile.Role.STUDENT)

    def test_teacher_can_manage_groups(self):
        # 1. Create group
        resp = self.teacher_client.post(
            "/teacher/groups/new/",
            {
                "name": "IELTS Prep",
                "slug": "ielts-prep",
                "cefr_level": "B2",
                "student_ids": [self.student.pk],
                "is_active": "on",
            },
        )
        self.assertRedirects(resp, "/teacher/groups/")
        group = Group.objects.get(slug="ielts-prep")
        self.assertEqual(group.name, "IELTS Prep")
        self.assertIn(self.student, group.students.all())

        # 2. View groups list
        resp = self.teacher_client.get("/teacher/groups/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "IELTS Prep")

    def test_block_and_topic_publish_toggles(self):
        # 1. Toggle block
        resp = self.teacher_client.post(
            f"/teacher/curriculum/blocks/{self.block.pk}/publish/",
            {"active": "0"},
        )
        self.assertRedirects(resp, "/teacher/curriculum/")
        self.block.refresh_from_db()
        self.assertFalse(self.block.is_active)

        # 2. Toggle topic
        resp = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/publish/",
            {"active": "0"},
        )
        self.assertRedirects(resp, "/teacher/curriculum/")
        self.topic.refresh_from_db()
        self.assertFalse(self.topic.is_active)


class AnalyticsTests(LMSCase):
    def test_gradebook_matrix(self):
        attempt = self.submit()
        self.review(attempt, grade=80)
        response = self.teacher_client.get("/teacher/analytics/")
        rows = response.context["rows"]
        self.assertEqual(len(rows), 2)
        row = next(item for item in rows if item["student"].pk == self.student.pk)
        self.assertEqual(row["cells"][0]["grade"], 80)
        self.assertEqual(row["percent"], 80)
        self.assertEqual(row["checked"], 1)
        self.assertEqual(response.context["summary"]["graded"], 1)

    def test_filters_and_invalid_values(self):
        other_block = Block.objects.create(name="Second", slug="second")
        other_topic = Topic.objects.create(block=other_block, title="Other", slug="other")
        Assignment.objects.create(topic=other_topic, title="Other task", description="x")
        response = self.teacher_client.get(f"/teacher/analytics/?block={other_block.pk}")
        self.assertEqual(len(response.context["assignments"]), 1)
        response = self.teacher_client.get(f"/teacher/analytics/?topic={self.topic.pk}")
        self.assertEqual(len(response.context["assignments"]), 1)
        response = self.teacher_client.get("/teacher/analytics/?block=abc&topic=xyz")
        self.assertEqual(len(response.context["assignments"]), 2)

    def test_csv_export(self):
        attempt = self.submit()
        self.review(attempt, grade=80)
        response = self.teacher_client.get("/teacher/analytics/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("\ufeff"))
        lines = body.strip().splitlines()
        self.assertIn("Ученик;Прогресс, %;Сдано;Проверено", lines[0])
        self.assertIn("English / Grammar / Past tense", lines[0])
        self.assertTrue(any("80/100" in line for line in lines[1:]))


class ConsoleSettingsTests(LMSCase):
    def test_settings_page_renders_profile_and_snippets(self):
        CommentSnippet.objects.create(
            title="Praise", code="praise", text="Хорошо!", author=self.teacher
        )
        response = self.teacher_client.get("/teacher/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["snippets"]), 1)

    def test_interface_preferences_are_stored_in_session(self):
        response = self.teacher_client.post(
            "/teacher/settings/", {"density": "compact", "hotkeys": "on"}
        )
        self.assertRedirects(response, "/teacher/settings/")
        session = self.teacher_client.session
        self.assertEqual(session["ui_density"], "compact")
        self.assertTrue(session["ui_hotkeys"])

        self.teacher_client.post("/teacher/settings/", {"density": "comfortable"})
        session = self.teacher_client.session
        self.assertEqual(session["ui_density"], "comfortable")
        self.assertFalse(session["ui_hotkeys"])

        # мусорное значение отбрасывается
        self.teacher_client.post("/teacher/settings/", {"density": "neon", "hotkeys": "on"})
        self.assertEqual(self.teacher_client.session["ui_density"], "comfortable")

    def test_profile_update(self):
        response = self.teacher_client.post(
            "/teacher/settings/",
            {
                "first_name": "Mary",
                "last_name": "Ivanova",
                "email": "mary@example.invalid",
                "telegram": "@mary",
                "comment": "IELTS, evenings",
            },
        )
        self.assertRedirects(response, "/teacher/settings/")
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.first_name, "Mary")
        self.assertEqual(self.teacher.email, "mary@example.invalid")
        self.teacher.profile.refresh_from_db()
        self.assertEqual(self.teacher.profile.telegram, "@mary")
        self.assertEqual(self.teacher.profile.comment, "IELTS, evenings")

    def test_snippet_crud(self):
        response = self.teacher_client.post(
            "/teacher/settings/snippets/new/",
            {"title": "Артикли", "code": "art", "text": "Проверьте артикли.", "is_shared": "on"},
        )
        self.assertRedirects(response, "/teacher/settings/")
        snippet = CommentSnippet.objects.get(code="art")
        self.assertEqual(snippet.author, self.teacher)

        response = self.teacher_client.post(
            f"/teacher/settings/snippets/{snippet.pk}/",
            {"title": "Артикли 2", "code": "art", "text": "Обновлённый текст."},
        )
        snippet.refresh_from_db()
        self.assertEqual(snippet.title, "Артикли 2")
        self.assertEqual(response.status_code, 302)

        self.teacher_client.post(f"/teacher/settings/snippets/{snippet.pk}/delete/")
        self.assertFalse(CommentSnippet.objects.filter(pk=snippet.pk).exists())

    def test_snippet_code_rejects_spaces(self):
        response = self.teacher_client.post(
            "/teacher/settings/snippets/new/",
            {"title": "Bad", "code": "two words", "text": "Text"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors.get("code"))

    def test_foreign_private_snippet_is_not_accessible(self):
        other = CommentSnippet.objects.create(
            title="Private", code="p", text="Secret", author=self.admin, is_shared=False
        )
        self.assertEqual(
            self.teacher_client.get(f"/teacher/settings/snippets/{other.pk}/").status_code, 403
        )
        self.assertEqual(
            self.teacher_client.post(f"/teacher/settings/snippets/{other.pk}/delete/").status_code,
            404,
        )
        self.assertTrue(CommentSnippet.objects.filter(pk=other.pk).exists())

    def test_shared_snippets_visible_to_every_teacher(self):
        CommentSnippet.objects.create(title="Shared", text="Общий шаблон", is_shared=True)
        response = self.teacher_client.get("/teacher/settings/")
        self.assertEqual(len(response.context["snippets"]), 1)


class DraftAndAttemptHousekeepingTests(LMSCase):
    def test_draft_is_removed_after_submission(self):
        AnswerDraft.objects.create(
            student=self.student, assignment=self.assignment, text="Черновик"
        )
        self.submit()
        self.assertEqual(AnswerDraft.objects.filter(student=self.student).count(), 0)

    def test_review_creates_event_and_keeps_submission_immutable(self):
        attempt = self.submit()
        text_before = attempt.text_answer
        self.review(attempt, grade=75, comment="Well done")
        attempt.refresh_from_db()
        self.assertEqual(attempt.text_answer, text_before)
        self.assertTrue(attempt.events.filter(action="reviewed").exists())
        self.assertEqual(attempt.review_revision, 1)
