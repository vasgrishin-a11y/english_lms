from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q

from lms.models import Assignment, Feedback


class Command(BaseCommand):
    help = "Read-only pre-upgrade check; works with the original 0001 schema."

    def handle(self, *args, **options):
        bad_maxima = list(
            Assignment.objects.filter(max_points__gt=1000).values_list("pk", flat=True)
        )
        bad_grades = list(
            Feedback.objects.filter(
                Q(grade__gt=1000) | Q(grade__gt=F("submission__assignment__max_points"))
            ).values_list("pk", flat=True)
        )
        if bad_grades or bad_maxima:
            raise CommandError(
                f"Review invalid values with the teacher. Assignment IDs: {bad_maxima}; Feedback IDs: {bad_grades}. Nothing was changed."
            )
        ambiguous = Feedback.objects.filter(
            submission__status__in=["new", "submitted", "in_review"]
        ).count()
        if ambiguous:
            self.stderr.write(
                f"WARNING: {ambiguous} legacy feedback records are attached to unreviewed statuses. Reconcile these manually; overwritten historical answers cannot be recovered."
            )
        self.stdout.write("Grade bounds are valid. No data was changed.")
