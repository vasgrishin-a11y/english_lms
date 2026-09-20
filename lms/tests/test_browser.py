import json
import os
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from lms.models import Assignment, Block, Profile, Submission, Topic


@skipUnless(
    os.getenv("RUN_BROWSER_TESTS") == "1", "Opt-in Playwright/axe suite; runs in browser CI job."
)
class BrowserWorkflowTests(StaticLiveServerTestCase):
    def test_login_submit_review_resubmit_and_accessibility(self):
        from playwright.sync_api import expect, sync_playwright

        password = "BrowserTestingPassword!542"
        User = get_user_model()
        student = User.objects.create_user("browser_student", password=password)
        teacher = User.objects.create_user("browser_teacher", password=password)
        teacher.profile.role = Profile.Role.TEACHER
        teacher.profile.save()
        block = Block.objects.create(name="Browser block", slug="browser")
        topic = Topic.objects.create(block=block, title="Topic", slug="topic")
        assignment = Assignment.objects.create(
            topic=topic, title="Browser assignment", description="Write a sentence."
        )
        axe_path = Path(__file__).resolve().parents[2] / "node_modules/axe-core/axe.min.js"
        self.assertTrue(axe_path.is_file(), "Run npm ci to install axe-core")
        with sync_playwright() as playwright:
            options = {"headless": True}
            if os.getenv("BROWSER_EXECUTABLE"):
                options["executable_path"] = os.environ["BROWSER_EXECUTABLE"]
                options["args"] = json.loads(os.getenv("BROWSER_ARGS", "[]"))
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.set_default_timeout(15000)

            def check_page(name):
                page.add_script_tag(path=str(axe_path))
                violations = page.evaluate(
                    "async () => (await axe.run(document)).violations.map(v => ({id:v.id, targets:v.nodes.map(n=>n.target)}))"
                )
                page.set_viewport_size({"width": 320, "height": 800})
                overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
                missing_help = page.evaluate(
                    "Array.from(document.querySelectorAll('[aria-describedby]')).some(el => el.getAttribute('aria-describedby').split(/\\s+/).some(id => !document.getElementById(id)))"
                )
                if violations or overflow or missing_help:
                    Path("test-results").mkdir(exist_ok=True)
                    page.screenshot(path=f"test-results/{name}.png", full_page=True)
                self.assertEqual(violations, [], name)
                self.assertFalse(overflow, name)
                self.assertFalse(missing_help, name)
                page.set_viewport_size({"width": 1280, "height": 900})

            def login(username):
                page.goto(self.live_server_url + "/accounts/login/")
                page.get_by_label("Имя пользователя", exact=True).fill(username)
                page.get_by_label("Пароль", exact=True).fill(password)
                page.get_by_role("button", name="Войти", exact=True).click()
                page.wait_for_url(
                    "**/teacher/review/" if username == "browser_teacher" else "**/my/"
                )

            try:
                page.goto(self.live_server_url + "/accounts/login/")
                check_page("login")
                login("browser_student")
                check_page("home")
                page.goto(self.live_server_url + "/assignments/")
                check_page("catalog")
                page.get_by_role("link", name="Browser assignment", exact=True).click()
                check_page("assignment")
                page.get_by_label("Текстовый ответ", exact=True).fill("Browser answer")
                page.get_by_role("button", name="Отправить на проверку").click()
                expect(page.get_by_role("heading", name="Попытка 1", exact=True)).to_be_visible()
                page.get_by_role("button", name="Выйти", exact=True).click()
                login("browser_teacher")
                check_page("queue")
                page.get_by_role("link", name="Открыть", exact=True).click()
                check_page("review")
                page.get_by_label("Балл", exact=True).fill("80")
                page.get_by_label("Комментарий преподавателя", exact=True).fill(
                    "Good browser answer"
                )
                page.get_by_role("button", name="Сохранить проверку").click()
                expect(page.get_by_role("status")).to_contain_text("Проверка сохранена")
                page.get_by_role("button", name="Выйти", exact=True).click()
                login("browser_student")
                page.goto(self.live_server_url + f"/assignments/{assignment.pk}/")
                expect(page.get_by_text("Good browser answer", exact=True)).to_be_visible()
                page.get_by_label("Текстовый ответ", exact=True).fill("Second browser answer")
                page.get_by_role("button", name="Отправить на проверку").click()
                expect(page.get_by_role("heading", name="Попытка 2", exact=True)).to_be_visible()
            finally:
                context.close()
                browser.close()
        self.assertEqual(
            Submission.objects.filter(student=student, assignment=assignment).count(), 2
        )
        self.assertFalse(
            Submission.objects.get(student=student, assignment=assignment, version=2)
            .events.filter(action="reviewed")
            .exists()
        )
