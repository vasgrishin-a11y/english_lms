"""Файлы не должны пропадать после переезда каталога MEDIA_ROOT.

В базе у файла хранится только относительное имя, каталог берётся из MEDIA_ROOT.
Из-за этого смена каталога по умолчанию (<repo>/media → <repo>/var/media) молча
превращала ранее загруженные картинки в 404: имя в карточке видно, превью пустое,
скачивание отдаёт «страница не найдена». Хранилище читает такие файлы из прежних
каталогов, а новые загрузки всё равно кладёт в актуальный MEDIA_ROOT.
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings

from lms.checks import legacy_media_files
from lms.models import Assignment, AssignmentAttachment

from .base import LMSCase

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class LegacyMediaRootTests(LMSCase):
    """Каталог MEDIA_ROOT переехал, файл остался лежать по старому пути."""

    def setUp(self):
        super().setUp()
        legacy = tempfile.TemporaryDirectory(prefix="lms-legacy-media-")
        self.addCleanup(legacy.cleanup)
        self.legacy_root = Path(legacy.name)
        fallback = override_settings(LMS_MEDIA_FALLBACK_ROOTS=[str(self.legacy_root)])
        fallback.enable()
        self.addCleanup(fallback.disable)

    def put_in_legacy(self, name, content=PNG):
        """Положить файл только в прежний каталог — как было до обновления кода."""
        path = self.legacy_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_material_uploaded_before_the_move_still_previews_and_downloads(self):
        name = "assignments/1/before-the-update.png"
        self.put_in_legacy(name)
        self.assignment.material_file = name
        self.assignment.save(update_fields=["material_file"])

        preview = self.teacher_client.get(f"/preview/{name}")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")
        self.assertEqual(b"".join(preview.streaming_content), PNG)

        download = self.teacher_client.get(f"/files/{name}")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), PNG)

    def test_attachment_uploaded_before_the_move_still_downloads(self):
        name = "assignments/1/attached-earlier.png"
        self.put_in_legacy(name)
        AssignmentAttachment.objects.create(assignment=self.assignment, file=name)
        response = self.teacher_client.get(f"/files/{name}")
        self.assertEqual(response.status_code, 200)

    def test_file_missing_everywhere_is_a_clean_404(self):
        self.assignment.material_file = "assignments/1/really-gone.png"
        self.assignment.save(update_fields=["material_file"])
        self.assertEqual(
            self.teacher_client.get("/preview/assignments/1/really-gone.png").status_code, 404
        )
        self.assertEqual(
            self.teacher_client.get("/files/assignments/1/really-gone.png").status_code, 404
        )

    def test_new_uploads_never_land_in_the_old_folder(self):
        """Загрузка после переезда пишется в актуальный MEDIA_ROOT."""
        self.put_in_legacy("assignments/1/photo.png")
        saved = default_storage.save("assignments/1/photo.png", ContentFile(b"new bytes"))
        self.assertTrue((Path(self.media_root) / saved).is_file())
        # Старый файл не перезаписан: оба остаются доступны по своим именам.
        self.assertEqual((self.legacy_root / "assignments/1/photo.png").read_bytes(), PNG)
        self.assertEqual(default_storage.open(saved).read(), b"new bytes")

    def test_storage_reports_size_and_existence_from_the_old_folder(self):
        name = "assignments/1/sized.png"
        self.put_in_legacy(name)
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(default_storage.size(name), len(PNG))
        self.assertEqual(default_storage.open(name, "rb").read(), PNG)

    def test_deleting_a_record_removes_the_file_from_the_old_folder_too(self):
        name = "assignments/1/to-be-deleted.png"
        path = self.put_in_legacy(name)
        assignment = Assignment.objects.create(
            topic=self.topic, title="Временное", description="x", material_file=name
        )
        with self.captureOnCommitCallbacks(execute=True):
            assignment.delete()
        self.assertFalse(path.exists())

    def test_teacher_form_keeps_showing_a_file_that_lives_in_the_old_folder(self):
        name = "assignments/1/still-here.png"
        self.put_in_legacy(name)
        self.assignment.material_file = name
        self.assignment.save(update_fields=["material_file"])
        response = self.teacher_client.get(f"/teacher/curriculum/assignments/{self.assignment.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "still-here.png")


class CheckMediaFilesCommandTests(LMSCase):
    """Диагностика: команда показывает, какие файлы потеряны и где нашлись."""

    def setUp(self):
        super().setUp()
        legacy = tempfile.TemporaryDirectory(prefix="lms-legacy-media-")
        self.addCleanup(legacy.cleanup)
        self.legacy_root = Path(legacy.name)
        fallback = override_settings(LMS_MEDIA_FALLBACK_ROOTS=[str(self.legacy_root)])
        fallback.enable()
        self.addCleanup(fallback.disable)

    def run_command(self, *args):
        out = StringIO()
        call_command("check_media_files", *args, stdout=out)
        return out.getvalue()

    def test_reports_everything_in_place(self):
        self.assignment.material_file.save("ok.png", SimpleUploadedFile("ok.png", PNG))
        self.assertIn("Все файлы на месте", self.run_command())

    def test_reports_a_file_found_only_in_the_old_folder(self):
        name = "assignments/1/legacy.png"
        (self.legacy_root / "assignments/1").mkdir(parents=True)
        (self.legacy_root / name).write_bytes(PNG)
        self.assignment.material_file = name
        self.assignment.save(update_fields=["material_file"])
        output = self.run_command()
        self.assertIn("из прежнего каталога 1", output)
        self.assertIn("migrate_media_folder", output)

    def test_reports_a_lost_file_with_its_name(self):
        self.assignment.material_file = "assignments/1/lost.png"
        self.assignment.save(update_fields=["material_file"])
        output = self.run_command("--list-missing")
        self.assertIn("потеряно 1", output)
        self.assertIn("assignments/1/lost.png", output)
        self.assertIn("постоянное хранилище", output)


class MigrateMediaFolderCommandTests(LMSCase):
    """Перенос старых файлов в актуальный каталог."""

    def setUp(self):
        super().setUp()
        legacy = tempfile.TemporaryDirectory(prefix="lms-legacy-media-")
        self.addCleanup(legacy.cleanup)
        self.legacy_root = Path(legacy.name)
        fallback = override_settings(LMS_MEDIA_FALLBACK_ROOTS=[str(self.legacy_root)])
        fallback.enable()
        self.addCleanup(fallback.disable)
        self.source = self.legacy_root / "assignments/1/old.png"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PNG)

    def run_command(self, *args):
        out = StringIO()
        call_command("migrate_media_folder", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertIn("перенесли бы 1", output)
        self.assertFalse((Path(self.media_root) / "assignments/1/old.png").exists())
        self.assertTrue(self.source.exists())

    def test_copies_into_the_current_media_root(self):
        self.run_command()
        self.assertEqual((Path(self.media_root) / "assignments/1/old.png").read_bytes(), PNG)
        self.assertTrue(self.source.exists(), "по умолчанию исходник остаётся как страховка")

    def test_second_run_does_not_overwrite(self):
        self.run_command()
        (Path(self.media_root) / "assignments/1/old.png").write_bytes(b"newer")
        output = self.run_command()
        self.assertIn("пропущено (уже есть на месте): 1", output)
        self.assertEqual((Path(self.media_root) / "assignments/1/old.png").read_bytes(), b"newer")

    def test_move_removes_the_source(self):
        self.run_command("--move")
        self.assertFalse(self.source.exists())
        self.assertTrue((Path(self.media_root) / "assignments/1/old.png").exists())


class LegacyMediaCheckTests(LMSCase):
    """Запуск сервера предупреждает, что файлы лежат в прежнем каталоге."""

    def setUp(self):
        super().setUp()
        legacy = tempfile.TemporaryDirectory(prefix="lms-legacy-media-")
        self.addCleanup(legacy.cleanup)
        self.legacy_root = Path(legacy.name)

    def run_check(self):
        with override_settings(LMS_MEDIA_FALLBACK_ROOTS=[str(self.legacy_root)]):
            return legacy_media_files(None)

    def test_quiet_when_the_old_folder_is_empty(self):
        self.assertEqual(self.run_check(), [])

    def test_ignores_unrelated_files(self):
        (self.legacy_root / "README.txt").write_text("not an upload")
        self.assertEqual(self.run_check(), [])

    def test_warns_about_uploads_left_behind(self):
        target = self.legacy_root / "assignments/1/old.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(PNG)
        warnings = self.run_check()
        self.assertEqual([warning.id for warning in warnings], ["lms.W001"])
        self.assertIn("migrate_media_folder", warnings[0].hint)


class MissingFileNoticeTests(LMSCase):
    """Потерянный файл честно назван потерянным, а не битой картинкой и 404."""

    def setUp(self):
        super().setUp()
        fallback = override_settings(LMS_MEDIA_FALLBACK_ROOTS=[])
        fallback.enable()
        self.addCleanup(fallback.disable)
        self.assignment.material_file = "assignments/1/vanished.png"
        self.assignment.save(update_fields=["material_file"])

    def test_student_sees_an_explanation_instead_of_a_dead_link(self):
        html = self.student_client.get(self.url).content.decode()
        self.assertIn("не найден на сервере", html)
        self.assertIn("Скажите преподавателю", html)
        self.assertNotIn("/preview/assignments/1/vanished.png", html)
        self.assertNotIn("/files/assignments/1/vanished.png", html)

    def test_teacher_is_told_how_to_fix_it(self):
        AssignmentAttachment.objects.create(
            assignment=self.assignment, file="assignments/1/gone-too.png"
        )
        html = self.teacher_client.get(
            f"/teacher/curriculum/assignments/{self.assignment.pk}/"
        ).content.decode()
        self.assertIn("не найден на сервере", html)
        self.assertIn("резервной копии", html)

    def test_a_present_file_is_shown_as_usual(self):
        self.assignment.material_file.save("here.png", SimpleUploadedFile("here.png", PNG))
        html = self.student_client.get(self.url).content.decode()
        self.assertNotIn("не найден на сервере", html)
        self.assertIn("Скачать:", html)
