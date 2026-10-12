"""Preview settings for E2B iframe demo."""

from .settings import *  # noqa
from .settings import env_bool

# Allow iframe embedding in Arena preview
X_FRAME_OPTIONS = "ALLOWALL"
# Allow all hosts for preview
ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = [
    "https://*.e2b.app",
    "https://*.arena.ai",
    "https://*.e2b.dev",
    "http://*.e2b.app",
    "https://8000-ikwsjqpl3fgtz3i1wt77u.e2b.app",
]
# For iframe cookies to work, the session and CSRF cookies must be SameSite=None.
# Браузер принимает SameSite=None только вместе с Secure — иначе cookie отбрасывается
# и вход в предпросмотре зацикливается. Превью проксируется по https, поэтому оба
# флага включены по умолчанию; для локальной отладки поверх http их можно снять
# переменными DJANGO_SESSION_COOKIE_SECURE=0 и DJANGO_CSRF_COOKIE_SECURE=0.
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", True)
CSRF_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", True)
# Disable secure redirect
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
# Allow proxy header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
