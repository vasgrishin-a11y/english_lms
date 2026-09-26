from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from lms.file_cleanup import delete_unreferenced_file
from lms.models import Assignment, Feedback, QuestionResponse, Submission


class Command(BaseCommand):
    help = (
        "Report orphan uploads; --delete removes only old, unreferenced files in upload namespaces."
    )

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true")
        parser.add_argument("--min-age-hours", type=int, default=24)

    def handle(self, *args, **options):
        if options["min_age_hours"] < 1:
            raise CommandError(
                "A grace period of at least one hour is required for in-flight uploads"
            )
        root = Path(settings.MEDIA_ROOT).resolve()
        cutoff = (timezone.now() - timedelta(hours=options["min_age_hours"])).timestamp()
        referenced = set(
            Assignment.objects.exclude(material_file="").values_list("material_file", flat=True)
        )
        referenced.update(
            Submission.objects.exclude(file_answer="").values_list("file_answer", flat=True)
        )
        referenced.update(
            Feedback.objects.exclude(audio_comment="").values_list("audio_comment", flat=True)
        )
        referenced.update(
            QuestionResponse.objects.exclude(file_answer="").values_list("file_answer", flat=True)
        )
        count = 0
        for namespace in ("assignments", "submissions"):
            for path in (root / namespace).rglob("*"):
                if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
                    continue
                name = path.relative_to(root).as_posix()
                if name in referenced or path.stat().st_mtime >= cutoff:
                    continue
                count += 1
                if options["delete"]:
                    delete_unreferenced_file(name)
        self.stdout.write(
            f"Old unreferenced files: {count}. "
            + ("Cleanup requested." if options["delete"] else "Dry run; use --delete to remove.")
        )
