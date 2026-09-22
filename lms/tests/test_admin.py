from django.contrib.auth import get_user_model

from lms.models import Profile, Submission

from .base import LMSCase


class AdminTests(LMSCase):
    def add_user(self, name, role="student", telegram="", comment=""):
        return self.admin_client.post(
            "/admin/auth/user/add/",
            {
                "username": name,
                "password1": self.password,
                "password2": self.password,
                "usable_password": "true",
                "profile-TOTAL_FORMS": "1",
                "profile-INITIAL_FORMS": "0",
                "profile-MIN_NUM_FORMS": "0",
                "profile-MAX_NUM_FORMS": "1",
                "profile-0-id": "",
                "profile-0-user": "",
                "profile-0-role": role,
                "profile-0-telegram": telegram,
                "profile-0-comment": comment,
                "_save": "Save",
            },
        )

    def test_plain_student_can_be_created(self):
        self.assertEqual(self.add_user("new_student").status_code, 302)
        user = get_user_model().objects.get(username="new_student")
        self.assertEqual(Profile.objects.filter(user=user).count(), 1)

    def test_teacher_with_profile_can_be_created_without_duplicate(self):
        response = self.add_user("new_teacher", "teacher", "@example", "Profile comment")
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username="new_teacher")
        self.assertEqual(Profile.objects.filter(user=user).count(), 1)
        self.assertEqual(
            (user.profile.role, user.profile.telegram, user.profile.comment),
            ("teacher", "@example", "Profile comment"),
        )

    def test_student_with_extra_profile_can_be_created(self):
        self.assertEqual(self.add_user("student_metadata", telegram="@student").status_code, 302)
        self.assertEqual(
            get_user_model().objects.get(username="student_metadata").profile.telegram, "@student"
        )

    def test_teacher_role_does_not_grant_admin_site(self):
        self.assertEqual(self.teacher_client.get("/admin/").status_code, 302)

    def test_cannot_add_submission_in_admin(self):
        self.assertEqual(self.admin_client.get("/admin/lms/submission/add/").status_code, 403)
        self.assertEqual(
            self.admin_client.post(
                "/admin/lms/submission/add/", {"status": "submitted"}
            ).status_code,
            403,
        )
        self.assertFalse(Submission.objects.exists())

    def test_admin_cannot_bypass_versioned_review_service(self):
        attempt = self.submit()
        feedback = self.review(attempt, decision="needs_revision", grade=None)
        response = self.admin_client.post(
            f"/admin/lms/feedback/{feedback.pk}/change/",
            {"comment": "Typo fixed", "grade": 100, "decision": "checked"},
        )
        self.assertEqual(response.status_code, 403)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "needs_revision")
        self.assertContains(
            self.admin_client.get(f"/admin/lms/feedback/{feedback.pk}/change/"), "Открыть проверку"
        )

    def test_all_admin_changelists_render(self):
        attempt = self.submit()
        self.review(attempt)
        for model in (
            "profile",
            "block",
            "topic",
            "assignment",
            "submission",
            "feedback",
            "submissionevent",
            "skill",
            "commentsnippet",
            "question",
            "choice",
            "quizattempt",
            "answerdraft",
            "flashcard",
            "cardreview",
        ):
            with self.subTest(model=model):
                self.assertEqual(self.admin_client.get(f"/admin/lms/{model}/").status_code, 200)
