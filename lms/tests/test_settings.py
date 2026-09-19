import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class ProductionSettingsTests(SimpleTestCase):
    def load_settings(self, **overrides):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DJANGO_", "PG")) and key != "DATABASE_URL"
        }
        env.update(
            {
                "DJANGO_DEBUG": "False",
                "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
                "DJANGO_ALLOWED_HOSTS": "lms.example.invalid",
                "DATABASE_URL": "postgres://ci:ci@127.0.0.1/example",
                "DJANGO_MEDIA_ROOT": "/tmp/lms-private-media",
            }
        )
        env.update(overrides)
        code = "import json; from core import settings as s; print(json.dumps([s.DEBUG,s.SECURE_SSL_REDIRECT,s.SESSION_COOKIE_SECURE,s.CSRF_COOKIE_SECURE,s.SECURE_HSTS_SECONDS]))"
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_secure_production_defaults(self):
        result = self.load_settings()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [False, True, True, True, 3600])

    def test_missing_or_placeholder_secret_rejected(self):
        for key in ("", "short", "replace-with-" + "a" * 70, "a" * 64):
            result = self.load_settings(DJANGO_SECRET_KEY=key)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_missing_database_and_sqlite_rejected_in_production(self):
        for url in ("", "sqlite:///:memory:"):
            result = self.load_settings(DATABASE_URL=url)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PostgreSQL", result.stderr)

    def test_missing_hosts_and_wildcard_rejected(self):
        for hosts in ("", "*"):
            result = self.load_settings(DJANGO_ALLOWED_HOSTS=hosts)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DJANGO_ALLOWED_HOSTS", result.stderr)

    def test_missing_relative_or_public_media_root_rejected(self):
        for root in ("", "relative/path", str(ROOT / "staticfiles"), str(ROOT), "/"):
            result = self.load_settings(DJANGO_MEDIA_ROOT=root)
            self.assertNotEqual(result.returncode, 0, root)

    def test_invalid_boolean_does_not_silently_disable_security(self):
        result = self.load_settings(DJANGO_SESSION_COOKIE_SECURE="tru")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a boolean", result.stderr)
