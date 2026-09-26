"""Сверка файлов из базы с диском: что на месте, что подобрано из старого каталога, чего нет.

Отвечает на вопрос «почему картинка в задании перестала открываться»: в базе
хранится только относительное имя файла, а каталог берётся из MEDIA_ROOT. Стоит
каталогу переехать (или пропасть вместе с контейнером без volume) — имя в
интерфейсе остаётся, а превью и скачивание отдают 404.
"""

import os
from collections import Counter

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from lms.models import Assignment, AssignmentAttachment, Feedback, QuestionResponse, Submission

SOURCES = (
    ("Материалы заданий", Assignment, "material_file"),
    ("Вложения заданий", AssignmentAttachment, "file"),
    ("Ответы учеников", Submission, "file_answer"),
    ("Голосовые комментарии", Feedback, "audio_comment"),
    ("Ответы по пунктам", QuestionResponse, "file_answer"),
)


class Command(BaseCommand):
    help = "Проверить, что все файлы из базы есть на диске (с учётом прежних каталогов)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--list-missing",
            action="store_true",
            help="Показать имена потерянных файлов, а не только количество",
        )

    def handle(self, *args, **options):
        self.stdout.write(f"MEDIA_ROOT: {settings.MEDIA_ROOT}")
        fallbacks = list(getattr(settings, "LMS_MEDIA_FALLBACK_ROOTS", []) or [])
        self.stdout.write(f"Прежние каталоги: {', '.join(fallbacks) if fallbacks else '—'}")
        self.stdout.write("")

        totals = Counter()
        missing_names = []
        legacy_names = []
        for label, model, field in SOURCES:
            names = [
                name
                for name in model.objects.exclude(**{field: ""}).values_list(field, flat=True)
                if name
            ]
            stats = Counter()
            for name in names:
                where = self._locate(name)
                stats[where] += 1
                totals[where] += 1
                if where == "missing":
                    missing_names.append(name)
                elif where == "legacy":
                    legacy_names.append(name)
            self.stdout.write(
                f"{label}: всего {len(names)}, на месте {stats['primary']}, "
                f"из прежнего каталога {stats['legacy']}, потеряно {stats['missing']}"
            )

        self.stdout.write("")
        if options["list_missing"]:
            for name in missing_names:
                self.stdout.write(f"  нет файла: {name}")

        if totals["legacy"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{totals['legacy']} файлов открываются из прежнего каталога. "
                    "Сейчас это работает, но лучше свести всё в один: "
                    "manage.py migrate_media_folder"
                )
            )
        if totals["missing"]:
            self.stdout.write(
                self.style.ERROR(
                    f"{totals['missing']} файлов нет ни в одном каталоге — именно они отдают 404. "
                    "Проверьте, что MEDIA_ROOT указывает на постоянное хранилище "
                    "(в Docker — именованный volume, а не каталог внутри образа)."
                )
            )
        if not totals["legacy"] and not totals["missing"]:
            self.stdout.write(self.style.SUCCESS("Все файлы на месте."))

    @staticmethod
    def _locate(name):
        resolver = getattr(default_storage, "resolved_path", None)
        if resolver is None:
            return "primary" if default_storage.exists(name) else "missing"
        primary = os.path.join(str(settings.MEDIA_ROOT), name)
        if os.path.isfile(primary):
            return "primary"
        return "legacy" if resolver(name) else "missing"
