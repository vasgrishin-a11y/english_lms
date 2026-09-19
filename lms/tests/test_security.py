from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, override_settings

from lms.models import Feedback, Profile, Submission
from lms.security import client_ip

from .base import LMSCase


class SecurityTests(LMSCase):
    def test_anonymous_redirected_to_login(self):
        for path in (
            "/",
            "/assignments/",
            self.url,
            "/teacher/submissions/",
            "/accounts/password/change/",
        ):
            self.assertEqual(Client().get(path).status_code, 302)

    def test_student_cannot_review(self):
        attempt = self.submit()
        response = self.student_client.post(
            f"/teacher/submissions/{attempt.pk}/",
            {
                "expected_version": 1,
                "expected_review_revision": 0,
                "grade": 100,
                "decision": "checked",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Feedback.objects.exists())

    def test_teacher_cannot_submit_as_student(self):
        self.assertEqual(
            self.teacher_client.post(
                self.url, {"expected_version": 0, "text_answer": "answer"}
            ).status_code,
            403,
        )
        self.assertFalse(Submission.objects.exists())

    def test_other_student_answer_not_in_html(self):
        self.submit(student=self.other, text_answer="PRIVATE_ANSWER_OTHER_STUDENT")
        self.assertNotContains(self.student_client.get(self.url), "PRIVATE_ANSWER_OTHER_STUDENT")

    def test_posted_student_and_grade_are_not_mass_assignable(self):
        self.student_client.post(
            self.url,
            {
                "expected_version": 0,
                "text_answer": "answer",
                "student": self.other.pk,
                "grade": 100,
                "status": "checked",
            },
        )
        attempt = Submission.objects.get()
        self.assertEqual(attempt.student_id, self.student.pk)
        self.assertEqual(attempt.status, "submitted")
        self.assertFalse(Feedback.objects.exists())

    def test_inactive_hierarchy_is_hidden_and_cannot_accept_new_answer(self):
        for obj in (self.assignment, self.topic, self.block):
            obj.is_active = False
            obj.save()
            self.assertEqual(self.student_client.get(self.url).status_code, 404)
            self.assertEqual(
                self.student_client.post(
                    self.url, {"expected_version": 0, "text_answer": "answer"}
                ).status_code,
                404,
            )
            obj.is_active = True
            obj.save()

    def test_csrf_and_logout_method(self):
        client = self.client_for(self.student, enforce_csrf_checks=True)
        self.assertEqual(
            client.post(self.url, {"text_answer": "No token", "expected_version": 0}).status_code,
            403,
        )
        self.assertEqual(
            client.post(
                "/accounts/login/", {"username": "student", "password": self.password}
            ).status_code,
            403,
        )
        self.assertEqual(client.get("/accounts/logout/").status_code, 405)

    def test_html_is_escaped(self):
        payload = "<script>alert('audit')</script>"
        attempt = self.submit(text_answer=payload)
        self.review(attempt, comment=payload)
        self.assignment.description = payload
        self.assignment.save()
        response = self.student_client.get(self.url)
        self.assertNotContains(response, payload)
        self.assertContains(response, "&lt;script&gt;")

    def test_external_next_is_rejected(self):
        response = Client().post(
            "/accounts/login/",
            {"username": "student", "password": self.password, "next": "https://example.invalid/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_login_throttle_and_other_user_independence(self):
        client = Client()
        responses = [
            client.post("/accounts/login/", {"username": "student", "password": "wrong"})
            for _ in range(3)
        ]
        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(
            client.post(
                "/accounts/login/", {"username": "student", "password": self.password}
            ).status_code,
            429,
        )
        self.assertEqual(
            client.post(
                "/accounts/login/", {"username": "other", "password": self.password}
            ).status_code,
            302,
        )

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_admin_login_is_throttled_too(self):
        client = Client()
        responses = [
            client.post(
                "/admin/login/", {"username": "owner", "password": "wrong", "next": "/admin/"}
            )
            for _ in range(3)
        ]
        self.assertEqual(responses[-1].status_code, 429)

    def test_untrusted_ip_header_is_ignored(self):
        request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1", HTTP_X_REAL_IP="203.0.113.5")
        self.assertEqual(client_ip(request), "192.0.2.1")

    @override_settings(TRUSTED_PROXY_IPS=["192.0.2.0/24"])
    def test_only_trusted_proxy_can_set_client_ip(self):
        request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1", HTTP_X_REAL_IP="203.0.113.5")
        self.assertEqual(client_ip(request), "203.0.113.5")
        request.META["HTTP_X_REAL_IP"] = "203.0.113.5, 1.2.3.4"
        self.assertIsNone(client_ip(request))

    def test_staff_is_not_implicitly_a_teacher(self):
        user = get_user_model().objects.create_user("staff", is_staff=True)
        self.assertEqual(user.profile.role, Profile.Role.STUDENT)

    def test_student_can_change_own_password(self):
        response = self.student_client.post(
            "/accounts/password/change/",
            {
                "old_password": self.password,
                "new_password1": "AnotherSecurePassword!2026",
                "new_password2": "AnotherSecurePassword!2026",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password("AnotherSecurePassword!2026"))
        self.assertEqual(self.student_client.get("/assignments/").status_code, 200)

    def test_health_endpoints(self):
        self.assertEqual(Client().get("/health/live/").json(), {"status": "ok"})
        self.assertEqual(Client().get("/health/ready/").json(), {"status": "ok"})
