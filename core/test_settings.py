"""Explicit isolated test profile. Never derives its DB from production DATABASE_URL."""

import os
import secrets
import tempfile
from pathlib import Path

os.environ["DJANGO_DEBUG"] = "True"
os.environ["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(64)
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or "sqlite:///:memory:"

from .settings import *  # noqa: E402,F403

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0
_MEDIA_TEMP = tempfile.TemporaryDirectory(prefix="english-lms-tests-")
MEDIA_ROOT = Path(_MEDIA_TEMP.name) / "media"
MEDIA_ROOT.mkdir()
STATIC_ROOT = Path(_MEDIA_TEMP.name) / "static"
STATIC_ROOT.mkdir()
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"axes": {"handlers": ["null"], "propagate": False}},
}
