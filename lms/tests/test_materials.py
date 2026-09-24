"""Материалы для занятий: без сдачи и оценок, но в структуре курса.

Ученик видит материалы в карте курса, но не отвечает на них: прогресс, сроки
и «продолжить» их не учитывают. Учитель не видит их в проверках и журнале.
"""

import io
import zipfile
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from lms.models import Assignment, Submission
from lms.services import save_answer_draft, submit_assignment

from .base import LMSCase


class MaterialAssignmentTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.material = Assignment.objects.create(
            topic=self.topic,
            title="Учебник, разворот 12–13",
            description="Прочитайте диалог и послушайте запись.",
            assignment_type=Assignment.Type.MATERIAL,
            order=5,
        )

    def material_url(self):
        return f"/assignments/{self.material.pk}/"

    # ── Сдача невозможна даже прямым вызовом ──────────────────────
    def test_submit_service_rejects_materials_and_cards(self):
        cards = self.card_assignment(topic=self.topic, title="Слова")
        for assignment in (self.material, cards):
            with self.subTest(assignment=assignment.assignment_type):
                with self.assertRaises(PermissionDenied):
                    submit_assignment(
                        student=self.student,
                        assignment_id=assignment.pk,
                        expected_version=0,
                        text_answer="Попытка обойти интерфейс",
                    )
        self.assertEqual(Submission.objects.count(), 0)

    def test_draft_service_rejects_materials_and_cards(self):
        cards = self.card_assignment(topic=self.topic, title="Слова")
        for assignment in (self.material, cards):
            with self.subTest(assignment=assignment.assignment_type):
                with self.assertRaises(PermissionDenied):
                    save_answer_draft(student=self.student, assignment_id=assignment.pk, text="x")

    def test_post_to_material_page_creates_nothing(self):
        response = self.student_client.post(
            self.material_url(), {"expected_version": 0, "text_answer": "Ответ"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Submission.objects.count(), 0)

    # ── Ученик: видно в структуре, но отвечать не надо ────────────
    def test_material_stays_in_catalog_but_out_of_progress(self):
        response = self.student_client.get("/assignments/")
        self.assertContains(response, "Учебник, разворот 12–13")
        totals = response.context["totals"]
        self.assertEqual(totals["total"], 1)
        self.assertEqual(totals["progress"], 0)

    def test_material_detail_has_no_answer_form_or_points(self):
        page = self.student_client.get(self.material_url())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Материалы для занятия")
        self.assertContains(page, "Проверка не нужна")
        self.assertNotContains(page, "Ваш ответ")
        self.assertNotContains(page, "Отправить на проверку")
        self.assertNotContains(page, "баллов")
        self.assertNotContains(page, "Не сдано")

    def test_home_ignores_material_in_totals_continue_and_deadlines(self):
        self.material.deadline = timezone.now() + timedelta(hours=5)
        self.material.save()
        response = self.student_client.get("/my/")
        totals = response.context["totals"]
        self.assertEqual(totals["total"], 1)
        self.assertNotEqual(response.context["continue_item"].pk, self.material.pk)
        self.assertNotIn(self.material.pk, [item.pk for item in response.context["due_soon"]])
        # А карточка класса материал учитывает: в структуре он остаётся.
        self.assertContains(response, "2 задания")

    def test_upcoming_has_no_materials(self):
        self.material.deadline = timezone.now() + timedelta(hours=5)
        self.material.save()
        response = self.student_client.get("/upcoming/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Учебник, разворот 12–13")

    def test_catalog_list_mode_offers_open_instead_of_not_submitted(self):
        response = self.student_client.get("/assignments/?mode=list")
        html = response.content.decode()
        self.assertIn("Учебник, разворот 12–13", html)
        # Кнопка «Открыть» — только у материала; «Не сдано» — легенда и обычное задание.
        self.assertEqual(html.count("Открыть"), 1)
        self.assertEqual(html.count("Не сдано"), 2)

    # ── Учитель: в проверках и журнале материалов нет ─────────────
    def legacy_submission(self):
        """Историческая сдача (например, тип сменили после ответа)."""
        return Submission.objects.create(
            student=self.student,
            assignment=self.material,
            text_answer="Старый ответ",
            status=Submission.Status.SUBMITTED,
        )

    def test_review_queue_hides_material_submissions(self):
        attempt = self.legacy_submission()
        for url in ("/teacher/submissions/", "/teacher/submissions/?status=all"):
            with self.subTest(url=url):
                page = self.teacher_client.get(url)
                self.assertNotIn(attempt.pk, [item.pk for item in page.context["submissions"]])
                self.assertNotContains(page, "Учебник, разворот 12–13")
        counts = self.teacher_client.get("/teacher/submissions/").context["counts"]
        self.assertEqual(counts["waiting"], 0)
        self.assertEqual(counts["total"], 0)

    def test_console_overview_hides_material_submissions(self):
        self.legacy_submission()
        response = self.teacher_client.get("/teacher/")
        self.assertEqual(response.context["overview"]["queue"]["waiting"], 0)
        self.assertEqual(response.context["waiting"], [])
        # А черновики и карта курса материалы показывают как обычно.
        self.material.status = Assignment.Publication.DRAFT
        self.material.save()
        drafts = self.teacher_client.get("/teacher/").context["drafts"]
        self.assertIn(self.material.pk, [item.pk for item in drafts])

    def test_gradebook_has_no_material_columns(self):
        self.legacy_submission()
        data = self.teacher_client.get("/teacher/analytics/").context
        self.assertNotIn(self.material.pk, [item.pk for item in data["assignments"]])
        self.assertEqual(data["summary"]["waiting"], 0)
        export = self.teacher_client.get("/teacher/analytics/export.xlsx")
        self.assertEqual(export.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertNotIn("Учебник, разворот 12–13", sheet)
        self.assertIn("Past tense", sheet)

    def test_student_detail_hides_material_attempts(self):
        attempt = self.legacy_submission()
        response = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertNotIn(attempt.pk, [item.pk for item in response.context["attempts"]])

    def test_assignment_form_has_no_submission_progress_for_material(self):
        response = self.teacher_client.get(f"/teacher/curriculum/assignments/{self.material.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["progress"])
        self.assertNotContains(response, "Кто сдал это задание")

    # ── Регрессия вёрстки: карточки в сетках без соседнего отступа ──
    def test_grid_cards_have_no_sibling_margin(self):
        css = (
            Path(__file__).resolve().parents[1] / "static" / "lms" / "css" / "components.css"
        ).read_text()
        for grid in ("home-grid", "archive-grid", "library-grid"):
            with self.subTest(grid=grid):
                self.assertIn(f".{grid} > .card + .card", css)
