"""Главы: уровень между классом и темой.

Класс → Глава → Тема → Задания. Глава обязательна: тема без явной главы
попадает в «Общее» своего класса. Ученик видит главу как подпись и группировку,
учитель — карточками внутри класса на странице курса и перетаскиванием.
"""

from django.urls import reverse

from lms.models import Assignment, Block, Chapter, Topic

from .base import LMSCase


class ChapterModelTests(LMSCase):
    def test_topic_without_chapter_falls_into_default_chapter(self):
        self.assertEqual(self.topic.chapter.slug, Chapter.DEFAULT_SLUG)
        self.assertEqual(self.topic.chapter.block, self.block)
        # «Общее» создаётся один раз на класс
        again = Topic.objects.create(block=self.block, title="Second", slug="second")
        self.assertEqual(again.chapter_id, self.topic.chapter_id)
        self.assertEqual(self.block.chapters.count(), 1)

    def test_topic_block_follows_its_chapter(self):
        other = Block.objects.create(name="Other", slug="other")
        foreign = Chapter.objects.create(block=other, title="Foreign", slug="foreign")
        topic = Topic.objects.create(block=self.block, title="Wrong", slug="wrong", chapter=foreign)
        topic.refresh_from_db()
        self.assertEqual(topic.block, other)

    def test_archived_chapter_hides_assignments_from_student(self):
        self.topic.chapter.is_active = False
        self.topic.chapter.save()
        self.assertNotIn(self.assignment, Assignment.objects.visible(user=self.student))
        response = self.student_client.get(f"/assignments/{self.assignment.pk}/")
        self.assertEqual(response.status_code, 404)


class ChapterConsoleTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.hobby = Chapter.objects.create(block=self.block, title="Хобби", slug="hobby", order=5)

    def test_chapter_form_creates_and_edits(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/new/?block={self.block.pk}",
            {"block": self.block.pk, "title": "Travel", "description": "", "order": 7},
        )
        self.assertRedirects(response, f"/teacher/curriculum/#block-{self.block.pk}")
        chapter = Chapter.objects.get(title="Travel")
        self.assertEqual(chapter.block, self.block)
        self.assertTrue(chapter.slug)

        page = self.teacher_client.get(f"/teacher/curriculum/chapters/{chapter.pk}/")
        self.assertContains(page, "Travel")
        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/{chapter.pk}/",
            {"block": self.block.pk, "title": "Travel & Trips", "description": "", "order": 7},
        )
        self.assertEqual(response.status_code, 302)
        chapter.refresh_from_db()
        self.assertEqual(chapter.title, "Travel & Trips")

    def test_curriculum_page_groups_topics_by_chapter(self):
        reading = Topic.objects.create(
            block=self.block, chapter=self.hobby, title="Reading", slug="reading"
        )
        response = self.teacher_client.get("/teacher/curriculum/")
        self.assertEqual(response.status_code, 200)
        block_data = response.context["blocks_data"][0]
        chapters = block_data["chapters"]
        titles = [entry["chapter"].title for entry in chapters]
        self.assertEqual(titles, [Chapter.DEFAULT_TITLE, "Хобби"])
        hobby_topics = [entry["topic"].pk for entry in chapters[1]["topics"]]
        self.assertEqual(hobby_topics, [reading.pk])
        self.assertContains(response, f'data-chapter-panel="{self.hobby.pk}"')
        self.assertContains(response, reverse("teacher_chapter_new") + f"?block={self.block.pk}")

    def test_topic_form_offers_chapters_of_selected_block(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/new/?block={self.block.pk}",
            {
                "block": self.block.pk,
                "chapter": self.hobby.pk,
                "title": "Speaking",
                "description": "",
                "order": 1,
            },
        )
        self.assertEqual(response.status_code, 302)
        topic = Topic.objects.get(title="Speaking")
        self.assertEqual(topic.chapter, self.hobby)

        other = Block.objects.create(name="Other", slug="other")
        foreign = Chapter.objects.create(block=other, title="Foreign", slug="foreign")
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{topic.pk}/",
            {
                "block": self.block.pk,
                "chapter": foreign.pk,
                "title": "Speaking",
                "description": "",
                "order": 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        topic.refresh_from_db()
        self.assertEqual(topic.chapter, self.hobby)

    def test_chapter_move_by_direction_and_by_before(self):
        general = self.block.default_chapter()
        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/{self.hobby.pk}/move/", {"direction": "up"}
        )
        self.assertEqual(response.status_code, 302)
        self.hobby.refresh_from_db()
        general.refresh_from_db()
        self.assertLess(self.hobby.order, general.order)

        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/{self.hobby.pk}/move/",
            {"before": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.hobby.refresh_from_db()
        general.refresh_from_db()
        self.assertGreater(self.hobby.order, general.order)

    def test_topic_drag_between_chapters_moves_not_copies(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/move/",
            {"target_chapter": self.hobby.pk, "before": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.topic.refresh_from_db()
        self.assertEqual(self.topic.chapter, self.hobby)
        self.assertEqual(Topic.objects.count(), 1)
        self.assertEqual(self.topic.assignments.count(), 1)

    def test_topic_drag_before_topic_of_other_chapter_moves_there(self):
        anchor = Topic.objects.create(
            block=self.block, chapter=self.hobby, title="Anchor", slug="anchor", order=1
        )
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/move/",
            {"before": anchor.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["ok"])
        self.topic.refresh_from_db()
        anchor.refresh_from_db()
        self.assertEqual(self.topic.chapter, self.hobby)
        self.assertLess(self.topic.order, anchor.order)

    def test_topic_drag_to_other_block_copies_into_target_chapter(self):
        other = Block.objects.create(name="Other", slug="other")
        travel = Chapter.objects.create(block=other, title="Travel", slug="travel")
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/move/",
            {"target_block": other.pk, "target_chapter": travel.pk, "before": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["ok"])
        self.topic.refresh_from_db()
        self.assertEqual(self.topic.block, self.block)  # оригинал на месте
        copy = Topic.objects.get(block=other)
        self.assertEqual(copy.chapter, travel)
        self.assertEqual(copy.assignments.count(), 1)
        self.assertEqual(copy.assignments.first().status, Assignment.Publication.DRAFT)

    def test_topic_copy_without_chapter_lands_in_default_chapter(self):
        other = Block.objects.create(name="Other", slug="other")
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/copy/",
            {"target_block": other.pk},
        )
        self.assertEqual(response.status_code, 302)
        copy = Topic.objects.get(block=other)
        self.assertEqual(copy.chapter.slug, Chapter.DEFAULT_SLUG)
        self.assertEqual(copy.chapter.block, other)

    def test_chapter_archive_restore_and_delete(self):
        Topic.objects.create(block=self.block, chapter=self.hobby, title="Reading", slug="reading")
        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/{self.hobby.pk}/publish/", {"active": "0"}
        )
        self.assertEqual(response.status_code, 302)
        self.hobby.refresh_from_db()
        self.assertFalse(self.hobby.is_active)

        archive = self.teacher_client.get("/teacher/archive/")
        self.assertContains(archive, "Хобби")
        self.assertContains(archive, "Архивные главы")

        response = self.teacher_client.post(
            f"/teacher/archive/chapter/{self.hobby.pk}/", {"action": "restore"}
        )
        self.assertEqual(response.status_code, 302)
        self.hobby.refresh_from_db()
        self.assertTrue(self.hobby.is_active)

        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/{self.hobby.pk}/delete/", {"confirm": "1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Chapter.objects.filter(pk=self.hobby.pk).exists())
        self.assertFalse(Topic.objects.filter(slug="reading").exists())
        self.assertTrue(Block.objects.filter(pk=self.block.pk).exists())

    def test_default_chapter_is_recreated_after_deletion(self):
        general = self.block.default_chapter()
        self.topic.chapter = self.hobby
        self.topic.save()
        response = self.teacher_client.post(
            f"/teacher/curriculum/chapters/{general.pk}/delete/", {"confirm": "1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Chapter.objects.filter(pk=general.pk).exists())
        fresh = Topic.objects.create(block=self.block, title="Fresh", slug="fresh")
        self.assertEqual(fresh.chapter.slug, Chapter.DEFAULT_SLUG)
        self.assertNotEqual(fresh.chapter_id, general.pk)

    def test_console_home_counts_chapters(self):
        response = self.teacher_client.get("/teacher/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "console-card")
        self.assertContains(response, "Классы курса")


class ChapterStudentTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.hobby = Chapter.objects.create(block=self.block, title="Хобби", slug="hobby", order=2)
        self.reading = Topic.objects.create(
            block=self.block, chapter=self.hobby, title="Reading", slug="reading"
        )
        self.questions = Assignment.objects.create(
            topic=self.reading, title="Ответь на вопросы", description="x", max_points=10
        )

    def test_assignment_page_shows_class_chapter_topic_path(self):
        response = self.student_client.get(f"/assignments/{self.questions.pk}/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Хобби", content)
        self.assertIn("Reading", content)
        self.assertIn(self.block.name, content)

    def test_assignments_list_groups_by_chapter(self):
        response = self.student_client.get("/assignments/?view=list")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Хобби", content)
        self.assertIn(Chapter.DEFAULT_TITLE, content)
        # глава «Общее» идёт первой, «Хобби» — второй
        self.assertLess(content.index(Chapter.DEFAULT_TITLE), content.index("Хобби"))

    def test_search_by_chapter_title_finds_assignments(self):
        response = self.student_client.get("/assignments/?q=Хобби")
        self.assertContains(response, "Ответь на вопросы")
        self.assertNotContains(response, "Past tense")
