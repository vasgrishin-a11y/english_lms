from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from lms.decorators import get_user_role
from lms.models import Profile


class Command(BaseCommand):
    help = "Irreversible student erasure, including attempts, feedback and unshared files. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--confirm-username")

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.select_for_update().get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError("Account not found") from exc
        if user.is_staff or user.is_superuser or get_user_role(user) != Profile.Role.STUDENT:
            raise CommandError("This command only erases non-staff student accounts")
        count = user.submissions.count()
        if options["confirm_username"] != user.username:
            self.stdout.write(
                f"Dry run: {count} attempts. Repeat with --confirm-username matching the username to erase."
            )
            return
        user.delete()
        self.stdout.write(
            f"Student and {count} attempts erased; unreferenced file deletion scheduled after commit."
        )
