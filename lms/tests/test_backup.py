"""Тесты выгрузки и загрузки резервной копии учебных материалов курса."""

import io
import json
from zipfile import ZipFile

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse

from lms.backup import (
    build_curriculum_zip,
    import_curriculum,
    load_backup_file,
)
from lms.models import (
    Assignment,
    AssignmentAttachment,
    Block,
    Chapter,
    Choice,
    Flashcard,
    Question,
    Topic,
)
from lms.tests.base import LMSCase


class BackupTests(LMSCase):
    def setUp(self):
        super().setUp()
        # Создаём главу
        self.chapter = Chapter.objects.create(
            block=self.block,
            title="Основы грамматики",
            slug="grammar-basics",
            order=1,
        )
        self.topic.chapter = self.chapter
        self.topic.save()
        Chapter.objects.filter(block=self.block, slug="obshchee").delete()

        # Добавляем к заданию material_file
        self.assignment.material_file.save(
            "worksheet.txt", ContentFile(b"Hello world material"), save=True
        )

        # Добавляем вложение
        self.attachment = AssignmentAttachment(
            assignment=self.assignment,
            title="Audio track",
            order=1,
        )
        self.attachment.file.save("audio.mp3", ContentFile(b"Fake audio track"), save=True)

        # Добавляем вопрос теста
        self.question = Question.objects.create(
            assignment=self.assignment,
            kind=Question.Kind.MCQ,
            text="Choose the correct verb form",
            points=5,
            order=1,
        )
        self.choice1 = Choice.objects.create(
            question=self.question,
            text="went",
            is_correct=True,
            order=1,
        )
        self.choice2 = Choice.objects.create(
            question=self.question,
            text="goed",
            is_correct=False,
            order=2,
        )

        # Добавляем карточки тренажёра
        self.card_assignment = self.card_assignment(
            topic=self.topic,
            title="Verbs Vocabulary",
        )
        self.card = Flashcard.objects.create(
            assignment=self.card_assignment,
            front="run",
            back="бежать",
            example="He runs fast",
            order=1,
        )

    # ── Права доступа ────────────────────────────────────────────────────────

    def test_backup_views_require_teacher(self):
        """Неавторизованные пользователи и ученики не могут получить доступ к бэкапу."""
        export_url = reverse("teacher_curriculum_export")
        backup_url = reverse("teacher_curriculum_backup")
        block_export_url = reverse("teacher_block_export", args=[self.block.pk])

        # Анонимный пользователь
        self.assertRedirects(self.client.get(export_url), f"/accounts/login/?next={export_url}")
        self.assertRedirects(self.client.get(backup_url), f"/accounts/login/?next={backup_url}")
        self.assertRedirects(
            self.client.get(block_export_url), f"/accounts/login/?next={block_export_url}"
        )

        # Ученик
        self.assertRedirects(self.student_client.get(export_url), reverse("student_assignments"))
        self.assertRedirects(self.student_client.get(backup_url), reverse("student_assignments"))
        self.assertRedirects(
            self.student_client.get(block_export_url), reverse("student_assignments")
        )
        # Ученик не может отправлять POST-запросы (403 Forbidden)
        res_post = self.student_client.post(backup_url, {"strategy": "merge"})
        self.assertEqual(res_post.status_code, 403)

        # Преподаватель
        res = self.teacher_client.get(backup_url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Резервная копия курса")
        self.assertContains(res, "Выгрузить материалы на компьютер")
        self.assertContains(res, "Загрузить материалы на сайт")

    # ── Экспорт курса ────────────────────────────────────────────────────────

    def test_curriculum_export_zip(self):
        """Выгрузка полного ZIP-архива курса содержит manifest.json и файлы."""
        res = self.teacher_client.get(reverse("teacher_curriculum_export"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/zip")
        self.assertIn("english_lms_backup_", res["Content-Disposition"])

        zf = ZipFile(io.BytesIO(b"".join(res.streaming_content)))
        names = zf.namelist()
        self.assertIn("backup.json", names)

        manifest = json.loads(zf.read("backup.json").decode("utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["blocks_count"], 1)
        self.assertEqual(manifest["assignments_count"], 2)

        # Проверяем наличие прикреплённых файлов в архиве
        file_entries = [name for name in names if name.startswith("files/")]
        self.assertGreaterEqual(len(file_entries), 2)
        # Проверяем чтение файла материала
        mat_entry = [name for name in file_entries if "materials" in name][0]
        self.assertEqual(zf.read(mat_entry), b"Hello world material")
        att_entry = [name for name in file_entries if "attachments" in name][0]
        self.assertEqual(zf.read(att_entry), b"Fake audio track")

    def test_curriculum_export_json(self):
        """Выгрузка структуры в формате JSON."""
        res = self.teacher_client.get(reverse("teacher_curriculum_export") + "?format=json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/json; charset=utf-8")
        data = json.loads(res.content.decode("utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["blocks"]), 1)
        self.assertEqual(data["blocks"][0]["name"], self.block.name)

    def test_block_export(self):
        """Выгрузка отдельного класса в ZIP."""
        res = self.teacher_client.get(reverse("teacher_block_export", args=[self.block.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/zip")
        self.assertIn(f"english_lms_{self.block.slug}", res["Content-Disposition"])

    # ── Полный цикл: выгрузка → удаление → загрузка (восстановление) ─────────

    def test_backup_and_restore_roundtrip(self):
        """Экспорт курса, удаление всех данных и восстановление из бэкапа."""
        # 1. Экспортируем в ZIP
        zip_buf = build_curriculum_zip()

        # 2. Удаляем все учебные материалы
        Block.objects.all().delete()
        self.assertEqual(Block.objects.count(), 0)
        self.assertEqual(Chapter.objects.count(), 0)
        self.assertEqual(Topic.objects.count(), 0)
        self.assertEqual(Assignment.objects.count(), 0)
        self.assertEqual(Question.objects.count(), 0)
        self.assertEqual(Flashcard.objects.count(), 0)

        # 3. Загружаем через интерфейс сайта
        zip_file = SimpleUploadedFile(
            "course_backup.zip",
            zip_buf.getvalue(),
            content_type="application/zip",
        )
        res = self.teacher_client.post(
            reverse("teacher_curriculum_backup"),
            {
                "backup_file": zip_file,
                "strategy": "merge",
                "duplicate_assignments": "skip",
                "publication_status": "preserve",
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Резервная копия успешно загружена")

        # 4. Проверяем, что всё восстановилось в базе
        self.assertEqual(Block.objects.count(), 1)
        restored_block = Block.objects.first()
        self.assertEqual(restored_block.name, "English")

        self.assertEqual(Chapter.objects.count(), 1)
        restored_chapter = Chapter.objects.first()
        self.assertEqual(restored_chapter.title, "Основы грамматики")

        self.assertEqual(Topic.objects.count(), 1)
        restored_topic = Topic.objects.first()
        self.assertEqual(restored_topic.title, "Grammar")

        self.assertEqual(Assignment.objects.count(), 2)
        restored_assn = Assignment.objects.get(title="Past tense")
        self.assertEqual(restored_assn.max_points, 100)

        # Проверяем восстановление файла материалов
        self.assertTrue(bool(restored_assn.material_file))
        with restored_assn.material_file.open("rb") as f:
            self.assertEqual(f.read(), b"Hello world material")

        # Проверяем восстановление вложений
        self.assertEqual(restored_assn.attachments.count(), 1)
        restored_att = restored_assn.attachments.first()
        self.assertEqual(restored_att.title, "Audio track")
        with restored_att.file.open("rb") as f:
            self.assertEqual(f.read(), b"Fake audio track")

        # Проверяем восстановление вопросов и вариантов
        self.assertEqual(restored_assn.questions.count(), 1)
        restored_q = restored_assn.questions.first()
        self.assertEqual(restored_q.choices.count(), 2)
        correct_choices = restored_q.choices.filter(is_correct=True)
        self.assertEqual(correct_choices.count(), 1)
        self.assertEqual(correct_choices.first().text, "went")

        # Проверяем восстановление карточек
        restored_card_assn = Assignment.objects.get(title="Verbs Vocabulary")
        self.assertEqual(restored_card_assn.cards.count(), 1)
        restored_card = restored_card_assn.cards.first()
        self.assertEqual(restored_card.front, "run")
        self.assertEqual(restored_card.back, "бежать")

    # ── Стратегии импорта ────────────────────────────────────────────────────

    def test_import_strategy_merge_and_skip_duplicates(self):
        """При стратегии merge и skip дубликаты заданий не создаются повторно."""
        zip_buf = build_curriculum_zip()
        zip_file = SimpleUploadedFile(
            "backup.zip", zip_buf.getvalue(), content_type="application/zip"
        )

        data, zf = load_backup_file(zip_file)
        stats = import_curriculum(
            data,
            zf,
            strategy="merge",
            duplicate_assignments="skip",
        )
        zf.close()

        # Класс и тема сопоставлены, задания пропущены
        self.assertEqual(stats["blocks_matched"], 1)
        self.assertEqual(stats["blocks_created"], 0)
        self.assertEqual(stats["assignments_skipped"], 2)
        self.assertEqual(stats["assignments_created"], 0)
        self.assertEqual(Assignment.objects.count(), 2)

    def test_import_strategy_copy(self):
        """При стратегии copy создаётся отдельная независимая копия класса."""
        zip_buf = build_curriculum_zip()
        zip_file = SimpleUploadedFile(
            "backup.zip", zip_buf.getvalue(), content_type="application/zip"
        )

        data, zf = load_backup_file(zip_file)
        stats = import_curriculum(
            data,
            zf,
            strategy="copy",
        )
        zf.close()

        self.assertEqual(stats["blocks_created"], 1)
        self.assertEqual(stats["assignments_created"], 2)
        # Стало 2 класса
        self.assertEqual(Block.objects.count(), 2)
        copy_block = Block.objects.exclude(pk=self.block.pk).first()
        self.assertIn("копия", copy_block.name)

    def test_import_strategy_draft_status(self):
        """При publication_status='draft' все задания восстанавливаются как черновики."""
        zip_buf = build_curriculum_zip()
        zip_file = SimpleUploadedFile(
            "backup.zip", zip_buf.getvalue(), content_type="application/zip"
        )

        data, zf = load_backup_file(zip_file)
        import_curriculum(
            data,
            zf,
            strategy="copy",
            publication_status="draft",
        )
        zf.close()

        copy_block = Block.objects.exclude(pk=self.block.pk).first()
        for assn in Assignment.objects.filter(topic__block=copy_block):
            self.assertEqual(assn.status, Assignment.Publication.DRAFT)

    # ── Обработка ошибок валидации ───────────────────────────────────────────

    def test_invalid_backup_files(self):
        """Некорректные файлы вызывают информативные ошибки валидации."""
        backup_url = reverse("teacher_curriculum_backup")

        # 1. Пустой запрос без файла
        res = self.teacher_client.post(backup_url, {}, follow=True)
        self.assertContains(res, "Выберите файл резервной копии")

        # 2. Невалидный ZIP
        bad_zip = SimpleUploadedFile("broken.zip", b"not a zip", content_type="application/zip")
        res = self.teacher_client.post(backup_url, {"backup_file": bad_zip}, follow=True)
        self.assertContains(res, "Ошибка загрузки резервной копии")

        # 3. ZIP без backup.json
        empty_zip_buf = io.BytesIO()
        with ZipFile(empty_zip_buf, "w") as z:
            z.writestr("something_else.txt", "hello")
        empty_zip = SimpleUploadedFile(
            "no_manifest.zip", empty_zip_buf.getvalue(), content_type="application/zip"
        )
        res = self.teacher_client.post(backup_url, {"backup_file": empty_zip}, follow=True)
        self.assertContains(res, "не найден файл структуры курса")

    # ── Дополнительные сценарии ──────────────────────────────────────────────

    def test_json_roundtrip_import(self):
        """Восстановление структуры курса из JSON-файла (без архива)."""
        res = self.teacher_client.get(reverse("teacher_curriculum_export") + "?format=json")
        json_file = SimpleUploadedFile(
            "course.json",
            res.content,
            content_type="application/json",
        )

        Block.objects.all().delete()
        self.assertEqual(Block.objects.count(), 0)

        post_res = self.teacher_client.post(
            reverse("teacher_curriculum_backup"),
            {"backup_file": json_file, "strategy": "merge"},
            follow=True,
        )
        self.assertEqual(post_res.status_code, 200)
        self.assertEqual(Block.objects.count(), 1)
        self.assertEqual(Assignment.objects.count(), 2)

    def test_quiz_question_kinds_and_groups_roundtrip(self):
        """Вопросы сопоставления (match) и группы доступа корректно экспортируются и восстанавливаются."""
        from lms.models import Group

        group = Group.objects.create(name="Advanced Group")
        self.block.groups.add(group)

        # Добавляем вопрос на сопоставление
        q_match = Question.objects.create(
            assignment=self.assignment,
            kind=Question.Kind.MATCH,
            text="Match opposites",
            points=2,
            order=2,
        )
        Choice.objects.create(
            question=q_match,
            text="hot",
            match_text="cold",
            order=1,
        )
        Choice.objects.create(
            question=q_match,
            text="big",
            match_text="small",
            order=2,
        )

        zip_buf = build_curriculum_zip()

        # Очищаем и восстанавливаем
        Block.objects.all().delete()

        upload = SimpleUploadedFile(
            "backup.zip", zip_buf.getvalue(), content_type="application/zip"
        )
        data, zf = load_backup_file(upload)
        import_curriculum(data, zf, strategy="merge")
        zf.close()

        restored_block = Block.objects.first()
        self.assertIn("Advanced Group", [g.name for g in restored_block.groups.all()])

        restored_assn = Assignment.objects.get(title="Past tense")
        restored_q = restored_assn.questions.get(kind=Question.Kind.MATCH)
        pairs = {c.text: c.match_text for c in restored_q.choices.all()}
        self.assertEqual(pairs, {"hot": "cold", "big": "small"})

    # ── Управление через консольные команды (CLI) ───────────────────────────

    def test_management_commands(self):
        """Консольные команды export_curriculum и import_curriculum работают штатно."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
            call_command("export_curriculum", output=tmp.name)
            self.assertGreater(len(tmp.read()), 0)

            # Импортируем поверх в режиме merge
            call_command(
                "import_curriculum",
                tmp.name,
                strategy="merge",
                duplicate_assignments="skip",
            )
