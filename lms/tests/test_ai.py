"""ИИ-помощник преподавателя: офлайн-разбор, провайдер через мок, импорт черновиками.

Сеть в тестах не используется: онлайн-режим проверяется подменой функции запроса
к провайдеру, поэтому CI работает без ключей и без интернета.
"""

import io
import json
import ssl
import urllib.error
import zipfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from lms import ai
from lms.models import Assignment, Block, Choice, Flashcard, Question, Topic

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
        self.assertIn("LMS_AI_LOCAL", ai.offline_notes("page.jpg"))
        self.assertIn("LMS_AI_LOCAL", ai.offline_notes("clip.mp4"))
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

    @override_settings(
        LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="ollama", LMS_AI_LOCAL=True
    )
    def test_online_answer_is_imported_as_drafts(self):
        with patch("lms.ai._provider_material", return_value=self.AI_PAYLOAD):
            response = self.teacher_client.post(
                reverse("teacher_ai"), {"target": "mixed", "prompt": "Сделай блок B1"}
            )
        self.assertContains(response, "разобрал ИИ")
        self.teacher_client.post(reverse("teacher_ai_import"))
        assignment = Assignment.objects.get(title="Airport quiz")
        self.assertEqual(assignment.status, Assignment.Publication.DRAFT)
        self.assertEqual(Block.objects.get(name="Travel B1").cefr_level, "B1")
        self.assertEqual(Flashcard.objects.count(), 2)

    @override_settings(
        LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="ollama", LMS_AI_LOCAL=True
    )
    def test_provider_failure_falls_back_to_offline(self):
        with patch("lms.ai._provider_material", side_effect=ai.AiError("Модель недоступна")):
            response = self.teacher_client.post(
                reverse("teacher_ai"), {"target": "mixed", "text": MARKDOWN}
            )
        self.assertContains(response, "разобран офлайн")
        self.assertContains(response, "Что получилось")

    @override_settings(
        LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="ollama", LMS_AI_LOCAL=True
    )
    def test_online_mode_sends_only_the_prompt_and_file(self):
        captured = {}

        def fake(spec, prompt_text, **kwargs):
            captured["prompt"] = prompt_text
            captured["spec"] = spec
            captured["kwargs"] = kwargs
            return self.AI_PAYLOAD

        with patch("lms.ai._provider_material", side_effect=fake):
            self.teacher_client.post(
                reverse("teacher_ai"), {"target": "cards", "text": "gate | выход"}
            )
        self.assertEqual(captured["spec"]["key"], "ollama")
        self.assertEqual(captured["kwargs"], {"filename": "", "blob": b""})
        self.assertIn("карточки", captured["prompt"].lower())
        self.assertIn("gate | выход", captured["prompt"])


class AssistantModelTests(LMSCase):
    def test_mode_switches_between_offline_and_online(self):
        self.assertEqual(ai.ai_mode(), "offline")
        with override_settings(LMS_AI_API_KEY="  "):
            self.assertEqual(ai.ai_mode(), "offline")
        with override_settings(LMS_AI_API_KEY="key"):
            self.assertEqual(ai.ai_mode(), "online")
            self.assertIn("Ollama", ai.ai_mode_label())
            self.assertIn("qwen3-vl:8b", ai.ai_mode_label())
        with override_settings(LMS_AI_API_KEY="", LMS_AI_LOCAL=True):
            self.assertEqual(ai.ai_mode(), "online")
            self.assertIn("локально", ai.ai_mode_label())
            self.assertIn("локальная модель", ai.ai_mode_label(mode=None).split("—")[0] or "")
        with override_settings(LMS_AI_API_KEY="", LMS_AI_LOCAL=False, LMS_AI_PROVIDER="lmstudio"):
            self.assertEqual(ai.ai_mode(), "offline")
            self.assertIn("LMS_AI_LOCAL", ai.ai_mode_label())
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
    """Локальная модель по умолчанию: Ollama, LM Studio и свой шлюз."""

    def test_default_provider_is_local_ollama_with_vision(self):
        self.assertEqual(ai.ai_provider(), "ollama")
        spec = ai.provider_spec()
        self.assertEqual(spec["kind"], "openai")
        self.assertEqual(spec["label"], "Ollama (локальная модель)")
        self.assertEqual(ai.ai_endpoint(), "http://localhost:11434/v1")
        self.assertEqual(ai.ai_model(), "qwen3-vl:8b")
        self.assertIn("image", ai.provider_uploads())
        self.assertIn("vision-модель", ai.provider_hint())
        self.assertTrue(spec["keyless"])

    def test_there_is_no_cloud_provider_anymore(self):
        for gone in ("gemini", "gigachat", "yandex", "proxyapi", "openrouter", "deepseek"):
            self.assertNotIn(gone, ai.PROVIDERS)
            # Незнакомое имя остаётся рабочим: это свой OpenAI-совместимый шлюз.
            with override_settings(LMS_AI_PROVIDER=gone, LMS_AI_ENDPOINT="https://gw.example/v1"):
                self.assertEqual(ai.provider_spec()["kind"], "openai")
                self.assertEqual(ai.ai_endpoint(), "https://gw.example/v1")

    def test_legacy_model_name_does_not_leak(self):
        with override_settings(
            LMS_AI_PROVIDER="ollama", LMS_AI_MODEL="gemini-2.0-flash", LMS_AI_ENDPOINT=""
        ):
            self.assertEqual(ai.ai_model(), "qwen3-vl:8b")
            self.assertEqual(ai.ai_endpoint(), "http://localhost:11434/v1")

    def test_lmstudio_requires_model_from_settings(self):
        with override_settings(LMS_AI_PROVIDER="lmstudio", LMS_AI_MODEL="", LMS_AI_ENDPOINT=""):
            self.assertEqual(ai.ai_endpoint(), "http://localhost:1234/v1")
            self.assertEqual(ai.ai_model(), "")

    def test_own_model_value_is_kept(self):
        with override_settings(
            LMS_AI_PROVIDER="lmstudio", LMS_AI_MODEL="qwen/qwen3-vl-8b", LMS_AI_ENDPOINT=""
        ):
            self.assertEqual(ai.ai_model(), "qwen/qwen3-vl-8b")
            self.assertEqual(ai.ai_endpoint(), "http://localhost:1234/v1")

    def test_own_gateway_keeps_model_and_endpoint_from_settings(self):
        # Свой шлюз не имеет значений по умолчанию: имя модели и адрес из окружения
        # не должны отбрасываться, даже если они совпадают с именами других провайдеров.
        with override_settings(
            LMS_AI_PROVIDER="my-gateway",
            LMS_AI_ENDPOINT="http://10.0.0.5:11434/v1",
            LMS_AI_MODEL="qwen3-vl:8b",
            LMS_AI_API_KEY="secret",
        ):
            self.assertEqual(ai.ai_model(), "qwen3-vl:8b")
            self.assertEqual(ai.ai_endpoint(), "http://10.0.0.5:11434/v1")

    def test_own_gateway_keeps_a_known_endpoint(self):
        with override_settings(
            LMS_AI_PROVIDER="my-gateway",
            LMS_AI_ENDPOINT="http://localhost:11434/v1",
            LMS_AI_MODEL="my-model",
        ):
            self.assertEqual(ai.ai_endpoint(), "http://localhost:11434/v1")

    def test_upload_kinds_are_overridable_and_typos_fall_back(self):
        with override_settings(LMS_AI_UPLOAD_KINDS="image"):
            self.assertEqual(ai.provider_uploads(), ("image",))
        with override_settings(LMS_AI_UPLOAD_KINDS="none"):
            self.assertEqual(ai.provider_uploads(), ())
        with override_settings(LMS_AI_UPLOAD_KINDS="image,none"):
            self.assertEqual(ai.provider_uploads(), ("image",))  # лишнее значение отброшено
        with override_settings(LMS_AI_UPLOAD_KINDS="мусор"):
            self.assertEqual(ai.provider_uploads(), ai.provider_spec()["uploads"])

    def test_local_mode_needs_an_explicit_flag(self):
        with override_settings(LMS_AI_LOCAL=False, LMS_AI_API_KEY=""):
            self.assertEqual(ai.ai_mode(), "offline")
            self.assertIn("LMS_AI_LOCAL", ai.ai_mode_label())
        with override_settings(LMS_AI_LOCAL=True):
            self.assertEqual(ai.ai_mode(), "online")
            self.assertTrue(ai.ai_local())
        with override_settings(LMS_AI_LOCAL=True, LMS_AI_PROVIDER="openai"):
            self.assertFalse(ai.ai_local())  # свой шлюз ключом и включается
            self.assertEqual(ai.ai_mode(), "offline")

    def test_unsupported_upload_note_explains_limits(self):
        spec = ai.provider_spec()
        self.assertIn("не читает видео", ai.unsupported_upload_note("clip.mp4", spec))
        self.assertIn("vision-модель", ai.unsupported_upload_note("clip.mp4", spec))
        self.assertIn("не читает PDF", ai.unsupported_upload_note("book.pdf", spec))
        self.assertEqual(ai.unsupported_upload_note("lesson.docx", spec), "")
        self.assertIn("вставьте текст", ai.unsupported_upload_note("virus.exe", spec))

    def test_json_mode_defaults_to_off_for_local_models(self):
        with override_settings(LMS_AI_JSON_MODE=False, LMS_AI_PROVIDER="ollama"):
            self.assertFalse(ai._json_mode(ai.provider_spec()))
        with override_settings(LMS_AI_JSON_MODE=True, LMS_AI_PROVIDER="ollama"):
            self.assertTrue(ai._json_mode(ai.provider_spec()))
        with override_settings(LMS_AI_JSON_MODE=False, LMS_AI_PROVIDER="openai"):
            self.assertFalse(ai._json_mode(ai.provider_spec()))

    def test_image_mime_by_extension(self):
        self.assertEqual(ai._image_mime("page.png"), "image/png")
        self.assertEqual(ai._image_mime("page.webp"), "image/webp")
        self.assertEqual(ai._image_mime("scan.heic"), "image/jpeg")

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
    """Транспорт локальной модели: запрос, фото data-url, ошибки сервера."""

    def openai_response(self, payload=None):
        return {"choices": [{"message": {"content": json.dumps(payload or {})}}]}

    def test_request_goes_to_ollama_without_key(self):
        material = {"blocks": [{"name": "Travel"}]}
        transport = FakeTransport(self.openai_response(material))
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_LOCAL=True, LMS_AI_API_KEY=""),
        ):
            payload = ai._provider_material(ai.provider_spec(), "промпт")
        self.assertEqual(payload, material)
        request = transport.requests[0]
        self.assertEqual(request["url"], "http://localhost:11434/v1/chat/completions")
        self.assertNotIn("authorization", request["headers"])
        body = json.loads(request["body"])
        self.assertEqual(body["model"], "qwen3-vl:8b")
        self.assertEqual(body["messages"][0]["content"], "промпт")
        self.assertNotIn("response_format", body)

    def test_photo_goes_as_data_url_for_vision_model(self):
        transport = FakeTransport(self.openai_response({"blocks": []}))
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_LOCAL=True, LMS_AI_MODEL="qwen2.5vl:7b"),
        ):
            ai._provider_material(
                ai.provider_spec(), "Разбери страницу", filename="page.png", blob=b"\x89PNG"
            )
        body = json.loads(transport.requests[0]["body"])
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Разбери страницу"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_json_mode_and_key_are_used_for_own_gateway(self):
        transport = FakeTransport(self.openai_response({"blocks": []}))
        with override_settings(
            LMS_AI_PROVIDER="my-gateway",
            LMS_AI_ENDPOINT="https://gw.example/v1",
            LMS_AI_API_KEY="secret",
            LMS_AI_MODEL="my-model",
            LMS_AI_JSON_MODE=True,
        ):
            with patch("lms.ai.urllib.request.urlopen", transport):
                ai._provider_material(ai.provider_spec(), "промпт")
        body = json.loads(transport.requests[0]["body"])
        self.assertEqual(transport.requests[0]["url"], "https://gw.example/v1/chat/completions")
        self.assertEqual(transport.requests[0]["headers"]["authorization"], "Bearer secret")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["model"], "my-model")

    def test_missing_model_and_endpoint_are_explained(self):
        with override_settings(LMS_AI_PROVIDER="lmstudio", LMS_AI_MODEL="", LMS_AI_ENDPOINT=""):
            with self.assertRaisesMessage(ai.AiError, "LMS_AI_MODEL"):
                ai._provider_material(ai.provider_spec(), "промпт")
        with override_settings(LMS_AI_PROVIDER="my-gateway", LMS_AI_ENDPOINT=""):
            with self.assertRaisesMessage(ai.AiError, "LMS_AI_ENDPOINT"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_connection_error_points_to_ollama(self):
        transport = FakeTransport(urllib.error.URLError(ConnectionRefusedError("refused")))
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_LOCAL=True),
        ):
            with self.assertRaisesMessage(ai.AiError, "Запущен ли Ollama"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_unknown_model_suggests_ollama_list(self):
        error = urllib.error.HTTPError(
            "http://localhost:11434/v1", 404, "Not Found", {}, io.BytesIO(b"")
        )
        transport = FakeTransport(error)
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_LOCAL=True),
        ):
            with self.assertRaisesMessage(ai.AiError, "ollama list"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_model_without_json_support_gets_a_readable_error(self):
        error = urllib.error.HTTPError(
            "http://localhost:11434/v1", 400, "Bad Request", {}, io.BytesIO(b'{"error":"json"}')
        )
        with (
            patch("lms.ai.urllib.request.urlopen", FakeTransport(error)),
            override_settings(LMS_AI_LOCAL=True),
        ):
            with self.assertRaisesMessage(ai.AiError, "LMS_AI_JSON_MODE=0"):
                ai._provider_material(ai.provider_spec(), "промпт")

    def test_content_parts_are_joined(self):
        payload = {
            "choices": [{"message": {"content": [{"text": '{"blocks":'}, {"text": " []}"}]}}]
        }
        transport = FakeTransport(payload)
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(LMS_AI_LOCAL=True),
        ):
            self.assertEqual(ai._provider_material(ai.provider_spec(), "п"), {"blocks": []})

    def test_tls_error_explains_ca_bundle(self):
        transport = FakeTransport(
            urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate in chain"))
        )
        with override_settings(
            LMS_AI_PROVIDER="gw", LMS_AI_ENDPOINT="https://gw.example/v1", LMS_AI_MODEL="my-model"
        ):
            with patch("lms.ai.urllib.request.urlopen", transport):
                with self.assertRaisesMessage(ai.AiError, "LMS_AI_CA_BUNDLE"):
                    ai._provider_material(ai.provider_spec(), "промпт")

    def test_ssl_settings_build_the_context(self):
        with override_settings(LMS_AI_VERIFY_SSL=True, LMS_AI_CA_BUNDLE=""):
            self.assertIsNone(ai._ssl_context())
        with override_settings(LMS_AI_CA_BUNDLE="/нет/такого/файла.pem"):
            with self.assertRaisesMessage(ai.AiError, "LMS_AI_CA_BUNDLE"):
                ai._ssl_context()
        with override_settings(LMS_AI_VERIFY_SSL=False):
            self.assertFalse(ai._ssl_context().verify_mode)  # CERT_NONE — явный отказ админа


class LocalModelFallbackTests(SimpleTestCase):
    """Что уходит в модель, что разбирается офлайн и как это объясняется."""

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_LOCAL=True)
    def test_photo_goes_to_vision_model_with_text_from_document(self):
        captured = {}

        def fake(spec, prompt_text, *, filename="", blob=b""):
            captured.update(filename=filename, blob=blob, prompt=prompt_text, label=spec["label"])
            raise ai.AiError("нет сети")

        upload = SimpleUploadedFile("page.jpg", b"\xff\xd8\xff")
        with patch("lms.ai._provider_material", side_effect=fake):
            material, meta = ai.build_material(upload=upload, text=MARKDOWN)
        self.assertEqual(captured["filename"], "page.jpg")
        self.assertEqual(captured["blob"], b"\xff\xd8\xff")
        self.assertIn("# Travel B1", captured["prompt"])
        self.assertEqual(meta["result"], "offline")
        self.assertEqual(meta["provider"], "ollama")
        self.assertIn("Ollama", meta["provider_label"])
        self.assertTrue(any("разобран офлайн" in note for note in meta["notes"]))
        self.assertTrue(material["blocks"])

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_LOCAL=True)
    def test_video_is_not_sent_to_model_and_is_reported(self):
        captured = {}

        def fake(spec, prompt_text, *, filename="", blob=b""):
            captured.update(filename=filename, blob=blob)
            raise ai.AiError("нет сети")

        upload = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42")
        with patch("lms.ai._provider_material", side_effect=fake):
            material, meta = ai.build_material(upload=upload, text=MARKDOWN)
        self.assertEqual(captured["filename"], "")
        self.assertEqual(captured["blob"], b"")
        self.assertTrue(any("не читает видео" in note for note in meta["notes"]))
        self.assertTrue(any("vision-модель" in note for note in meta["notes"]))
        self.assertTrue(material["blocks"])

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_LOCAL=True)
    def test_video_without_text_asks_for_manual_text(self):
        upload = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00\x18ftypmp42")
        with patch("lms.ai._provider_material", side_effect=AssertionError("не вызывается")):
            with self.assertRaisesMessage(ai.AiError, "вставьте текст вручную"):
                ai.build_material(upload=upload)

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_LOCAL=False, LMS_AI_API_KEY="")
    def test_offline_mode_reports_that_photo_cannot_be_read(self):
        upload = SimpleUploadedFile("page.jpg", b"\xff\xd8\xff")
        _, meta = ai.build_material(upload=upload, prompt="Соберите тему Travel")
        self.assertEqual(meta["result"], "offline")
        self.assertTrue(any("LMS_AI_LOCAL=1" in note for note in meta["notes"]))


class AiCheckCommandTests(SimpleTestCase):
    """`manage.py ai_check`: показать настройки и по флагу --live проверить модель."""

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

    def test_offline_mode_is_reported_without_network(self):
        output = io.StringIO()
        call_command("ai_check", stdout=output)
        text = output.getvalue()
        self.assertIn("Офлайн-разбор", text)
        self.assertIn("Ollama (локальная модель)", text)
        self.assertIn("http://localhost:11434/v1", text)
        self.assertIn("LMS_AI_LOCAL", text)

    @override_settings(LMS_AI_ENABLED=False)
    def test_disabled_assistant_stops_the_check(self):
        with self.assertRaisesMessage(CommandError, "выключен"):
            call_command("ai_check")

    @override_settings(LMS_AI_LOCAL=True)
    def test_live_check_confirms_the_model_answer(self):
        transport = FakeTransport(
            {"choices": [{"message": {"content": json.dumps(self.SAMPLE_MATERIAL)}}]}
        )
        output = io.StringIO()
        with patch("lms.ai.urllib.request.urlopen", transport):
            call_command("ai_check", "--live", stdout=output)
        self.assertIn("Модель ответила", output.getvalue())
        self.assertIn("Импорт не выполнялся", output.getvalue())
        self.assertEqual(
            [request["url"] for request in transport.requests],
            ["http://localhost:11434/v1/chat/completions"],
        )
        json.loads(transport.requests[0]["body"])  # тело — корректный JSON
        self.assertIn("Travel A2", transport.requests[0]["body"])

    @override_settings(LMS_AI_LOCAL=True)
    def test_live_check_fails_with_the_model_reason(self):
        transport = FakeTransport(urllib.error.URLError("network down"))
        with patch("lms.ai.urllib.request.urlopen", transport):
            with self.assertRaisesMessage(CommandError, "Онлайн-разбор не сработал"):
                call_command("ai_check", "--live")

    @override_settings(LMS_AI_LOCAL=True)
    def test_live_check_warns_about_plain_http_gateway(self):
        transport = FakeTransport(
            {"choices": [{"message": {"content": json.dumps(self.SAMPLE_MATERIAL)}}]}
        )
        output = io.StringIO()
        with (
            patch("lms.ai.urllib.request.urlopen", transport),
            override_settings(
                LMS_AI_PROVIDER="gw",
                LMS_AI_ENDPOINT="http://gw.internal/v1",
                LMS_AI_MODEL="m",
                LMS_AI_API_KEY="secret",
            ),
        ):
            call_command("ai_check", "--live", stdout=output)
        self.assertIn("без HTTPS", output.getvalue())


ONLINE = override_settings(
    LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True, LMS_AI_PROVIDER="ollama", LMS_AI_LOCAL=True
)

REVISION_QUIZ = {
    "title": "Revised quiz",
    "description": "New conditions.",
    "max_points": 4,
    "skills": ["grammar"],
    "questions": [
        {
            "kind": "mcq",
            "text": "Q1?",
            "points": 2,
            "choices": [
                {"text": "yes", "correct": True},
                {"text": "no", "correct": False},
            ],
        },
        {
            "kind": "gap",
            "text": "Fill ___ in.",
            "points": 2,
            "choices": [{"text": "it", "correct": True}],
        },
    ],
    "cards": [],
}

REVISION_CARDS = {
    "title": "New cards",
    "description": "New words.",
    "max_points": 0,
    "skills": [],
    "questions": [],
    "cards": [{"front": f"word{i}", "back": f"слово{i}", "example": ""} for i in range(1, 4)],
}


class AssignmentRevisionTests(LMSCase):
    """Правка существующего задания с ИИ: генерация, предпросмотр, применение."""

    def make_quiz(self):
        quiz = Assignment.objects.create(
            topic=self.topic,
            title="Old quiz",
            description="Old.",
            assignment_type=Assignment.Type.QUIZ,
            max_points=1,
            status=Assignment.Publication.PUBLISHED,
        )
        question = Question.objects.create(
            assignment=quiz, kind=Question.Kind.MCQ, text="Old Q?", points=1, order=1
        )
        Choice.objects.create(question=question, text="yes", is_correct=True, order=1)
        Choice.objects.create(question=question, text="no", is_correct=False, order=2)
        return quiz

    def ai_url(self, assignment):
        return reverse("teacher_assignment_ai", args=[assignment.pk])

    def apply_url(self, assignment):
        return reverse("teacher_assignment_ai_apply", args=[assignment.pk])

    def test_offline_mode_explains_unavailability(self):
        response = self.teacher_client.get(self.ai_url(self.assignment))
        self.assertContains(response, "Правка с ИИ недоступна")

    def test_offline_mode_rejects_generation(self):
        response = self.teacher_client.post(
            self.ai_url(self.assignment), {"instruction": "сделай сложнее"}
        )
        self.assertContains(response, "Правка с ИИ доступна")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Past tense")

    def test_assignment_form_links_to_ai_page(self):
        response = self.teacher_client.get(
            reverse("teacher_assignment_form", args=[self.assignment.pk])
        )
        self.assertContains(response, self.ai_url(self.assignment))

    @ONLINE
    def test_generate_and_apply_quiz(self):
        quiz = self.make_quiz()
        with patch("lms.ai._provider_material", return_value=REVISION_QUIZ):
            response = self.teacher_client.post(
                self.ai_url(quiz), {"instruction": "Сделай сложнее"}
            )
        self.assertContains(response, "Revised quiz")
        self.assertContains(response, "ещё не применена")
        quiz.refresh_from_db()
        self.assertEqual(quiz.title, "Old quiz")  # пока только предпросмотр

        response = self.teacher_client.post(self.apply_url(quiz))
        self.assertRedirects(
            response,
            reverse("teacher_assignment_form", args=[quiz.pk]),
            fetch_redirect_response=False,
        )
        quiz.refresh_from_db()
        self.assertEqual(quiz.title, "Revised quiz")
        self.assertEqual(quiz.description, "New conditions.")
        self.assertEqual(quiz.max_points, 4)  # сумма баллов новых вопросов
        self.assertEqual(quiz.questions.count(), 2)
        self.assertFalse(quiz.questions.filter(text="Old Q?").exists())
        self.assertEqual(quiz.status, Assignment.Publication.PUBLISHED)  # статус сохранён

    @ONLINE
    def test_prompt_contains_instruction_and_current_content(self):
        quiz = self.make_quiz()
        captured = {}

        def fake(_spec, prompt_text, **_kwargs):
            captured["prompt"] = prompt_text
            return REVISION_QUIZ

        with patch("lms.ai._provider_material", side_effect=fake):
            self.teacher_client.post(
                self.ai_url(quiz), {"instruction": "Добавь вопрос про Past Simple"}
            )
        self.assertIn("Добавь вопрос про Past Simple", captured["prompt"])
        self.assertIn("Old Q?", captured["prompt"])
        self.assertIn("Old quiz", captured["prompt"])

    @ONLINE
    def test_empty_instruction_is_rejected(self):
        response = self.teacher_client.post(self.ai_url(self.assignment), {"instruction": "   "})
        self.assertContains(response, "Опишите, что изменить")

    @ONLINE
    def test_apply_without_proposal(self):
        response = self.teacher_client.post(self.apply_url(self.assignment), follow=True)
        self.assertContains(response, "Нет подготовленной версии")

    @ONLINE
    def test_revision_replaces_flashcards(self):
        trainer = self.card_assignment()
        Flashcard.objects.create(assignment=trainer, front="cat", back="кот", order=1)
        Flashcard.objects.create(assignment=trainer, front="dog", back="пёс", order=2)
        with patch("lms.ai._provider_material", return_value=REVISION_CARDS):
            self.teacher_client.post(self.ai_url(trainer), {"instruction": "Обнови слова"})
            self.teacher_client.post(self.apply_url(trainer))
        trainer.refresh_from_db()
        self.assertEqual(trainer.title, "New cards")
        self.assertEqual(trainer.cards.count(), 3)
        self.assertFalse(trainer.cards.filter(front="cat").exists())

    @ONLINE
    def test_revision_for_text_assignment_ignores_questions(self):
        payload = dict(REVISION_QUIZ, title="Revised text", max_points=25)
        with patch("lms.ai._provider_material", return_value=payload):
            self.teacher_client.post(self.ai_url(self.assignment), {"instruction": "Перепиши"})
            self.teacher_client.post(self.apply_url(self.assignment))
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.title, "Revised text")
        self.assertEqual(self.assignment.max_points, 25)
        self.assertEqual(self.assignment.assignment_type, Assignment.Type.TEXT)
        self.assertEqual(self.assignment.questions.count(), 0)  # вопросы вне типа отброшены

    @ONLINE
    def test_existing_submissions_show_warning(self):
        self.submit()  # сдача по self.assignment
        response = self.teacher_client.get(self.ai_url(self.assignment))
        self.assertContains(response, "уже есть сдач")

    def test_revision_page_requires_teacher(self):
        response = self.student_client.get(self.ai_url(self.assignment))
        self.assertRedirects(
            response,
            reverse("student_assignments"),
            fetch_redirect_response=False,
        )


class NormaliseRevisionTests(SimpleTestCase):
    def test_forces_original_type_and_drops_questions_for_text(self):
        revision = ai.normalise_revision(REVISION_QUIZ, assignment_type=Assignment.Type.TEXT)
        self.assertEqual(revision["type"], Assignment.Type.TEXT)
        self.assertEqual(revision["questions"], [])
        self.assertEqual(revision["cards"], [])

    def test_quiz_requires_questions(self):
        payload = dict(REVISION_QUIZ, questions=[])
        with self.assertRaisesMessage(ai.AiError, "не вернула вопросов"):
            ai.normalise_revision(payload, assignment_type=Assignment.Type.QUIZ)

    def test_flashcards_require_two_cards(self):
        payload = dict(REVISION_CARDS, cards=[{"front": "a", "back": "б", "example": ""}])
        with self.assertRaisesMessage(ai.AiError, "карточек"):
            ai.normalise_revision(payload, assignment_type=Assignment.Type.FLASHCARDS)

    def test_missing_title_is_an_error(self):
        with self.assertRaisesMessage(ai.AiError, "названия или условия"):
            ai.normalise_revision({"description": "only"}, assignment_type=Assignment.Type.TEXT)
