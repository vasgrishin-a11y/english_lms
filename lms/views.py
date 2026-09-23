"""Общие представления: маршрутизация по ролям, защищённые файлы, здоровье.

Файлы никогда не отдаются по сырому пути из URL: имя сначала разрешается в
запись БД, затем проверяется роль и принадлежность, и только потом открывается
файл. Скачивание — всегда attachment; inline-превью разрешено только для
изображений и аудио с повторной проверкой сигнатуры.
"""

import logging
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .decorators import get_user_role
from .models import Assignment, Profile, QuestionResponse, Submission
from .search import student_suggest, teacher_suggest

logger = logging.getLogger(__name__)

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
}
PREVIEW_TYPES = {**IMAGE_TYPES, **AUDIO_TYPES}
PREVIEW_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".wav": (b"RIFF",),
    ".mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"),
    ".m4a": (b"ftyp",),
    ".aac": (b"\xff\xf1", b"\xff\xf9", b"ID3", b"ftyp"),
    ".ogg": (b"OggS",),
}


def _add_validation_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


@login_required
@require_GET
def suggest(request):
    """Подсказки поиска: только объекты из БД, в контексте роли и экрана."""
    query = request.GET.get("q", "")
    scope = request.GET.get("scope", "")
    if get_user_role(request.user) == Profile.Role.TEACHER:
        items = teacher_suggest(scope, query)
    else:
        items = student_suggest(scope, query, request.user)
    return JsonResponse({"items": items})


@login_required
@require_GET
def dashboard(request):
    """Единая точка входа: учитель — в консоль, ученик — на свою главную."""
    if get_user_role(request.user) == Profile.Role.TEACHER:
        return redirect("teacher_home")
    return redirect("student_home")


def _resolve_private_file(request, name):
    """Найти файл по имени из БД и проверить доступ. Возвращает FileField или None."""
    teacher = get_user_role(request.user) == Profile.Role.TEACHER
    materials = Assignment.objects.filter(material_file=name)
    attempts = Submission.objects.filter(file_answer=name)
    items = QuestionResponse.objects.filter(file_answer=name)
    if not teacher:
        visible = Assignment.objects.visible()
        materials = materials.filter(pk__in=visible)
        attempts = attempts.filter(
            student=request.user,
            assignment__in=visible,
        )
        items = items.filter(student=request.user, assignment__in=visible)
    for queryset, field in ((materials, "material_file"), (attempts, "file_answer")):
        found = queryset.first()
        if found:
            return getattr(found, field)
    item = items.first()
    return item.file_answer if item else None


def _signature_matches(name, file):
    extension = Path(name).suffix.lower()
    expected = PREVIEW_SIGNATURES.get(extension)
    if not expected:
        return False
    position = file.tell()
    try:
        file.seek(0)
        header = file.read(16)
    finally:
        file.seek(position)
    if any(header.startswith(prefix) for prefix in expected):
        return True
    # MP4-контейнер: 'ftyp' находится на смещении 4.
    return extension in {".m4a", ".aac"} and header[4:8] == b"ftyp"


def _file_response(file, *, inline, content_type):
    try:
        response = FileResponse(
            file.open("rb"),
            as_attachment=not inline,
            filename=Path(file.name).name,
            content_type=content_type,
        )
    except FileNotFoundError as exc:
        raise Http404 from exc
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Frame-Options"] = "DENY"
    response["Referrer-Policy"] = "no-referrer"
    if inline:
        # Превью не исполняет ничего: ни скриптов, ни внешних ресурсов.
        response["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
        )
        response["Content-Disposition"] = f'inline; filename="{Path(file.name).name}"'
    return response


@login_required
@never_cache
def private_file(request, name):
    """Скачивание: только имена из БД, только с проверкой роли и принадлежности."""
    file = _resolve_private_file(request, name)
    if not file:
        raise Http404
    return _file_response(file, inline=False, content_type="application/octet-stream")


@login_required
@require_GET
@never_cache
def media_preview(request, name):
    """Inline-превью изображения или аудио. SVG, PDF и HTML не превьюируются никогда."""
    extension = Path(name).suffix.lower()
    content_type = PREVIEW_TYPES.get(extension)
    if not content_type:
        raise Http404
    file = _resolve_private_file(request, name)
    if not file:
        raise Http404
    if file.size > settings.LMS_MAX_FILE_BYTES:
        raise Http404
    try:
        with file.open("rb") as handle:
            if not _signature_matches(name, handle):
                raise Http404
    except FileNotFoundError as exc:
        raise Http404 from exc
    return _file_response(file, inline=True, content_type=content_type)


@require_GET
@never_cache
def health_live(request):
    return JsonResponse({"status": "ok"})


@require_GET
@never_cache
def health_ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        Submission.objects.exists()
        with tempfile.TemporaryFile(dir=settings.MEDIA_ROOT):
            pass
    except Exception:
        logger.exception("health.readiness_failed")
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
