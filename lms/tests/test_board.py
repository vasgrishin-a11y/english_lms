"""Быстрые действия на карте курса.

Переименование, порядок перетаскиванием (точное место через before/topic),
резервные кнопки ↑/↓ без JS и массовая публикация по теме и блоку.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from lms.models import Assignment, Topic

from .base import LMSCase


def make_assignment(topic, title, **kwargs):
    kwargs.setdefault("description", "Do the task.")
    return Assignment.objects.create(topic=topic, title=title, **kwargs)


class BoardControlsTests(LMSCase):
    def test_board_renders_quick_controls(self):
        html = self.teacher_client.get(reverse("teacher_curriculum")).content.decode()
        for fragment in (
            "data-curriculum-board",
            'data-topic-list="',
            'data-dnd-handle="assignment"',
            'data-dnd-handle="topic"',
            "data-rename-url",
            "data-board-toggle",
            reverse("teacher_assignment_move", args=[self.assignment.pk]),
            reverse("teacher_assignment_rename", args=[self.assignment.pk]),
            reverse("teacher_assignment_ai", args=[self.assignment.pk]),
            reverse("teacher_topic_assignments_publish", args=[self.topic.pk]),
            reverse("teacher_block_assignments_publish", args=[self.block.pk]),
        ):
            self.assertIn(fragment, html)


class BoardRenameTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.rename_url = reverse("teacher_assignment_rename", args=[self.assignment.pk])

    def test_rename_via_ajax(self):
        response = self.teacher_client.post(
            self.rename_url, {"title": "  New title  "}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "title": "New title"})
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "New title")

    def test_rename_empty_is_rejected(self):
        response = self.teacher_client.post(
            self.rename_url, {"title": "   "}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.status_code, 400)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Past tense")

    def test_rename_requires_teacher(self):
        response = self.student_client.post(self.rename_url, {"title": "Hack"})
        self.assertEqual(response.status_code, 403)

    def test_rename_without_js_redirects_back(self):
        response = self.teacher_client.post(
            self.rename_url,
            {"title": "Renamed", "next": "/teacher/curriculum/#block-%s" % self.block.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/teacher/curriculum/#block-{self.block.pk}")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Renamed")


class BoardMoveTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.t1 = Topic.objects.create(block=self.block, title="Moves", slug="moves")
        self.t2 = Topic.objects.create(block=self.block, title="Target", slug="target")
        self.a1 = make_assignment(self.t1, "A1")
        self.a2 = make_assignment(self.t1, "A2")
        self.a3 = make_assignment(self.t1, "A3")
        self.b1 = make_assignment(self.t2, "B1")

    @staticmethod
    def titles(topic):
        return list(topic.assignments.order_by("order", "pk").values_list("title", flat=True))

    def move(self, assignment, **data):
        return self.teacher_client.post(
            reverse("teacher_assignment_move", args=[assignment.pk]),
            data,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_move_before_reorders_within_topic(self):
        response = self.move(self.a3, before=self.a1.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(self.t1), ["A3", "A1", "A2"])

    def test_move_without_before_appends_to_end(self):
        response = self.move(self.a1, before="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(self.t1), ["A2", "A3", "A1"])

    def test_move_across_topics(self):
        response = self.move(self.a2, topic=self.t2.pk, before=self.b1.pk)
        self.assertEqual(response.status_code, 200)
        self.a2.refresh_from_db()
        self.assertEqual(self.a2.topic_id, self.t2.pk)
        self.assertEqual(self.titles(self.t2), ["A2", "B1"])
        self.assertEqual(self.titles(self.t1), ["A1", "A3"])

    def test_move_to_empty_topic(self):
        empty = Topic.objects.create(block=self.block, title="Empty", slug="empty")
        self.move(self.a3, topic=empty.pk, before="")
        self.assertEqual(self.titles(empty), ["A3"])
        self.assertEqual(self.titles(self.t1), ["A1", "A2"])

    def test_move_rejects_before_from_other_topic(self):
        response = self.move(self.a1, topic=self.t1.pk, before=self.b1.pk)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.titles(self.t1), ["A1", "A2", "A3"])

    def test_direction_buttons_work_without_js(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_move", args=[self.a2.pk]), {"direction": "up"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.titles(self.t1), ["A2", "A1", "A3"])

    def test_direction_first_item_stays_put(self):
        response = self.move(self.a1, direction="up")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.titles(self.t1), ["A1", "A2", "A3"])

    def test_move_requires_teacher(self):
        response = self.student_client.post(
            reverse("teacher_assignment_move", args=[self.a1.pk]), {"before": ""}
        )
        self.assertEqual(response.status_code, 403)

    def test_topic_move_before_reorders_within_block(self):
        extra = Topic.objects.create(block=self.block, title="Third", slug="third")
        response = self.teacher_client.post(
            reverse("teacher_topic_move", args=[extra.pk]),
            {"before": self.topic.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        titles = list(self.block.topics.order_by("order", "pk").values_list("title", flat=True))
        self.assertEqual(titles[0], "Third")

    def test_topic_move_rejects_before_from_other_block(self):
        from lms.models import Block

        other_block = Block.objects.create(name="Other", slug="other")
        alien = Topic.objects.create(block=other_block, title="Alien", slug="alien")
        extra = Topic.objects.create(block=self.block, title="Third", slug="third")
        before = list(self.block.topics.order_by("order", "pk").values_list("pk", flat=True))
        response = self.teacher_client.post(
            reverse("teacher_topic_move", args=[extra.pk]),
            {"before": alien.pk},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 404)
        after = list(self.block.topics.order_by("order", "pk").values_list("pk", flat=True))
        self.assertEqual(before, after)


class BoardPublishTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.other = Topic.objects.create(block=self.block, title="Other topic", slug="other")
        self.draft1 = make_assignment(self.topic, "Draft 1", status=Assignment.Publication.DRAFT)
        self.draft2 = make_assignment(self.other, "Draft 2", status=Assignment.Publication.DRAFT)

    def test_assignment_publish_ajax_returns_status(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_publish", args=[self.draft1.pk]),
            {"publish": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.json(), {"ok": True, "status": "published", "published": True})
        self.draft1.refresh_from_db()
        self.assertEqual(self.draft1.status, Assignment.Publication.PUBLISHED)

    def test_topic_bulk_publish_and_unpublish(self):
        url = reverse("teacher_topic_assignments_publish", args=[self.topic.pk])
        self.teacher_client.post(url, {"publish": "1"})
        self.draft1.refresh_from_db()
        self.draft2.refresh_from_db()
        self.assertEqual(self.draft1.status, Assignment.Publication.PUBLISHED)
        self.assertEqual(self.draft2.status, Assignment.Publication.DRAFT)

        self.teacher_client.post(url, {"publish": "0"})
        self.assignment.refresh_from_db()
        self.draft1.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Publication.DRAFT)
        self.assertEqual(self.draft1.status, Assignment.Publication.DRAFT)

    def test_block_bulk_publish_clears_scheduled_publish(self):
        self.draft1.publish_at = timezone.now() + timedelta(days=2)
        self.draft1.save(update_fields=["publish_at", "updated_at"])
        self.teacher_client.post(
            reverse("teacher_block_assignments_publish", args=[self.block.pk]), {"publish": "1"}
        )
        self.draft1.refresh_from_db()
        self.draft2.refresh_from_db()
        self.assertEqual(self.draft1.status, Assignment.Publication.PUBLISHED)
        self.assertIsNone(self.draft1.publish_at)
        self.assertEqual(self.draft2.status, Assignment.Publication.PUBLISHED)

    def test_bulk_noop_keeps_statuses(self):
        # В теме «Other topic» только draft2: «в черновики всё» там — холостой ход.
        response = self.teacher_client.post(
            reverse("teacher_topic_assignments_publish", args=[self.other.pk]),
            {"publish": "0"},
            follow=True,
        )
        self.assertContains(response, "Менять нечего")
        self.draft2.refresh_from_db()
        self.assertEqual(self.draft2.status, Assignment.Publication.DRAFT)

    def test_bulk_publish_requires_teacher(self):
        response = self.student_client.post(
            reverse("teacher_block_assignments_publish", args=[self.block.pk]), {"publish": "1"}
        )
        self.assertEqual(response.status_code, 403)
