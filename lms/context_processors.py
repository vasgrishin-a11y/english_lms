"""Общий контекст интерфейса: роль, настройки плотности, счётчик очереди.

Счётчик очереди кэшируется на минуту в процессе (см.
``curriculum.cached_queue_counts``), поэтому он не добавляет запрос к каждой
странице и не заставляет базу сохранять сессию. Экран очереди кладёт свежие
значения сам.
"""

from django.core.exceptions import PermissionDenied

from . import ui_text
from .curriculum import cached_queue_counts
from .decorators import get_user_role
from .models import Profile


def is_fragment_request(request):
    """Ответ-фрагмент для htmx/AJAX: навигация в нём не рисуется.

    takim запросам не нужен бейдж «Проверка» — ради них нельзя перечитывать
    агрегат по всем сдачам и писать сессию.
    """
    headers = request.headers
    return bool(headers.get("HX-Request")) or headers.get("X-Requested-With") == "XMLHttpRequest"


def console_context(request):
    context = {
        "role": None,
        "is_teacher": False,
        "ui_density": request.session.get("ui_density", "comfortable"),
        "ui_hotkeys": request.session.get("ui_hotkeys", True),
        "pending_count": None,
        "workspace": None,
        "is_impersonating": False,
        "impersonating_teacher_name": "",
        "ui_lang": getattr(request, "ui_lang", None) or ui_text.DEFAULT_LANGUAGE,
        "ui_languages": ui_text.LANGUAGES,
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
    context["is_impersonating"] = bool(
        not context["is_teacher"] and request.session.get("impersonating_teacher_id")
    )
    context["impersonating_teacher_name"] = request.session.get("impersonating_teacher_name", "")
    if not context["is_teacher"]:
        return context
    if is_fragment_request(request):
        return context
    counts = getattr(request, "lms_queue_counts", None) or cached_queue_counts()
    context["pending_count"] = counts.get("waiting")
    return context
