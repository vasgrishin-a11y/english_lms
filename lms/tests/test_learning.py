"""Ученик: главная, карта курса, тесты с автопроверкой, черновики, тренажёр.

Отдельно покрыты интервальное повторение (SM-2), inline-превью файлов и
фильтры шаблонов — они видны ученику практически на каждом экране.
"""

import io
import wave
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Template
from django.test import override_settings
from django.utils import timezone

from lms.models import (
    AnswerDraft,
    Assignment,
    CardReview,
    Choice,
    Flashcard,
    FlashcardDeck,
    Question,
    QuizAttempt,
    Skill,
    Submission,
)
from lms.services import (
    RATING_CHOICES,
    apply_sm2,
    deck_stats,
    practice_queue,
    review_flashcard,
)
from lms.templatetags.lms_tags import (
    answered,
    dictkey,
    due_state,
    grade_class,
    human_due,
    initials,
    media_kind,
    percent_of,
    skill_icon,
    status_icon,
    status_label,
    type_icon,
)

from .base import LMSCase

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)
    return stream.getvalue()


class StudentHomeTests(LMSCase):
    def test_home_shows_progress_continue_and_deadlines(self):
        attempt = self.submit()
        self.review(attempt, grade=90)
        second = Assignment.objects.create(
            topic=self.topic,
            title="Essay",
            description="Write",
            order=2,
            deadline=timezone.now() + timedelta(hours=10),
        )
        response = self.student_client.get("/my/")
        self.assertEqual(response.status_code, 200)
        totals = response.context["totals"]
        self.assertEqual(totals["total"], 2)
        self.assertEqual(totals["done"], 1)
        self.assertEqual(totals["progress"], 50)
        self.assertEqual(response.context["continue_item"].pk, second.pk)
        self.assertEqual([item.pk for item in response.context["due_soon"]], [second.pk])
        self.assertContains(response, "Essay")

    def test_home_offers_draft_to_continue(self):
        AnswerDraft.objects.create(
            student=self.student, assignment=self.assignment, text="Начатый ответ"
        )
        response = self.student_client.get("/my/")
        self.assertEqual(response.context["continue_item"].pk, self.assignment.pk)
        self.assertEqual(response.context["continue_draft"].text, "Начатый ответ")
        self.assertContains(response, "Начатый ответ")
        self.assertContains(response, "Продолжить черновик")

    def test_home_shows_recent_feedback_and_overdue(self):
        self.assignment.deadline = timezone.now() - timedelta(hours=3)
        self.assignment.save()
        attempt = self.submit()
        self.review(attempt, grade=None, decision="needs_revision", comment="Доработайте времена")
        response = self.student_client.get("/my/")
        self.assertEqual([item.pk for item in response.context["due_soon"]], [self.assignment.pk])
        self.assertEqual(response.context["recent"][0].pk, attempt.pk)
        self.assertContains(response, "Доработайте времена")

    def test_assignment_page_shows_teacher_comment_once(self):
        """Комментарий живёт в карточке попытки и не дублируется в боковой панели.

        Дубль ломает не только чтение с экрана, но и локаторы браузерных тестов
        (strict mode violation), поэтому проверяем количество вхождений.
        """
        attempt = self.submit()
        self.review(attempt, grade=85, comment="Отличная работа над временами")
        html = self.student_client.get(self.url).content.decode()
        self.assertEqual(html.count("Отличная работа над временами"), 1)
        self.assertContains(self.student_client.get(self.url), "Решение преподавателя")

    def test_checked_work_does_not_nag_about_deadline(self):
        attempt = self.submit()
        self.review(attempt, grade=90)
        response = self.student_client.get("/my/")
        self.assertEqual(response.context["due_soon"], [])
        self.assertIsNone(response.context["continue_item"])

    def test_home_with_empty_course(self):
        Assignment.objects.all().delete()
        response = self.student_client.get("/my/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["total"], 0)
        self.assertIsNone(response.context["continue_item"])
        self.assertContains(response, "Заданий пока нет")

    def test_dashboard_routes_by_role(self):
        self.assertRedirects(
            self.teacher_client.get("/"), "/teacher/", fetch_redirect_response=False
        )
        self.assertRedirects(self.student_client.get("/"), "/my/", fetch_redirect_response=False)


class CatalogTests(LMSCase):
    def test_map_mode_groups_by_block_and_topic(self):
        response = self.student_client.get("/assignments/")
        self.assertEqual(response.context["mode"], "map")
        self.assertContains(response, "English")
        self.assertContains(response, "Grammar")
        self.assertContains(response, "Past tense")
        self.assertEqual(response.context["totals"]["total"], 1)

    def test_list_mode_and_invalid_mode_fallback(self):
        self.assertEqual(self.student_client.get("/assignments/?mode=list").context["mode"], "list")
        self.assertEqual(self.student_client.get("/assignments/?mode=chaos").context["mode"], "map")

    def test_hidden_assignments_are_not_listed(self):
        Assignment.objects.create(
            topic=self.topic,
            title="Draft only",
            description="x",
            status=Assignment.Publication.DRAFT,
        )
        Assignment.objects.create(
            topic=self.topic,
            title="Scheduled",
            description="x",
            publish_at=timezone.now() + timedelta(days=1),
        )
        response = self.student_client.get("/assignments/")
        self.assertNotContains(response, "Draft only")
        self.assertNotContains(response, "Scheduled")
        self.assertEqual(response.context["totals"]["total"], 1)

    def test_search_by_title_topic_and_block(self):
        Assignment.objects.create(
            topic=self.topic, title="Vocabulary drill", description="x", order=2
        )
        cases = [("vocab", 1), ("Past", 1), ("Grammar", 2), ("English", 2), ("zzz", 0)]
        for query, expected in cases:
            with self.subTest(query=query):
                page = self.student_client.get(f"/assignments/?q={query}").context["page_obj"]
                self.assertEqual(len(page), expected)

    def test_state_reflects_latest_attempt_only(self):
        self.submit()
        attempt = self.submit(version=1)
        self.review(attempt, grade=70)
        page = self.student_client.get("/assignments/").context["page_obj"]
        self.assertEqual(page[0].state["status"], Submission.Status.CHECKED)
        self.assertEqual(page[0].state["grade"], 70)

    def test_pagination_links_keep_filters(self):
        for index in range(30):
            Assignment.objects.create(
                topic=self.topic, title=f"Task {index}", description="x", order=index + 1
            )
        response = self.student_client.get("/assignments/?mode=list&q=Task")
        self.assertEqual(len(response.context["page_obj"]), 25)
        self.assertContains(response, "page=2")
        self.assertContains(response, "q=Task")

    def test_totals_count_waiting_work(self):
        self.submit()
        totals = self.student_client.get("/assignments/").context["totals"]
        self.assertEqual(totals["waiting"], 1)
        self.assertEqual(totals["done"], 0)
        self.assertEqual(totals["progress"], 0)


class QuizFlowTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.quiz = Assignment.objects.create(
            topic=self.topic,
            title="Tenses quiz",
            description="Choose the correct form.",
            assignment_type=Assignment.Type.QUIZ,
            max_points=0,
            order=2,
        )
        self.single = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.MCQ, text="I ___ done.", points=2, order=1
        )
        self.right = Choice.objects.create(
            question=self.single, text="have", is_correct=True, order=1
        )
        Choice.objects.create(question=self.single, text="has", order=2)
        self.multi = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.MULTI, text="Markers", points=4, order=2
        )
        self.marker_a = Choice.objects.create(
            question=self.multi, text="yet", is_correct=True, order=1
        )
        self.marker_b = Choice.objects.create(
            question=self.multi, text="since", is_correct=True, order=2
        )
        Choice.objects.create(question=self.multi, text="yesterday", order=3)
        self.gap = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.GAP, text="She ___ here.", points=2, order=3
        )
        Choice.objects.create(question=self.gap, text="lives", is_correct=True, order=1)
        self.match = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.MATCH, text="Match", points=2, order=4
        )
        self.pair = Choice.objects.create(
            question=self.match,
            text="look after",
            match_text="заботиться",
            is_correct=True,
            order=1,
        )
        self.quiz.max_points = sum(question.points for question in self.quiz.questions.all())
        self.quiz.save()
        self.quiz_url = f"/assignments/{self.quiz.pk}/"

    def answers(self, *, multi_full=True, gap="lives", match=True, single=True, version=0):
        """POST-набор ответов: ожидаемая версия плюс поля каждого вопроса."""
        return {
            "expected_version": version,
            f"q_{self.single.pk}": str(self.right.pk if single else 2),
            f"q_{self.gap.pk}": gap,
            f"q_{self.match.pk}_{self.pair.pk}": str(self.pair.pk) if match else "",
            f"q_{self.multi.pk}": (
                [str(self.marker_a.pk), str(self.marker_b.pk)]
                if multi_full
                else [str(self.marker_a.pk)]
            ),
        }

    def test_quiz_page_shows_questions_without_answers(self):
        response = self.student_client.get(self.quiz_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["questions"]), 4)
        self.assertContains(response, "Tenses quiz")
        self.assertContains(response, "Отправить и проверить")
        self.assertNotContains(response, "Результат автопроверки")

    def test_perfect_quiz_is_checked_automatically(self):
        response = self.student_client.post(self.quiz_url, self.answers())
        self.assertRedirects(response, self.quiz_url)
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        self.assertEqual(attempt.status, Submission.Status.CHECKED)
        self.assertEqual(attempt.max_points_snapshot, 10)
        result = QuizAttempt.objects.get(submission=attempt)
        self.assertEqual(result.score, 10)
        self.assertEqual(result.correct_count, 4)
        self.assertEqual(attempt.feedback.grade, 10)
        self.assertIsNone(attempt.feedback.teacher)
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "Результат автопроверки")
        self.assertContains(page, "Верных ответов: 4 из 4")

    def test_partial_scoring(self):
        self.student_client.post(
            self.quiz_url, self.answers(multi_full=False, gap="LIVES!", match=False, single=False)
        )
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        result = QuizAttempt.objects.get(submission=attempt)
        # 0 за одиночный выбор, 2 из 4 за несколько, 2 за пропуск, 0 за соответствие
        self.assertEqual(result.score, 4)
        self.assertEqual(result.correct_count, 1)
        self.assertEqual(attempt.feedback.grade, 4)
        detail = result.answers[str(self.multi.pk)]
        self.assertTrue(detail["partially"])
        self.assertFalse(detail["correct"])

    def test_quiz_summary_is_stored_in_attempt_text(self):
        self.student_client.post(self.quiz_url, self.answers())
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        self.assertIn("Автоматическая проверка теста", attempt.text_answer)
        self.assertIn("Баллы: 10 из 10", attempt.text_answer)

    def test_retake_creates_new_version(self):
        self.student_client.post(self.quiz_url, self.answers())
        second = self.student_client.post(self.quiz_url, self.answers(single=False, version=1))
        self.assertRedirects(second, self.quiz_url)
        attempts = list(
            Submission.objects.filter(student=self.student, assignment=self.quiz).order_by(
                "version"
            )
        )
        self.assertEqual([item.version for item in attempts], [1, 2])
        self.assertEqual(attempts[1].feedback.grade, 8)
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "Попытка 1")
        self.assertContains(page, "Попытка 2")

    def test_stale_version_is_conflict(self):
        self.student_client.post(self.quiz_url, self.answers())
        response = self.student_client.post(self.quiz_url, self.answers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            Submission.objects.filter(student=self.student, assignment=self.quiz).count(), 1
        )

    def test_empty_quiz_reports_instead_of_submitting(self):
        empty = Assignment.objects.create(
            topic=self.topic,
            title="Empty quiz",
            description="x",
            assignment_type=Assignment.Type.QUIZ,
            max_points=0,
            order=3,
        )
        response = self.student_client.post(f"/assignments/{empty.pk}/", {"expected_version": 0})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "нет вопросов")
        self.assertEqual(Submission.objects.filter(assignment=empty).count(), 0)

    def test_rate_limit_returns_retry_after(self):
        self.student_client.post(self.quiz_url, self.answers())
        with override_settings(LMS_SUBMISSIONS_PER_HOUR=1):
            response = self.student_client.post(self.quiz_url, self.answers(version=1))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "3600")

    def test_teacher_cannot_open_student_quiz_page(self):
        self.assertEqual(self.teacher_client.get(self.quiz_url).status_code, 302)

    def test_quiz_maximum_follows_questions(self):
        self.assertEqual(self.quiz.max_points, 10)
        extra = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.GAP, text="Extra", points=3, order=5
        )
        Choice.objects.create(question=extra, text="answer", is_correct=True, order=1)
        response = self.student_client.post(
            self.quiz_url, {**self.answers(version=0), f"q_{extra.pk}": "answer"}
        )
        self.assertRedirects(response, self.quiz_url)
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.max_points, 13)


class DraftAutosaveTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.draft_url = f"/assignments/{self.assignment.pk}/draft/"

    def test_draft_is_saved_and_updated(self):
        response = self.student_client.post(self.draft_url, {"text": "Первая строка"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Черновик сохранён")
        draft = AnswerDraft.objects.get(student=self.student, assignment=self.assignment)
        self.assertEqual(draft.text, "Первая строка")

        self.student_client.post(self.draft_url, {"text": "Вторая строка"})
        draft.refresh_from_db()
        self.assertEqual(draft.text, "Вторая строка")
        self.assertEqual(AnswerDraft.objects.count(), 1)

    def test_draft_does_not_create_attempt(self):
        self.student_client.post(self.draft_url, {"text": "Черновик"})
        self.assertEqual(Submission.objects.count(), 0)

    def test_empty_draft_reports_state(self):
        self.student_client.post(self.draft_url, {"text": "Черновик"})
        response = self.student_client.post(self.draft_url, {"text": ""})
        self.assertContains(response, "Черновик пуст")

    def test_long_text_is_truncated(self):
        self.student_client.post(self.draft_url, {"text": "x" * 25000})
        self.assertEqual(len(AnswerDraft.objects.get().text), 20000)

    def test_draft_prefills_answer_form(self):
        AnswerDraft.objects.create(
            student=self.student, assignment=self.assignment, text="Начатый текст"
        )
        response = self.student_client.get(self.url)
        self.assertContains(response, "Начатый текст")

    def test_draft_is_denied_for_quiz(self):
        quiz = Assignment.objects.create(
            topic=self.topic,
            title="Quiz",
            description="x",
            assignment_type=Assignment.Type.QUIZ,
        )
        response = self.student_client.post(f"/assignments/{quiz.pk}/draft/", {"text": "abc"})
        self.assertContains(response, "недоступен")
        self.assertEqual(AnswerDraft.objects.count(), 0)

    def test_teacher_cannot_save_student_draft(self):
        self.assertEqual(self.teacher_client.post(self.draft_url, {"text": "x"}).status_code, 403)
        self.assertEqual(AnswerDraft.objects.count(), 0)

    def test_draft_removed_after_submission(self):
        self.student_client.post(self.draft_url, {"text": "Черновик"})
        self.student_client.post(self.url, {"expected_version": 0, "text_answer": "Ответ"})
        self.assertEqual(AnswerDraft.objects.count(), 0)


class TrainerTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.deck = FlashcardDeck.objects.create(topic=self.topic, title="Travel")
        self.cards = [
            Flashcard.objects.create(
                deck=self.deck, front=f"word {index}", back=f"слово {index}", order=index
            )
            for index in range(1, 4)
        ]
        self.session_url = f"/trainer/{self.deck.pk}/"

    def rate(self, rating):
        return self.student_client.post(self.session_url, {"rating": rating})

    def test_trainer_lists_decks_with_due_counts(self):
        response = self.student_client.get("/trainer/")
        decks = response.context["decks"]
        self.assertEqual(len(decks), 1)
        self.assertEqual(decks[0].card_total, 3)
        self.assertEqual(decks[0].due_count, 3)
        self.assertEqual(response.context["due_total"], 3)

    def test_session_queue_is_stored_and_advances(self):
        page = self.student_client.get(self.session_url)
        self.assertEqual(page.context["card"].pk, self.cards[0].pk)
        self.assertEqual(len(page.context["queue"]), 3)

        response = self.rate("good")
        self.assertRedirects(response, self.session_url)
        page = self.student_client.get(self.session_url)
        self.assertEqual(page.context["card"].pk, self.cards[1].pk)
        self.assertEqual(len(page.context["queue"]), 2)

    def test_rating_good_builds_interval(self):
        self.rate("good")
        review = CardReview.objects.get(card=self.cards[0], student=self.student)
        self.assertEqual(review.repetitions, 1)
        self.assertEqual(review.interval_days, 1)
        self.assertGreater(review.due_at, timezone.now())

        apply_sm2(review, RATING_CHOICES["good"])
        self.assertEqual(review.interval_days, 6)
        apply_sm2(review, RATING_CHOICES["good"])
        self.assertEqual(review.repetitions, 3)
        self.assertGreaterEqual(review.interval_days, 6)

    def test_rating_again_resets_and_penalises_ease(self):
        review_flashcard(
            student=self.student, card_id=self.cards[0].pk, rating=RATING_CHOICES["good"]
        )
        review = CardReview.objects.get(card=self.cards[0], student=self.student)
        ease_before = review.ease
        review_flashcard(
            student=self.student, card_id=self.cards[0].pk, rating=RATING_CHOICES["again"]
        )
        review.refresh_from_db()
        self.assertEqual(review.repetitions, 0)
        self.assertEqual(review.interval_days, 0)
        self.assertEqual(review.lapses, 1)
        self.assertLess(review.ease, ease_before)
        self.assertLessEqual(review.due_at, timezone.now() + timedelta(minutes=11))

    def test_ease_stays_within_bounds(self):
        hard = CardReview(card=self.cards[0], student=self.student, ease=1.35)
        for _ in range(6):
            apply_sm2(hard, RATING_CHOICES["again"])
        self.assertGreaterEqual(hard.ease, 1.3)

        easy = CardReview(card=self.cards[1], student=self.student, ease=3.45)
        for _ in range(6):
            apply_sm2(easy, RATING_CHOICES["easy"])
        self.assertLessEqual(easy.ease, 3.5)

    def test_invalid_rating_keeps_queue(self):
        page = self.student_client.get(self.session_url)
        response = self.rate("perfect")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите оценку повторения")
        self.assertEqual(CardReview.objects.count(), 0)
        self.assertEqual(
            len(self.student_client.get(self.session_url).context["queue"]),
            len(page.context["queue"]),
        )

    def test_htmx_request_gets_fragment(self):
        self.student_client.get(self.session_url)
        response = self.student_client.post(
            self.session_url, {"rating": "good"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")
        self.assertContains(response, "trainer-stage")

    def test_fragment_reports_finished_session(self):
        self.student_client.get(self.session_url)
        for _ in self.cards:
            self.student_client.post(self.session_url, {"rating": "easy"}, HTTP_HX_REQUEST="true")
        response = self.student_client.post(
            self.session_url, {"rating": "easy"}, HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, "Сессия завершена")
        page = self.student_client.get(self.session_url)
        self.assertIsNone(page.context["card"])
        self.assertContains(page, "Сессия завершена")

    def test_reset_starts_new_session(self):
        self.rate("good")
        response = self.student_client.post(self.session_url, {"reset": "1"})
        self.assertRedirects(response, self.session_url)
        self.assertEqual(CardReview.objects.count(), 1)
        page = self.student_client.get(self.session_url)
        self.assertIsNotNone(page.context["card"])
        self.assertEqual(len(page.context["queue"]), 2)

    def test_inactive_deck_is_hidden(self):
        self.deck.is_active = False
        self.deck.save()
        self.assertEqual(self.student_client.get(self.session_url).status_code, 404)
        self.assertEqual(len(self.student_client.get("/trainer/").context["decks"]), 0)

    def test_empty_deck_finishes_immediately(self):
        empty = FlashcardDeck.objects.create(topic=self.topic, title="Empty")
        response = self.student_client.get(f"/trainer/{empty.pk}/")
        self.assertIsNone(response.context["card"])
        self.assertTrue(response.context["done"])

    def test_teacher_cannot_train(self):
        self.assertEqual(self.teacher_client.get(self.session_url).status_code, 302)


class SpacedRepetitionServiceTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.deck = FlashcardDeck.objects.create(topic=self.topic, title="Travel")
        self.cards = [
            Flashcard.objects.create(
                deck=self.deck, front=f"w{index}", back=f"с{index}", order=index
            )
            for index in range(1, 5)
        ]

    def test_queue_orders_due_before_fresh_and_respects_limit(self):
        now = timezone.now()
        CardReview.objects.create(
            card=self.cards[0], student=self.student, due_at=now + timedelta(days=1)
        )
        CardReview.objects.create(
            card=self.cards[1], student=self.student, due_at=now - timedelta(days=1)
        )
        CardReview.objects.create(
            card=self.cards[2],
            student=self.student,
            due_at=now - timedelta(hours=2),
            interval_days=30,
        )
        result = practice_queue(student=self.student, deck=self.deck, limit=3)
        self.assertEqual(
            [card.pk for card in result["queue"]],
            [self.cards[1].pk, self.cards[2].pk, self.cards[3].pk],
        )
        self.assertEqual(result["due"], 2)
        self.assertEqual(result["fresh"], 1)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["learned"], 1)

    def test_limit_is_at_least_one(self):
        result = practice_queue(student=self.student, deck=self.deck, limit=0)
        self.assertEqual(len(result["queue"]), 1)

    def test_deck_stats_counts_reviewed_and_due(self):
        CardReview.objects.create(
            card=self.cards[0], student=self.student, due_at=timezone.now() - timedelta(minutes=5)
        )
        decks = deck_stats(self.student)
        self.assertEqual(len(decks), 1)
        self.assertEqual(decks[0].card_total, 4)
        self.assertEqual(decks[0].reviewed_count, 1)
        self.assertEqual(decks[0].due_count, 4)

    def test_review_flashcard_validates_rating_and_access(self):
        with self.assertRaises(ValidationError):
            review_flashcard(student=self.student, card_id=self.cards[0].pk, rating=99)

        self.deck.is_active = False
        self.deck.save()
        with self.assertRaises(PermissionDenied):
            review_flashcard(student=self.student, card_id=self.cards[0].pk, rating=3)

        self.deck.is_active = True
        self.deck.save()
        with self.assertRaises(PermissionDenied):
            review_flashcard(student=self.teacher, card_id=self.cards[0].pk, rating=3)
        with self.assertRaises(PermissionDenied):
            review_flashcard(student=self.student, card_id=999999, rating=3)
        self.assertEqual(CardReview.objects.count(), 0)

    def test_review_flashcard_reuses_existing_state(self):
        review_flashcard(
            student=self.student, card_id=self.cards[0].pk, rating=RATING_CHOICES["good"]
        )
        review_flashcard(
            student=self.student, card_id=self.cards[0].pk, rating=RATING_CHOICES["good"]
        )
        self.assertEqual(CardReview.objects.count(), 1)
        self.assertEqual(CardReview.objects.get().repetitions, 2)


class GradesAndUpcomingTests(LMSCase):
    def test_grades_show_average_and_latest_attempts(self):
        first = self.submit()
        self.review(first, grade=80)
        second_task = Assignment.objects.create(
            topic=self.topic, title="Essay", description="x", order=2, max_points=50
        )
        second = self.submit(assignment_id=second_task.pk)
        self.review(second, grade=25)
        response = self.student_client.get("/grades/")
        self.assertEqual(response.context["checked"], 2)
        self.assertEqual(response.context["average"], 70)
        self.assertEqual(len(response.context["attempts"]), 2)
        self.assertContains(response, "Good work")

    def test_grades_without_feedback(self):
        self.submit()
        response = self.student_client.get("/grades/")
        self.assertIsNone(response.context["average"])
        self.assertEqual(response.context["checked"], 0)
        self.assertIsNone(response.context["average"])
        self.assertContains(response, "Сдана")

    def test_grades_empty_state_before_first_answer(self):
        response = self.student_client.get("/grades/")
        self.assertEqual(response.context["checked"], 0)
        self.assertContains(response, "Оценок пока нет")

    def test_upcoming_buckets(self):
        overdue = Assignment.objects.create(
            topic=self.topic,
            title="Overdue",
            description="x",
            order=2,
            deadline=timezone.now() - timedelta(hours=2),
        )
        soon = Assignment.objects.create(
            topic=self.topic,
            title="Soon",
            description="x",
            order=3,
            deadline=timezone.now() + timedelta(days=2),
        )
        later = Assignment.objects.create(
            topic=self.topic,
            title="Later",
            description="x",
            order=4,
            deadline=timezone.now() + timedelta(days=30),
        )
        response = self.student_client.get("/upcoming/")
        self.assertEqual([item.pk for item in response.context["overdue"]], [overdue.pk])
        self.assertEqual(
            sorted(item.pk for item in response.context["week"]),
            sorted([soon.pk, self.assignment.pk]),
        )
        self.assertEqual([item.pk for item in response.context["items"]][:1], [overdue.pk])
        self.assertIn(later.pk, [item.pk for item in response.context["items"]])

    def test_upcoming_skips_checked_work(self):
        attempt = self.submit()
        self.review(attempt, grade=90)
        response = self.student_client.get("/upcoming/")
        self.assertNotIn(self.assignment.pk, [item.pk for item in response.context["items"]])

    def test_upcoming_marks_revision_as_open(self):
        attempt = self.submit()
        self.review(attempt, grade=None, decision="needs_revision")
        response = self.student_client.get("/upcoming/")
        self.assertIn(self.assignment.pk, [item.pk for item in response.context["items"]])

    def test_late_attempt_is_flagged(self):
        self.assignment.deadline = timezone.now() - timedelta(hours=1)
        self.assignment.save()
        attempt = self.submit()
        self.assertTrue(attempt.is_late)
        self.assertContains(self.student_client.get("/grades/"), "после дедлайна")


class MediaPreviewTests(LMSCase):
    def test_material_image_previews_inline_with_csp(self):
        self.assignment.material_file.save("prompt.png", ContentFile(PNG_BYTES), save=True)
        response = self.student_client.get(f"/preview/{self.assignment.material_file.name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_audio_answer_previews_for_owner_only(self):
        self.assignment.assignment_type = Assignment.Type.AUDIO
        self.assignment.save()
        attempt = self.submit(
            file_answer=SimpleUploadedFile("answer.wav", wav_bytes(), "audio/wav")
        )
        name = attempt.file_answer.name
        self.assertEqual(self.student_client.get(f"/preview/{name}").status_code, 200)
        self.assertEqual(self.client_for(self.other).get(f"/preview/{name}").status_code, 404)
        self.assertEqual(self.teacher_client.get(f"/preview/{name}").status_code, 200)

    def test_non_previewable_types_are_rejected(self):
        self.assignment.material_file.save("prompt.png", ContentFile(PNG_BYTES), save=True)
        base = self.assignment.material_file.name
        for suffix in [".svg", ".pdf", ".html", ".txt"]:
            with self.subTest(suffix=suffix):
                name = base.replace(".png", suffix)
                self.assertEqual(self.student_client.get(f"/preview/{name}").status_code, 404)
        self.assertEqual(self.student_client.get("/preview/missing.png").status_code, 404)

    def test_signature_mismatch_is_rejected(self):
        self.assignment.material_file.save("fake.png", ContentFile(b"not a png at all"), save=True)
        response = self.student_client.get(f"/preview/{self.assignment.material_file.name}")
        self.assertEqual(response.status_code, 404)

    def test_private_file_downloads_as_attachment(self):
        self.assignment.material_file.save("prompt.png", ContentFile(PNG_BYTES), save=True)
        response = self.student_client.get(f"/files/{self.assignment.material_file.name}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_anonymous_cannot_preview(self):
        self.assignment.material_file.save("prompt.png", ContentFile(PNG_BYTES), save=True)
        response = self.client.get(f"/preview/{self.assignment.material_file.name}")
        self.assertEqual(response.status_code, 302)


class TemplateFilterTests(LMSCase):
    def test_media_kind(self):
        self.assertEqual(media_kind("a/b/photo.PNG"), "image")
        self.assertEqual(media_kind("a/b/voice.mp3"), "audio")
        self.assertEqual(media_kind("a/b/doc.pdf"), "other")
        self.assertEqual(media_kind(None), "other")

    def test_dictkey_and_answered(self):
        detail = {"points": 2, "given": ["a", "b"], "expected": "a, b"}
        self.assertEqual(dictkey({"7": detail}, 7), detail)
        self.assertEqual(dictkey({"7": detail}, "7"), detail)
        self.assertIsNone(dictkey({"7": detail}, 8))
        self.assertIsNone(dictkey(None, 1))
        self.assertEqual(answered({"7": detail}, 7), "a, b")
        self.assertEqual(answered({"7": {"given": "x"}}, 7), "x")
        self.assertEqual(answered({"7": {"given": {"1": "2"}}}, 7), "2")
        self.assertEqual(answered({"7": {}}, 7), "")
        self.assertEqual(answered(None, 7), "")

    def test_percent_of(self):
        self.assertEqual(percent_of(3, 4), 75)
        self.assertEqual(percent_of(1, 0), 0)
        self.assertEqual(percent_of(None, 10), 0)

    def test_status_and_type_icons(self):
        self.assertEqual(status_label("checked"), "Проверена")
        self.assertEqual(status_label("unknown"), "Не отправлено")
        self.assertEqual(status_label(""), "Не отправлено")
        self.assertEqual(status_icon("needs_revision"), "alert")
        self.assertEqual(status_icon("unknown"), "circle-dashed")
        self.assertEqual(type_icon("quiz"), "target")
        self.assertEqual(type_icon("audio"), "mic")
        self.assertEqual(type_icon("unknown"), "file-text")
        self.assertEqual(skill_icon("listening"), "headphones")
        self.assertEqual(skill_icon("unknown"), "tag")

    def test_initials(self):
        self.student.first_name = "Anna"
        self.student.last_name = "Petrova"
        self.assertEqual(initials(self.student), "AP")
        self.student.first_name = ""
        self.student.last_name = ""
        self.assertEqual(initials(self.student), "st")

    def test_grade_class(self):
        self.assertEqual(grade_class(40, 100), "is-low")
        self.assertEqual(grade_class(60, 100), "is-mid")
        self.assertEqual(grade_class(90, 100), "")
        self.assertEqual(grade_class(None, 100), "is-empty")
        self.assertEqual(grade_class(5, 0), "")
        self.assertEqual(grade_class(5, "abc"), "")

    def test_human_due_and_due_state(self):
        now = timezone.now()
        self.assertIn("просрочено", human_due(now - timedelta(hours=2)))
        self.assertIn("сегодня", human_due(now + timedelta(hours=2)))
        self.assertIn("завтра", human_due(now + timedelta(hours=30)))
        self.assertIn("через", human_due(now + timedelta(days=3)))
        self.assertRegex(human_due(now + timedelta(days=30)), r"\d{2}\.\d{2}\.\d{4}")
        self.assertEqual(human_due(None), "")
        self.assertIn("мин", human_due(now - timedelta(minutes=10)))
        self.assertIn("дн", human_due(now - timedelta(days=5)))

        self.assertEqual(due_state(now - timedelta(hours=1)), "late")
        self.assertEqual(due_state(now + timedelta(hours=1)), "today")
        self.assertEqual(due_state(now + timedelta(hours=30)), "soon")
        self.assertEqual(due_state(now + timedelta(days=10)), "later")
        self.assertEqual(due_state(None), "none")

    def test_icon_tag_escapes_dynamic_title(self):
        rendered = Template('{% load lms_tags %}{% icon "check" "icon" title=label %}').render(
            Context({"label": "<script>alert(1)</script>"})
        )
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn('role="img"', rendered)

    def test_decorative_icon_is_hidden_from_screen_readers(self):
        rendered = Template('{% load lms_tags %}{% icon "check" %}').render(Context({}))
        self.assertIn('aria-hidden="true"', rendered)
        self.assertNotIn("role=", rendered)


class SkillsContextTests(LMSCase):
    def test_skills_are_shown_on_assignment_page(self):
        grammar = Skill.objects.create(name="Grammar", slug="grammar", kind="grammar")
        self.assignment.skills.set([grammar])
        response = self.student_client.get(self.url)
        self.assertContains(response, "Grammar")
        self.assertEqual(list(response.context["assignment"].skills.all()), [grammar])
