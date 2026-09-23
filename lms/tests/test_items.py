"""Пошаговая проверка пунктов: «Принять» → ✓/✗, три попытки, голосовые ответы, лимит записи."""

import io
import wave

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from lms.forms import AssignmentForm, DurationLimitField, QuestionForm
from lms.models import Assignment, Choice, Question, QuestionResponse, Submission

from .base import LMSCase


def wav_bytes(seconds, rate=16000):
    """Настоящий WAV нужной длины: сервер проверяет длительность по заголовку."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def wav_upload(seconds, name="answer.wav"):
    return SimpleUploadedFile(name, wav_bytes(seconds), content_type="audio/wav")


class ItemFlowBase(LMSCase):
    def setUp(self):
        super().setUp()
        self.quiz = Assignment.objects.create(
            topic=self.topic,
            title="Items",
            description="Answer each item.",
            assignment_type=Assignment.Type.QUIZ,
            max_points=0,
            order=5,
        )
        self.mcq = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.MCQ, text="I ___ done.", points=2, order=1
        )
        self.right = Choice.objects.create(question=self.mcq, text="have", is_correct=True)
        self.wrong = Choice.objects.create(question=self.mcq, text="has")
        self.gap = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.GAP, text="She ___ here.", points=3, order=2
        )
        Choice.objects.create(question=self.gap, text="lives", is_correct=True)
        Choice.objects.create(question=self.gap, text="is living", is_correct=True)
        self.quiz.max_points = 5
        self.quiz.save()
        self.quiz_url = f"/assignments/{self.quiz.pk}/"

    def check(self, question, value, *, htmx=True, round_=None):
        data = {f"q_{question.pk}": value}
        if round_ is not None:
            data["round"] = round_
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.student_client.post(
            f"/assignments/{self.quiz.pk}/items/{question.pk}/check/", data, **headers
        )

    def response_for(self, question):
        return QuestionResponse.objects.get(student=self.student, question=question)


class ItemCheckTests(ItemFlowBase):
    def test_correct_answer_shows_check_and_full_points(self):
        response = self.check(self.mcq, str(self.right.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Верно")
        self.assertContains(response, 'id="quiz-progress"')
        item = self.response_for(self.mcq)
        self.assertEqual(item.state, QuestionResponse.State.CORRECT)
        self.assertEqual(item.points, 2)
        self.assertFalse(Submission.objects.exists(), "Ещё не все пункты закрыты")

    def test_wrong_answer_spends_try_and_hides_solution(self):
        response = self.check(self.gap, "live")
        self.assertContains(response, "Осталось попыток: 2")
        self.assertNotContains(response, "is living")
        item = self.response_for(self.gap)
        self.assertEqual(item.state, QuestionResponse.State.OPEN)
        self.assertEqual(item.tries_used, 1)

    def test_blank_answer_does_not_spend_try(self):
        response = self.check(self.gap, "   ")
        self.assertContains(response, "Сначала ответьте")
        self.assertFalse(
            QuestionResponse.objects.filter(question=self.gap).exclude(tries=[]).exists()
        )

    def test_three_wrong_tries_close_item_and_reveal_answer(self):
        self.check(self.gap, "a")
        self.check(self.gap, "b")
        response = self.check(self.gap, "c")
        self.assertContains(response, "lives")
        item = self.response_for(self.gap)
        self.assertEqual(item.state, QuestionResponse.State.FAILED)
        self.assertEqual(item.points, 0)
        blocked = self.check(self.gap, "lives")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.response_for(self.gap).tries_used, 3)

    def test_attempt_number_does_not_reduce_points(self):
        self.check(self.gap, "wrong")
        self.check(self.gap, "also wrong")
        self.check(self.gap, "is living")
        item = self.response_for(self.gap)
        self.assertEqual(item.state, QuestionResponse.State.CORRECT)
        self.assertEqual(item.points, 3)

    def test_teacher_sets_number_of_tries(self):
        self.quiz.max_tries = 1
        self.quiz.save()
        self.check(self.mcq, str(self.wrong.pk))
        self.assertEqual(self.response_for(self.mcq).state, QuestionResponse.State.FAILED)

    def test_last_item_creates_checked_submission_with_history(self):
        self.check(self.mcq, str(self.wrong.pk))
        self.check(self.mcq, str(self.right.pk))
        self.check(self.gap, "lives")
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        self.assertEqual(attempt.status, Submission.Status.CHECKED)
        self.assertEqual(attempt.feedback.grade, 5)
        self.assertEqual(attempt.quiz_attempt.correct_count, 2)
        self.assertEqual(
            QuestionResponse.objects.filter(submission=attempt).count(), 2, "Пункты привязаны"
        )
        page = self.teacher_client.get(f"/teacher/review/{attempt.pk}/")
        self.assertContains(page, "Верно с 2-й попытки")
        self.assertContains(page, "has", msg_prefix="Неверная попытка видна преподавателю")

    def test_progress_survives_reload(self):
        self.check(self.gap, "wrong")
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "Осталось попыток: 2")
        self.assertContains(page, "Выполнено 0 из 2")

    def test_without_js_redirects_to_item_anchor(self):
        response = self.check(self.mcq, str(self.right.pk), htmx=False)
        self.assertRedirects(
            response, f"{self.quiz_url}#q-{self.mcq.pk}", fetch_redirect_response=False
        )

    def test_stale_round_is_conflict(self):
        self.check(self.mcq, str(self.right.pk))
        self.check(self.gap, "lives")
        response = self.check(self.mcq, str(self.right.pk), round_=1)
        self.assertEqual(response.status_code, 409)

    def test_foreign_question_is_not_found(self):
        other = Assignment.objects.create(
            topic=self.topic, title="Other", description="x", assignment_type="quiz"
        )
        response = self.student_client.post(
            f"/assignments/{other.pk}/items/{self.mcq.pk}/check/", {"round": 1}
        )
        self.assertEqual(response.status_code, 404)

    def test_teacher_cannot_check_items(self):
        response = self.teacher_client.post(
            f"/assignments/{self.quiz.pk}/items/{self.mcq.pk}/check/", {}
        )
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(QuestionResponse.objects.exists())

    def test_retake_only_when_allowed(self):
        self.check(self.mcq, str(self.right.pk))
        self.check(self.gap, "lives")
        denied = self.student_client.post(f"/assignments/{self.quiz.pk}/retake/")
        self.assertEqual(denied.status_code, 404)
        self.quiz.allow_retake = True
        self.quiz.save()
        self.student_client.post(f"/assignments/{self.quiz.pk}/retake/")
        response = self.check(self.mcq, str(self.right.pk), round_=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(QuestionResponse.objects.filter(round=2, question=self.mcq).count(), 1)

    def test_item_results_matrix(self):
        self.check(self.mcq, str(self.wrong.pk))
        self.check(self.mcq, str(self.right.pk))
        self.check(self.gap, "lives")
        page = self.teacher_client.get(f"/teacher/curriculum/assignments/{self.quiz.pk}/results/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "student")
        self.assertContains(page, "<sup>2</sup>", html=False)
        self.assertEqual(page.context["columns"][0]["rate"], 100)


class ManualItemTests(ItemFlowBase):
    def setUp(self):
        super().setUp()
        self.voice = Question.objects.create(
            assignment=self.quiz,
            kind=Question.Kind.VOICE,
            text="Tell about your weekend.",
            points=5,
            order=3,
            recording_limit_seconds=20,
        )
        self.essay = Question.objects.create(
            assignment=self.quiz, kind=Question.Kind.TEXT, text="Write 3 lines.", points=4, order=4
        )

    def answer(self, question, *, text="", file=None):
        data = {f"q_{question.pk}": text}
        if file is not None:
            data[f"q_{question.pk}_file"] = file
        return self.student_client.post(
            f"/assignments/{self.quiz.pk}/items/{question.pk}/answer/",
            data,
            HTTP_HX_REQUEST="true",
        )

    def finish_auto(self):
        self.check(self.mcq, str(self.right.pk))
        self.check(self.gap, "lives")

    def test_page_shows_recorder_with_item_limit(self):
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "data-recorder")
        self.assertContains(page, 'data-limit="20"')

    def test_voice_answer_is_saved_and_can_be_replaced(self):
        self.answer(self.voice, file=wav_upload(3))
        first = self.response_for(self.voice).file_answer.name
        self.assertTrue(first.endswith(".wav"))
        self.student_client.post(
            f"/assignments/{self.quiz.pk}/items/{self.voice.pk}/reset/", HTTP_HX_REQUEST="true"
        )
        reset = self.response_for(self.voice)
        self.assertEqual(reset.state, QuestionResponse.State.OPEN)
        self.assertFalse(reset.file_answer)
        self.answer(self.voice, file=wav_upload(4))
        again = self.response_for(self.voice)
        self.assertEqual(again.state, QuestionResponse.State.ANSWERED)
        self.assertNotEqual(again.file_answer.name, first)

    def test_recording_over_limit_is_rejected(self):
        response = self.answer(self.voice, file=wav_upload(30))
        self.assertContains(response, "Запись длиннее лимита")
        item = QuestionResponse.objects.filter(question=self.voice).first()
        self.assertTrue(item is None or not item.file_answer)

    def test_non_audio_is_rejected_for_voice(self):
        upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        response = self.answer(self.voice, file=upload)
        self.assertContains(response, "аудио", status_code=200)

    def test_manual_items_wait_for_explicit_send(self):
        self.finish_auto()
        self.answer(self.voice, file=wav_upload(2))
        self.answer(self.essay, text="Line one.\nLine two.")
        self.assertFalse(Submission.objects.exists(), "До нажатия «Отправить» запись можно менять")
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "Отправить преподавателю")
        self.student_client.post(f"/assignments/{self.quiz.pk}/finish/", {"round": 1})
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        self.assertEqual(attempt.status, Submission.Status.SUBMITTED)
        self.assertEqual(attempt.quiz_attempt.score, 5)

    def test_finish_requires_all_items(self):
        self.finish_auto()
        self.student_client.post(f"/assignments/{self.quiz.pk}/finish/", {"round": 1})
        self.assertFalse(Submission.objects.exists())

    def test_teacher_grades_manual_items_and_total_is_summed(self):
        self.finish_auto()
        self.answer(self.voice, file=wav_upload(2))
        self.answer(self.essay, text="Short text.")
        self.student_client.post(f"/assignments/{self.quiz.pk}/finish/", {"round": 1})
        attempt = Submission.objects.get()
        page = self.teacher_client.get(f"/teacher/review/{attempt.pk}/")
        self.assertContains(page, "<audio", html=False)
        self.assertContains(page, "Short text.")
        voice = self.response_for(self.voice)
        essay = self.response_for(self.essay)
        response = self.teacher_client.post(
            f"/teacher/review/{attempt.pk}/",
            {
                "expected_version": attempt.version,
                "expected_review_revision": attempt.review_revision,
                "grade": "",
                "comment": "Good job",
                "decision": Submission.Status.CHECKED,
                f"item_{voice.pk}": 4,
                f"item_{essay.pk}": 3,
            },
        )
        self.assertIn(response.status_code, (200, 302))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Submission.Status.CHECKED)
        self.assertEqual(attempt.feedback.grade, 5 + 4 + 3)
        voice.refresh_from_db()
        self.assertEqual(voice.teacher_points, 4)

    def test_item_points_above_maximum_are_rejected(self):
        self.finish_auto()
        self.answer(self.voice, file=wav_upload(2))
        self.answer(self.essay, text="Short text.")
        self.student_client.post(f"/assignments/{self.quiz.pk}/finish/", {"round": 1})
        attempt = Submission.objects.get()
        voice = self.response_for(self.voice)
        essay = self.response_for(self.essay)
        self.teacher_client.post(
            f"/teacher/review/{attempt.pk}/",
            {
                "expected_version": attempt.version,
                "expected_review_revision": attempt.review_revision,
                "grade": "",
                "comment": "",
                "decision": Submission.Status.CHECKED,
                f"item_{voice.pk}": 50,
                f"item_{essay.pk}": 1,
            },
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Submission.Status.SUBMITTED)

    def test_student_hears_own_recording_but_not_others(self):
        self.answer(self.voice, file=wav_upload(2))
        name = self.response_for(self.voice).file_answer.name
        own = self.student_client.get(f"/preview/{name}")
        self.assertEqual(own.status_code, 200)
        other = self.client_for(self.other).get(f"/preview/{name}")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(self.teacher_client.get(f"/preview/{name}").status_code, 200)

    @override_settings(LMS_STUDENT_QUOTA_BYTES=10)
    def test_quota_applies_to_voice_answers(self):
        response = self.answer(self.voice, file=wav_upload(2))
        self.assertContains(response, "Квота")


class RecordingLimitFormTests(LMSCase):
    def test_duration_field_converts_minutes_and_seconds(self):
        field = DurationLimitField(required=False)
        self.assertEqual(field.clean(["2", "min"]), 120)
        self.assertEqual(field.clean(["45", "sec"]), 45)
        self.assertIsNone(field.clean(["", "min"]))

    def test_duration_field_enforces_range(self):
        field = DurationLimitField(required=False)
        for value in (["5", "sec"], ["11", "min"]):
            with self.subTest(value=value), self.assertRaises(Exception):
                field.clean(value)

    def test_assignment_form_saves_limit(self):
        form = AssignmentForm(
            data={
                "topic": self.topic.pk,
                "title": "Speak",
                "description": "Talk for a minute.",
                "assignment_type": Assignment.Type.AUDIO,
                "max_points": 10,
                "order": 1,
                "status": Assignment.Publication.PUBLISHED,
                "is_active": "on",
                "recording_limit_seconds_0": "90",
                "recording_limit_seconds_1": "sec",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["recording_limit_seconds"], 90)

    def test_question_form_keeps_limit_only_for_voice(self):
        voice = QuestionForm(
            data={
                "kind": Question.Kind.VOICE,
                "text": "Describe the picture",
                "points": 3,
                "order": 1,
                "recording_limit_seconds_0": "1",
                "recording_limit_seconds_1": "min",
            }
        )
        self.assertTrue(voice.is_valid(), voice.errors)
        self.assertEqual(voice.cleaned_data["recording_limit_seconds"], 60)
        gap = QuestionForm(
            data={
                "kind": Question.Kind.GAP,
                "text": "She ___ here",
                "points": 1,
                "order": 2,
                "choices_text": "lives",
                "recording_limit_seconds_0": "1",
                "recording_limit_seconds_1": "min",
            }
        )
        self.assertTrue(gap.is_valid(), gap.errors)
        self.assertIsNone(gap.cleaned_data["recording_limit_seconds"])

    def test_audio_assignment_rejects_long_recording(self):
        self.assignment.assignment_type = Assignment.Type.AUDIO
        self.assignment.recording_limit_seconds = 10
        self.assignment.save()
        response = self.student_client.post(
            self.url, {"expected_version": 0, "file_answer": wav_upload(25)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Запись длиннее лимита")
        self.assertFalse(Submission.objects.exists())
        ok = self.student_client.post(
            self.url, {"expected_version": 0, "file_answer": wav_upload(8)}
        )
        self.assertEqual(ok.status_code, 302)

    def test_audio_page_shows_limit(self):
        self.assignment.assignment_type = Assignment.Type.AUDIO
        self.assignment.recording_limit_seconds = 90
        self.assignment.save()
        page = self.student_client.get(self.url)
        self.assertContains(page, 'data-limit="90"')


class ExamModeTests(ItemFlowBase):
    """Контрольная: одна попытка, без ✓/✗ и правильного ответа до завершения."""

    def setUp(self):
        super().setUp()
        self.quiz.exam_mode = True
        self.quiz.max_tries = 3
        self.quiz.save()

    def test_wrong_answer_is_sealed_without_feedback(self):
        response = self.check(self.gap, "live")
        self.assertContains(response, "увидите после завершения")
        self.assertNotContains(response, "Неверно")
        self.assertNotContains(response, "lives", msg_prefix="Правильный ответ скрыт")
        self.assertNotContains(response, "mark-bad")
        item = self.response_for(self.gap)
        self.assertEqual(item.state, QuestionResponse.State.FAILED, "Одна попытка — пункт закрыт")
        again = self.check(self.gap, "lives")
        self.assertEqual(again.status_code, 409)

    def test_correct_answer_is_sealed_too(self):
        response = self.check(self.mcq, str(self.right.pk))
        self.assertNotContains(response, "Верно!")
        self.assertNotContains(response, "mark-ok")
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "Контрольная")
        self.assertContains(page, "Выполнено 1 из 2")

    def test_review_opens_after_last_item(self):
        self.check(self.mcq, str(self.wrong.pk))
        self.check(self.gap, "lives")
        attempt = Submission.objects.get(student=self.student, assignment=self.quiz)
        self.assertEqual(attempt.feedback.grade, 3)
        page = self.student_client.get(self.quiz_url)
        self.assertContains(page, "Задание завершено")
        self.assertContains(page, "Правильный ответ")
        self.assertContains(page, "have")

    def test_teacher_form_saves_exam_mode(self):
        form = AssignmentForm(
            data={
                "topic": self.topic.pk,
                "title": "Exam",
                "description": "Control work.",
                "assignment_type": Assignment.Type.QUIZ,
                "max_points": 0,
                "order": 1,
                "status": Assignment.Publication.PUBLISHED,
                "is_active": "on",
                "exam_mode": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["exam_mode"])


class ItemResultsExportTests(ItemFlowBase):
    def test_xlsx_contains_marks_and_rates(self):
        import zipfile

        self.check(self.mcq, str(self.wrong.pk))
        self.check(self.mcq, str(self.right.pk))
        self.check(self.gap, "a")
        self.check(self.gap, "b")
        self.check(self.gap, "c")
        response = self.teacher_client.get(
            f"/teacher/curriculum/assignments/{self.quiz.pk}/results/export.xlsx"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as book:
            sheet = book.read("xl/worksheets/sheet1.xml").decode()
            strings = sheet + (
                book.read("xl/sharedStrings.xml").decode()
                if "xl/sharedStrings.xml" in book.namelist()
                else ""
            )
        self.assertIn("✓ 2/2 (попытка 2)", strings)
        self.assertIn("✗ 0/3", strings)
        self.assertIn("Доля верных, %", strings)
        self.assertIn("student", strings)

    def test_students_cannot_export(self):
        response = self.student_client.get(
            f"/teacher/curriculum/assignments/{self.quiz.pk}/results/export.xlsx"
        )
        self.assertNotEqual(response.status_code, 200)
