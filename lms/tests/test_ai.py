"""ИИ-помощник преподавателя: офлайн-разбор, провайдер через мок, импорт черновиками.

Сеть в тестах не используется: онлайн-режим проверяется подменой функции запроса
к провайдеру, поэтому CI работает без ключей и без интернета.
"""

import io
import json
import ssl
import time
import urllib.error
import zipfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from lms import ai
from lms.models import Assignment, Block, Flashcard, Question, Topic

from .base import LMSCase

MARKDOWN = """# Travel B1
## At the airport
### Check-in [quiz]
Прочитайте диалог и ответьте на вопросы.
? What do you show at the check-in desk?
* passport
- luggage
? Выберите два удобных места [multi]
* window seat
* aisle seat
- cargo hold
### Слова темы [карточки]
Слова из диалога на стойке регистрации.
- to book | бронировать | We booked a table.
- itinerary | маршрут
- delay | задержка
"""


def docx_blob(text):
    """Минимальный DOCX: только текст, как его читает офлайн-разбор."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


def xlsx_blob(rows):
    """Минимальный XLSX: таблица слов «слово | перевод»."""
    buffer = io.BytesIO()
    shared = "".join(f"<si><t>{cell}</t></si>" for row in rows for cell in row)
    sheet_rows = []
    for index, row in enumerate(rows):
        cells = "".join(
            f'<c r="{chr(65 + position)}{index + 1}" t="s"><v>{index * len(row) + position}</v></c>'
            for position in range(len(row))
        )
        sheet_rows.append(f'<row r="{index + 1}">{cells}</row>')
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>",
        )
    return buffer.getvalue()


class FakeResponse:
    """Минимальный ответ urlopen: JSON-тело и контекстный менеджер."""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeTransport:
    """Подмена urlopen: записывает запросы и отдаёт заготовленные ответы по порядку."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None, context=None):
        body = request.data or b""
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {key.lower(): value for key, value in request.header_items()},
                "body": body.decode("utf-8", errors="ignore"),
                "raw": body,
            }
        )
        payload = self.responses.pop(0) if self.responses else {}
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)


class AssistantAccessTests(LMSCase):
    def test_student_cannot_open_assistant(self):
        self.assertEqual(self.student_client.get(reverse("teacher_ai")).status_code, 302)

    def test_anonymous_is_redirected_to_login(self):
        self.assertEqual(self.client.get(reverse("teacher_ai")).status_code, 302)

    def test_teacher_sees_offline_mode_without_key(self):
        response = self.teacher_client.get(reverse("teacher_ai"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Офлайн-разбор")
        self.assertContains(response, "Формат подсказки")

    @override_settings(LMS_AI_ENABLED=False)
    def test_disabled_assistant_explains_itself(self):
        response = self.teacher_client.get(reverse("teacher_ai"))
        self.assertContains(response, "Помощник выключен")
        self.assertNotContains(response, "Собрать материал")

    def test_curriculum_links_to_assistant(self):
        content = self.teacher_client.get(reverse("teacher_curriculum")).content.decode()
        self.assertIn(reverse("teacher_ai"), content)


class OfflineParsingTests(LMSCase):
    def test_markdown_becomes_block_topics_questions_and_cards(self):
        material = ai.normalise(ai.parse_text(MARKDOWN, source="Travel"))
        summary = ai.material_summary(material)
        self.assertEqual(summary["blocks"], 1)
        self.assertEqual(summary["topics"], 1)
        self.assertEqual(summary["questions"], 2)
        self.assertEqual(summary["card_sets"], 1)
        self.assertEqual(summary["cards"], 3)

        topic = material["blocks"][0]["topics"][0]
        quiz = topic["assignments"][0]
        self.assertEqual(quiz["type"], Assignment.Type.QUIZ)
        self.assertEqual(quiz["questions"][0]["choices"][0]["correct"], True)
        self.assertEqual(quiz["questions"][0]["choices"][1]["correct"], False)
        self.assertEqual(quiz["questions"][1]["kind"], Question.Kind.MULTI)
        card_set = topic["cards"][0]
        self.assertEqual(card_set["title"], "Слова темы")
        self.assertEqual(card_set["cards"][0]["example"], "We booked a table.")

    def test_plain_text_without_headings_still_builds_a_topic(self):
        material = ai.normalise(
            ai.parse_text("Прочитайте текст и напишите ответ на вопрос.", source="Заметка")
        )
        summary = ai.material_summary(material)
        self.assertEqual(summary["blocks"], 1)
        self.assertEqual(summary["topics"], 1)
        self.assertEqual(summary["assignments"], 1)

    def test_limits_cut_the_answer_instead_of_failing(self):
        payload = {
            "blocks": [
                {
                    "name": f"Блок {index}",
                    "topics": [
                        {
                            "title": "Тема",
                            "assignments": [
                                {
                                    "type": "text",
                                    "title": "Задание",
                                    "description": "Условие",
                                }
                            ],
                        }
                    ],
                }
                for index in range(20)
            ]
        }
        material = ai.normalise(payload)
        self.assertEqual(len(material["blocks"]), ai.limits()["blocks"])

    def test_broken_payload_is_rejected_with_a_hint(self):
        with self.assertRaises(ai.AiError):
            ai.normalise({"blocks": []})
        with self.assertRaises(ai.AiError):
            ai.normalise("не структура")

    def test_docx_and_xlsx_text_is_extracted_offline(self):
        docx_text = ai.extract_text("lesson.docx", docx_blob("# Блок\n## Тема\nЗадание"))
        self.assertIn("Тема", docx_text)
        sheet_text = ai.extract_text(
            "words.xlsx", xlsx_blob([["to book", "бронировать"], ["delay", "задержка"]])
        )
        self.assertIn("to book", sheet_text)
        self.assertIn("задержка", sheet_text)

    def test_extensions_and_notes_are_explained(self):
        self.assertEqual(ai.upload_kind("page.jpg"), "image")
        self.assertEqual(ai.upload_kind("lesson.docx"), "docx")
        self.assertIn("нужен ключ", ai.offline_notes("page.jpg"))
        self.assertIn("нужен ключ", ai.offline_notes("clip.mp4"))
        self.assertEqual(ai.offline_notes("text.txt"), "")


class AssistantFormTests(LMSCase):
    def post_form(self, **data):
        payload = {"target": "mixed", "prompt": "", "text": "", "target_topic": ""}
        payload.update(data)
        return self.teacher_client.post(reverse("teacher_ai"), payload)

    def test_empty_request_asks_for_material(self):
        response = self.post_form()
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            None,
            "Приложите файл, вставьте текст или опишите задачу словами.",
        )

    def test_unsupported_file_is_rejected(self):
        upload = SimpleUploadedFile("virus.exe", b"MZ")
        response = self.post_form(upload=upload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["upload"])

    @override_settings(LMS_AI_MAX_FILE_BYTES=1024)
    def test_too_large_file_is_rejected(self):
        upload = SimpleUploadedFile("big.txt", b"x" * 2048)
        response = self.post_form(upload=upload)
        self.assertTrue(response.context["form"].errors["upload"])

    def test_preview_and_reset(self):
        response = self.post_form(text=MARKDOWN)
        self.assertContains(response, "Что получилось")
        self.assertContains(response, "Импортировать черновиками")
        cleared = self.teacher_client.post(reverse("teacher_ai"), {"reset": "1"}, follow=True)
        self.assertNotContains(cleared, "Что получилось")


class OfflineImportTests(LMSCase):
    def build(self, **data):
        payload = {"target": "mixed", "prompt": "", "text": MARKDOWN, "target_topic": ""}
        payload.update(data)
        return self.teacher_client.post(reverse("teacher_ai"), payload)

    def import_material(self):
        return self.teacher_client.post(reverse("teacher_ai_import"))

    def test_import_creates_only_drafts(self):
        self.build()
        response = self.import_material()
        self.assertRedirects(response, reverse("teacher_curriculum"))

        block = Block.objects.get(name="Travel B1")
        self.assertTrue(block.is_active)
        assignment = Assignment.objects.get(title="Check-in")
        self.assertEqual(assignment.status, Assignment.Publication.DRAFT)
        self.assertEqual(assignment.assignment_type, Assignment.Type.QUIZ)
        self.assertEqual(assignment.questions.count(), 2)
        self.assertTrue(
            assignment.questions.get(kind=Question.Kind.MCQ)
            .choices.filter(is_correct=True)
            .exists()
        )
        self.assertEqual(assignment.max_points, assignment.total_question_points)
        self.assertTrue(assignment.skills.exists())

        cards = Assignment.objects.get(title="Слова темы")
        self.assertEqual(cards.assignment_type, Assignment.Type.FLASHCARDS)
        self.assertEqual(cards.status, Assignment.Publication.DRAFT)
        self.assertEqual(cards.cards.count(), 3)
        self.assertEqual(Flashcard.objects.count(), 3)

        # Ученик не видит ни один черновик — даже когда в курсе есть опубликованное задание.
        self.assertFalse(Assignment.objects.visible(self.student).filter(title="Check-in").exists())
        self.assertFalse(
            Assignment.objects.visible(self.student).filter(title="Слова темы").exists()
        )

    def test_import_into_existing_topic_keeps_structure(self):
        topic = Topic.objects.create(block=self.block, title="Airport", slug="airport")
        self.build(target_topic=topic.pk)
        self.import_material()
        self.assertEqual(Block.objects.count(), 1)
        self.assertEqual(Topic.objects.filter(block=self.block).count(), 2)
        titles = set(Assignment.objects.filter(topic=topic).values_list("title", flat=True))
        self.assertEqual(titles, {"Check-in", "Слова темы"})

    def test_second_import_does_not_duplicate_cards(self):
        self.build()
        self.import_material()
        self.build()
        response = self.import_material()
        self.assertEqual(Flashcard.objects.count(), 3)
        self.assertEqual(
            Assignment.objects.filter(assignment_type=Assignment.Type.FLASHCARDS).count(), 1
        )
        messages = [message.message for message in response.wsgi_request._messages]
        self.assertTrue(any("Повторов пропущено" in message for message in messages))

    def test_import_without_material_redirects_with_error(self):
        response = self.import_material()
        self.assertRedirects(response, reverse("teacher_ai"))
        self.assertEqual(Assignment.objects.count(), 1)  # только задание из базового LMSCase

    def test_uploaded_docx_material_is_imported(self):
        upload = SimpleUploadedFile("lesson.docx", docx_blob("Прочитайте текст и напишите эссе."))
        self.build(text="", upload=upload)
        self.import_material()
        self.assertTrue(Assignment.objects.filter(status=Assignment.Publication.DRAFT).exists())


class OnlineModeTests(LMSCase):
    AI_PAYLOAD = {
        "title": "Travel speaking",
        "blocks": [
            {
                "name": "Travel B1",
                "cefr_level": "B1",
                "description": "Разговорный блок про путешествия.",
                "topics": [
                    {
                        "title": "At the airport",
                        "description": "Лексика и диалоги.",
                        "assignments": [
                            {
                                "type": "quiz",
                                "title": "Airport quiz",
                                "description": "Ответьте на вопросы.",
                                "max_points": 10,
                                "skills": ["vocabulary"],
                                "questions": [
                                    {
                                        "kind": "mcq",
                                        "text": "Where do you check in?",
                                        "choices": [
                                            {"text": "At the desk", "correct": True},
                                            {"text": "In the sky", "correct": False},
                                        ],
                                    }
                                ],
                            }
                        ],
                        "cards": [
                            {
                                "title": "Airport words",
                                "description": "",
                                "cards": [
                                    {"front": "gate", "back": "выход на посадку", "example": ""},
                                    {"front": "delay", "back": "задержка", "example": ""},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    @override_settings(LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="gemini")
    def test_online_answer_is_imported_as_drafts(self):
        with patch("lms.ai._gemini_material", return_value=self.AI_PAYLOAD):
            response = self.teacher_client.post(
                reverse("teacher_ai"), {"target": "mixed", "prompt": "Сделай блок B1"}
            )
        self.assertContains(response, "разобрал ИИ")
        self.teacher_client.post(reverse("teacher_ai_import"))
        assignment = Assignment.objects.get(title="Airport quiz")
        self.assertEqual(assignment.status, Assignment.Publication.DRAFT)
        self.assertEqual(Block.objects.get(name="Travel B1").cefr_level, "B1")
        self.assertEqual(Flashcard.objects.count(), 2)

    @override_settings(LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="gemini")
    def test_provider_failure_falls_back_to_offline(self):
        with patch("lms.ai._gemini_material", side_effect=ai.AiError("Провайдер недоступен")):
            response = self.teacher_client.post(
                reverse("teacher_ai"), {"target": "mixed", "text": MARKDOWN}
            )
        self.assertContains(response, "разобран офлайн")
        self.assertContains(response, "Что получилось")

    @override_settings(LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="gemini")
    def test_online_mode_sends_only_the_prompt_and_file(self):
        captured = {}

        def fake(parts):
            captured["parts"] = parts
            return self.AI_PAYLOAD

        with patch("lms.ai._gemini_material", side_effect=fake):
            self.teacher_client.post(
                reverse("teacher_ai"), {"target": "cards", "text": "gate | выход"}
            )
        self.assertEqual(len(captured["parts"]), 1)
        self.assertIn("карточки", captured["parts"][0]["text"].lower())


class AssistantModelTests(LMSCase):
    def test_mode_switches_between_offline_and_online(self):
        self.assertEqual(ai.ai_mode(), "offline")
        with override_settings(LMS_AI_API_KEY="  "):
            self.assertEqual(ai.ai_mode(), "offline")
        with override_settings(LMS_AI_API_KEY="key"):
            self.assertEqual(ai.ai_mode(), "online")
            self.assertIn("GigaChat", ai.ai_mode_label())
            self.assertIn("GigaChat-2", ai.ai_mode_label())
        with override_settings(LMS_AI_API_KEY="key", LMS_AI_PROVIDER="gemini"):
            self.assertIn("gemini-2.0-flash", ai.ai_mode_label())
        with override_settings(LMS_AI_ENABLED=False):
            self.assertEqual(ai.ai_mode(), "off")
            self.assertIn("выключен", ai.ai_mode_label())

    def test_prompt_contains_schema_and_teacher_wishes(self):
        prompt = ai.build_prompt(
            "текст", prompt="побольше лексики", target="quiz", filename="a.txt"
        )
        self.assertIn("побольше лексики", prompt)
        self.assertIn("JSON", prompt)
        self.assertIn("текст", prompt)


class ProviderSettingsTests(SimpleTestCase):
    """Выбор провайдера: доступный из РФ по умолчанию, пресеты и переопределения."""

    def test_default_provider_works_in_russia_without_vpn(self):
        self.assertEqual(ai.ai_provider(), "gigachat")
        spec = ai.provider_spec()
        self.assertEqual(spec["kind"], "gigachat")
        self.assertEqual(spec["label"], "GigaChat (Сбер)")
        self.assertEqual(ai.ai_model(), "GigaChat-2")
        self.assertEqual(ai.ai_endpoint(), "https://api.giga.chat/v1")
        self.assertIn("image", ai.provider_uploads())
        self.assertIn("сертификат", ai.provider_hint())

    def test_legacy_gemini_model_and_endpoint_do_not_leak(self):
        with override_settings(
            LMS_AI_PROVIDER="gigachat",
            LMS_AI_MODEL="gemini-2.0-flash",
            LMS_AI_ENDPOINT="https://generativelanguage.googleapis.com/v1beta/models",
        ):
            self.assertEqual(ai.ai_model(), "GigaChat-2")
            self.assertEqual(ai.ai_endpoint(), "https://api.giga.chat/v1")

    def test_own_model_value_is_kept_for_openai_compatible_gateway(self):
        with override_settings(
            LMS_AI_PROVIDER="openrouter",
            LMS_AI_MODEL="google/gemma-3-27b-it:free",
            LMS_AI_ENDPOINT="",
        ):
            self.assertEqual(ai.ai_endpoint(), "https://openrouter.ai/api/v1")
            self.assertEqual(ai.ai_model(), "google/gemma-3-27b-it:free")
            self.assertEqual(ai.provider_spec()["kind"], "openai")

    def test_yandex_preset_has_no_vision_and_ollama_can_be_local(self):
        with override_settings(LMS_AI_PROVIDER="yandex", LMS_AI_ENDPOINT=""):
            self.assertEqual(ai.ai_endpoint(), "https://ai.api.cloud.yandex.net/v1")
            self.assertEqual(ai.provider_uploads(), ())
        with override_settings(
            LMS_AI_PROVIDER="ollama", LMS_AI_MODEL="qwen3:8b", LMS_AI_ENDPOINT=""
        ):
            self.assertEqual(ai.ai_endpoint(), "http://localhost:11434/v1")
            self.assertEqual(ai.ai_model(), "qwen3:8b")

    def test_unknown_provider_is_treated_as_openai_compatible(self):
        with override_settings(
            LMS_AI_PROVIDER="my-gateway", LMS_AI_ENDPOINT="https://gw.example/v1/"
        ):
            spec = ai.provider_spec()
            self.assertEqual(spec["kind"], "openai")
            self.assertIn("my-gateway", spec["label"])
            self.assertEqual(ai.ai_endpoint(), "https://gw.example/v1")

    def test_upload_kinds_are_overridable_and_typos_fall_back(self):
        with override_settings(LMS_AI_UPLOAD_KINDS="pdf"):
            self.assertEqual(ai.provider_uploads(), ("pdf",))
        with override_settings(LMS_AI_UPLOAD_KINDS="none"):
            self.assertEqual(ai.provider_uploads(), ())
        with override_settings(LMS_AI_UPLOAD_KINDS="мусор"):
            self.assertEqual(ai.provider_uploads(), ai.provider_spec()["uploads"])

    def test_unsupported_upload_note_explains_provider_limits(self):
        spec = ai.provider_spec("yandex")
        self.assertIn("не читает видео", ai.unsupported_upload_note("clip.mp4", spec))
        self.assertIn("не читает фото", ai.unsupported_upload_note("page.jpg", spec))
        self.assertEqual(ai.unsupported_upload_note("lesson.docx", spec), "")
        self.assertIn("вставьте текст", ai.unsupported_upload_note("virus.exe", spec))

    def test_mov_extension_maps_to_quicktime(self):
        self.assertEqual(ai._mime_type("clip.mov", b"x"), "video/quicktime")

    def test_json_from_text_strips_fences_and_prose(self):
        self.assertEqual(ai._json_from_text('```json\n{"blocks": []}\n```'), {"blocks": []})
        self.assertEqual(
            ai._json_from_text('Материал: {"blocks": []} — проверьте.'), {"blocks": []}
        )
        with self.assertRaises(ai.AiError):
            ai._json_from_text("   ")
        with self.assertRaises(ai.AiError):
            ai._json_from_text("не JSON")


class ProviderTransportTests(SimpleTestCase):
    """Транспорт адаптеров: Gemini, OpenAI-совместимый шлюз и GigaChat с OAuth."""

    def setUp(self):
        ai._GIGACHAT_TOKEN.update(value="", expires_at=0.0)

    def gemini_response(self, payload=None):
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload or {})}]}}]}

    def openai_response(self, payload=None):
        return {"choices": [{"message": {"content": json.dumps(payload or {})}}]}

    def test_gemini_sends_key_in_header_and_photo_inline(self):
        material = {"blocks": [{"name": "Travel"}]}
        transport = FakeTransport(self.gemini_response(material))
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_API_KEY="secret", LMS_AI_PROVIDER="gemini"),
        ):
            payload = ai._provider_material(
                ai.provider_spec(), "промпт", filename="page.jpg", blob=b"\xff\xd8\xff"
            )
        self.assertEqual(payload, material)
        request = transport.requests[0]
        self.assertEqual(
            request["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.0-flash:generateContent",
        )
        self.assertEqual(request["headers"]["x-goog-api-key"], "secret")
        body = json.loads(request["body"])
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "промпт")
        self.assertEqual(body["contents"][0]["parts"][1]["inline_data"]["mime_type"], "image/jpeg")

    def test_openai_compatible_sends_data_url_and_json_mode(self):
        material = {"blocks": []}
        transport = FakeTransport(self.openai_response(material))
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(
                LMS_AI_API_KEY="k",
                LMS_AI_PROVIDER="openrouter",
                LMS_AI_MODEL="google/gemma-3-27b-it:free",
            ),
        ):
            payload = ai._provider_material(
                ai.provider_spec(), "промпт", filename="page.png", blob=b"\x89PNG"
            )
        self.assertEqual(payload, material)
        request = transport.requests[0]
        body = json.loads(request["body"])
        self.assertEqual(request["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request["headers"]["authorization"], "Bearer k")
        self.assertEqual(body["model"], "google/gemma-3-27b-it:free")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        image = body["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/png;base64,"))

    def test_local_model_without_json_mode_and_without_key_header(self):
        transport = FakeTransport(self.openai_response(), self.openai_response())
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(
                LMS_AI_API_KEY="local",
                LMS_AI_PROVIDER="ollama",
                LMS_AI_JSON_MODE=False,
                LMS_AI_UPLOAD_KINDS="none",
            ),
        ):
            ai._provider_material(ai.provider_spec(), "промпт")
        body = json.loads(transport.requests[0]["body"])
        self.assertEqual(transport.requests[0]["url"], "http://localhost:11434/v1/chat/completions")
        self.assertNotIn("response_format", body)
        self.assertEqual(body["messages"][0]["content"], "промпт")

    def test_openai_provider_requires_endpoint(self):
        with override_settings(LMS_AI_PROVIDER="openai", LMS_AI_ENDPOINT=""):
            with self.assertRaisesMessage(ai.AiError, "LMS_AI_ENDPOINT"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_gigachat_uploads_attachment_then_reuses_oauth_token(self):
        material = {"blocks": [{"name": "Travel"}]}
        transport = FakeTransport(
            {"access_token": "tok-1", "expires_at": (time.time() + 1800) * 1000},
            {"id": "file-1"},
            {"choices": [{"message": {"content": f"```json\n{json.dumps(material)}\n```"}}]},
        )
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_API_KEY="auth-key"),
        ):
            payload = ai._provider_material(
                ai.provider_spec(), "промпт", filename="page.jpg", blob=b"\xff\xd8\xff"
            )
        self.assertEqual(payload, material)
        oauth, upload, chat = transport.requests
        self.assertEqual(oauth["url"], "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
        self.assertEqual(oauth["headers"]["authorization"], "Basic auth-key")
        self.assertEqual(len(oauth["headers"]["rquid"]), 36)
        self.assertIn("scope=GIGACHAT_API_PERS", oauth["body"])
        self.assertEqual(upload["url"], "https://api.giga.chat/v1/files")
        self.assertEqual(upload["headers"]["authorization"], "Bearer tok-1")
        self.assertIn('filename="page.jpg"', upload["body"])
        self.assertIn("Content-Type: image/jpeg", upload["body"])
        self.assertEqual(json.loads(chat["body"])["messages"][0]["attachments"], ["file-1"])
        self.assertEqual(json.loads(chat["body"])["model"], "GigaChat-2")

        cached = FakeTransport(self.openai_response())
        with (
            patch("lms.ai.urllib.request.urlopen", cached),
            override_settings(LMS_AI_API_KEY="auth-key"),
        ):
            ai._provider_material(ai.provider_spec(), "промпт")
        self.assertEqual(len(cached.requests), 1)  # токен взят из кэша

    def test_gigachat_reads_v2_content_parts_and_expired_token(self):
        ai._GIGACHAT_TOKEN.update(value="old", expires_at=0.0)
        transport = FakeTransport(
            {"access_token": "tok-2", "expires_at": 0},
            {"choices": [{"message": {"content": [{"text": '{"blocks": []}'}]}}]},
        )
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_API_KEY="auth-key"),
        ):
            payload = ai._provider_material(ai.provider_spec(), "промпт")
        self.assertEqual(payload, {"blocks": []})
        self.assertEqual(len(transport.requests), 2)  # OAuth + генерация

    def test_gigachat_scope_and_auth_endpoint_can_be_overridden(self):
        transport = FakeTransport(
            {"access_token": "tok"}, {"choices": [{"message": {"content": "{}"}}]}
        )
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(
                LMS_AI_API_KEY="key",
                LMS_AI_SCOPE="GIGACHAT_API_B2B",
                LMS_AI_AUTH_ENDPOINT="https://auth.example/oauth",
            ),
        ):
            ai._provider_material(ai.provider_spec(), "промпт")
        self.assertEqual(transport.requests[0]["url"], "https://auth.example/oauth")
        self.assertIn("scope=GIGACHAT_API_B2B", transport.requests[0]["body"])

    def test_key_is_rejected_with_a_readable_error(self):
        error = urllib.error.HTTPError(
            "https://api.giga.chat/v1/chat/completions", 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )
        transport = FakeTransport({"access_token": "tok"}, error)
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_API_KEY="bad"),
        ):
            with self.assertRaisesMessage(ai.AiError, "Ключ ИИ отклонён"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_tls_error_points_to_mindigital_certificate(self):
        transport = FakeTransport(
            urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate in chain"))
        )
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_API_KEY="k"),
        ):
            with self.assertRaisesMessage(ai.AiError, "НУЦ Минцифры"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_ssl_settings_build_the_context(self):
        with override_settings(LMS_AI_VERIFY_SSL=True, LMS_AI_CA_BUNDLE=""):
            self.assertIsNone(ai._ssl_context())
        with override_settings(LMS_AI_CA_BUNDLE="/нет/такого/файла.pem"):
            with self.assertRaisesMessage(ai.AiError, "LMS_AI_CA_BUNDLE"):
                ai._ssl_context()
        with override_settings(LMS_AI_VERIFY_SSL=False):
            self.assertFalse(ai._ssl_context().verify_mode)  # CERT_NONE — явный отказ админа


class ProviderFallbackTests(SimpleTestCase):
    """Файл, который провайдер не читает, честно уходит в офлайн-разбор."""

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_API_KEY="key", LMS_AI_PROVIDER="yandex")
    def test_video_is_not_sent_to_text_provider_and_is_reported(self):
        captured = {}

        def fake(spec, prompt_text, *, filename="", blob=b""):
            captured.update(filename=filename, blob=blob, label=spec["label"])
            raise ai.AiError("нет сети")

        upload = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42")
        with patch("lms.ai._provider_material", side_effect=fake):
            material, meta = ai.build_material(upload=upload, text=MARKDOWN)
        self.assertEqual(captured["filename"], "")
        self.assertEqual(captured["blob"], b"")
        self.assertEqual(meta["result"], "offline")
        self.assertEqual(meta["provider"], "yandex")
        self.assertIn("Yandex AI Studio", meta["provider_label"])
        self.assertTrue(any("не читает видео" in note for note in meta["notes"]))
        self.assertTrue(any("разобран офлайн" in note for note in meta["notes"]))
        self.assertTrue(material["blocks"])

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_API_KEY="key", LMS_AI_PROVIDER="yandex")
    def test_photo_without_text_asks_for_manual_text(self):
        upload = SimpleUploadedFile("page.jpg", b"\xff\xd8\xff")
        with patch("lms.ai._provider_material", side_effect=AssertionError("не вызывается")):
            with self.assertRaisesMessage(ai.AiError, "вставьте текст вручную"):
                ai.build_material(upload=upload)

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_API_KEY="")
    def test_offline_mode_reports_that_photo_cannot_be_read(self):
        upload = SimpleUploadedFile("page.jpg", b"\xff\xd8\xff")
        _, meta = ai.build_material(upload=upload, prompt="Соберите тему Travel")
        self.assertEqual(meta["result"], "offline")
        self.assertTrue(any("нужен ключ ИИ" in note for note in meta["notes"]))


class AiCheckCommandTests(SimpleTestCase):
    """`manage.py ai_check`: показать настройки и по флагу --live проверить ключ."""

    SAMPLE_MATERIAL = {
        "blocks": [
            {
                "name": "Travel A2",
                "description": "",
                "cefr_level": "A2",
                "topics": [
                    {
                        "title": "At the airport",
                        "description": "",
                        "assignments": [
                            {
                                "type": "quiz",
                                "title": "Airport quiz",
                                "description": "Ответьте на вопросы.",
                                "questions": [
                                    {
                                        "kind": "mcq",
                                        "text": "Where do you check in?",
                                        "choices": [{"text": "At the desk", "correct": True}],
                                    }
                                ],
                            }
                        ],
                        "cards": [],
                    }
                ],
            }
        ]
    }

    def setUp(self):
        ai._GIGACHAT_TOKEN.update(value="", expires_at=0.0)

    def test_offline_mode_is_reported_without_network(self):
        output = io.StringIO()
        call_command("ai_check", stdout=output)
        text = output.getvalue()
        self.assertIn("Офлайн-разбор", text)
        self.assertIn("GigaChat (Сбер)", text)
        self.assertIn("https://api.giga.chat/v1", text)

    @override_settings(LMS_AI_ENABLED=False)
    def test_disabled_assistant_stops_the_check(self):
        with self.assertRaisesMessage(CommandError, "выключен"):
            call_command("ai_check")

    @override_settings(LMS_AI_API_KEY="key")
    def test_live_check_confirms_the_provider_answer(self):
        transport = FakeTransport(
            {"access_token": "tok", "expires_at": (time.time() + 1800) * 1000},
            {"choices": [{"message": {"content": json.dumps(self.SAMPLE_MATERIAL)}}]},
        )
        output = io.StringIO()
        with patch("lms.ai.urllib.request.urlopen", transport):
            call_command("ai_check", "--live", stdout=output)
        self.assertIn("Провайдер ответил", output.getvalue())
        self.assertIn("Импорт не выполнялся", output.getvalue())
        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                "https://api.giga.chat/v1/chat/completions",
            ],
        )

    @override_settings(LMS_AI_API_KEY="key")
    def test_live_check_fails_with_the_provider_reason(self):
        transport = FakeTransport(
            urllib.error.URLError("network down"), urllib.error.URLError("network down")
        )
        with patch("lms.ai.urllib.request.urlopen", transport):
            with self.assertRaisesMessage(CommandError, "Онлайн-разбор не сработал"):
                call_command("ai_check", "--live")
