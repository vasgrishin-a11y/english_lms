"""Возврат работы на доработку: ученик должен снова получить доступ к заданию.

Регрессия: у теста с автопроверкой статус «Нужно доработать» ничего не открывал —
проход считался завершённым, а кнопка «Пройти заново» показывалась только при
включённой галочке «Можно пройти заново». Ученик видел «на доработке» и не мог
ничего сделать.
"""

from lms.models import Assignment, Choice, Question, QuestionResponse, Submission
from lms.services import review_submission, round_state

from .base import LMSCase


class ReturnedForRevisionTests(LMSCase):
    def send_back(self, submission, comment="Переделайте вторую часть"):
        return review_submission(
            teacher=self.teacher,
            submission_id=submission.pk,
            expected_version=submission.version,
            expected_review_revision=submission.review_revision,
            grade=0,
            comment=comment,
            decision="needs_revision",
        )

    def latest(self):
        return (
            Submission.objects.filter(student=self.student, assignment=self.assignment)
            .order_by("-version")
            .first()
        )

    # ── Задания с обычным ответом ───────────────────────────────────────────
    def test_text_assignment_can_be_resubmitted(self):
        self.send_back(self.submit())
        page = self.student_client.get(self.url)
        self.assertTrue(page.context["needs_revision"])
        self.assertContains(page, "вернул работу на доработку")
        self.assertContains(page, "Переделайте вторую часть")
        response = self.student_client.post(
            self.url, {"expected_version": 1, "text_answer": "Исправленный ответ"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.latest().version, 2)
        self.assertEqual(self.latest().status, Submission.Status.SUBMITTED)

    # ── Тест с автопроверкой ────────────────────────────────────────────────
    def make_quiz(self, *, exam_mode=False):
        assignment = self.assignment
        assignment.assignment_type = Assignment.Type.QUIZ
        assignment.max_points = 2
        assignment.exam_mode = exam_mode
        assignment.save()
        self.mcq = Question.objects.create(
            assignment=assignment, kind=Question.Kind.MCQ, text="I ___ done.", points=2, order=1
        )
        self.right = Choice.objects.create(question=self.mcq, text="have", is_correct=True)
        self.wrong = Choice.objects.create(question=self.mcq, text="has")
        return assignment

    def answer(self, choice):
        return self.student_client.post(
            f"{self.url}items/{self.mcq.pk}/check/", {f"q_{self.mcq.pk}": str(choice.pk)}
        )

    def fail_the_quiz(self):
        for _ in range(self.assignment.tries_per_item):
            self.answer(self.wrong)
        submission = self.latest()
        self.assertIsNotNone(submission, "Проход должен закрыться сдачей")
        return submission

    def test_quiz_reopens_for_the_student(self):
        self.make_quiz()
        self.assertFalse(self.assignment.allow_retake, "Галочка «пройти заново» выключена")
        self.send_back(self.fail_the_quiz())

        page = self.student_client.get(self.url)
        self.assertTrue(page.context["quiz_accepting"], "Задание должно снова принимать ответы")
        self.assertTrue(page.context["needs_revision"])
        self.assertContains(page, "вернул работу на доработку")
        self.assertNotContains(page, "Задание завершено")

        self.answer(self.right)
        self.assertEqual(
            QuestionResponse.objects.filter(
                student=self.student, assignment=self.assignment, round=2
            ).count(),
            1,
        )
        submission = self.latest()
        self.assertEqual(submission.version, 2)
        self.assertEqual(submission.quiz_attempt.score, 2)

    def test_exam_mode_quiz_also_reopens(self):
        self.make_quiz(exam_mode=True)
        self.send_back(self.fail_the_quiz())
        current, latest, accepting = round_state(self.student, self.assignment)
        self.assertEqual((current, latest), (2, 1))
        self.assertTrue(accepting)

    def test_checked_work_stays_closed_without_retake(self):
        """Обычная проверка ничего не открывает — поведение не изменилось."""
        self.make_quiz()
        submission = self.fail_the_quiz()
        review_submission(
            teacher=self.teacher,
            submission_id=submission.pk,
            expected_version=submission.version,
            expected_review_revision=submission.review_revision,
            grade=1,
            comment="Зачтено",
            decision="checked",
        )
        page = self.student_client.get(self.url)
        self.assertFalse(page.context["quiz_accepting"])
        self.assertFalse(page.context["needs_revision"])
        self.assertContains(page, "Задание завершено")
        self.assertEqual(self.student_client.post(f"{self.url}retake/").status_code, 404)

    def test_second_attempt_closes_the_quiz_again(self):
        """После новой сдачи задание снова закрыто и ждёт проверки."""
        self.make_quiz()
        self.send_back(self.fail_the_quiz())
        self.answer(self.right)
        page = self.student_client.get(self.url)
        self.assertFalse(page.context["quiz_accepting"])
        self.assertFalse(page.context["needs_revision"])
        # Полностью автопроверяемый тест закрывается сразу — «на доработке» снято.
        self.assertNotEqual(self.latest().status, Submission.Status.NEEDS_REVISION)
        self.assertEqual(self.latest().version, 2)
