"""Доработки интерфейса: навыки, форма, поиск, меню, дашборд, язык, сцена."""

from django.urls import reverse

from lms.models import Assignment, Group, Skill
from lms.skills import apply_default_skills, skills_for_type

from .base import LMSCase


class DashboardHomeTests(LMSCase):
    def test_teacher_root_opens_console_home(self):
        response = self.teacher_client.get("/")
        self.assertRedirects(response, "/teacher/", fetch_redirect_response=False)
        home = self.teacher_client.get("/teacher/")
        self.assertContains(home, "Консоль преподавателя")
        self.assertContains(home, "Самые старые работы")


class NavToggleTests(LMSCase):
    def test_menu_button_is_icon_only(self):
        html = self.teacher_client.get("/teacher/").content.decode()
        self.assertIn('id="nav-toggle"', html)
        self.assertNotIn(">Навигация<", html)
        self.assertIn("app-nav-toggle-label", html)
        self.assertIn("data-raccoon-scene", html)
        self.assertIn("raccoon-scene-canvas", html)
        self.assertIn("raccoon-face.svg", html)
        self.assertNotIn("fox", html.lower())
        self.assertNotIn("🌲", html)
        self.assertNotIn("🦊", html)


class AssignmentSkillsTests(LMSCase):
    def test_empty_skills_are_filled_by_type(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_new"),
            {
                "topic": self.topic.pk,
                "title": "Letter home",
                "description": "Write a letter.",
                "assignment_type": Assignment.Type.TEXT,
                "max_points": 20,
                "status": Assignment.Publication.DRAFT,
                "order": 0,
                "is_active": "on",
            },
        )
        if response.status_code == 200:
            self.fail(response.context["form"].errors.as_text())
        assignment = Assignment.objects.get(title="Letter home")
        kinds = list(assignment.skills.values_list("kind", flat=True))
        self.assertEqual(kinds, [Skill.Kind.WRITING])

    def test_manual_empty_skills_are_kept(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_new"),
            {
                "topic": self.topic.pk,
                "title": "No skills",
                "description": "Teacher cleared chips.",
                "assignment_type": Assignment.Type.TEXT,
                "max_points": 10,
                "status": Assignment.Publication.DRAFT,
                "order": 0,
                "is_active": "on",
                "skills_manual": "1",
            },
        )
        if response.status_code == 200:
            self.fail(response.context["form"].errors.as_text())
        assignment = Assignment.objects.get(title="No skills")
        self.assertFalse(assignment.skills.exists())

    def test_audio_defaults_to_speaking(self):
        skills = skills_for_type(Assignment.Type.AUDIO)
        self.assertEqual([skill.kind for skill in skills], [Skill.Kind.SPEAKING])

    def test_new_quiz_redirects_to_questions(self):
        response = self.teacher_client.post(
            reverse("teacher_assignment_new"),
            {
                "topic": self.topic.pk,
                "title": "Auto quiz",
                "description": "Check tenses.",
                "assignment_type": Assignment.Type.QUIZ,
                "max_points": 0,
                "status": Assignment.Publication.DRAFT,
                "order": 0,
                "is_active": "on",
            },
        )
        quiz = Assignment.objects.get(title="Auto quiz")
        self.assertRedirects(
            response,
            reverse("teacher_questions", args=[quiz.pk]),
            fetch_redirect_response=False,
        )
        apply_default_skills(quiz)
        self.assertTrue(quiz.skills.filter(kind=Skill.Kind.GRAMMAR).exists())

    def test_form_shows_type_cards_and_embeds_quiz_questions(self):
        self.assignment.assignment_type = Assignment.Type.QUIZ
        self.assignment.save()
        page = self.teacher_client.get(reverse("teacher_assignment_new"))
        self.assertContains(page, "type-cards")
        self.assertContains(page, "Быстрые шаблоны заданий")
        self.assertContains(page, 'id="assignment-skills-by-type"')
        edit = self.teacher_client.get(
            reverse("teacher_assignment_form", args=[self.assignment.pk])
        )
        self.assertContains(edit, "quiz-editor")
        self.assertContains(edit, "Новый вопрос")


class SuggestSearchTests(LMSCase):
    def test_teacher_student_suggest_is_limited_and_from_db(self):
        Group.objects.create(name="Alpha group", slug="alpha")
        response = self.teacher_client.get("/suggest/?scope=students&q=stu")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertLessEqual(len(items), 8)
        labels = [item["label"] for item in items]
        self.assertTrue(any("student" in label.lower() or label for label in labels))
        self.assertTrue(all(item["type"] == "student" for item in items))

        curriculum = self.teacher_client.get("/suggest/?scope=curriculum&q=Past")
        types = {item["type"] for item in curriculum.json()["items"]}
        self.assertTrue(types & {"topic", "assignment", "block"})

    def test_student_cannot_suggest_other_students(self):
        response = self.student_client.get("/suggest/?scope=students&q=a")
        self.assertEqual(response.json()["items"], [])
        course = self.student_client.get("/suggest/?scope=curriculum&q=Past")
        items = course.json()["items"]
        self.assertTrue(items)
        self.assertFalse(any(item["type"] == "student" for item in items))

    def test_search_inputs_keep_get_without_js(self):
        html = self.teacher_client.get("/teacher/students/").content.decode()
        self.assertIn('data-suggest="', html)
        self.assertIn('name="q"', html)
        self.assertIn('method="get"', html.lower())


class InterfaceLanguageTests(LMSCase):
    """Переключатель RU|ENG: меню и действия переводятся, курс — нет."""

    def test_russian_is_default(self):
        html = self.teacher_client.get("/teacher/").content.decode()
        self.assertIn('<html lang="ru"', html)
        self.assertIn("Консоль", html)
        self.assertIn("lang-switch", html)
        self.assertIn('class="lang-switch-option is-active"', html)

    def test_english_mode_translates_menu_and_actions(self):
        html = self.teacher_client.get("/teacher/?lang=eng").content.decode()
        self.assertIn('<html lang="en"', html)
        self.assertIn(">Console<", html)
        self.assertIn(">Course<", html)
        self.assertIn(">Analytics<", html)
        self.assertNotIn(">Консоль<", html)

    def test_choice_is_remembered_in_cookie(self):
        response = self.teacher_client.get("/teacher/?lang=eng")
        self.assertEqual(response.cookies["lms_lang"].value, "eng")
        self.assertIn(">Console<", self.teacher_client.get("/teacher/").content.decode())
        self.teacher_client.get("/teacher/?lang=ru")
        self.assertIn(">Консоль<", self.teacher_client.get("/teacher/").content.decode())

    def test_learning_content_stays_english(self):
        html = self.student_client.get(
            reverse("assignment_detail", args=[self.assignment.pk]) + "?lang=eng"
        ).content.decode()
        self.assertIn("Past tense", html)  # задание и его текст не переводятся
        self.assertIn(">Submit for review<", html)  # а действие — да

    def test_unknown_language_falls_back_to_russian(self):
        html = self.teacher_client.get("/teacher/?lang=de").content.decode()
        self.assertIn('<html lang="ru"', html)
        self.assertIn(">Консоль<", html)

    def test_language_parameter_does_not_redirect(self):
        response = self.teacher_client.get("/teacher/?lang=eng")
        self.assertEqual(response.status_code, 200)

    def test_login_and_logout_labels_are_translated(self):
        anonymous = self.client.get("/accounts/login/?lang=eng")
        self.assertContains(anonymous, "Sign in")


class DescriptionEditorTests(LMSCase):
    """«Условия задания»: большой редактор и возврат к прежнему размеру."""

    def test_editor_controls_are_rendered(self):
        html = self.teacher_client.get(
            reverse("teacher_assignment_form", args=[self.assignment.pk])
        ).content.decode()
        self.assertIn("data-description-editor", html)
        self.assertIn("data-editor-expand", html)
        self.assertIn("data-editor-restore", html)
        self.assertIn("Вернуть прежний размер", html)
        self.assertIn("#i-expand", html)

    def test_solution_text_survives_round_trip(self):
        long_text = "Task: write 200 words.\n\nCriteria:\n- structure\n- vocabulary"
        response = self.teacher_client.post(
            reverse("teacher_assignment_form", args=[self.assignment.pk]),
            {
                "topic": self.topic.pk,
                "title": self.assignment.title,
                "description": long_text,
                "assignment_type": self.assignment.assignment_type,
                "max_points": self.assignment.max_points,
                "status": self.assignment.status,
                "order": self.assignment.order,
            },
        )
        if response.status_code == 200:
            self.fail(response.context["form"].errors.as_text())
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.description, long_text)


class UploadDropzoneTests(LMSCase):
    """Материалы прикрепляются перетаскиванием, поле остаётся обычным input."""

    def test_student_answer_field_is_a_dropzone(self):
        self.assignment.assignment_type = Assignment.Type.MIXED
        self.assignment.save(update_fields=["assignment_type"])
        html = self.student_client.get(
            reverse("assignment_detail", args=[self.assignment.pk])
        ).content.decode()
        self.assertIn('data-dropzone="1"', html)
        self.assertIn('type="file"', html)
        self.assertIn("upload-block", html)
        self.assertIn('accept="', html)

    def test_ai_material_field_is_a_dropzone(self):
        html = self.teacher_client.get(reverse("teacher_ai")).content.decode()
        self.assertIn('data-dropzone="1"', html)
        self.assertIn("data-dropzone-hint", html)

    def test_text_only_assignment_has_no_file_field(self):
        self.assignment.assignment_type = Assignment.Type.TEXT
        self.assignment.save(update_fields=["assignment_type"])
        html = self.student_client.get(
            reverse("assignment_detail", args=[self.assignment.pk])
        ).content.decode()
        self.assertNotIn('data-dropzone="1"', html)


class RaccoonSceneTests(LMSCase):
    """Лису заменил енот: те же гарантии сцены, другие имена и рисунки."""

    def test_scene_is_rendered_with_new_assets(self):
        html = self.teacher_client.get("/teacher/").content.decode()
        self.assertIn("raccoon-face.svg", html)
        self.assertIn("raccoon_scene.js", html)
        self.assertIn("Мордочка енота в шарфе", html)

    def test_raccoon_assets_exist(self):
        from django.contrib.staticfiles import finders

        for name in (
            "lms/img/raccoon-face.svg",
            "lms/img/raccoon-sleep.svg",
            "lms/img/raccoon-books.svg",
            "lms/js/raccoon_scene.js",
        ):
            self.assertIsNotNone(finders.find(name), name)
        self.assertIsNone(finders.find("lms/img/fox-run.svg"))
        self.assertIsNone(finders.find("lms/img/raccoon-run.svg"))
