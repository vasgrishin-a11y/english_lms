"""Назначения по иерархии: класс → глава → тема → задание.

Назначения наследуются вниз, но каждый нижний уровень может исключить
унаследованную группу или ученика и при необходимости назначить их снова.
Если назначений нет во всей цепочке — материал общий.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse

from lms import audience
from lms.models import Assignment, Block, Chapter, Group, Topic

from .base import LMSCase

User = get_user_model()


class AudienceVisibilityTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.group_a = Group.objects.create(name="ОГЭ-А", slug="oge-a")
        self.group_b = Group.objects.create(name="ОГЭ-Б", slug="oge-b")
        self.group_a.students.add(self.student)
        self.group_b.students.add(self.other)
        self.outsider = User.objects.create_user("outsider", password=self.password)

    def visible_for(self, user):
        return set(Assignment.objects.visible(user=user).values_list("pk", flat=True))

    def assert_single_check_matches_queryset(self):
        assignment = Assignment.objects.select_related("topic__block", "topic__chapter").get(
            pk=self.assignment.pk
        )
        for user in (self.student, self.other, self.outsider):
            self.assertEqual(
                audience.assignment_is_visible_to(assignment, user),
                assignment.pk in self.visible_for(user),
                f"расхождение для {user.username}",
            )

    def clear_rules(self):
        for obj in (self.block, self.topic.chapter, self.topic, self.assignment):
            obj.groups.clear()
            obj.students.clear()
            obj.excluded_groups.clear()
            obj.excluded_students.clear()

    def test_single_assignment_helper_matches_queryset(self):
        self.assert_single_check_matches_queryset()
        self.block.groups.add(self.group_a)
        self.assert_single_check_matches_queryset()
        self.topic.chapter.excluded_groups.add(self.group_a)
        self.assert_single_check_matches_queryset()
        self.assignment.groups.add(self.group_a)
        self.assert_single_check_matches_queryset()
        self.clear_rules()
        self.block.students.add(self.outsider)
        self.topic.excluded_students.add(self.outsider)
        self.assert_single_check_matches_queryset()
        self.clear_rules()
        # Ученик в двух группах: исключение одной не отменяет доступ через другую.
        self.group_b.students.add(self.student)
        self.block.groups.add(self.group_a, self.group_b)
        self.topic.excluded_groups.add(self.group_a)
        self.assert_single_check_matches_queryset()
        self.clear_rules()
        self.block.is_active = False
        self.block.save(update_fields=["is_active"])
        self.assert_single_check_matches_queryset()
        self.block.is_active = True
        self.block.save(update_fields=["is_active"])
        self.assignment.status = Assignment.Publication.DRAFT
        self.assignment.save(update_fields=["status"])
        self.assert_single_check_matches_queryset()

    def test_nothing_assigned_means_everyone(self):
        for user in (self.student, self.other, self.outsider):
            self.assertIn(self.assignment.pk, self.visible_for(user))

    def test_block_assignment_restricts_everything_inside(self):
        self.block.groups.add(self.group_a)
        self.assertIn(self.assignment.pk, self.visible_for(self.student))
        self.assertNotIn(self.assignment.pk, self.visible_for(self.other))
        self.assertNotIn(self.assignment.pk, self.visible_for(self.outsider))
        # страница задания недоступна тем, кому класс не назначен
        self.assertEqual(
            self.client_for(self.other).get(f"/assignments/{self.assignment.pk}/").status_code,
            404,
        )
        self.assertEqual(
            self.student_client.get(f"/assignments/{self.assignment.pk}/").status_code, 200
        )

    def test_lower_level_adds_audience_union(self):
        self.block.groups.add(self.group_a)
        self.topic.groups.add(self.group_b)
        # класс открыт для А, тема дополнительно для Б — видят обе группы
        self.assertIn(self.assignment.pk, self.visible_for(self.student))
        self.assertIn(self.assignment.pk, self.visible_for(self.other))
        self.assertNotIn(self.assignment.pk, self.visible_for(self.outsider))

    def test_lower_level_can_exclude_student_in_inherited_group(self):
        self.block.groups.add(self.group_a)
        self.topic.excluded_students.add(self.student)
        self.assertNotIn(self.assignment.pk, self.visible_for(self.student))
        self.assertEqual(set(audience.expected_students(self.assignment)), set())
        result = audience.effective(self.assignment)
        self.assertFalse(result["open"])
        self.assertTrue(result["has_inherited"])

    def test_lower_level_can_exclude_group_and_descendant_can_add_it_again(self):
        self.block.groups.add(self.group_a)
        self.topic.chapter.excluded_groups.add(self.group_a)
        self.assertNotIn(self.assignment.pk, self.visible_for(self.student))
        self.assignment.groups.add(self.group_a)
        self.assertIn(self.assignment.pk, self.visible_for(self.student))

    def test_same_level_group_can_exclude_one_member(self):
        self.topic.groups.add(self.group_a)
        self.topic.excluded_students.add(self.student)
        self.assertNotIn(self.assignment.pk, self.visible_for(self.student))
        self.assignment.assigned_students.add(self.student)
        self.assertIn(self.assignment.pk, self.visible_for(self.student))

    def test_lower_level_can_exclude_personal_assignment(self):
        self.block.students.add(self.outsider)
        self.topic.excluded_students.add(self.outsider)
        self.assertNotIn(self.assignment.pk, self.visible_for(self.outsider))
        self.assignment.assigned_students.add(self.outsider)
        self.assertIn(self.assignment.pk, self.visible_for(self.outsider))

    def test_personal_student_at_chapter_level(self):
        self.block.groups.add(self.group_a)
        self.topic.chapter.students.add(self.outsider)
        self.assertIn(self.assignment.pk, self.visible_for(self.outsider))
        self.assertNotIn(self.assignment.pk, self.visible_for(self.other))

    def test_assignment_level_groups_and_students(self):
        self.assignment.groups.add(self.group_b)
        self.assignment.assigned_students.add(self.outsider)
        self.assertNotIn(self.assignment.pk, self.visible_for(self.student))
        self.assertIn(self.assignment.pk, self.visible_for(self.other))
        self.assertIn(self.assignment.pk, self.visible_for(self.outsider))

    def test_other_assignments_in_same_block_stay_open(self):
        other_topic = Topic.objects.create(block=self.block, title="Other", slug="other")
        open_task = Assignment.objects.create(
            topic=other_topic, title="Open", description="x", max_points=10
        )
        self.topic.groups.add(self.group_a)
        self.assertNotIn(self.assignment.pk, self.visible_for(self.other))
        self.assertIn(open_task.pk, self.visible_for(self.other))

    def test_student_map_and_home_respect_audience(self):
        self.block.groups.add(self.group_a)
        other_client = self.client_for(self.other)
        self.assertNotContains(other_client.get("/assignments/"), "Past tense")
        self.assertContains(self.student_client.get("/assignments/"), "Past tense")
        self.assertNotContains(other_client.get("/my/"), "Past tense")

    def test_teacher_student_card_hides_class_of_another_group(self):
        """В карточке ученика класс, назначенный другой группе, не показывается."""
        self.block.groups.add(self.group_a)
        other_block = Block.objects.create(name="Business English", slug="business")
        other_block.groups.add(self.group_b)
        other_topic = Topic.objects.create(block=other_block, title="Meetings", slug="meetings")
        Assignment.objects.create(
            topic=other_topic, title="Email to client", description="x", max_points=10
        )

        card = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertContains(card, "Past tense")
        self.assertNotContains(card, "Business English")
        self.assertNotContains(card, "Meetings")
        self.assertEqual(card.context["totals"]["blocks"], 1)
        self.assertEqual(card.context["totals"]["assignments"], 1)

        other_card = self.teacher_client.get(f"/teacher/students/{self.other.pk}/")
        self.assertContains(other_card, "Email to client")
        self.assertNotContains(other_card, "Past tense")
        self.assertEqual(other_card.context["totals"]["blocks"], 1)

    def test_teacher_student_card_hides_chapter_excluded_for_student(self):
        """Внутри доступного класса исключённая глава в карточке ученика не видна."""
        self.block.groups.add(self.group_a)
        other_chapter = Chapter.objects.create(
            block=self.block, title="Чужая глава", slug="other-chapter"
        )
        other_chapter.excluded_groups.add(self.group_a)
        other_topic = Topic.objects.create(
            block=self.block, chapter=other_chapter, title="Letters", slug="letters"
        )
        Assignment.objects.create(
            topic=other_topic, title="Letter to friend", description="x", max_points=10
        )

        card = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertContains(card, "Past tense")
        self.assertNotContains(card, "Чужая глава")
        self.assertNotContains(card, "Letter to friend")
        self.assertNotContains(card, "Letters")
        [block_item] = card.context["blocks_data"]
        self.assertEqual([item["chapter"].title for item in block_item["chapters"]], ["Общее"])

    def test_teacher_student_card_explains_empty_course_and_no_assignments(self):
        """Пустая карточка различает «в курсе нет классов» и «нет назначений»."""
        self.block.groups.add(self.group_b)
        card = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertContains(card, "не назначено ни одного класса")
        self.assertEqual(card.context["totals"]["assignments"], 0)
        self.assertEqual(card.context["blocks_data"], [])

        Block.objects.update(is_active=False)
        empty = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        self.assertContains(empty, "В курсе пока нет классов.")

    def test_teacher_student_card_and_student_map_show_same_classes(self):
        """Карточка ученика и его карта курса перечисляют одни и те же классы."""
        self.block.groups.add(self.group_a)
        other_block = Block.objects.create(name="Business English", slug="business")
        other_block.groups.add(self.group_b)
        other_topic = Topic.objects.create(block=other_block, title="Meetings", slug="meetings")
        Assignment.objects.create(
            topic=other_topic, title="Email to client", description="x", max_points=10
        )

        card = self.teacher_client.get(f"/teacher/students/{self.student.pk}/")
        course_map = self.student_client.get("/assignments/")
        card_blocks = [item["block"].name for item in card.context["blocks_data"]]
        map_blocks = [item["block"].name for item in course_map.context["blocks_data"]]
        self.assertEqual(card_blocks, map_blocks)
        self.assertEqual(card_blocks, [self.block.name])

    def test_effective_and_label(self):
        self.block.groups.add(self.group_a)
        self.topic.students.add(self.outsider)
        result = audience.effective(self.assignment)
        self.assertFalse(result["open"])
        self.assertEqual([group.pk for group in result["groups"]], [self.group_a.pk])
        self.assertEqual([user.pk for user in result["students"]], [self.outsider.pk])
        self.assertEqual(result["label"], "ОГЭ-А · +1 ученик")
        self.assertFalse(result["has_own"])
        self.assertTrue(result["has_inherited"])
        self.assertEqual(audience.effective(self.block)["label"], "ОГЭ-А")
        self.assertEqual(audience.effective(self.topic)["own"]["students"], [self.outsider])

    def test_expected_students_follow_audience(self):
        everyone = set(audience.expected_students(self.assignment).values_list("pk", flat=True))
        self.assertEqual(everyone, {self.student.pk, self.other.pk, self.outsider.pk})
        self.block.groups.add(self.group_a)
        self.assignment.assigned_students.add(self.outsider)
        expected = set(audience.expected_students(self.assignment).values_list("pk", flat=True))
        self.assertEqual(expected, {self.student.pk, self.outsider.pk})


class AudienceConsoleTests(LMSCase):
    def setUp(self):
        super().setUp()
        self.group_a = Group.objects.create(name="ОГЭ-А", slug="oge-a")
        self.group_a.students.add(self.student)

    def test_block_form_saves_groups_and_students(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/blocks/{self.block.pk}/",
            {
                "name": self.block.name,
                "cefr_level": "",
                "description": "",
                "order": 1,
                "is_active": "on",
                "groups": [self.group_a.pk],
                "students": [self.other.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.block.groups.all()), [self.group_a])
        self.assertEqual(list(self.block.students.all()), [self.other])

    def test_chapter_and_topic_forms_show_inherited_audience(self):
        self.block.groups.add(self.group_a)
        chapter_page = self.teacher_client.get(
            f"/teacher/curriculum/chapters/{self.topic.chapter_id}/"
        )
        self.assertContains(chapter_page, "Уже назначено выше по иерархии")
        self.assertContains(chapter_page, "ОГЭ-А")
        topic_page = self.teacher_client.get(f"/teacher/curriculum/topics/{self.topic.pk}/")
        self.assertContains(topic_page, "Уже назначено выше по иерархии")
        new_topic_page = self.teacher_client.get(
            f"/teacher/curriculum/topics/new/?block={self.block.pk}"
        )
        self.assertContains(new_topic_page, "Уже назначено выше по иерархии")
        fresh_block_page = self.teacher_client.get("/teacher/curriculum/blocks/new/")
        self.assertContains(fresh_block_page, "Кому доступно")
        self.assertNotContains(fresh_block_page, "Уже назначено выше по иерархии")

    def test_topic_form_can_save_inherited_exclusions(self):
        self.block.groups.add(self.group_a)
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/",
            {
                "block": self.block.pk,
                "chapter": self.topic.chapter_id,
                "title": self.topic.title,
                "description": "",
                "order": 1,
                "is_active": "on",
                "excluded_groups": [self.group_a.pk],
                "excluded_students": [self.student.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.topic.excluded_groups.all()), [self.group_a])
        self.assertEqual(list(self.topic.excluded_students.all()), [self.student])

    def test_topic_form_saves_audience(self):
        response = self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/",
            {
                "block": self.block.pk,
                "chapter": self.topic.chapter_id,
                "title": self.topic.title,
                "description": "",
                "order": 1,
                "is_active": "on",
                "groups": [self.group_a.pk],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.topic.groups.all()), [self.group_a])

    def test_assignment_form_accepts_multiple_groups(self):
        group_b = Group.objects.create(name="ОГЭ-Б", slug="oge-b")
        response = self.teacher_client.post(
            reverse("teacher_assignment_form", args=[self.assignment.pk]),
            {
                "topic": self.topic.pk,
                "title": self.assignment.title,
                "description": "Write an answer.",
                "assignment_type": Assignment.Type.TEXT,
                "max_points": 100,
                "max_tries": 1,
                "order": 0,
                "status": Assignment.Publication.PUBLISHED,
                "groups": [self.group_a.pk, group_b.pk],
                "assigned_students": [self.other.pk],
                "recording_limit_seconds_0": "",
                "recording_limit_seconds_1": "sec",
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        self.assertEqual(set(self.assignment.groups.all()), {self.group_a, group_b})
        self.assertEqual(list(self.assignment.assigned_students.all()), [self.other])

    def test_curriculum_shows_own_and_inherited_badges(self):
        self.block.groups.add(self.group_a)
        response = self.teacher_client.get("/teacher/curriculum/")
        self.assertEqual(response.status_code, 200)
        block_item = response.context["blocks_data"][0]
        self.assertTrue(block_item["audience"]["has_own"])
        topic_item = block_item["topics"][0]
        self.assertFalse(topic_item["audience"]["has_own"])
        self.assertTrue(topic_item["audience"]["has_inherited"])
        self.assertContains(response, 'class="audience-badge"')
        self.assertContains(response, 'class="audience-badge is-inherited"')

    def test_topic_board_shows_assignment_badge(self):
        self.assignment.groups.add(self.group_a)
        response = self.teacher_client.get(f"/teacher/curriculum/topics/{self.topic.pk}/board/")
        self.assertContains(response, "ОГЭ-А")
        self.assertContains(response, 'class="audience-badge"')

    def test_progress_panel_counts_only_audience(self):
        self.block.groups.add(self.group_a)
        response = self.teacher_client.get(
            reverse("teacher_assignment_form", args=[self.assignment.pk])
        )
        self.assertEqual(response.context["progress"]["total"], 1)

    def test_topic_copy_keeps_topic_audience(self):
        self.topic.groups.add(self.group_a)
        self.assignment.groups.add(self.group_a)
        other = Block.objects.create(name="Other", slug="other")
        self.teacher_client.post(
            f"/teacher/curriculum/topics/{self.topic.pk}/copy/", {"target_block": other.pk}
        )
        copy = Topic.objects.get(block=other)
        self.assertEqual(list(copy.groups.all()), [self.group_a])
        self.assertEqual(list(copy.assignments.first().groups.all()), [self.group_a])

    def test_groups_page_counts_assignments_via_new_relation(self):
        self.assignment.groups.add(self.group_a)
        response = self.teacher_client.get("/teacher/groups/")
        self.assertEqual(response.status_code, 200)

    def test_default_chapter_audience_is_empty(self):
        chapter = Chapter.objects.get(pk=self.topic.chapter_id)
        self.assertFalse(chapter.has_own_audience)


class AudienceGradebookTests(LMSCase):
    def test_gradebook_marks_unassigned_cells_and_totals(self):
        group = Group.objects.create(name="ОГЭ-А", slug="oge-a")
        group.students.add(self.student)
        self.block.groups.add(group)
        response = self.teacher_client.get("/teacher/analytics/")
        self.assertEqual(response.status_code, 200)
        rows = {row["student"].pk: row for row in response.context["rows"]}
        self.assertEqual(rows[self.student.pk]["total"], 1)
        self.assertTrue(rows[self.student.pk]["cells"][0]["assigned"])
        self.assertEqual(rows[self.other.pk]["total"], 0)
        self.assertFalse(rows[self.other.pk]["cells"][0]["assigned"])
        self.assertContains(response, "Задание не назначено этому ученику")


class AudienceDetailsTests(LMSCase):
    """Подсказки с именами: наведение на бейдж и формы редактирования."""

    def setUp(self):
        super().setUp()
        self.student.first_name = "Анна"
        self.student.last_name = "Смирнова"
        self.student.save()
        self.group = Group.objects.create(name="ОГЭ-А", slug="oge-a")
        self.group.students.add(self.student)

    def test_details_lists_groups_and_full_names(self):
        self.block.groups.add(self.group)
        self.topic.students.add(self.student)
        result = audience.effective(self.assignment)
        self.assertEqual(result["details"], "ОГЭ-А, Анна Смирнова")
        self.assertEqual(result["label"], "ОГЭ-А · +1 ученик")

    def test_details_falls_back_to_login(self):
        self.topic.students.add(self.other)
        self.assertEqual(audience.effective(self.assignment)["details"], "other")
        self.other.first_name = "Максим"
        self.other.save()
        self.assertEqual(audience.effective(self.assignment)["details"], "Максим")

    def test_badge_tooltip_contains_names(self):
        self.block.groups.add(self.group)
        self.block.students.add(self.student)
        page = self.teacher_client.get("/teacher/curriculum/")
        self.assertContains(page, "Назначено здесь: ОГЭ-А, Анна Смирнова. Нажмите, чтобы изменить.")

    def test_edit_forms_show_inherited_names_on_every_level(self):
        self.block.groups.add(self.group)
        self.block.students.add(self.other)
        chapter = Chapter.objects.get(pk=self.topic.chapter_id)
        urls = [
            reverse("teacher_chapter_edit", args=[chapter.pk]),
            reverse("teacher_topic_edit", args=[self.topic.pk]),
            reverse("teacher_assignment_form", args=[self.assignment.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                page = self.teacher_client.get(url)
                self.assertContains(page, "Уже назначено выше по иерархии")
                self.assertContains(page, "ОГЭ-А")
                # У other имени нет — виден логин.
                self.assertContains(page, "other")

    def test_block_form_checkboxes_show_full_names(self):
        page = self.teacher_client.get(reverse("teacher_block_edit", args=[self.block.pk]))
        self.assertContains(page, "Анна Смирнова")
        self.assertNotContains(page, "Уже назначено выше по иерархии")
