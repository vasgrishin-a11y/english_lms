"""Команда загрузки и восстановления материалов курса из файла резервной копии."""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError

from lms.backup import import_curriculum, load_backup_file


class Command(BaseCommand):
    help = "Загрузить материалы курса из файла резервной копии (ZIP или JSON)."

    def add_arguments(self, parser):
        parser.add_argument("file", help="Путь к файлу резервной копии (.zip или .json)")
        parser.add_argument(
            "--strategy",
            choices=["merge", "copy"],
            default="merge",
            help="merge: дополнять существующие классы; copy: создавать новые копии",
        )
        parser.add_argument(
            "--duplicate-assignments",
            choices=["skip", "add"],
            default="skip",
            help="skip: пропускать существующие задания; add: создавать повторно",
        )
        parser.add_argument(
            "--publication-status",
            choices=["preserve", "draft"],
            default="preserve",
            help="preserve: сохранять статус публикации; draft: сделать всё черновиками",
        )

    def handle(self, *args, **options):
        file_path = Path(options["file"])
        if not file_path.exists():
            raise CommandError(f"Файл не найден: {file_path}")

        data_bytes = file_path.read_bytes()
        upload = SimpleUploadedFile(
            name=file_path.name,
            content=data_bytes,
            content_type="application/zip" if file_path.suffix == ".zip" else "application/json",
        )

        try:
            data, zip_obj = load_backup_file(upload)
            stats = import_curriculum(
                data=data,
                zip_file=zip_obj,
                strategy=options["strategy"],
                duplicate_assignments=options["duplicate_assignments"],
                publication_status=options["publication_status"],
            )
            if zip_obj:
                zip_obj.close()
        except ValidationError as exc:
            msg = exc.messages[0] if hasattr(exc, "messages") and exc.messages else str(exc)
            raise CommandError(f"Ошибка валидации бэкапа: {msg}") from exc
        except Exception as exc:
            raise CommandError(f"Ошибка восстановления бэкапа: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Курс успешно восстановлен! Статистика: "
                f"классов создано: {stats['blocks_created']}, "
                f"глав создано: {stats['chapters_created']}, "
                f"тем создано: {stats['topics_created']}, "
                f"заданий создано: {stats['assignments_created']}, "
                f"вопросов: {stats['questions_created']}, "
                f"карточек: {stats['cards_created']}, "
                f"вложений: {stats['attachments_created']}, "
                f"материалов файлов: {stats['materials_restored']}, "
                f"пропущено заданий: {stats['assignments_skipped']}."
            )
        )
