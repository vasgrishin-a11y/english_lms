"""Доработки интерфейса: навыки, форма, поиск, меню, дашборд, лиса."""

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
        self.assertIn("data-fox-scene", html)
        self.assertIn("fox-scene-canvas", html)
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
        self.assertRedirects(response, reverse("teacher_curriculum"))
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
        self.assertRedirects(response, reverse("teacher_curriculum"))
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
