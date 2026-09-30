"""Новые возможности проверки: балл необязателен, подсветка, счётчик слов,
несколько голосовых комментариев и проверка ИИ текстовых/аудио ответов.
"""

import io
import json
import wave
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from lms import ai
from lms.highlights import parse_submitted_highlights, render_highlighted_html
from lms.models import Assignment, Feedback, FeedbackAudioComment, FeedbackHighlight
from lms.templatetags.lms_tags import highlighted_answer, word_count

from .base import LMSCase


def _wav_file(name="clip.wav"):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    return SimpleUploadedFile(name, stream.getvalue(), "audio/wav")


class WordCountFilterTests(SimpleTestCase):
    def test_counts_whitespace_separated_words(self):
        self.assertEqual(word_count("One two three"), 3)

    def test_empty_text_is_zero(self):
        self.assertEqual(word_count(""), 0)
        self.assertEqual(word_count(None), 0)

    def test_extra_whitespace_is_ignored(self):
        self.assertEqual(word_count("  One   two\n\nthree  "), 3)


class HighlightedAnswerFilterTests(LMSCase):
    def test_returns_plain_text_without_feedback(self):
        attempt = self.submit(text_answer="Hello world")
        html = highlighted_answer(attempt)
        self.assertEqual(str(html), "Hello world")

    def test_wraps_highlighted_span_from_feedback(self):
        attempt = self.submit(text_answer="Hello brave world")
        feedback = self.review(attempt, grade=90)
        FeedbackHighlight.objects.create(
            feedback=feedback,
            start=6,
            end=11,
            quote="brave",
            comment="Хорошее слово",
            source="teacher",
        )
        html = str(highlighted_answer(attempt))
        self.assertIn('class="answer-highlight answer-highlight--teacher"', html)
        self.assertIn("brave", html)
        self.assertIn("Хорошее слово", html)


class GradeOptionalWorkflowTests(LMSCase):
    def test_review_without_grade_saves_and_flags_warning(self):
        attempt = self.submit()
        response = self.review_post(attempt, grade="")
        self.assertEqual(response.status_code, 302)
        feedback = Feedback.objects.get(submission=attempt)
        self.assertIsNone(feedback.grade)
        self.assertTrue(feedback.grade_missing)

    def test_review_with_grade_is_not_flagged(self):
        attempt = self.submit()
        self.review_post(attempt, grade=70)
        feedback = Feedback.objects.get(submission=attempt)
        self.assertEqual(feedback.grade, 70)
        self.assertFalse(feedback.grade_missing)


class InlineHighlightSubmitTests(LMSCase):
    def test_teacher_can_attach_highlight_via_review_form(self):
        attempt = self.submit(text_answer="This is a sample answer.")
        highlights = json.dumps(
            [{"start": 0, "end": 4, "comment": "Хорошее начало", "source": "teacher"}]
        )
        response = self.teacher_client.post(
            reverse("teacher_submission_review", args=[attempt.pk]),
            {
                "expected_version": attempt.version,
                "expected_review_revision": attempt.review_revision,
                "grade": 80,
                "comment": "Общий комментарий",
                "decision": "checked",
                "highlights_json": highlights,
                "remove_audio_comment_ids": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        feedback = Feedback.objects.get(submission=attempt)
        saved = list(feedback.highlights.all())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].quote, "This")
        self.assertEqual(saved[0].comment, "Хорошее начало")

    def test_student_sees_highlight_rendered_in_answer(self):
        attempt = self.submit(text_answer="This is a sample answer.")
        feedback = self.review(attempt, grade=80)
        FeedbackHighlight.objects.create(
            feedback=feedback,
            start=0,
            end=4,
            quote="This",
            comment="Хорошее начало",
            source="teacher",
        )
        response = self.student_client.get(self.url)
        self.assertContains(response, "answer-highlight")
        self.assertContains(response, "Хорошее начало")

    def test_saving_review_without_touching_highlights_keeps_existing_ones(self):
        """Пустое поле highlights_json не должно стирать уже сохранённые подсказки."""
        attempt = self.submit(text_answer="This is a sample answer.")
        feedback = self.review(attempt, grade=80)
        FeedbackHighlight.objects.create(
            feedback=feedback,
            start=0,
            end=4,
            quote="This",
            comment="Есть комментарий",
            source="teacher",
        )
        attempt.refresh_from_db()
        response = self.teacher_client.post(
            reverse("teacher_submission_review", args=[attempt.pk]),
            {
                "expected_version": attempt.version,
                "expected_review_revision": attempt.review_revision,
                "grade": 85,
                "comment": "Обновили балл",
                "decision": "checked",
                "highlights_json": json.dumps(
                    [{"start": 0, "end": 4, "comment": "Есть комментарий", "source": "teacher"}]
                ),
                "remove_audio_comment_ids": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        feedback.refresh_from_db()
        self.assertEqual(feedback.highlights.count(), 1)


class HighlightsHelperTests(SimpleTestCase):
    def test_render_highlighted_html_escapes_and_wraps(self):
        html = render_highlighted_html(
            "A <b> tag & text",
            [{"start": 2, "end": 5, "comment": "note", "source": "teacher"}],
        )
        self.assertIn("&lt;b&gt;", str(html))
        self.assertIn("answer-highlight", str(html))

    def test_parse_submitted_highlights_drops_overlaps(self):
        raw = json.dumps(
            [
                {"start": 0, "end": 5, "comment": "first"},
                {"start": 2, "end": 8, "comment": "overlaps, should drop"},
                {"start": 6, "end": 10, "comment": "second"},
            ]
        )
        parsed = parse_submitted_highlights("Hello world foo", raw)
        self.assertEqual(len(parsed), 2)


class MultiAudioCommentTests(LMSCase):
    def test_teacher_can_attach_several_audio_comments_in_one_review(self):
        attempt = self.submit()
        response = self.teacher_client.post(
            reverse("teacher_submission_review", args=[attempt.pk]),
            {
                "expected_version": attempt.version,
                "expected_review_revision": attempt.review_revision,
                "grade": 80,
                "comment": "Общий комментарий",
                "decision": "checked",
                "remove_audio_comment_ids": "",
                "audio_comments": [_wav_file("a1.wav"), _wav_file("a2.wav")],
            },
        )
        self.assertEqual(response.status_code, 302)
        feedback = Feedback.objects.get(submission=attempt)
        self.assertEqual(feedback.audio_comments.count(), 2)
        orders = list(feedback.audio_comments.order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [0, 1])

    def test_teacher_can_remove_one_of_several_existing_audio_comments(self):
        attempt = self.submit()
        feedback = self.review(attempt, grade=80)
        first = FeedbackAudioComment.objects.create(
            feedback=feedback, audio=_wav_file("a1.wav"), order=0
        )
        second = FeedbackAudioComment.objects.create(
            feedback=feedback, audio=_wav_file("a2.wav"), order=1
        )
        attempt.refresh_from_db()

        response = self.teacher_client.post(
            reverse("teacher_submission_review", args=[attempt.pk]),
            {
                "expected_version": attempt.version,
                "expected_review_revision": attempt.review_revision,
                "grade": 80,
                "comment": "Общий комментарий",
                "decision": "checked",
                "remove_audio_comment_ids": str(first.pk),
            },
        )
        self.assertEqual(response.status_code, 302)
        remaining = list(feedback.audio_comments.values_list("pk", flat=True))
        self.assertEqual(remaining, [second.pk])

    def test_student_sees_all_audio_comments(self):
        attempt = self.submit()
        feedback = self.review(attempt, grade=80)
        FeedbackAudioComment.objects.create(feedback=feedback, audio=_wav_file("a1.wav"), order=0)
        FeedbackAudioComment.objects.create(feedback=feedback, audio=_wav_file("a2.wav"), order=1)
        response = self.student_client.get(self.url)
        self.assertEqual(response.content.decode().count('class="feedback-audio"'), 2)


@override_settings(
    LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="ollama", LMS_AI_LOCAL=True
)
class AiGradeTextViewTests(LMSCase):
    def test_teacher_gets_grade_and_comment_from_ai(self):
        attempt = self.submit(text_answer="This is a sample answer with several words.")
        payload = {
            "grade": 8,
            "comment": "Хорошая работа, но есть ошибки.",
            "criteria_note": "Оценено по общим критериям.",
            "highlights": [{"quote": "This", "comment": "Начало предложения"}],
        }
        with patch("lms.ai._provider_material", return_value=payload):
            response = self.teacher_client.post(
                reverse("teacher_review_ai_grade_text", args=[attempt.pk])
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["grade"], 8)
        self.assertIn("Хорошая работа", data["comment"])
        self.assertEqual(len(data["highlights"]), 1)

    def test_student_cannot_call_ai_grade_endpoint(self):
        attempt = self.submit(text_answer="Something to grade.")
        response = self.student_client.post(
            reverse("teacher_review_ai_grade_text", args=[attempt.pk])
        )
        self.assertIn(response.status_code, (302, 403))

    def test_ai_error_is_reported_as_json_error(self):
        attempt = self.submit(text_answer="Something to grade.")
        with patch("lms.ai._provider_material", side_effect=ai.AiError("Модель недоступна")):
            response = self.teacher_client.post(
                reverse("teacher_review_ai_grade_text", args=[attempt.pk])
            )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertIn("Модель недоступна", data["error"])

    def test_ai_grade_requires_online_mode(self):
        attempt = self.submit(text_answer="Something to grade.")
        with override_settings(LMS_AI_ENABLED=False):
            response = self.teacher_client.post(
                reverse("teacher_review_ai_grade_text", args=[attempt.pk])
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])


@override_settings(
    LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="ollama", LMS_AI_LOCAL=True
)
class AiGradeAudioViewTests(LMSCase):
    def _audio_assignment(self):
        return Assignment.objects.create(
            topic=self.topic,
            title="Speaking task",
            description="Say something.",
            assignment_type=Assignment.Type.AUDIO,
            max_points=10,
        )

    def test_teacher_gets_grade_from_ai_for_audio_submission(self):
        assignment = self._audio_assignment()
        attempt = self.submit(
            assignment_id=assignment.pk, text_answer="", file_answer=_wav_file("answer.wav")
        )
        payload = {
            "grade": 7,
            "comment": "Понятная речь, но есть акцент.",
            "criteria_note": "Общие критерии устного ответа.",
            "transcript": "Hello, this is my answer.",
        }
        with patch("lms.ai._provider_material", return_value=payload):
            response = self.teacher_client.post(
                reverse("teacher_review_ai_grade_audio", args=[attempt.pk])
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["grade"], 7)
        self.assertIn("акцент", data["comment"])

    def test_no_audio_file_returns_error(self):
        # Обычная текстовая попытка без аудиофайла — эндпоинт должен вежливо отказать.
        attempt = self.submit(text_answer="No audio here.")
        response = self.teacher_client.post(
            reverse("teacher_review_ai_grade_audio", args=[attempt.pk])
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("аудиофайла", response.json()["error"])
