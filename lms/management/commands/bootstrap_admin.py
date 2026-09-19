import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Explicit, idempotent administrator bootstrap; never promotes or resets existing users."

    @transaction.atomic
    def handle(self, *args, **options):
        username = os.getenv("DJANGO_SUPERUSER_USERNAME", "")
        if not username:
            raise CommandError("DJANGO_SUPERUSER_USERNAME is required")
        User = get_user_model()
        existing = User.objects.filter(username=username).first()
        if existing:
            if not (existing.is_superuser and existing.is_staff and existing.is_active):
                raise CommandError(
                    "Username belongs to an account which is not an active administrator"
                )
            self.stdout.write("Administrator already exists; credentials unchanged.")
            return
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD", "")
        if not password:
            raise CommandError("DJANGO_SUPERUSER_PASSWORD is required for a new administrator")
        user = User(
            username=username,
            email=os.getenv("DJANGO_SUPERUSER_EMAIL", ""),
            is_staff=True,
            is_superuser=True,
        )
        try:
            validate_password(password, user=user)
            user.set_password(password)
            user.full_clean()
        except ValidationError as exc:
            raise CommandError(
                "Invalid administrator credentials: " + "; ".join(exc.messages)
            ) from exc
        user.save()
        self.stdout.write(
            "Administrator created. Remove bootstrap credentials from the deployment environment."
        )
