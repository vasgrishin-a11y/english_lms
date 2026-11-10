"""Текст с пропусками: разметка, проверка и ответы прямо в тексте.

Ключевое отличие от «вписать ответ»: полей много, они стоят внутри одного
текста, а балл делится по числу верно заполненных пропусков. Здесь закреплены
и разметка для преподавателя, и поведение ученика, и разбор в проверке.
"""

import io
import zipfile

from django.utils import timezone

from lms.cloze import (
    answer_key,
    build_segments,
    filled_text,
    gap_count,
    layout_error,
    parse_cloze,
    score_cloze,
)
from lms.forms import QuestionForm
from lms.models import Assignment, Choice, Question, QuestionResponse, Submission
from lms.scoring import (
    answer_parts,
    describe_answer,
    is_blank_answer,
    score_question,
)

from .base import LMSCase

TEXT = (
    "Yesterday Anna [[1]] (go) to the park. She ___ (not / see) her friends, "
    "but she [[3]] (meet) a neighbour."
)
ACCEPTED = [["went"], ["didn't see", "did not see"], ["met"]]


class ClozeMarkupTests(LMSCase):
    def test_both_markup_styles_become_numbered_gaps(self):
        segments = parse_cloze("A [[1]] b ___ c [[3]]")
        gaps = [segment for segment in segments if segment["type"] == "gap"]
        self.assertEqual([gap["number"] for gap in gaps], [1, 2, 3])
        self.assertEqual(gap_count("A [[1]] b ___ c [[3]]"), 3)

    def test_plain_text_has_no_gaps(self):
        self.assertEqual(gap_count("No gaps here"), 0)
        self.assertEqual(parse_cloze("No gaps here"), [{"type": "text", "text": "No gaps here"}])

    def test_underscores_shorter_than_three_are_not_gaps(self):
        self.assertEqual(gap_count("A __ b"), 0)

    def test_explicit_numbers_must_go_in_order(self):
        self.assertEqual(layout_error("A [[1]] b ___"), "")
        self.assertIn("по порядку", layout_error("A [[2]] b ___"))
        self.assertIn("хотя бы один пропуск", layout_error("Нет пропусков"))
        self.assertIn("Слишком много", layout_error("___ " * 41))

    def test_accepted_answers_grouped_by_number(self):
        choices = [Choice(text="went", order=1), Choice(text="met", order=3)]
        from lms.cloze import accepted_by_gap

        self.assertEqual(accepted_by_gap(choices, 3), [["went"], [], ["met"]])
        self.assertEqual(answer_key(ACCEPTED)[0], {"number": 1, "answers": ["went"]})

    def test_filled_text_replaces_gaps_with_first_accepted_variant(self):
        self.assertEqual(
            filled_text(TEXT, ACCEPTED),
            "Yesterday Anna went (go) to the park. She didn't see (not / see) her friends, "
            "but she met (meet) a neighbour.",
        )

    def test_segments_show_student_answers_and_marks(self):
        segments = build_segments(
            TEXT,
            ACCEPTED,
            {"1": "went", "2": "did NOT  see", "3": ""},
            show_answers=True,
        )
        gaps = [segment for segment in segments if segment["type"] == "gap"]
        self.assertEqual([gap["status"] for gap in gaps], ["ok", "ok", "empty"])
        self.assertEqual(gaps[2]["title"], "Правильно: met")

    def test_open_item_does_not_leak_answers(self):
        segments = build_segments(TEXT, ACCEPTED, {"1": "go"}, show_answers=False)
        gaps = [segment for segment in segments if segment["type"] == "gap"]
        self.assertTrue(all(gap["status"] == "" for gap in gaps))
        self.assertTrue(all(gap["title"] == "" for gap in gaps))


class ClozeScoringTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()
        self.question = Question.objects.create(
            assignment=self.assignment,
            kind=Question.Kind.CLOZE,
            text=TEXT,
            points=6,
            order=1,
        )
        for number, variants in enumerate(ACCEPTED, start=1):
            for variant in variants:
                Choice.objects.create(
                    question=self.question, text=variant, is_correct=True, order=number
                )

    def test_full_answer_gets_all_points(self):
        result = score_question(self.question, {"1": "went", "2": "didn't see", "3": "met"})
        self.assertEqual(result["points"], 6)
        self.assertTrue(result["correct"])
        self.assertEqual(result["ratio"], 1.0)

    def test_partial_answer_gets_share_of_points(self):
        result = score_question(self.question, {"1": "went", "2": "saw", "3": "met"})
        self.assertEqual(result["points"], 4)
        self.assertFalse(result["correct"])
        self.assertTrue(result["partially"])
        self.assertEqual(result["ratio"], 0.6667)

    def test_case_punctuation_and_extra_spaces_are_ignored(self):
        result = score_question(self.question, {"1": "  WENT ", "2": "did not  see", "3": "Met."})
        self.assertEqual(result["points"], 6)

    def test_empty_gap_counts_as_mistake(self):
        result = score_question(self.question, {"1": "went", "3": "met"})
        self.assertEqual(result["points"], 4)
        self.assertEqual(result["given"]["2"], "")

    def test_expected_is_the_filled_in_text(self):
        result = score_question(self.question, {})
        self.assertIn("went", result["expected"])
        self.assertNotIn("[[1]]", result["expected"])

    def test_parts_mark_each_gap(self):
        parts = answer_parts(self.question, {"1": "went", "2": "saw", "3": "met"})
        self.assertEqual(parts["gaps"], {"1": True, "2": False, "3": True})
        self.assertEqual((parts["hits"], parts["total"]), (2, 3))

    def test_describe_answer_lists_gaps(self):
        text = describe_answer(self.question, {"1": "went", "2": "", "3": "met"})
        self.assertEqual(text, "(1) went; (2) —; (3) met")

    def test_blank_only_when_every_gap_is_empty(self):
        self.assertTrue(is_blank_answer(self.question, {"1": "", "2": " ", "3": ""}))
        self.assertFalse(is_blank_answer(self.question, {"1": "", "2": "saw", "3": ""}))
        self.assertTrue(is_blank_answer(self.question, None))

    def test_score_cloze_handles_missing_accepted_variants(self):
        result = score_cloze({"1": "went"}, [["went"], [], []], 3)
        self.assertEqual((result["hits"], result["total"]), (1, 3))


class ClozeFormTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()

    def post(self, **overrides):
        data = {
            "kind": Question.Kind.CLOZE,
            "text": TEXT,
            "choices_text": "went\ndidn't see / did not see\nmet",
            "points": 6,
            "explanation": "",
            "order": 1,
        }
        data.update(overrides)
        return QuestionForm(data)

    def test_valid_form_keeps_gap_numbers(self):
        form = self.post()
        self.assertTrue(form.is_valid(), form.errors.as_text())
        question = form.save(commit=False)
        question.assignment = self.assignment
        question.save()
        form.save_choices(question)
        self.assertEqual(question.cloze_count, 3)
        self.assertEqual(question.cloze_accepted, ACCEPTED)
        # Номер пропуска — это order варианта, а не позиция в списке.
        self.assertEqual(list(question.choices.values_list("order", flat=True)), [1, 2, 2, 3])

    def test_round_trip_serialization(self):
        question = self.post().save(commit=False)
        question.assignment = self.assignment
        question.save()
        form = self.post()
        form.is_valid()
        form.save_choices(question)
        self.assertEqual(
            QuestionForm(instance=question).initial["choices_text"],
            "went\ndidn't see / did not see\nmet",
        )

    def test_line_count_must_match_gap_count(self):
        form = self.post(choices_text="went\nmet")
        self.assertFalse(form.is_valid())
        self.assertIn("3 пропуска", form.errors["choices_text"][0])

    def test_text_without_gaps_is_rejected(self):
        form = self.post(text="No gaps at all")
        self.assertFalse(form.is_valid())
        self.assertIn("хотя бы один пропуск", form.errors["text"][0])

    def test_out_of_order_numbers_are_rejected(self):
        form = self.post(text="A [[2]] b ___")
        self.assertFalse(form.is_valid())
        self.assertIn("по порядку", form.errors["text"][0])

    def test_empty_answer_line_is_rejected(self):
        form = self.post(choices_text="went\n \nmet")
        self.assertFalse(form.is_valid())
        self.assertIn("пусто в 2", form.errors["choices_text"][0])

    def test_other_kinds_are_untouched(self):
        form = QuestionForm(
            {
                "kind": Question.Kind.MCQ,
                "text": "Pick one",
                "choices_text": "*one\ntwo",
                "points": 1,
                "explanation": "",
                "order": 1,
            }
        )
        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(len(form.cleaned_data["choices_text"]), 2)


def _make_cloze_quiz(case, *, exam=False):
    """Задание с одним пунктом «текст с пропусками» для потоковых сценариев."""
    quiz = Assignment.objects.create(
        topic=case.topic,
        title="Cloze",
        description="Fill the gaps.",
        assignment_type=Assignment.Type.QUIZ,
        max_points=6,
        exam_mode=exam,
        order=5,
    )
    question = Question.objects.create(
        assignment=quiz, kind=Question.Kind.CLOZE, text=TEXT, points=6, order=1
    )
    for number, variants in enumerate(ACCEPTED, start=1):
        for variant in variants:
            Choice.objects.create(question=question, text=variant, is_correct=True, order=number)
    return quiz, question


class ClozeStudentFlowTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.quiz, self.question = _make_cloze_quiz(self)

    def check(self, values, *, htmx=True):
        data = {f"q_{self.question.pk}_gap_{n}": value for n, value in values.items()}
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.student_client.post(
            f"/assignments/{self.quiz.pk}/items/{self.question.pk}/check/", data, **headers
        )

    def test_gap_fields_are_inside_the_text(self):
        page = self.student_client.get(f"/assignments/{self.quiz.pk}/")
        html = page.content.decode()
        self.assertIn("cloze-gap", html)
        self.assertIn(f'name="q_{self.question.pk}_gap_1"', html)
        self.assertIn(f'name="q_{self.question.pk}_gap_3"', html)
        self.assertNotIn("[[1]]", html)
        # Текст и поля идут вперемешку, а не отдельным списком вопросов.
        self.assertLess(
            html.index("Yesterday Anna"), html.index(f'name="q_{self.question.pk}_gap_1"')
        )
        self.assertLess(html.index(f'name="q_{self.question.pk}_gap_1"'), html.index("to the park"))

    def test_correct_answers_close_item_with_full_points(self):
        response = self.check({"1": "went", "2": "didn't see", "3": "met"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Верно")
        item = QuestionResponse.objects.get(student=self.student, question=self.question)
        self.assertEqual(item.state, QuestionResponse.State.CORRECT)
        self.assertEqual(item.points, 6)

    def test_partial_answer_keeps_try_open_and_hides_solution(self):
        response = self.check({"1": "went", "2": "saw", "3": "met"})
        self.assertContains(response, "Осталось попыток: 2")
        self.assertNotContains(response, "Правильно:")
        item = QuestionResponse.objects.get(student=self.student, question=self.question)
        self.assertEqual(item.state, QuestionResponse.State.OPEN)
        self.assertEqual(item.tries_used, 1)
        self.assertEqual(item.tries[0]["parts"]["gaps"], {"1": True, "2": False, "3": True})
        self.assertEqual(item.tries[0]["display"], "(1) went; (2) saw; (3) met")

    def test_empty_submission_does_not_spend_try(self):
        response = self.check({"1": "", "2": "", "3": ""})
        self.assertContains(response, "Сначала ответьте")
        self.assertFalse(QuestionResponse.objects.exists())

    def test_one_filled_gap_is_enough_to_check(self):
        self.check({"1": "went"})
        item = QuestionResponse.objects.get(student=self.student, question=self.question)
        self.assertEqual(item.tries_used, 1)
        self.assertEqual(item.points, 0)

    def test_after_last_try_gaps_are_marked_and_answers_revealed(self):
        for attempt in ({"1": "go", "2": "see", "3": "meet"},) * 2:
            self.check(attempt)
        response = self.check({"1": "gone", "2": "seen", "3": "met"})
        # Разбор — тот же текст: неверные пропуски помечены, верный — галочкой,
        # а правильный вариант озвучен для экранного диктора.
        self.assertContains(response, "cloze-gap-ok")
        self.assertContains(response, "cloze-gap-bad")
        self.assertContains(response, "правильно: went")
        self.assertContains(response, "Правильный ответ:")
        item = QuestionResponse.objects.get(student=self.student, question=self.question)
        self.assertEqual(item.state, QuestionResponse.State.FAILED)
        self.assertEqual(item.points, 2, "Один верный пропуск из трёх при 6 баллах")

    def test_last_item_finishes_assignment(self):
        self.check({"1": "went", "2": "didn't see", "3": "met"})
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        self.assertEqual(attempt.status, Submission.Status.CHECKED)
        self.assertEqual(attempt.quiz_attempt.score, 6)

    def test_teacher_preview_shows_number_of_gaps(self):
        page = self.teacher_client.get(f"/teacher/curriculum/assignments/{self.quiz.pk}/preview/")
        self.assertEqual(page.status_code, 200)

    def test_review_page_marks_each_gap(self):
        self.check({"1": "went", "2": "saw", "3": "met"})
        self.check({"1": "went", "2": "didn't see", "3": "met"})
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        page = self.teacher_client.get(f"/teacher/review/{attempt.pk}/")
        self.assertContains(page, "Верно заполнено 3 из 3")


class ClozeExamModeTests(LMSCase):
    """Контрольная: одна попытка, результат и разбор — после завершения задания."""

    def setUp(self):
        super().setUp()
        self.quiz, self.question = _make_cloze_quiz(self, exam=True)
        # Второй пункт держит задание открытым: иначе единственный пункт
        # сразу завершает работу и разбор открывается по правилам контрольной.
        self.second = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.GAP, text="She ___ here.", points=1, order=2
        )
        Choice.objects.create(question=self.second, text="lives", is_correct=True)

    def quiz_items(self):
        from lms.quiz_items import build_items

        return {item["question"].pk: item for item in build_items(self.quiz, self.student)}

    def check(self, values, *, htmx=True):
        data = {f"q_{self.question.pk}_gap_{n}": value for n, value in values.items()}
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.student_client.post(
            f"/assignments/{self.quiz.pk}/items/{self.question.pk}/check/", data, **headers
        )

    def test_result_is_sealed_after_first_answer(self):
        response = self.check({"1": "went", "2": "saw", "3": "met"})
        self.assertContains(response, "Верно или нет — увидите после завершения задания")
        # Ни одного признака правильности в разметке: ни отметок у пропусков,
        # ни эталона, ни числа верных пропусков.
        self.assertNotContains(response, "cloze-gap-bad")
        self.assertNotContains(response, "cloze-gap-ok")
        self.assertNotContains(response, "Правильный ответ")
        self.assertNotContains(response, "Верно заполнено")
        # Ответ при этом сохранён в попытке, но без показа ученику.
        response_item = QuestionResponse.objects.get(student=self.student, question=self.question)
        self.assertEqual(response_item.tries[0]["given"]["1"], "went")
        self.assertNotIn('name="gap_', response.content.decode())

    def test_second_attempt_is_rejected(self):
        self.check({"1": "went", "2": "saw", "3": "met"})
        blocked = self.check({"1": "went", "2": "didn't see", "3": "met"})
        self.assertEqual(blocked.status_code, 409)


class ClozeWithoutJsTests(LMSCase):
    """Без htmx форма отправляется обычным POST и возвращает к пункту."""

    def setUp(self):
        super().setUp()
        self.quiz, self.question = _make_cloze_quiz(self)

    def test_plain_post_redirects_to_item_anchor(self):
        data = {
            f"q_{self.question.pk}_gap_{n}": v
            for n, v in {"1": "went", "2": "didn't see", "3": "met"}.items()
        }
        response = self.student_client.post(
            f"/assignments/{self.quiz.pk}/items/{self.question.pk}/check/", data
        )
        self.assertRedirects(
            response,
            f"/assignments/{self.quiz.pk}/#q-{self.question.pk}",
            fetch_redirect_response=False,
        )


class ClozeTeacherViewsTests(LMSCase):
    """Учительские списки показывают текст без служебных скобок ``[[n]]``."""

    def setUp(self):
        super().setUp()
        self.quiz, self.question = _make_cloze_quiz(self)
        Submission.objects.create(
            student=self.student,
            assignment=self.quiz,
            status=Submission.Status.SUBMITTED,
            submitted_at=timezone.now(),
            max_points_snapshot=self.quiz.max_points,
        )

    def page(self, path):
        response = self.teacher_client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_questions_page_shows_blank_instead_of_marker(self):
        html = self.page(f"/teacher/curriculum/assignments/{self.quiz.pk}/questions/")
        self.assertNotRegex(html, r"\[\[\s*\d+\s*\]\]")
        self.assertIn("______", html)

    def test_topic_board_shows_blank_instead_of_marker(self):
        html = self.page(f"/teacher/curriculum/topics/{self.topic.pk}/board/")
        self.assertNotRegex(html, r"\[\[\s*\d+\s*\]\]")
        self.assertIn("______", html)

    def test_results_matrix_header_and_export_are_clean(self):
        html = self.page(f"/teacher/curriculum/assignments/{self.quiz.pk}/results/")
        self.assertNotRegex(html, r"\[\[\s*\d+\s*\]\]")
        response = self.teacher_client.get(
            f"/teacher/curriculum/assignments/{self.quiz.pk}/results/export.xlsx"
        )
        self.assertEqual(response.status_code, 200)
        blob = b"".join(response.streaming_content) if response.streaming else response.content
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            xml = "".join(
                archive.read(name).decode() for name in archive.namelist() if name.endswith(".xml")
            )
        self.assertNotRegex(xml, r"\[\[\s*\d+\s*\]\]")
        self.assertIn("______", xml)

    def test_preview_marks_all_gaps_as_correct(self):
        html = self.page(f"/teacher/curriculum/assignments/{self.quiz.pk}/preview/?answers=1")
        self.assertEqual(html.count("cloze-gap cloze-gap-ok"), 3)
        self.assertNotIn("cloze-gap-bad", html)
