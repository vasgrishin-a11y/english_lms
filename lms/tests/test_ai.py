"""ИИ-помощник преподавателя: офлайн-разбор, провайдер через мок, импорт черновиками.

Сеть в тестах не используется: онлайн-режим проверяется подменой функции запроса
к провайдеру, поэтому CI работает без ключей и без интернета.
"""

import io
import zipfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
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

    @override_settings(LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True)
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

    @override_settings(LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True)
    def test_provider_failure_falls_back_to_offline(self):
        with patch("lms.ai._gemini_material", side_effect=ai.AiError("Провайдер недоступен")):
            response = self.teacher_client.post(
                reverse("teacher_ai"), {"target": "mixed", "text": MARKDOWN}
            )
        self.assertContains(response, "разобран офлайн")
        self.assertContains(response, "Что получилось")

    @override_settings(LMS_AI_API_KEY="test-key", LMS_AI_ENABLED=True)
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
            self.assertIn("gemini", ai.ai_mode_label())
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
