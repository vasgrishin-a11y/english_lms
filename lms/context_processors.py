"""Общий контекст интерфейса: роль, настройки плотности, счётчик очереди.

Счётчик очереди кэшируется в сессии на минуту, чтобы не добавлять запрос к
каждой странице; экран очереди кладёт свежие значения сам.
"""

import time

from django.core.exceptions import PermissionDenied

from .curriculum import queue_counts
from .decorators import get_user_role
from .models import Profile

CACHE_KEY = "_queue_counts"
CACHE_TTL = 60


def console_context(request):
    context = {
        "role": None,
        "is_teacher": False,
        "ui_density": request.session.get("ui_density", "comfortable"),
        "ui_hotkeys": request.session.get("ui_hotkeys", True),
        "pending_count": None,
        "workspace": None,
    }
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return context
    try:
        role = get_user_role(user)
    except PermissionDenied:
        return context
    context["role"] = role
    context["is_teacher"] = role == Profile.Role.TEACHER
    if not context["is_teacher"]:
        return context
    cached = getattr(request, "lms_queue_counts", None)
    if cached is None:
        stored = request.session.get(CACHE_KEY) or {}
        if time.time() - float(stored.get("ts", 0)) > CACHE_TTL:
            stored = {"ts": time.time(), **queue_counts()}
            request.session[CACHE_KEY] = stored
        cached = stored
    context["pending_count"] = cached.get("waiting")
    return context
