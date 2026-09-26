import base64

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from lms.forms import QuestionForm
from lms.models import (
    Assignment,
    AssignmentAttachment,
    Choice,
    Feedback,
    Question,
    QuestionResponse,
    QuizAttempt,
)
from lms.services import regrade_assignment

from .base import LMSCase


class TopicEditingTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.question = Question.objects.create(
            assignment=self.assignment, kind="mcq", text="City?", points=10
        )
        self.a = Choice.objects.create(question=self.question, text="London", is_correct=True)
        self.b = Choice.objects.create(question=self.question, text="Paris", order=1)

    def payload(self):
        return {
            "assignment_type": "quiz",
            "title": "Updated task",
            "description": "New instructions",
            "status": "published",
            "order": 1,
            "max_points": 10,
            "max_tries": 3,
            f"question-{self.question.pk}-kind": "mcq",
            f"question-{self.question.pk}-text": "City?",
            f"question-{self.question.pk}-points": 10,
            f"question-{self.question.pk}-order": 0,
            f"question-{self.question.pk}-choices_text": "London\n*Paris",
        }

    def test_inline_editor_saves_key_and_preserves_choice_ids(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]), self.payload()
        )
        self.assertEqual(response.status_code, 302)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertFalse(self.a.is_correct)
        self.assertTrue(self.b.is_correct)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Updated task")
        html = self.teacher_client.get(reverse("teacher_topic_board", args=[self.topic.pk]))
        self.assertContains(html, "data-open-editor=")
        self.assertContains(html, f'name="question-{self.question.pk}-choices_text"')
        self.assertNotContains(html, "Результаты по пунктам")

    def test_invalid_key_keeps_entire_assignment_unchanged(self):
        data = self.payload()
        data[f"question-{self.question.pk}-choices_text"] = "London\nParis"
        response = self.teacher_client.post(
            reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]), data
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Updated task")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Past tense")
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_correct)

    def test_uploads_are_validated_and_multiple_attachments_are_saved(self):
        data = self.payload()
        data["new_attachments"] = [
            SimpleUploadedFile("one.txt", b"First attachment", "text/plain"),
            SimpleUploadedFile("two.txt", b"Second attachment", "text/plain"),
        ]
        url = reverse("teacher_assignment_quick_edit", args=[self.assignment.pk])
        self.assertEqual(self.teacher_client.post(url, data).status_code, 302)
        self.assertEqual(self.assignment.attachments.count(), 2)
        data = self.payload()
        data["title"] = "Must not save"
        data["new_attachments"] = SimpleUploadedFile("bad.png", b"not an image", "image/png")
        self.assertEqual(self.teacher_client.post(url, data).status_code, 200)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Updated task")
        self.assertEqual(self.assignment.attachments.count(), 2)

    def test_clearing_shared_material_does_not_break_copy(self):
        self.assignment.material_file.save(
            "shared.txt", SimpleUploadedFile("shared.txt", b"Shared material", "text/plain")
        )
        name = self.assignment.material_file.name
        from lms.models import Assignment

        copy = Assignment.objects.create(
            topic=self.topic, title="Copy", description="Copy", material_file=name
        )
        data = self.payload()
        data["material_file-clear"] = "on"
        with self.captureOnCommitCallbacks(execute=True):
            response = self.teacher_client.post(
                reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]), data
            )
        self.assertEqual(response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.material_file)
        self.assertTrue(copy.material_file.storage.exists(name))
        download = self.student_client.get(reverse("private_file", args=[name]))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"Shared material")

    def test_student_cannot_edit(self):
        response = self.student_client.post(
            reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]), self.payload()
        )
        self.assertEqual(response.status_code, 403)

    def test_regrade_updates_auto_and_preserves_teacher_grade(self):
        submission = self.submit()
        attempt = QuizAttempt.objects.create(
            submission=submission,
            score=0,
            max_score=10,
            total_count=1,
            answers={str(self.question.pk): {"given": str(self.b.pk), "points": 0}},
        )
        response = QuestionResponse.objects.create(
            student=self.student,
            assignment=self.assignment,
            question=self.question,
            submission=submission,
            state="failed",
            tries=[{"given": str(self.b.pk), "correct": False}],
            points=0,
        )
        feedback = Feedback.objects.create(submission=submission, grade=0, teacher=None)
        self.teacher_client.post(
            reverse("teacher_assignment_quick_edit", args=[self.assignment.pk]), self.payload()
        )
        attempt.refresh_from_db()
        response.refresh_from_db()
        feedback.refresh_from_db()
        self.assertEqual(attempt.score, 10)
        self.assertEqual(response.points, 10)
        self.assertEqual(feedback.grade, 10)
        feedback.teacher = self.teacher
        feedback.grade = 7
        feedback.comment = "Manual feedback"
        feedback.save()
        regrade_assignment(self.assignment)
        feedback.refresh_from_db()
        self.assertEqual((feedback.grade, feedback.comment), (7, "Manual feedback"))

    def test_reordering_keeps_ids_and_changing_kind_is_rejected_after_submission(self):
        form = QuestionForm(
            {
                "kind": "mcq",
                "text": "City?",
                "points": 10,
                "order": 0,
                "choices_text": "*Paris\nLondon",
            },
            instance=self.question,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save_choices(self.question)
        self.assertEqual(
            list(self.question.choices.values_list("pk", flat=True)), [self.b.pk, self.a.pk]
        )
        submission = self.submit()
        QuizAttempt.objects.create(submission=submission)
        form = QuestionForm(
            {"kind": "gap", "text": "City?", "points": 10, "order": 0, "choices_text": "Paris"},
            instance=self.question,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("kind", form.errors)


class TopicAttachmentTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
        )
        self.attachment = AssignmentAttachment.objects.create(
            assignment=self.assignment,
            file=SimpleUploadedFile("grammar.png", self.png, "image/png"),
        )

    def test_teacher_and_student_can_preview_and_download_png(self):
        for client in (self.teacher_client, self.student_client):
            for route in ("media_preview", "private_file"):
                response = client.get(reverse(route, args=[self.attachment.file.name]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b"".join(response.streaming_content), self.png)
        for route, pk in (
            ("teacher_topic_board", self.topic.pk),
            ("teacher_assignment_preview", self.assignment.pk),
        ):
            response = self.teacher_client.get(reverse(route, args=[pk]))
            self.assertContains(
                response, reverse("media_preview", args=[self.attachment.file.name])
            )
            self.assertContains(response, reverse("private_file", args=[self.attachment.file.name]))
        response = self.student_client.get(self.url)
        self.assertContains(response, reverse("media_preview", args=[self.attachment.file.name]))

    def test_missing_png_returns_404_not_server_error(self):
        self.attachment.file.storage.delete(self.attachment.file.name)
        for route in ("media_preview", "private_file"):
            self.assertEqual(
                self.teacher_client.get(
                    reverse(route, args=[self.attachment.file.name])
                ).status_code,
                404,
            )

    def test_unassigned_student_cannot_read_private_attachment(self):
        self.assignment.assigned_students.add(self.other)
        for route in ("media_preview", "private_file"):
            self.assertEqual(
                self.student_client.get(
                    reverse(route, args=[self.attachment.file.name])
                ).status_code,
                404,
            )


class AssignmentTypeSwitchTests(LMSCase):
    """Смена типа у уже созданного задания: «Тест с автопроверкой» → любой другой.

    Регрессия: оставшиеся пункты теста молча возвращали тип обратно на quiz,
    поэтому учителю казалось, что поле «Тип задания» не сохраняется.
    """

    def setUp(self):
        super().setUp()
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.max_points = 3
        self.assignment.save()
        self.question = Question.objects.create(
            assignment=self.assignment, kind="mcq", text="City?", points=3, order=1
        )
        Choice.objects.create(question=self.question, text="London", is_correct=True, order=0)
        Choice.objects.create(question=self.question, text="Paris", order=1)

    def quick_payload(self, assignment_type, with_questions=True):
        data = {
            "assignment_type": assignment_type,
            "title": self.assignment.title,
            "description": self.assignment.description,
            "status": "published",
            "order": self.assignment.order,
            "max_points": 50,
            "max_tries": 3,
        }
        if with_questions:
            data.update(
                {
                    f"question-{self.question.pk}-kind": "mcq",
                    f"question-{self.question.pk}-text": "City?",
                    f"question-{self.question.pk}-points": 3,
                    f"question-{self.question.pk}-order": 1,
                    f"question-{self.question.pk}-choices_text": "*London\nParis",
                }
            )
        return data

    def full_payload(self, assignment_type):
        return {
            "topic": self.topic.pk,
            "title": self.assignment.title,
            "description": self.assignment.description,
            "assignment_type": assignment_type,
            "max_points": 50,
            "max_tries": 3,
            "status": "published",
            "order": self.assignment.order,
            "is_active": "on",
        }

    def test_inline_editor_switches_type_away_from_quiz(self):
        url = reverse("teacher_assignment_quick_edit", args=[self.assignment.pk])
        response = self.teacher_client.post(url, self.quick_payload(Assignment.Type.TEXT))
        self.assertEqual(response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assignment_type, Assignment.Type.TEXT)
        # Баллы, выставленные вручную, больше не перетираются суммой пунктов.
        self.assertEqual(self.assignment.max_points, 50)
        # Пункты никуда не пропали — вернутся вместе с типом «Тест».
        self.assertEqual(self.assignment.questions.count(), 1)

    def test_inline_editor_switches_type_without_question_payload(self):
        """Тип меняется, даже если пункты не пришли в POST и не прошли бы валидацию."""
        url = reverse("teacher_assignment_quick_edit", args=[self.assignment.pk])
        response = self.teacher_client.post(
            url, self.quick_payload(Assignment.Type.MATERIAL, with_questions=False)
        )
        self.assertEqual(response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assignment_type, Assignment.Type.MATERIAL)
        self.assertEqual(self.assignment.questions.count(), 1)

    def test_inline_editor_still_syncs_points_while_assignment_stays_quiz(self):
        url = reverse("teacher_assignment_quick_edit", args=[self.assignment.pk])
        data = self.quick_payload(Assignment.Type.QUIZ)
        data[f"question-{self.question.pk}-points"] = 7
        self.assertEqual(self.teacher_client.post(url, data).status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assignment_type, Assignment.Type.QUIZ)
        self.assertEqual(self.assignment.max_points, 7)

    def test_full_editor_switches_type_for_every_target(self):
        url = reverse("teacher_assignment_form", args=[self.assignment.pk])
        for target in (
            Assignment.Type.TEXT,
            Assignment.Type.FILE,
            Assignment.Type.AUDIO,
            Assignment.Type.MIXED,
            Assignment.Type.FLASHCARDS,
            Assignment.Type.MATERIAL,
        ):
            with self.subTest(target=target):
                Assignment.objects.filter(pk=self.assignment.pk).update(
                    assignment_type=Assignment.Type.QUIZ
                )
                self.teacher_client.post(url, self.full_payload(target))
                self.assignment.refresh_from_db()
                self.assertEqual(self.assignment.assignment_type, target)

    def test_editing_or_deleting_leftover_items_keeps_the_new_type(self):
        Assignment.objects.filter(pk=self.assignment.pk).update(
            assignment_type=Assignment.Type.TEXT
        )
        extra = Question.objects.create(
            assignment=self.assignment, kind="mcq", text="Second?", points=2, order=2
        )
        Choice.objects.create(question=extra, text="a", is_correct=True, order=0)
        Choice.objects.create(question=extra, text="b", order=1)

        self.teacher_client.post(
            reverse("teacher_question_edit", args=[self.question.pk]),
            {
                "kind": "mcq",
                "text": "City, really?",
                "choices_text": "*London\nParis",
                "points": 3,
                "order": 1,
            },
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assignment_type, Assignment.Type.TEXT)

        self.teacher_client.post(reverse("teacher_question_delete", args=[extra.pk]))
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.assignment_type, Assignment.Type.TEXT)

    def test_adding_an_item_still_turns_a_plain_assignment_into_a_quiz(self):
        plain = Assignment.objects.create(topic=self.topic, title="Plain", description="x", order=7)
        self.teacher_client.post(
            reverse("teacher_questions", args=[plain.pk]),
            {
                "kind": "mcq",
                "text": "New?",
                "choices_text": "*yes\nno",
                "points": 4,
                "order": 1,
            },
        )
        plain.refresh_from_db()
        self.assertEqual(plain.assignment_type, Assignment.Type.QUIZ)
        self.assertEqual(plain.max_points, 4)
