"""Сведение файлов из прежних каталогов MEDIA_ROOT в актуальный.

В базе у файла хранится только относительное имя, каталог берётся из MEDIA_ROOT.
Поэтому переезд каталога (<repo>/media → <repo>/var/media, смена DJANGO_MEDIA_ROOT)
оставлял старые файлы лежать по старому пути. Читать их приложение умеет и так —
lms/storage.py заглядывает в settings.LMS_MEDIA_FALLBACK_ROOTS, — но держать всё
в одном каталоге надёжнее: бэкап, volume и очистка «сирот» смотрят только туда.

Команда безопасна для повторного запуска: существующие файлы не перезаписываются.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Перенести файлы из прежних каталогов (например <repo>/media) в текущий MEDIA_ROOT"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Только показать, что будет перенесено"
        )
        parser.add_argument(
            "--move",
            action="store_true",
            help="Удалять исходный файл после успешного копирования",
        )

    def handle(self, *args, **options):
        target = Path(settings.MEDIA_ROOT)
        sources = [
            path
            for path in (Path(item) for item in getattr(settings, "LMS_MEDIA_FALLBACK_ROOTS", []))
            if path.is_dir()
        ]
        self.stdout.write(f"Куда: {target}")
        if not sources:
            self.stdout.write("Прежних каталогов с файлами не найдено — переносить нечего.")
            return

        copied = skipped = 0
        for source in sources:
            files = [path for path in source.rglob("*") if path.is_file()]
            self.stdout.write(f"Откуда: {source} (файлов: {len(files)})")
            for src in files:
                dst = target / src.relative_to(source)
                if dst.exists():
                    skipped += 1
                    continue
                copied += 1
                if options["dry_run"]:
                    self.stdout.write(f"  перенесли бы: {src.relative_to(source)}")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                if options["move"]:
                    src.unlink()

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(f"Пробный запуск: перенесли бы {copied}, пропустили {skipped}.")
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Перенесено файлов: {copied}, пропущено (уже есть на месте): {skipped}. "
                "Проверьте результат: manage.py check_media_files"
            )
        )
