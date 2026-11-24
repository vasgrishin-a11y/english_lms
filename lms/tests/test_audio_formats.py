"""Форматы аудио, которые браузер не играет (WMA): приём и перекодирование в MP3.

Жалоба «Формат .wma здесь не принимается» приходила именно с ИИ-помощника, но
формат — общий для всех загрузок, поэтому проверяем два уровня: валидатор и
серверную конвертацию, а затем сквозной путь через ИИ-помощник и через ответ
ученика на аудио-задание.

В тестах WMA собирается PyAV (он же и перекодирует): внешние файлы и сеть не
нужны, а сама конвертация проверяется на настоящем ASF-контейнере, а не на
заглушке.
"""

import io
import math
import struct
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from lms import ai, audio_formats
from lms.models import Assignment, Submission
from lms.validators import (
    ALLOWED_FILE_EXTENSIONS,
    UPLOAD_AUDIO_EXTENSIONS,
    validate_audio_content,
    validate_audio_upload,
)

from .base import LMSCase


def _tone(rate=44100, seconds=1.0, layout="stereo"):
    import av

    samples = int(rate * seconds)
    data = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(samples)
    )
    if layout == "stereo":  # s16 interleaved: каждый сэмпл дважды
        data = b"".join(data[index : index + 2] * 2 for index in range(0, len(data), 2))
    frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
    frame.sample_rate = rate
    frame.planes[0].update(data)
    frame.pts = 0
    return frame


def wma_bytes(seconds=1.0):
    """Настоящий WMA (ASF + wmav2): так диктофон и Windows Media Player и пишут."""
    import av

    buffer = io.BytesIO()
    container = av.open(buffer, "w", format="asf")
    # wmav2 открывается только с явным битрейтом: значения по умолчанию
    # ffmpeg отвергает («Invalid argument»), хотя для декодирования WMA он
    # полностью пригоден.
    stream = container.add_stream("wmav2", rate=44100)
    stream.layout = "stereo"
    stream.bit_rate = 128000
    for packet in stream.encode(_tone(rate=44100, seconds=seconds, layout="stereo")):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buffer.getvalue()


def m4b_bytes(seconds=1.0):
    """M4B — это MP4-контейнер с AAC: так его сохраняют аудиокниги и телефоны."""
    import av

    buffer = io.BytesIO()
    container = av.open(buffer, "w", format="mp4")
    stream = container.add_stream("aac", rate=44100)
    stream.layout = "stereo"
    stream.bit_rate = 128000
    for packet in stream.encode(_tone(rate=44100, seconds=seconds, layout="stereo")):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buffer.getvalue()


class AudioFormatSupportTests(SimpleTestCase):
    """Список форматов и подпись поля загрузки."""

    def test_wma_is_offered_and_is_audio(self):
        self.assertIn(".wma", ai.UPLOAD_EXTENSIONS)
        self.assertIn(".wma", ai.upload_accept().split(","))
        self.assertEqual(ai.upload_kind("lesson.wma"), "audio")
        self.assertEqual(ai.upload_kind("lesson.WMA"), "audio")
        for extension in ("wma", "wmv", "asf", "m4b"):
            self.assertIn(extension, ALLOWED_FILE_EXTENSIONS)
            self.assertIn(extension, UPLOAD_AUDIO_EXTENSIONS)

    def test_audio_pickers_offer_wma(self):
        """Все поля аудио предлагают WMA, а не только поле ИИ-помощника."""
        from lms.forms import audio_accept_attribute
        from lms.templatetags.lms_tags import audio_accept

        for accept in (audio_accept_attribute(), audio_accept()):
            extensions = accept.split(",")
            self.assertIn(".wma", extensions)
            self.assertIn(".wmv", extensions)
            self.assertIn(".asf", extensions)
            self.assertIn(".m4b", extensions)
            self.assertIn("audio/*", extensions)

    def test_every_outer_format_is_validated_as_audio(self):
        """ASF под любым именем и M4B (MP4) проверяются как аудио, а не как «мусор»."""
        from lms.validators import validate_upload

        cases = (
            ("lesson.wma", wma_bytes()),
            ("dictaphone.wmv", wma_bytes()),
            ("recording.asf", wma_bytes()),
            ("audiobook.m4b", m4b_bytes()),
        )
        for name, blob in cases:
            with self.subTest(name=name):
                validate_upload(SimpleUploadedFile(name, blob))
                self.assertGreater(validate_audio_content(blob, name).info.length, 0.5)
        # Под видом M4B — текст: это не аудио.
        with self.assertRaises(ValidationError):
            validate_audio_content(b"not an mp4" * 20, "fake.m4b")

    def test_ai_upload_validation_accepts_wma(self):
        upload = SimpleUploadedFile("lesson.wma", wma_bytes(), "audio/x-ms-wma")
        ai.validate_upload(upload)  # не бросает AiError

    def test_wma_content_is_checked_like_any_audio(self):
        blob = wma_bytes()
        self.assertEqual(type(validate_audio_content(blob, "lesson.wma")).__name__, "ASF")
        # Расширение врёт: под .wma лежит текст — это не аудио.
        with self.assertRaises(ValidationError):
            validate_audio_content(b"just text" * 10, "fake.wma")
        # ASF узнаётся и по заголовку, даже если расширение не .wma.
        validate_audio_content(blob, "dictaphone.wmv")

    def test_inline_preview_stays_browser_only(self):
        """WMA не становится плеером, который молча ничего не играет."""
        from lms.validators import audio_content_matches

        self.assertFalse(audio_content_matches(io.BytesIO(wma_bytes()), "wma"))


class AudioConversionTests(SimpleTestCase):
    def test_wma_becomes_playable_mp3(self):
        data = audio_formats.convert_to_mp3(wma_bytes(seconds=1.0), filename="lesson.wma")
        self.assertTrue(data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xfa"))
        import mutagen

        audio = mutagen.File(io.BytesIO(data))
        self.assertEqual(type(audio).__name__, "MP3")
        self.assertGreater(audio.info.length, 0.5)
        self.assertEqual(audio.info.sample_rate, audio_formats.TARGET_SAMPLE_RATE)

    def test_m4b_becomes_playable_mp3(self):
        """M4B — второй «внешний» контейнер: demuxer не должен быть жёстко ASF."""
        import mutagen

        data = audio_formats.convert_to_mp3(m4b_bytes(seconds=1.0), filename="book.m4b")
        audio = mutagen.File(io.BytesIO(data))
        self.assertEqual(type(audio).__name__, "MP3")
        self.assertGreater(audio.info.length, 0.5)

    def test_converted_file_is_smaller_than_asf(self):
        source = wma_bytes(seconds=1.0)
        result = audio_formats.convert_to_mp3(source, filename="lesson.wma")
        self.assertLess(len(result), len(source) + 1024)

    def test_broken_wma_reports_a_readable_error(self):
        head = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
        with self.assertRaises(audio_formats.AudioConvertError) as caught:
            audio_formats.convert_to_mp3(head + b"\x00" * 200, filename="broken.wma")
        self.assertIn("повреждён", str(caught.exception))

    def test_missing_converter_is_explained(self):
        head = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
        with (
            patch.dict("sys.modules", {"av": None}),
            patch("lms.audio_formats.validate_audio_content"),
        ):
            with self.assertRaises(audio_formats.AudioConvertError) as caught:
                audio_formats.convert_to_mp3(head + b"\x00" * 64, filename="lesson.wma")
        self.assertIn("av", str(caught.exception))

    def test_mp3_name_keeps_the_original_stem(self):
        self.assertEqual(audio_formats.mp3_name("Урок 5.wma"), "Урок 5.mp3")
        self.assertEqual(audio_formats.mp3_name("no-extension"), "no-extension.mp3")
        self.assertEqual(audio_formats.mp3_name("dir/lesson.WMA"), "lesson.mp3")

    def test_only_exotic_audio_needs_conversion(self):
        self.assertTrue(audio_formats.needs_conversion("lesson.wma"))
        self.assertFalse(audio_formats.needs_conversion("voice.mp3"))
        self.assertFalse(audio_formats.needs_conversion("page.jpg"))
        self.assertFalse(audio_formats.needs_conversion("book.pdf"))
        # ASF под незнакомым именем — тоже аудио, которое не играет браузер.
        self.assertTrue(audio_formats.needs_conversion("recording", wma_bytes()))
        self.assertFalse(audio_formats.needs_conversion("voice.mp3", wma_bytes()))

    def test_audio_upload_is_replaced_with_mp3(self):
        upload = SimpleUploadedFile("lesson.wma", wma_bytes(), "audio/x-ms-wma")
        converted = validate_audio_upload(upload)
        self.assertTrue(converted.name.endswith(".mp3"))
        self.assertEqual(converted.content_type, "audio/mpeg")
        self.assertGreater(converted.audio_duration or 0, 0.5)

    def test_browser_audio_and_documents_are_untouched(self):
        upload = SimpleUploadedFile("voice.mp3", b"anything")
        self.assertIs(validate_audio_upload(upload), upload)
        document = SimpleUploadedFile("essay.pdf", b"%PDF-1.7\n")
        self.assertIs(validate_audio_upload(document), document)

    def test_broken_wma_is_rejected_with_a_message(self):
        upload = SimpleUploadedFile("broken.wma", b"\x00" * 128, "audio/x-ms-wma")
        with self.assertRaises(ValidationError) as caught:
            validate_audio_upload(upload)
        self.assertIn("формату", str(caught.exception))


class AssistantWmaTests(LMSCase):
    """Сквозной путь: WMA на странице ИИ-помощника больше не «не принимается»."""

    PAYLOAD = {
        "title": "Listening",
        "blocks": [
            {
                "name": "B1",
                "topics": [
                    {
                        "title": "Dictation",
                        "assignments": [
                            {
                                "type": "text",
                                "title": "Listen and write",
                                "description": "Прослушайте запись и напишите текст.",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    def post_ai(self, **data):
        payload = {"target": "mixed", "prompt": "", "text": "", "structure_confirmed": "on"}
        payload.update(data)
        return self.teacher_client.post(reverse("teacher_ai"), payload)

    # Аудио уходит модели как input_audio, а это умеют только аудио-модели:
    # администратор включает вид вложения явно, иначе провайдер объявляет
    # только фото (см. LMS_AI_UPLOAD_KINDS).
    @override_settings(
        LMS_AI_ENABLED=True, LMS_AI_LOCAL=True, LMS_AI_API_KEY="", LMS_AI_UPLOAD_KINDS="image,audio"
    )
    def test_wma_is_accepted_and_converted_before_the_model(self):
        captured = {}

        def fake(spec, prompt_text, *, filename="", blob=b"", **kwargs):
            captured.update(filename=filename, blob=blob, prompt=prompt_text, **kwargs)
            return self.PAYLOAD

        upload = SimpleUploadedFile("dictation.wma", wma_bytes(), "audio/x-ms-wma")
        with patch("lms.ai._provider_material", side_effect=fake):
            response = self.post_ai(prompt="Разбери запись", upload=upload)

        self.assertFalse(response.context["form"].errors)
        # Модель получила MP3 с новым именем, а не WMA, которого она не читает.
        self.assertEqual(captured["filename"], "dictation.mp3")
        self.assertIn("Файлы запроса: dictation.mp3", captured["prompt"])
        self.assertContains(response, "перекодирован в MP3")
        self.assertContains(response, "Что получилось")

    @override_settings(LMS_AI_ENABLED=True, LMS_AI_LOCAL=True, LMS_AI_API_KEY="")
    def test_offline_mode_accepts_wma_without_500(self):
        """Офлайн-разбор не читает аудио, но и не должен отказывать в формате."""
        upload = SimpleUploadedFile("dictation.wma", wma_bytes(), "audio/x-ms-wma")
        response = self.post_ai(prompt="Собери тему по записи", upload=upload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "перекодирован в MP3")

    def test_broken_wma_is_reported_not_accepted_silently(self):
        upload = SimpleUploadedFile("broken.wma", b"\x30" * 64, "audio/x-ms-wma")
        response = self.post_ai(prompt="Разбери запись", upload=upload)
        self.assertEqual(response.status_code, 200)
        # Формат принят, а не отвергнут: сообщение — о содержимом файла.
        self.assertContains(response, "broken.wma")
        self.assertContains(response, "пропущен")

    @override_settings(
        LMS_AI_ENABLED=True, LMS_AI_LOCAL=True, LMS_AI_API_KEY="", LMS_AI_UPLOAD_KINDS="image,audio"
    )
    def test_revision_with_wma_converts_and_attaches_mp3(self):
        """«Приложи запись к заданию»: к заданию попадёт MP3, который играет браузер."""
        with patch("lms.ai._provider_material", return_value=self.PAYLOAD):
            self.post_ai(prompt="Собери тему")
        with patch("lms.ai._provider_material", return_value=self.PAYLOAD) as provider:
            response = self.teacher_client.post(
                reverse("teacher_ai"),
                {
                    "revise_material": "1",
                    "revision_instruction": "Приложи запись dictation.wma к заданию",
                    "revision_upload": SimpleUploadedFile(
                        "dictation.wma", wma_bytes(), "audio/x-ms-wma"
                    ),
                },
            )
        self.assertContains(response, "с учётом приложенных файлов")
        self.assertContains(response, "перекодирован в MP3")
        prompt = provider.call_args.args[1]
        self.assertIn("dictation.mp3", prompt)
        # Исходное имя осталось только в пожелании преподавателя; в списке
        # файлов и в доступных вложениях — уже MP3, который играет браузер.
        self.assertIn(
            "Файлы, доступные для приложения к заданию (поле attach_files): dictation.mp3", prompt
        )
        self.assertNotIn("приложить файл «dictation.wma»", prompt)


class WmaAnswerTests(LMSCase):
    """Ученик и преподаватель тоже прикладывают WMA — запись должна играть."""

    def test_audio_assignment_converts_wma_answer(self):
        self.assignment.assignment_type = Assignment.Type.AUDIO
        self.assignment.save(update_fields=["assignment_type"])
        response = self.student_client.post(
            self.url,
            {
                "expected_version": 0,
                "file_answer": SimpleUploadedFile("answer.wma", wma_bytes(), "audio/x-ms-wma"),
            },
        )
        self.assertEqual(response.status_code, 302)
        attempt = Submission.objects.get()
        self.assertTrue(attempt.file_answer.name.endswith(".mp3"))
        preview = self.student_client.get(reverse("media_preview", args=[attempt.file_answer.name]))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "audio/mpeg")
        b"".join(preview.streaming_content)

    def test_teacher_voice_comment_converts_wma(self):
        attempt = self.submit(text_answer="Ready")
        self.review_post(
            attempt,
            audio_comment=SimpleUploadedFile("note.wma", wma_bytes(), "audio/x-ms-wma"),
        )
        attempt.refresh_from_db()
        self.assertTrue(attempt.feedback.audio_comment.name.endswith(".mp3"))
        played = self.student_client.get(
            reverse("media_preview", args=[attempt.feedback.audio_comment.name])
        )
        self.assertEqual(played.status_code, 200)
        b"".join(played.streaming_content)
