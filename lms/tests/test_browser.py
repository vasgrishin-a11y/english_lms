import json
import os
import re
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from lms.models import Assignment, Block, Profile, Submission, Topic


@skipUnless(
    os.getenv("RUN_BROWSER_TESTS") == "1", "Opt-in Playwright/axe suite; runs in browser CI job."
)
class BrowserWorkflowTests(StaticLiveServerTestCase):
    def check_cascade_delete_panel(self, page):
        """Удаление блока целиком спрятано за подтверждением, а не висит на виду."""
        from playwright.sync_api import expect

        zone = page.locator(".danger-zone").first
        expect(zone).to_be_visible()
        zone.locator("summary").click()
        expect(zone.get_by_role("button", name="Удалить всё", exact=False)).to_be_visible()
        expect(zone.get_by_role("button", name="В архив", exact=False)).to_be_visible()
        zone.locator("summary").click()

    def check_description_editor(self, page):
        """«Условия задания» разворачиваются и складываются обратно."""
        from playwright.sync_api import expect

        block = page.locator("[data-editor-block]")
        expect(block).to_have_count(1)
        page.get_by_role("button", name="Развернуть", exact=False).click()
        expect(block).to_have_class(re.compile("is-expanded"))
        expect(
            page.get_by_role("button", name="Вернуть прежний размер", exact=False)
        ).to_be_visible()
        page.get_by_role("button", name="Вернуть прежний размер", exact=False).click()
        expect(block).not_to_have_class(re.compile("is-expanded"))

    def check_dropzone(self, page):
        """Файл, выбранный в скрытом поле, показывается в дропзоне с размером."""
        from playwright.sync_api import expect

        zone = page.locator(".dropzone").first
        expect(zone).to_be_visible()
        # Кнопка «Выбрать файл» открывает системный диалог — то есть поле живое.
        with page.expect_file_chooser() as chooser:
            zone.locator("[data-dropzone-pick]").click()
        chooser.value.set_files(
            files=[
                {
                    "name": "page.png",
                    "mimeType": "image/png",
                    "buffer": b"\x89PNG\r\n\x1a\n" + b"0" * 2048,
                }
            ]
        )
        # Имя и человекочитаемый размер: 2056 байт → «2,0 КБ».
        expect(zone.locator("[data-dropzone-name]")).to_have_text(
            re.compile(r"page\.png\s*2,0\s*КБ")
        )
        expect(zone).to_have_class(re.compile("is-filled"))
        page.locator("[data-dropzone-clear]").first.click()
        expect(zone.locator("[data-dropzone-name]")).to_be_hidden()

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
                overflow_info = ""
                if overflow:
                    overflow_info = page.evaluate(
                        """() => {
                          const vw = innerWidth;
                          const els = Array.from(document.querySelectorAll('*'))
                            .filter(el => el.scrollWidth > vw + 1)
                            .map(el => {
                              const r = el.getBoundingClientRect();
                              return `${el.tagName}${el.id ? '#'+el.id : ''}${el.className ? '.'+String(el.className).split(' ').slice(0,3).join('.') : ''} scrollW=${el.scrollWidth} rectW=${Math.round(r.width)} left=${Math.round(r.left)}`;
                            }).slice(0,80);
                          const form = document.getElementById('assignment-form');
                          let inside = [];
                          if(form){
                            const all = Array.from(form.querySelectorAll('*'));
                            for(const el of all){
                              if(el.scrollWidth > el.clientWidth + 2){
                                const r = el.getBoundingClientRect();
                                const cls = el.className ? String(el.className).slice(0,80) : '';
                                inside.push(`FORM-OVER ${el.tagName}${el.id ? '#'+el.id : ''} .${cls} scrollW=${el.scrollWidth} clientW=${el.clientWidth} rectW=${Math.round(r.width)} html=${el.outerHTML.slice(0,300).replace(/\\n/g,' ')}`);
                              }
                            }
                          }
                          // also check form sections themselves
                          const sections = Array.from(document.querySelectorAll('.form-section')).map(el=>{
                            const r=el.getBoundingClientRect();
                            return `SEC ${el.tagName} .${String(el.className).slice(0,60)} scrollW=${el.scrollWidth} clientW=${el.clientWidth} rectW=${Math.round(r.width)} left=${Math.round(r.left)}`;
                          });
                          return `docScroll=${document.documentElement.scrollWidth} inner=${innerWidth} bodyScroll=${document.body.scrollWidth}\\n` + els.join('\\n') + '\\n--- sections ---\\n' + sections.join('\\n') + '\\n--- form overflows ---\\n' + inside.slice(0,100).join('\\n');
                        }"""
                    )
                    print(f"OVERFLOW {name}:\\n{overflow_info}")
                missing_help = page.evaluate(
                    "Array.from(document.querySelectorAll('[aria-describedby]')).some(el => el.getAttribute('aria-describedby').split(/\\s+/).some(id => !document.getElementById(id)))"
                )
                if violations or overflow or missing_help:
                    Path("test-results").mkdir(exist_ok=True)
                    page.screenshot(path=f"test-results/{name}.png", full_page=True)
                self.assertEqual(violations, [], name)
                self.assertFalse(overflow, f"{name} overflow:\\n{overflow_info}")
                self.assertFalse(missing_help, name)
                page.set_viewport_size({"width": 1280, "height": 900})

            def login(username):
                page.goto(self.live_server_url + "/accounts/login/")
                page.get_by_label("Имя пользователя", exact=True).fill(username)
                page.get_by_label("Пароль", exact=True).fill(password)
                page.get_by_role("button", name="Войти", exact=True).click()
                page.wait_for_url("**/teacher/" if username == "browser_teacher" else "**/my/")

            def check_language(page):
                """Переключатель RU|ENG переводит меню и действия, выбор помнит cookie."""
                page.goto(self.live_server_url + "/teacher/")
                expect(page.locator(".topbar-side .lang-switch-option.is-active")).to_have_text(
                    "RU"
                )
                expect(
                    page.locator(".app-nav-label", has_text=re.compile("^Консоль$"))
                ).to_have_count(1)
                page.get_by_role("link", name="ENG", exact=True).click()
                page.wait_for_load_state("load")
                expect(
                    page.locator(".app-nav-label", has_text=re.compile("^Console$"))
                ).to_have_count(1)
                expect(
                    page.locator(".app-nav-label", has_text=re.compile("^Консоль$"))
                ).to_have_count(0)
                expect(page.locator(".topbar-side .lang-switch-option.is-active")).to_have_text(
                    "ENG"
                )
                page.goto(self.live_server_url + "/teacher/")
                expect(
                    page.locator(".app-nav-label", has_text=re.compile("^Console$"))
                ).to_have_count(1)
                page.get_by_role("link", name="RU", exact=True).click()
                page.wait_for_load_state("load")
                expect(
                    page.locator(".app-nav-label", has_text=re.compile("^Консоль$"))
                ).to_have_count(1)

            try:
                page.goto(self.live_server_url + "/accounts/login/")
                check_page("login")
                login("browser_student")
                check_page("home")
                page.goto(self.live_server_url + "/my/words/")
                check_page("dictionary")
                page.goto(self.live_server_url + "/assignments/")
                check_page("catalog")
                page.get_by_role("link", name="Browser assignment", exact=True).click()
                check_page("assignment")
                page.get_by_label("Текстовый ответ", exact=True).fill("Browser answer")
                page.get_by_role("button", name="Отправить на проверку").click()
                expect(page.get_by_role("heading", name="Попытка 1", exact=True)).to_be_visible()
                page.get_by_role("button", name="Выйти", exact=True).click()
                login("browser_teacher")
                check_language(page)
                check_page("queue")
                page.goto(self.live_server_url + "/teacher/curriculum/")
                check_page("curriculum")
                self.check_cascade_delete_panel(page)
                page.goto(self.live_server_url + "/teacher/analytics/")
                check_page("analytics")
                expect(page.get_by_role("link", name="Выгрузить XLSX", exact=False)).to_be_visible()
                page.goto(self.live_server_url + "/teacher/ai/")
                check_page("assistant")
                self.check_dropzone(page)
                page.goto(
                    self.live_server_url + f"/teacher/curriculum/assignments/{assignment.pk}/"
                )
                check_page("assignment-form")
                self.check_description_editor(page)
                page.goto(self.live_server_url + "/teacher/")
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


@skipUnless(
    os.getenv("RUN_BROWSER_TESTS") == "1", "Opt-in Playwright suite; runs in browser CI job."
)
class BrowserItemFlowTests(StaticLiveServerTestCase):
    """«Принять» → ✓/✗ без перезагрузки и запись с (поддельного) микрофона с лимитом."""

    def test_items_check_and_voice_recording(self):
        from playwright.sync_api import expect, sync_playwright

        from lms.models import Choice, Question, QuestionResponse

        password = "BrowserTestingPassword!542"
        student = get_user_model().objects.create_user("item_student", password=password)
        block = Block.objects.create(name="Items block", slug="items")
        topic = Topic.objects.create(block=block, title="Topic", slug="topic")
        quiz = Assignment.objects.create(
            topic=topic,
            title="Item lesson",
            description="Answer each item.",
            assignment_type=Assignment.Type.QUIZ,
            max_points=6,
        )
        gap = Question.objects.create(
            assignment=quiz, kind=Question.Kind.GAP, text="She ___ here.", points=2, order=1
        )
        Choice.objects.create(question=gap, text="lives", is_correct=True)
        voice = Question.objects.create(
            assignment=quiz,
            kind=Question.Kind.VOICE,
            text="Say hello.",
            points=4,
            order=2,
            recording_limit_seconds=10,
        )
        axe_path = Path(__file__).resolve().parents[2] / "node_modules/axe-core/axe.min.js"
        with sync_playwright() as playwright:
            # Поддельный микрофон Chromium: разрешение выдаётся без диалога, звук — тон.
            args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
            options = {"headless": True, "args": args}
            if os.getenv("BROWSER_EXECUTABLE"):
                options["executable_path"] = os.environ["BROWSER_EXECUTABLE"]
                options["args"] = args + json.loads(os.getenv("BROWSER_ARGS", "[]"))
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.grant_permissions(["microphone"])
            page = context.new_page()
            page.set_default_timeout(20000)
            try:
                page.goto(self.live_server_url + "/accounts/login/")
                page.get_by_label("Имя пользователя", exact=True).fill("item_student")
                page.get_by_label("Пароль", exact=True).fill(password)
                page.get_by_role("button", name="Войти", exact=True).click()
                page.wait_for_url("**/my/")
                page.goto(self.live_server_url + f"/assignments/{quiz.pk}/")
                if axe_path.is_file():
                    page.add_script_tag(path=str(axe_path))
                    violations = page.evaluate(
                        "async () => (await axe.run(document)).violations.map(v => v.id)"
                    )
                    self.assertEqual(violations, [], "quiz items")

                item = page.locator(f"#q-{gap.pk}")
                item.locator("input[type=text]").fill("live")
                item.get_by_role("button", name="Принять").click()
                item = page.locator(f"#q-{gap.pk}")
                expect(item).to_contain_text("Осталось попыток: 2")
                item.locator("input[type=text]").fill("lives")
                item.get_by_role("button", name="Принять").click()
                expect(page.locator(f"#q-{gap.pk}")).to_contain_text("Верно")
                expect(page.locator("#quiz-progress")).to_contain_text("Выполнено 1 из 2")

                recorder = page.locator(f"#q-{voice.pk} [data-recorder]")
                expect(recorder).to_have_attribute("data-limit", "10")
                recorder.get_by_role("button", name="Начать запись").click()
                expect(recorder).to_have_class(re.compile("is-recording"))
                page.wait_for_timeout(1500)
                recorder.get_by_role("button", name="Остановить").click()
                expect(recorder.locator("[data-recorder-take]")).to_be_visible()
                expect(recorder.locator("[data-recorder-status]")).to_contain_text("Запись готова")
                # Удалить и записать заново — без ограничения на число записей.
                recorder.get_by_role("button", name="Удалить запись").click()
                expect(recorder.locator("[data-recorder-take]")).to_be_hidden()
                recorder.get_by_role("button", name="Начать запись").click()
                page.wait_for_timeout(1200)
                recorder.get_by_role("button", name="Остановить").click()
                expect(recorder.locator("[data-recorder-take]")).to_be_visible()
                # Лимит 10 секунд: запись останавливается сама, с предупреждением заранее.
                recorder.get_by_role("button", name="Удалить запись").click()
                recorder.get_by_role("button", name="Начать запись").click()
                expect(recorder).to_have_class(re.compile("is-ending"), timeout=4000)
                expect(recorder.locator("[data-recorder-take]")).to_be_visible(timeout=15000)
                expect(recorder).not_to_have_class(re.compile("is-recording"))
                page.locator(f"#q-{voice.pk}").get_by_role("button", name="Принять").click()
                expect(page.locator(f"#q-{voice.pk}")).to_contain_text("Ответ принят")
                expect(page.locator("#quiz-progress")).to_contain_text("Отправить преподавателю")
            finally:
                context.close()
                browser.close()
        response = QuestionResponse.objects.get(student=student, question=voice)
        self.assertTrue(response.file_answer.name.endswith(".wav"))
        with response.file_answer.open("rb") as handle:
            header = handle.read(44)
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(int.from_bytes(header[24:28], "little"), 16000, "WAV 16 кГц")
        self.assertEqual(int.from_bytes(header[22:24], "little"), 1, "моно")
