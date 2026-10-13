"""Индикатор файлового хранилища: подсчёт места и карточка в «Настройках консоли».

Индикатор отвечает на вопрос «сколько уже занято и сколько осталось», поэтому
проверяются и арифметика (общий файл считается один раз, потерянные файлы не
считаются, файлы из прежнего каталога считаются), и подача: лимит из настроек,
место на диске, разбивка по разделам и ссылка «Обновить» вместо ожидания кэша.
"""

import io
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from lms.models import (
    Assignment,
    AssignmentAttachment,
    Feedback,
    FeedbackAudioComment,
    QuestionResponse,
)
from lms.storage_usage import Gauge, collect, locate

from .base import LMSCase

SETTINGS_URL = "/teacher/settings/"


def wav_file(name="comment.wav"):
    """Настоящий WAV: голосовые комментарии проверяются содержимым, а не именем."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)
    return SimpleUploadedFile(name, stream.getvalue(), "audio/wav")


class GaugeTests(SimpleTestCase):
    """Проценты и цвет полосы: только арифметика порогов, без базы."""

    def test_zero_total_does_not_divide_by_zero(self):
        gauge = Gauge.of(0, 0)
        self.assertEqual((gauge.percent, gauge.bar_percent), (0, 0))
        self.assertEqual(gauge.bar_class, "progress-success")

    def test_thresholds_change_the_colour(self):
        self.assertEqual(Gauge.of(69, 100).bar_class, "progress-success")
        self.assertEqual(Gauge.of(70, 100).bar_class, "progress-warning")
        self.assertEqual(Gauge.of(89, 100).bar_class, "progress-warning")
        self.assertEqual(Gauge.of(90, 100).bar_class, "progress-danger")

    def test_over_the_limit_keeps_the_real_percent_and_caps_the_bar(self):
        gauge = Gauge.of(150, 100)
        self.assertEqual(gauge.percent, 150)
        self.assertEqual(gauge.bar_percent, 100)
        self.assertEqual(gauge.bar_class, "progress-danger")


class StorageUsageTests(LMSCase):
    """Что именно попадает в сумму и как ведёт себя снимок."""

    def setUp(self):
        super().setUp()
        # Кэш живёт между тестами дольше, чем снимок хранилища.
        cache.clear()
        self.addCleanup(cache.clear)
        # Ответы с файлом принимает только задание типа «файл».
        self.assignment.assignment_type = Assignment.Type.FILE
        self.assignment.save()

    def upload_material(self, size=2048, name="material.bin"):
        self.assignment.material_file.save(name, SimpleUploadedFile(name, b"x" * size))
        return self.assignment.material_file.name

    def test_counts_every_section_of_the_storage(self):
        self.upload_material(size=1000)
        AssignmentAttachment.objects.create(
            assignment=self.assignment, file=SimpleUploadedFile("extra.bin", b"a" * 500)
        )
        attempt = self.submit(
            file_answer=SimpleUploadedFile("answer.txt", b"b" * 400, "text/plain")
        )
        feedback = Feedback.objects.create(submission=attempt)
        comment = wav_file("comment.wav")
        feedback.audio_comment.save("comment.wav", comment)
        extra = wav_file("extra.wav")
        FeedbackAudioComment.objects.create(feedback=feedback, audio=extra)
        QuestionResponse.objects.create(
            student=self.student,
            assignment=self.assignment,
            file_answer=SimpleUploadedFile("item.txt", b"e" * 100, "text/plain"),
        )

        usage = collect()

        self.assertEqual(usage.size, 1000 + 500 + 400 + comment.size + extra.size + 100)
        self.assertEqual(usage.files, 6)
        self.assertEqual((usage.missing, usage.legacy), (0, 0))
        self.assertEqual([source.files for source in usage.sources], [1, 1, 1, 1, 1, 1])
        self.assertEqual(
            [source.size for source in usage.sources],
            [1000, 500, 400, comment.size, extra.size, 100],
        )

    def test_shared_file_is_counted_once(self):
        first = self.submit(file_answer=SimpleUploadedFile("answer.txt", b"z" * 1024, "text/plain"))
        second = self.submit(version=1, file_answer=first.file_answer)
        self.assertEqual(first.file_answer.name, second.file_answer.name)

        usage = collect()

        self.assertEqual((usage.files, usage.size), (1, 1024))

    def test_missing_file_is_reported_and_not_counted(self):
        self.assignment.material_file = "assignments/1/lost.bin"
        self.assignment.save(update_fields=["material_file"])

        usage = collect()

        self.assertEqual((usage.files, usage.size, usage.missing), (1, 0, 1))
        self.assertEqual(locate("assignments/1/lost.bin"), "missing")

    def test_file_from_the_previous_root_is_counted(self):
        directory = tempfile.TemporaryDirectory(prefix="lms-legacy-media-")
        self.addCleanup(directory.cleanup)
        legacy_root = Path(directory.name)
        (legacy_root / "assignments/1").mkdir(parents=True)
        (legacy_root / "assignments/1/old.bin").write_bytes(b"x" * 4096)

        with override_settings(LMS_MEDIA_FALLBACK_ROOTS=[str(legacy_root)]):
            self.assignment.material_file = "assignments/1/old.bin"
            self.assignment.save(update_fields=["material_file"])
            usage = collect()

        self.assertEqual((usage.size, usage.legacy, usage.missing), (4096, 1, 0))

    def test_snapshot_is_cached_until_forced(self):
        self.assertEqual(collect().size, 0)

        self.upload_material(size=1024)

        self.assertEqual(collect().size, 0)
        self.assertEqual(collect(force=True).size, 1024)

    def test_unreadable_media_folder_does_not_break_the_indicator(self):
        with patch("lms.storage_usage.shutil.disk_usage", side_effect=OSError("нет доступа")):
            usage = collect()

        self.assertEqual((usage.disk_total, usage.disk_free), (0, 0))
        self.assertIsNone(usage.disk_gauge)


class SettingsIndicatorTests(LMSCase):
    """Карточка «Файловое хранилище» на странице настроек."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def upload_material(self, size=1024, name="material.bin"):
        self.assignment.material_file.save(name, SimpleUploadedFile(name, b"x" * size))

    @override_settings(LMS_STORAGE_QUOTA_BYTES=4096)
    def test_page_shows_used_space_and_share_of_the_limit(self):
        self.upload_material(size=1024)

        response = self.teacher_client.get(SETTINGS_URL)

        self.assertEqual(response.status_code, 200)
        usage = response.context["storage"]
        self.assertEqual((usage.size, usage.quota), (1024, 4096))
        self.assertEqual(usage.quota_gauge.percent, 25)
        self.assertFalse(usage.over_quota)
        self.assertContains(response, "Файловое хранилище")
        self.assertContains(response, "25% от")
        self.assertContains(response, "Материалы заданий")

    @override_settings(LMS_STORAGE_QUOTA_BYTES=0)
    def test_without_a_limit_the_page_points_at_the_disk(self):
        response = self.teacher_client.get(SETTINGS_URL)

        self.assertIsNone(response.context["storage"].quota_gauge)
        self.assertContains(response, "Лимит не задан")
        self.assertContains(response, "Диск сервера")

    @override_settings(LMS_STORAGE_QUOTA_BYTES=1024)
    def test_refresh_link_recomputes_without_waiting_for_the_cache(self):
        first = self.teacher_client.get(SETTINGS_URL)
        self.assertEqual(first.context["storage"].size, 0)

        self.upload_material(size=2048)

        cached = self.teacher_client.get(SETTINGS_URL)
        self.assertEqual(cached.context["storage"].size, 0)

        refreshed = self.teacher_client.get(f"{SETTINGS_URL}?refresh=storage")
        usage = refreshed.context["storage"]
        self.assertEqual(usage.size, 2048)
        self.assertTrue(usage.over_quota)
        self.assertEqual(usage.quota_gauge.bar_class, "progress-danger")
        self.assertContains(refreshed, "Лимит превышен")
        # Лимит справочный: ученик не должен терять возможность сдать работу.
        self.assertContains(refreshed, "Загрузки не блокируются")
