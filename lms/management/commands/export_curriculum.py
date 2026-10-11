"""Команда выгрузки материалов курса в файл резервной копии."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from lms.backup import build_curriculum_zip, serialize_curriculum
from lms.models import Block


class Command(BaseCommand):
    help = "Выгрузить материалы курса (классы, главы, темы, задания, файлы) в ZIP или JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            help="Путь к файлу для сохранения архива (по умолчанию: english_lms_backup_<дата>.zip)",
        )
        parser.add_argument(
            "--block",
            help="Идентификатор или URL-slug конкретного класса для выгрузки (по умолчанию: все)",
        )
        parser.add_argument(
            "--format",
            choices=["zip", "json"],
            default="zip",
            help="Формат выгрузки: zip (полный архив с файлами) или json (структура без файлов)",
        )

    def handle(self, *args, **options):
        block_arg = options.get("block")
        blocks = None
        if block_arg:
            if block_arg.isdigit():
                blocks = Block.objects.filter(pk=int(block_arg))
            else:
                blocks = Block.objects.filter(slug=block_arg)
            if not blocks.exists():
                raise CommandError(f"Класс не найден: {block_arg}")

        out_format = options.get("format", "zip")
        now_str = timezone.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"english_lms_backup_{now_str}.{out_format}"
        out_path = Path(options.get("output") or default_name)

        if out_format == "json":
            import json

            manifest, _ = serialize_curriculum(blocks=blocks)
            out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            buf = build_curriculum_zip(blocks=blocks)
            out_path.write_bytes(buf.getvalue())

        self.stdout.write(
            self.style.SUCCESS(f"Резервная копия успешно выгружена: {out_path.resolve()}")
        )
