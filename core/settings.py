"""Fail-closed production settings; opt into local development with DJANGO_DEBUG=True."""

import os
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    if value.strip().lower() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ImproperlyConfigured(f"{name} must be a boolean")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default, minimum=1):
    try:
        value = int(os.getenv(name, default))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc
    if value < minimum:
        raise ImproperlyConfigured(f"{name} must be >= {minimum}")
    return value


DEBUG = env_bool("DJANGO_DEBUG")
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY; generate one with secrets.token_urlsafe(64)")
if not DEBUG and (
    len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or SECRET_KEY.startswith(("django-insecure-", "replace-", "local-development-"))
):
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a random secret of at least 50 characters")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "").split(",")
    if host.strip()
]
if not DEBUG and (not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS):
    raise ImproperlyConfigured("Set explicit DJANGO_ALLOWED_HOSTS in production (no wildcard)")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "lms.apps.LmsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "lms.middleware.UILanguageMiddleware",
    "lms.middleware.UploadLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]
ROOT_URLCONF = "core.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "lms.context_processors.console_context",
            ]
        },
    }
]
WSGI_APPLICATION = "core.wsgi.application"

_database_url = os.getenv("DATABASE_URL")
if not _database_url and os.getenv("PGHOST"):
    _database_url = "postgres://{user}:{password}@{host}:{port}/{name}".format(
        user=quote(os.getenv("PGUSER", "postgres"), safe=""),
        password=quote(os.getenv("PGPASSWORD", ""), safe=""),
        host=os.environ["PGHOST"],
        port=os.getenv("PGPORT", "5432"),
        name=quote(os.getenv("PGDATABASE", "postgres"), safe=""),
    )
if not _database_url:
    if not DEBUG:
        raise ImproperlyConfigured("DATABASE_URL (PostgreSQL) is required in production")
    _database_url = f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
DATABASES = {
    "default": dj_database_url.parse(_database_url, conn_max_age=600, conn_health_checks=True)
}
if not DEBUG and DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured(
        "Use PostgreSQL in production; SQLite is for explicit local development/tests"
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = env_int("DJANGO_LOGIN_FAILURE_LIMIT", 5)
AXES_COOLOFF_TIME = timedelta(minutes=env_int("DJANGO_LOGIN_COOLOFF_MINUTES", 15))
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False
AXES_LOCKOUT_TEMPLATE = "lms/locked_out.html"
AXES_HTTP_RESPONSE_CODE = 429
AXES_CLIENT_IP_CALLABLE = "lms.security.client_ip"
AXES_VERBOSE = False
AXES_SENSITIVE_PARAMETERS = ["username", "ip_address", "user_agent"]
# Only these peers may supply X-Real-IP. The ingress must overwrite that header.
TRUSTED_PROXY_IPS = [
    ip.strip() for ip in os.getenv("DJANGO_TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = Path(os.getenv("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
STORAGES = {
    # Приватные файлы: пишем в MEDIA_ROOT, читаем и из прежних каталогов —
    # см. LMS_MEDIA_FALLBACK_ROOTS ниже и lms/storage.py.
    "default": {"BACKEND": "lms.storage.PrivateMediaStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        "OPTIONS": {"file_permissions_mode": 0o644, "directory_permissions_mode": 0o755},
    },
}
# This is an authorized Django route, NEVER an alias to a public media directory.
MEDIA_URL = "/files/"
_media_root = os.getenv("DJANGO_MEDIA_ROOT")
if not DEBUG and not _media_root:
    raise ImproperlyConfigured("Set DJANGO_MEDIA_ROOT to a persistent private directory")
# В разработке по умолчанию — <repo>/var/media (а не <repo>/media), чтобы
# совпадать с путём в Docker (/app/var/media) и переживать git pull / rebuild:
# папка var/ уже в .gitignore и .dockerignore, а volume private-media в
# compose.yaml монтируется именно туда. Переопределить можно DJANGO_MEDIA_ROOT.
_default_media = BASE_DIR / "var" / "media"
_legacy_media = BASE_DIR / "media"
MEDIA_ROOT = Path(_media_root or (_default_media if DEBUG else BASE_DIR / "media"))
if not MEDIA_ROOT.is_absolute():
    raise ImproperlyConfigured("DJANGO_MEDIA_ROOT must be an absolute path")
if (
    MEDIA_ROOT.resolve() == BASE_DIR
    or MEDIA_ROOT.resolve() in BASE_DIR.parents
    or MEDIA_ROOT.resolve() == STATIC_ROOT.resolve()
    or STATIC_ROOT.resolve() in MEDIA_ROOT.resolve().parents
):
    raise ImproperlyConfigured("Private media must not be stored inside STATIC_ROOT")
# Прежние каталоги MEDIA_ROOT. В базе у файла хранится только относительное имя,
# поэтому переезд каталога (<repo>/media → <repo>/var/media) молча превращал все
# ранее загруженные картинки в 404. Читаем их со старого места — записи всё равно
# идут в актуальный MEDIA_ROOT. Свести файлы в один каталог: manage.py
# migrate_media_folder. Дополнительные каталоги — DJANGO_MEDIA_FALLBACK_ROOTS
# (через «,» или os.pathsep).
_fallback_separator = r"[,%s]" % re.escape(os.pathsep)
_fallback_roots = [
    Path(item.strip())
    for item in re.split(_fallback_separator, os.getenv("DJANGO_MEDIA_FALLBACK_ROOTS", ""))
    if item.strip()
]
LMS_MEDIA_FALLBACK_ROOTS = [
    str(path)
    for path in dict.fromkeys(_fallback_roots + [_legacy_media, _default_media])
    if path.is_absolute() and path.resolve() != MEDIA_ROOT.resolve()
]
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
# Создаём каталог для файлов в разработке, чтобы первый upload не падал
# из-за отсутствия папки (в production это делает entrypoint + volume).
if DEBUG:
    try:
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
# ── ИИ-помощник преподавателя ─────────────────────────────────────────────
# Локальная модель: помощник не ходит в облако, материалы остаются на сервере.
# Пустое включение не делается молча: без LMS_AI_LOCAL=1 (или ключа для своего
# шлюза) помощник работает офлайн-эвристиками. Выключить целиком — LMS_AI_ENABLED=0.
LMS_AI_ENABLED = env_bool("LMS_AI_ENABLED", True)
# Провайдер: ollama (по умолчанию), lmstudio или свой OpenAI-совместимый шлюз
# (незнакомое имя тоже считается OpenAI-совместимым — задайте LMS_AI_ENDPOINT).
LMS_AI_PROVIDER = os.getenv("LMS_AI_PROVIDER", "ollama").strip().lower() or "ollama"
LMS_AI_API_KEY = os.getenv("LMS_AI_API_KEY", "").strip()
# Локальная модель работает без ключа: включается явным флагом.
LMS_AI_LOCAL = env_bool("LMS_AI_LOCAL", False)
LMS_AI_MODEL = os.getenv("LMS_AI_MODEL", "").strip()
LMS_AI_ENDPOINT = os.getenv("LMS_AI_ENDPOINT", "").strip()
# TLS: нужно только для своего шлюза с внутренним удостоверяющим центром.
LMS_AI_CA_BUNDLE = os.getenv("LMS_AI_CA_BUNDLE", "").strip()
LMS_AI_VERIFY_SSL = env_bool("LMS_AI_VERIFY_SSL", True)
# response_format=json_object: у Ollama/LM Studio выключен по умолчанию,
# принудительно включается LMS_AI_JSON_MODE=1 (или выключается =0).
LMS_AI_JSON_MODE = env_bool("LMS_AI_JSON_MODE", False)
# Какие файлы отправлять модели как вложение: image или none. Пусто — по провайдеру.
LMS_AI_UPLOAD_KINDS = os.getenv("LMS_AI_UPLOAD_KINDS", "").strip()
LMS_AI_TIMEOUT = env_int("LMS_AI_TIMEOUT", 60)
LMS_MAX_FILE_BYTES = env_int("LMS_MAX_FILE_BYTES", 20 * 1024 * 1024)
LMS_STUDENT_QUOTA_BYTES = env_int("LMS_STUDENT_QUOTA_BYTES", 200 * 1024 * 1024)
LMS_SUBMISSIONS_PER_HOUR = env_int("LMS_SUBMISSIONS_PER_HOUR", 30)
LMS_PAGE_SIZE = env_int("LMS_PAGE_SIZE", 25)
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 5
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_HANDLERS = [
    "lms.uploads.LimitedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = LOGIN_URL
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
# Opt in only behind an ingress which strips user-supplied forwarding headers.
if env_bool("DJANGO_TRUST_PROXY_SSL_HEADER"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0 if DEBUG else 3600, minimum=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS")
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD")
SECURE_REDIRECT_EXEMPT = [r"^health/(live|ready)/$"]
# Deliberate, documented exceptions: shared-host tenants cannot promise HTTPS on
# every subdomain or enroll somebody else's domain in the browser preload list.
# All other deployment warnings remain blocking in CI.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "lms": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "axes": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
