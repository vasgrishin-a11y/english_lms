"""Автоматическая проверка тестов: чистые функции оценивания.

Здесь закреплены методические решения: одиночный выбор и пропуск — всё или
ничего, несколько вариантов и соответствие — честный частичный балл.
"""

from lms.models import Assignment, Choice, Question
from lms.scoring import answers_summary, normalize_gap, score_question, score_quiz

from .base import LMSCase


class NormalizeGapTests(LMSCase):
    def test_ignores_case_punctuation_and_extra_spaces(self):
        for value in ["  London! ", "london", "London...", '  "London"  ']:
            with self.subTest(value=value):
                self.assertEqual(normalize_gap(value), "london")
        # лишние пробелы схлопываются, но одиночные разделители остаются
        self.assertEqual(normalize_gap("L O N D O N"), "l o n d o n")

    def test_treats_yo_as_ye(self):
        self.assertEqual(normalize_gap("Ёлка"), normalize_gap("елка"))

    def test_handles_empty_and_none(self):
        self.assertEqual(normalize_gap(""), "")
        self.assertEqual(normalize_gap(None), "")


class ScoreQuestionTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()

    def question(self, kind, points=2, choices=()):
        question = Question.objects.create(
            assignment=self.assignment, kind=kind, text="Prompt", points=points
        )
        Choice.objects.bulk_create(
            [
                Choice(
                    question=question,
                    text=text,
                    match_text=match_text,
                    is_correct=correct,
                    order=index,
                )
                for index, (text, match_text, correct) in enumerate(choices, start=1)
            ]
        )
        return question

    def test_single_choice_is_all_or_nothing(self):
        question = self.question(
            Question.Kind.MCQ, choices=[("have finished", "", True), ("finished", "", False)]
        )
        correct = question.choices.get(text="have finished")
        wrong = question.choices.get(text="finished")
        hit = score_question(question, str(correct.pk))
        miss = score_question(question, str(wrong.pk))
        self.assertEqual((hit["points"], hit["correct"], hit["ratio"]), (2, True, 1.0))
        self.assertEqual((miss["points"], miss["correct"], miss["ratio"]), (0, False, 0.0))
        self.assertEqual(hit["expected"], "have finished")
        self.assertEqual(score_question(question, None)["points"], 0)

    def test_single_choice_without_correct_option_scores_zero(self):
        question = self.question(Question.Kind.MCQ, choices=[("a", "", False), ("b", "", False)])
        self.assertEqual(score_question(question, str(question.choices.first().pk))["points"], 0)

    def test_multiple_choice_awards_partial_credit(self):
        question = self.question(
            Question.Kind.MULTI,
            points=4,
            choices=[("yet", "", True), ("since", "", True), ("yesterday", "", False)],
        )
        correct = [str(choice.pk) for choice in question.choices.filter(is_correct=True)]
        wrong = str(question.choices.get(text="yesterday").pk)
        full = score_question(question, correct)
        half = score_question(question, correct[:1])
        penalty = score_question(question, [*correct, wrong])
        self.assertEqual(full["points"], 4)
        self.assertEqual(half["points"], 2)
        self.assertTrue(half["partially"])
        self.assertFalse(half["correct"])
        # два верных и один лишний: (2 − 1) / 2 = 0.5 от четырёх баллов
        self.assertEqual(penalty["points"], 2)

    def test_multiple_choice_never_goes_below_zero(self):
        question = self.question(
            Question.Kind.MULTI,
            points=4,
            choices=[("a", "", True), ("b", "", False), ("c", "", False)],
        )
        wrong = [str(choice.pk) for choice in question.choices.filter(is_correct=False)]
        self.assertEqual(score_question(question, wrong)["points"], 0)

    def test_multiple_choice_ignores_foreign_ids(self):
        question = self.question(Question.Kind.MULTI, choices=[("a", "", True), ("b", "", False)])
        answer = [str(question.choices.get(text="a").pk), "999999", ""]
        self.assertEqual(score_question(question, answer)["points"], 2)

    def test_gap_accepts_any_listed_variant_and_normalizes(self):
        question = self.question(
            Question.Kind.GAP, choices=[("has lived", "", True), ("have lived", "", True)]
        )
        self.assertEqual(score_question(question, "  HAS LIVED! ")["points"], 2)
        self.assertEqual(score_question(question, "lived")["points"], 0)
        self.assertIn("have lived", score_question(question, "x")["expected"])

    def test_gap_without_marked_answer_accepts_all_variants(self):
        question = self.question(Question.Kind.GAP, choices=[("London", "", False)])
        self.assertEqual(score_question(question, "london")["points"], 2)

    def test_gap_with_only_empty_variants_scores_zero(self):
        question = self.question(Question.Kind.GAP, choices=[("   ", "", True)])
        self.assertEqual(score_question(question, "")["points"], 0)

    def test_match_counts_correct_pairs(self):
        question = self.question(
            Question.Kind.MATCH,
            points=3,
            choices=[
                ("look after", "заботиться", True),
                ("give up", "бросить", True),
                ("put off", "отложить", True),
            ],
        )
        pairs = list(question.choices.all())
        full = {str(choice.pk): str(choice.pk) for choice in pairs}
        partial = {str(pairs[0].pk): str(pairs[0].pk)}
        rotated = {
            str(choice.pk): str(pairs[(index + 1) % len(pairs)].pk)
            for index, choice in enumerate(pairs)
        }
        self.assertEqual(score_question(question, full)["points"], 3)
        self.assertEqual(score_question(question, partial)["points"], 1)
        self.assertTrue(score_question(question, partial)["partially"])
        self.assertEqual(score_question(question, rotated)["points"], 0)
        self.assertFalse(score_question(question, rotated)["correct"])

    def test_match_requires_mapping_payload(self):
        question = self.question(Question.Kind.MATCH, points=2, choices=[("a", "b", True)])
        self.assertEqual(score_question(question, "not-a-mapping")["points"], 0)
        self.assertEqual(score_question(question, None)["points"], 0)

    def test_unknown_kind_scores_zero_without_crash(self):
        question = self.question("weird", choices=[("a", "", True)])
        result = score_question(question, "a")
        self.assertEqual(result["points"], 0)
        self.assertFalse(result["correct"])


class ScoreQuizTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()
        self.first = Question.objects.create(
            assignment=self.assignment, kind=Question.Kind.MCQ, text="One", points=2, order=1
        )
        self.second = Question.objects.create(
            assignment=self.assignment, kind=Question.Kind.GAP, text="Two", points=3, order=2
        )
        self.right = Choice.objects.create(question=self.first, text="yes", is_correct=True)
        self.wrong = Choice.objects.create(question=self.first, text="no", is_correct=False)
        Choice.objects.create(question=self.second, text="London", is_correct=True)

    def test_aggregates_score_and_counts(self):
        result = score_quiz(
            [self.first, self.second],
            {str(self.first.pk): str(self.right.pk), str(self.second.pk): "london"},
        )
        self.assertEqual(result["score"], 5)
        self.assertEqual(result["max_score"], 5)
        self.assertEqual(result["correct_count"], 2)
        self.assertEqual(result["total_count"], 2)
        self.assertTrue(result["details"][str(self.first.pk)]["correct"])

    def test_missing_answers_are_zero_but_counted(self):
        result = score_quiz([self.first, self.second], {})
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["max_score"], 5)
        self.assertEqual(result["correct_count"], 0)
        self.assertEqual(len(result["details"]), 2)

    def test_tolerates_none_payload(self):
        self.assertEqual(score_quiz([self.first], None)["score"], 0)

    def test_answers_summary_is_human_readable_and_bounded(self):
        result = score_quiz([self.first, self.second], {str(self.first.pk): str(self.wrong.pk)})
        summary = answers_summary([self.first, self.second], result)
        self.assertIn("Автоматическая проверка теста", summary)
        self.assertIn("Верных ответов: 0 из 2", summary)
        self.assertIn("[✗] One", summary)
        self.assertIn("0/2", summary)
        self.assertLessEqual(len(summary), 20000)

    def test_answers_summary_shows_partial_mark(self):
        multi = Question.objects.create(
            assignment=self.assignment, kind=Question.Kind.MULTI, text="Multi", points=4, order=3
        )
        first = Choice.objects.create(question=multi, text="a", is_correct=True, order=1)
        Choice.objects.create(question=multi, text="b", is_correct=True, order=2)
        result = score_quiz([multi], {str(multi.pk): [str(first.pk)]})
        self.assertIn("[~] Multi", answers_summary([multi], result))
