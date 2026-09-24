"""Библиотека контента преподавателя: шаблоны заданий, заготовки и наборы курсов.

Здесь проверяются и «продуктовые» гарантии (учитель видит заготовки и может
добавить курс одним действием), и инварианты контента: типы заданий и вопросов
соответствуют моделям, у вопроса с выбором есть правильный ответ, повторный
импорт не плодит дубликаты.
"""

from django.urls import reverse
from django.utils.html import escape

from lms.library import (
    ASSIGNMENT_PRESETS,
    BLOCK_SUGGESTIONS,
    COURSE_PACKS,
    TOPIC_SUGGESTIONS,
    import_course_pack,
    packs_with_state,
)
from lms.models import (
    Assignment,
    Block,
    Flashcard,
    Question,
    Skill,
)

from .base import LMSCase


class LibraryAccessTests(LMSCase):
    def test_student_cannot_open_library(self):
        response = self.student_client.get(reverse("teacher_library"))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_needs_login(self):
        self.assertEqual(self.client.get(reverse("teacher_library")).status_code, 302)

    def test_teacher_sees_every_pack(self):
        response = self.teacher_client.get(reverse("teacher_library"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for pack in COURSE_PACKS:
            with self.subTest(pack=pack["slug"]):
                self.assertIn(pack["name"], content)
                self.assertIn(reverse("teacher_library_import", args=[pack["slug"]]), content)

    def test_curriculum_page_links_to_library(self):
        content = self.teacher_client.get(reverse("teacher_curriculum")).content.decode()
        self.assertIn(reverse("teacher_library"), content)


class LibraryImportTests(LMSCase):
    def test_import_creates_blocks_topics_and_draft_assignments(self):
        result = import_course_pack("general-a2")
        self.assertGreater(result["created"]["blocks"], 0)
        self.assertGreater(result["created"]["topics"], 0)
        self.assertGreater(result["created"]["assignments"], 0)
        imported = Assignment.objects.filter(topic__block__slug__startswith="generala2")
        self.assertEqual(imported.count(), result["created"]["assignments"])
        for assignment in imported:
            with self.subTest(assignment=assignment.title):
                self.assertEqual(assignment.status, Assignment.Publication.DRAFT)
                self.assertTrue(assignment.description.strip())

    def test_import_builds_quiz_questions_and_cards(self):
        result = import_course_pack("general-a2")
        quiz = Assignment.objects.filter(assignment_type=Assignment.Type.QUIZ).first()
        self.assertIsNotNone(quiz)
        self.assertTrue(quiz.questions.exists())
        for question in quiz.questions.all():
            with self.subTest(question=question.text):
                self.assertTrue(question.choices.filter(is_correct=True).exists())
        self.assertEqual(quiz.max_points, quiz.total_question_points)
        self.assertTrue(Flashcard.objects.filter(assignment__title__contains="Travel").exists())
        # Счётчик карточек — это карточки, а не наборы.
        self.assertEqual(Flashcard.objects.count(), result["created"]["cards"])
        self.assertEqual(result["created"]["questions"], Question.objects.count())

    def test_second_import_is_idempotent(self):
        first = import_course_pack("exam-prep")
        second = import_course_pack("exam-prep")
        self.assertEqual(second["created"]["blocks"], 0)
        self.assertEqual(second["created"]["topics"], 0)
        self.assertEqual(second["created"]["assignments"], 0)
        self.assertEqual(
            sum(second["skipped"].values()),
            sum(first["created"][key] for key in ("blocks", "topics", "assignments")),
        )
        pack = next(item for item in COURSE_PACKS if item["slug"] == "exam-prep")
        self.assertEqual(
            Block.objects.filter(slug__startswith="examprep").count(), len(pack["blocks"])
        )

    def test_import_skips_unknown_pack(self):
        with self.assertRaises(LookupError):
            import_course_pack("no-such-pack")

    def test_import_view_reports_created_objects(self):
        response = self.teacher_client.post(
            reverse("teacher_library_import", args=["general-b1b2"])
        )
        self.assertRedirects(response, reverse("teacher_curriculum"))
        messages = [message.message for message in response.wsgi_request._messages]
        self.assertTrue(any("Добавлено в курс" in message for message in messages))
        self.assertTrue(Block.objects.filter(slug__startswith="generalb1b2").exists())

    def test_import_view_rejects_unknown_pack(self):
        response = self.teacher_client.post(reverse("teacher_library_import", args=["unknown"]))
        self.assertEqual(response.status_code, 404)

    def test_import_links_existing_skills_only(self):
        Skill.objects.create(name="Грамматика", slug="grammar", kind=Skill.Kind.GRAMMAR)
        Skill.objects.create(name="Письмо", slug="writing", kind=Skill.Kind.WRITING)
        import_course_pack("business-english")
        assignment = Assignment.objects.filter(title="Write three business emails").get()
        self.assertEqual(list(assignment.skills.values_list("slug", flat=True)), ["writing"])
        # Навыки, которых нет в системе, не создаются импортом.
        self.assertEqual(Skill.objects.count(), 2)

    def test_state_counts_reflect_what_is_already_in_course(self):
        import_course_pack("business-english")
        entry = next(
            item for item in packs_with_state() if item["pack"]["slug"] == "business-english"
        )
        self.assertEqual(entry["counts"]["imported_blocks"], entry["counts"]["blocks"])
        self.assertEqual(entry["counts"]["imported_topics"], entry["counts"]["topics"])
        other = next(item for item in packs_with_state() if item["pack"]["slug"] == "general-a2")
        self.assertEqual(other["counts"]["imported_blocks"], 0)


class LibraryContentTests(LMSCase):
    """Статические проверки контента: дешевле поймать опечатку здесь, чем в проде."""

    def test_presets_match_model_choices(self):
        types = {value for value, _ in Assignment.Type.choices}
        ids = set()
        for preset in ASSIGNMENT_PRESETS:
            with self.subTest(preset=preset["id"]):
                self.assertNotIn(preset["id"], ids)
                ids.add(preset["id"])
                fields = preset["fields"]
                self.assertIn(fields["assignment_type"], types)
                self.assertTrue(fields["title"].strip())
                self.assertTrue(fields["description"].strip())
                self.assertGreaterEqual(fields["max_points"], 1)
                self.assertLessEqual(fields["max_points"], 1000)

    def test_presets_reference_known_skills(self):
        known = {value for value, _ in Skill.Kind.choices}
        for preset in ASSIGNMENT_PRESETS:
            for skill in preset["skills"]:
                self.assertIn(skill, known)

    def test_block_and_topic_suggestions_are_filled(self):
        self.assertGreaterEqual(len(BLOCK_SUGGESTIONS), 5)
        self.assertGreaterEqual(len(TOPIC_SUGGESTIONS), 10)
        levels = {value for value, _ in Block._meta.get_field("cefr_level").choices}
        for suggestion in BLOCK_SUGGESTIONS:
            with self.subTest(block=suggestion["name"]):
                self.assertTrue(suggestion["description"].strip())
                if suggestion["cefr_level"]:
                    self.assertIn(suggestion["cefr_level"], levels)
        for suggestion in TOPIC_SUGGESTIONS:
            with self.subTest(topic=suggestion["title"]):
                self.assertTrue(suggestion["description"].strip())

    def test_packs_are_well_formed(self):
        slugs = set()
        types = {value for value, _ in Assignment.Type.choices}
        kinds = {value for value, _ in Question.Kind.choices}
        for pack in COURSE_PACKS:
            with self.subTest(pack=pack["slug"]):
                self.assertNotIn(pack["slug"], slugs)
                slugs.add(pack["slug"])
                self.assertTrue(pack["summary"].strip())
                self.assertGreaterEqual(len(pack["blocks"]), 2)
                for block in pack["blocks"]:
                    self.assertTrue(block["topics"], f"В блоке {block['name']} нет тем")
                    for topic in block["topics"]:
                        self.assertTrue(
                            topic.get("assignments") or topic.get("cards"),
                            f"В теме {topic['title']} нет ни заданий, ни карточек",
                        )
                        for item in topic.get("assignments", []):
                            self.assertIn(item["type"], types)
                            self.assertTrue(item["description"].strip())
                            self.assertLessEqual(len(item["title"]), 200)
                            for question in item.get("questions", []):
                                self.assertIn(question["kind"], kinds)
                                self.assertTrue(
                                    any(choice.get("correct") for choice in question["choices"]),
                                    f"У вопроса «{question['text']}» нет правильного ответа",
                                )
                        for card_set in topic.get("cards", []):
                            self.assertGreaterEqual(len(card_set["cards"]), 5)
                            for card in card_set["cards"]:
                                self.assertTrue(card["front"].strip())
                                self.assertTrue(card["back"].strip())


class FormPresetsTests(LMSCase):
    def test_assignment_form_offers_presets(self):
        response = self.teacher_client.get(reverse("teacher_assignment_new"))
        content = response.content.decode()
        self.assertIn("Быстрые шаблоны заданий", content)
        self.assertIn('id="assignment-presets"', content)
        for preset in ASSIGNMENT_PRESETS:
            with self.subTest(preset=preset["id"]):
                # Метки содержат «&» — в HTML они экранируются.
                self.assertIn(escape(preset["label"]), content)
                self.assertIn(f'data-preset="{preset["id"]}"', content)

    def test_assignment_form_shows_expected_vs_submitted(self):
        self.submit()
        response = self.teacher_client.get(
            reverse("teacher_assignment_form", args=[self.assignment.pk])
        )
        content = response.content.decode()
        self.assertIn("Кто сдал это задание", content)
        # Сдал только self.student, второй ученик (self.other) — в списке должников.
        self.assertIn("Ещё не сдали", content)
        self.assertIn("other", content)

    def test_new_assignment_form_has_no_progress_panel(self):
        content = self.teacher_client.get(reverse("teacher_assignment_new")).content.decode()
        self.assertNotIn("Кто сдал это задание", content)

    def test_block_and_topic_forms_offer_suggestions(self):
        block_page = self.teacher_client.get(reverse("teacher_block_new")).content.decode()
        self.assertIn("Готовые варианты классов", block_page)
        self.assertIn('id="block-suggestions"', block_page)
        self.assertIn(BLOCK_SUGGESTIONS[0]["name"], block_page)

        topic_page = self.teacher_client.get(reverse("teacher_topic_new")).content.decode()
        self.assertIn("Готовые темы по английскому", topic_page)
        self.assertIn('id="topic-suggestions"', topic_page)
        self.assertIn(TOPIC_SUGGESTIONS[0]["title"], topic_page)

    def test_assignment_form_still_saves(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_new"),
            {
                "topic": self.topic.pk,
                "title": "Preset based task",
                "description": "Write 100 words about your weekend.",
                "assignment_type": Assignment.Type.TEXT,
                "max_points": 20,
                "status": Assignment.Publication.DRAFT,
                "order": 0,
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("teacher_curriculum"))
        self.assertTrue(Assignment.objects.filter(title="Preset based task").exists())

    def test_library_imported_assignments_are_editable(self):
        import_course_pack("general-a2")
        assignment = Assignment.objects.filter(topic__block__slug__startswith="generala2").first()
        response = self.teacher_client.get(reverse("teacher_assignment_form", args=[assignment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(assignment.title, response.content.decode())
