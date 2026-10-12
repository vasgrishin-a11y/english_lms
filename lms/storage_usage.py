"""Сколько места занимают файлы LMS: индикатор заполнения хранилища.

Преподаватель видит в «Настройках консоли», чем заполнено хранилище: материалы
заданий, вложения, ответы учеников и голосовые комментарии. Сумма сравнивается с
лимитом ``LMS_STORAGE_QUOTA_BYTES`` (0 — лимит не задан), а рядом показано
свободное место на диске с ``MEDIA_ROOT``: именно оно заканчивается первым, когда
лимит не задан. Лимит информационный и не блокирует загрузки.

Размер берётся через ``default_storage``, а не по пути ``MEDIA_ROOT`` напрямую:
часть файлов может открываться из прежних каталогов (см. ``lms/storage.py``) и
тоже занимает место. Одно и то же имя считается один раз — повторная попытка
переиспользует файл предыдущей и не должна удваивать счётчик.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage

from .models import (
    Assignment,
    AssignmentAttachment,
    Feedback,
    FeedbackAudioComment,
    QuestionResponse,
    Submission,
)

#: Подсчёт делает по одному обращению к хранилищу на файл, поэтому результат
#: недолго живёт в кэше. Кнопка «Обновить» в настройках считает заново.
CACHE_KEY = "lms:storage-usage:v1"
CACHE_SECONDS = 60

#: Порог предупреждения и тревоги для лимита хранилища, в процентах.
WARNING_PERCENT = 70
DANGER_PERCENT = 90
#: Диск занят не только файлами LMS, поэтому тревога — только у самого края.
DISK_WARNING_PERCENT = 80
DISK_DANGER_PERCENT = 95

#: Что и где хранится. Порядок совпадает с отчётом ``check_media_files``.
SOURCES = (
    ("Материалы заданий", Assignment, "material_file"),
    ("Вложения заданий", AssignmentAttachment, "file"),
    ("Ответы учеников", Submission, "file_answer"),
    ("Голосовые комментарии", Feedback, "audio_comment"),
    ("Голосовые комментарии (доп.)", FeedbackAudioComment, "audio"),
    ("Ответы по пунктам", QuestionResponse, "file_answer"),
)


def locate(name):
    """Где лежит файл: ``primary``, ``legacy`` (прежний каталог) или ``missing``."""
    resolver = getattr(default_storage, "resolved_path", None)
    if resolver is None:
        return "primary" if default_storage.exists(name) else "missing"
    primary = os.path.join(str(settings.MEDIA_ROOT), name)
    if os.path.isfile(primary):
        return "primary"
    return "legacy" if resolver(name) else "missing"


@dataclass(frozen=True)
class SourceUsage:
    """Раздел хранилища: материалы, вложения, ответы или комментарии."""

    label: str
    files: int
    size: int
    legacy: int
    missing: int


@dataclass(frozen=True)
class Gauge:
    """Заполнение для индикатора: проценты, ширина полосы и её цвет."""

    percent: int
    bar_class: str = "progress-success"

    @property
    def bar_percent(self):
        return min(self.percent, 100)

    @classmethod
    def of(cls, used, total, *, warning=WARNING_PERCENT, danger=DANGER_PERCENT):
        if total <= 0:
            return cls(0)
        percent = max(round(used * 100 / total), 0)
        if percent >= danger:
            return cls(percent, "progress-danger")
        if percent >= warning:
            return cls(percent, "progress-warning")
        return cls(percent)


@dataclass(frozen=True)
class StorageUsage:
    """Снимок хранилища: занятое место, лимит и место на диске."""

    sources: tuple
    files: int
    size: int
    legacy: int
    missing: int
    quota: int
    disk_total: int
    disk_free: int

    @property
    def has_quota(self):
        return self.quota > 0

    @property
    def quota_gauge(self):
        """Заполнение лимита, если лимит задан."""
        return Gauge.of(self.size, self.quota) if self.has_quota else None

    @property
    def quota_free(self):
        return max(self.quota - self.size, 0)

    @property
    def over_quota(self):
        return self.has_quota and self.size > self.quota

    @property
    def disk_used(self):
        return max(self.disk_total - self.disk_free, 0)

    @property
    def disk_gauge(self):
        """Заполнение диска с MEDIA_ROOT: 0, если каталог недоступен."""
        if not self.disk_total:
            return None
        return Gauge.of(
            self.disk_used,
            self.disk_total,
            warning=DISK_WARNING_PERCENT,
            danger=DISK_DANGER_PERCENT,
        )


def collect(*, force=False):
    """Снимок хранилища для страницы настроек.

    ``force=True`` (ссылка «Обновить») считает заново, не дожидаясь истечения
    кэша: после большой загрузки преподаватель хочет видеть новое число сразу.
    """
    key = _cache_key()
    if not force:
        cached = cache.get(key)
        if isinstance(cached, StorageUsage):
            return cached
    usage = _measure()
    cache.set(key, usage, CACHE_SECONDS)
    return usage


def _cache_key():
    # Ключ зависит от каталога файлов и лимита: тесты и переезд MEDIA_ROOT не
    # должны видеть чужой снимок.
    return f"{CACHE_KEY}:{settings.MEDIA_ROOT}:{_quota_bytes()}"


def _quota_bytes():
    return max(int(getattr(settings, "LMS_STORAGE_QUOTA_BYTES", 0) or 0), 0)


def _names(model, field):
    """Уникальные имена файлов раздела."""
    return {
        name for name in model.objects.exclude(**{field: ""}).values_list(field, flat=True) if name
    }


def _size_of(name):
    """Размер файла или ``None``, если файла на диске нет."""
    try:
        return default_storage.size(name)
    except OSError:
        return None


def _disk_usage():
    """Полный объём и свободное место файловой системы с MEDIA_ROOT."""
    try:
        usage = shutil.disk_usage(str(settings.MEDIA_ROOT))
    except OSError:
        return 0, 0
    return usage.total, usage.free


def _measure():
    sizes = {}
    locations = {}
    sources = []
    for label, model, field in SOURCES:
        names = _names(model, field)
        size = legacy = missing = 0
        for name in names:
            if name not in locations:
                locations[name] = locate(name)
                sizes[name] = _size_of(name)
            if sizes[name] is None:
                missing += 1
                continue
            size += sizes[name]
            if locations[name] == "legacy":
                legacy += 1
        sources.append(
            SourceUsage(label=label, files=len(names), size=size, legacy=legacy, missing=missing)
        )
    disk_total, disk_free = _disk_usage()
    return StorageUsage(
        sources=tuple(sources),
        files=len(locations),
        size=sum(size for size in sizes.values() if size),
        legacy=sum(1 for location in locations.values() if location == "legacy"),
        missing=sum(1 for location in locations.values() if location == "missing"),
        quota=_quota_bytes(),
        disk_total=disk_total,
        disk_free=disk_free,
    )
