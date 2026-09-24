"""Preview settings for E2B iframe demo."""

from .settings import *  # noqa

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
# For iframe cookies to work, SameSite=None and Secure must be handled.
# In preview we are https-terminated by proxy, but Django sees http, so we need to allow.
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SAMESITE = "None"
CSRF_COOKIE_SECURE = False
# Disable secure redirect
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
# Allow proxy header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
