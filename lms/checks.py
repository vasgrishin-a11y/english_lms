"""Проверки запуска: предупредить о файлах, оставшихся в прежнем каталоге MEDIA_ROOT.

Раньше такая ситуация проявлялась только как «картинка в задании пропала»: имя
файла в базе есть, а по новому пути файла нет. Теперь приложение читает старый
каталог (lms/storage.py), но молчать об этом не стоит — бэкапы, volume и очистка
«сирот» смотрят только в актуальный MEDIA_ROOT.
"""

import os

from django.conf import settings
from django.core.checks import Warning, register

MEDIA_NAMESPACES = ("assignments", "submissions")


def _has_files(root):
    for namespace in MEDIA_NAMESPACES:
        directory = os.path.join(root, namespace)
        if not os.path.isdir(directory):
            continue
        for _, _, files in os.walk(directory):
            if files:
                return True
    return False


@register("lms")
def legacy_media_files(app_configs, **kwargs):
    """W001: в прежнем каталоге ещё лежат загруженные файлы."""
    stale = [
        root
        for root in getattr(settings, "LMS_MEDIA_FALLBACK_ROOTS", []) or []
        if os.path.isdir(root) and _has_files(root)
    ]
    if not stale:
        return []
    return [
        Warning(
            "Загруженные файлы остались в прежнем каталоге: " + ", ".join(stale),
            hint=(
                "Сейчас они открываются оттуда, но сведите всё в один каталог: "
                "manage.py migrate_media_folder (проверить — manage.py check_media_files). "
                f"Актуальный MEDIA_ROOT: {settings.MEDIA_ROOT}"
            ),
            id="lms.W001",
        )
    ]
