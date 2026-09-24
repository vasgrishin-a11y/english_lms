"""Действия на странице «Классы»: удаление темы, панели подтверждения, копирование при создании класса."""

from django.urls import reverse

from lms.models import Assignment, Block, Submission, Topic

from .base import LMSCase


class TopicDeleteButtonTests(LMSCase):
    def test_topic_row_has_delete_panel_instead_of_copy_dropdown(self):
        response = self.teacher_client.get(reverse("teacher_curriculum"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("data-copy-toggle", html)
        self.assertNotIn("Копировать в класс:", html)
        self.assertIn(reverse("teacher_topic_delete", args=[self.topic.pk]), html)
        self.assertIn("Удалить тему «Grammar» целиком?", html)
        # Перенос/копирование остаются через перетаскивание — адреса нужны JS.
        self.assertIn(
            f'data-move-url="{reverse("teacher_topic_move", args=[self.topic.pk])}"', html
        )
        self.assertIn(
            f'data-copy-url="{reverse("teacher_topic_copy", args=[self.topic.pk])}"', html
        )
        # Панели подтверждения не должны обрезаться контейнерами класса и темы.
        for marker in ("data-block-drop", "data-topic-panel"):
            start = html.index(marker)
            tag = html[start : html.index(">", start)]
            self.assertNotIn("overflow:hidden", tag)

    def test_topic_delete_requires_confirm_and_protects_submissions(self):
        url = reverse("teacher_topic_delete", args=[self.topic.pk])
        response = self.teacher_client.post(url, {})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Topic.objects.filter(pk=self.topic.pk).exists())

        Submission.objects.create(
            assignment=self.assignment, student=self.student, text_answer="done"
        )
        response = self.teacher_client.post(url, {"confirm": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Topic.objects.filter(pk=self.topic.pk).exists())

    def test_topic_delete_removes_topic_with_assignments(self):
        url = reverse("teacher_topic_delete", args=[self.topic.pk])
        response = self.teacher_client.post(url, {"confirm": "1"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Topic.objects.filter(pk=self.topic.pk).exists())
        self.assertFalse(Assignment.objects.filter(pk=self.assignment.pk).exists())
        self.assertTrue(Block.objects.filter(pk=self.block.pk).exists())

    def test_student_cannot_delete_topic(self):
        response = self.student_client.post(
            reverse("teacher_topic_delete", args=[self.topic.pk]), {"confirm": "1"}
        )
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(Topic.objects.filter(pk=self.topic.pk).exists())


class DangerPanelTests(LMSCase):
    def test_all_danger_panels_render_readable_text_and_actions(self):
        response = self.teacher_client.get(reverse("teacher_curriculum"))
        html = response.content.decode()
        self.assertIn("Удалить класс «English» целиком?", html)
        self.assertIn(reverse("teacher_block_delete", args=[self.block.pk]), html)
        self.assertIn(reverse("teacher_chapter_delete", args=[self.topic.chapter.pk]), html)
        self.assertEqual(html.count('<details class="danger-zone">'), 3)

    def test_danger_panel_is_not_squeezed_by_generic_panel_width(self):
        """layout.css ограничивает .panel шириной родителя — для всплывающей
        панели это ширина кнопки. Переопределение обязано быть в CSS."""
        from pathlib import Path

        css = Path("lms/static/lms/css/components.css").read_text(encoding="utf-8")
        rule = css[css.index(".danger-zone > .danger-panel") :]
        rule = rule[: rule.index("}")]
        self.assertIn("max-width: none", rule)


class BlockCreateCopiesTopicsTests(LMSCase):
    def test_new_block_form_lists_topics_from_other_classes(self):
        response = self.teacher_client.get(reverse("teacher_block_new"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Добавить темы из других классов", html)
        self.assertIn(f'name="copy_topic_ids" value="{self.topic.pk}"', html)
        # Список лежит внутри основной формы: сохраняется вместе с классом.
        form_start = html.index("data-preset-form")
        form_end = html.index("</form>", form_start)
        self.assertIn("copy_topic_ids", html[form_start:form_end])

    def test_new_block_copies_selected_topics_on_save(self):
        response = self.teacher_client.post(
            reverse("teacher_block_new"),
            {
                "name": "B1 — Новый класс",
                "cefr_level": "",
                "description": "",
                "order": 5,
                "is_active": "on",
                "copy_topic_ids": [str(self.topic.pk)],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        created = Block.objects.get(name="B1 — Новый класс")
        copies = Topic.objects.filter(block=created)
        self.assertEqual(copies.count(), 1)
        self.assertEqual(copies.first().title, self.topic.title)
        self.assertEqual(copies.first().assignments.count(), 1)
        # Оригинал на месте
        self.assertTrue(Topic.objects.filter(pk=self.topic.pk, block=self.block).exists())
        self.assertContains(response, "Скопировано тем: 1")

    def test_new_block_without_selection_creates_empty_class(self):
        response = self.teacher_client.post(
            reverse("teacher_block_new"),
            {"name": "Пустой", "cefr_level": "", "description": "", "order": 6, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)
        created = Block.objects.get(name="Пустой")
        self.assertEqual(created.topics.count(), 0)

    def test_edit_form_still_offers_separate_copy_form(self):
        other = Block.objects.create(name="Other", slug="other")
        response = self.teacher_client.get(reverse("teacher_block_edit", args=[other.pk]))
        html = response.content.decode()
        self.assertIn("Добавить тему из другого класса", html)
        self.assertIn('name="copy_topics" value="1"', html)
        self.assertIn(f'name="copy_topic_ids" value="{self.topic.pk}"', html)
