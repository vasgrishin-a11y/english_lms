"""Перенос файлов из старой папки media/ в новую var/media/ для сохранения после обновления.

Старый путь <repo>/media/ был по умолчанию в DEBUG, новый — <repo>/var/media/,
который совпадает с /app/var/media в Docker и монтируется как volume.
Команда копирует файлы, если новая папка пуста, а старая содержит данные.
Безопасна для повторного запуска: не перезаписывает существующие файлы.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Перенести файлы из <repo>/media в <repo>/var/media (если новая пуста)"

    def handle(self, *args, **options):
        base = Path(settings.BASE_DIR)
        legacy = base / "media"
        new = base / "var" / "media"

        # Если задан DJANGO_MEDIA_ROOT извне — ничего не делаем, это production volume
        import os

        if os.getenv("DJANGO_MEDIA_ROOT"):
            self.stdout.write(
                self.style.WARNING(
                    "DJANGO_MEDIA_ROOT задан явно — это production volume, перенос не нужен."
                )
            )
            return

        if not legacy.exists():
            self.stdout.write("Старая папка media/ не найдена — перенос не нужен.")
            return

        new.mkdir(parents=True, exist_ok=True)

        legacy_files = list(legacy.rglob("*"))
        if not legacy_files:
            self.stdout.write("Старая папка media/ пуста — перенос не нужен.")
            return

        new_files = list(new.rglob("*"))
        # Считаем только файлы, не директории
        new_file_count = sum(1 for p in new_files if p.is_file())
        if new_file_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Новая папка {new} уже содержит {new_file_count} файлов — "
                    "автоматический перенос пропущен, чтобы не перезаписать. "
                    "Если нужно объединить, скопируйте вручную: cp -a media/* var/media/"
                )
            )
            return

        copied = 0
        for src in legacy.rglob("*"):
            if src.is_dir():
                continue
            rel = src.relative_to(legacy)
            dst = new / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                continue
            shutil.copy2(src, dst)
            copied += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Скопировано файлов: {copied} из {legacy} в {new}. "
                "Проверьте, что всё на месте, затем можете удалить старую папку "
                "или оставить — настройки теперь используют var/media."
            )
        )
