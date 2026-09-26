"""Приватное хранилище файлов с чтением из прежних каталогов MEDIA_ROOT.

В базе у файла хранится только относительное имя (``assignments/2026/01/x.png``),
а каталог берётся из ``MEDIA_ROOT``. Поэтому любой переезд каталога превращает
все ранее загруженные файлы в 404: имя в карточке задания видно, превью нет,
скачивание отдаёт «страница не найдена».

Ровно это и происходило после обновления кода, когда каталог по умолчанию
переехал с ``<repo>/media`` на ``<repo>/var/media``.

Запись всегда идёт в актуальный ``MEDIA_ROOT``. Чтение — сначала оттуда, затем
из каталогов ``settings.LMS_MEDIA_FALLBACK_ROOTS``, поэтому старые файлы
продолжают открываться без ручного переноса. Свести всё в один каталог можно
командой ``manage.py migrate_media_folder``.
"""

import os
from datetime import datetime, timezone

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from django.utils._os import safe_join
from django.utils.deconstruct import deconstructible
from django.utils.functional import cached_property

FALLBACK_SETTING = "LMS_MEDIA_FALLBACK_ROOTS"


@deconstructible(path="lms.storage.PrivateMediaStorage")
class PrivateMediaStorage(FileSystemStorage):
    """FileSystemStorage, который умеет читать файлы из прежних MEDIA_ROOT."""

    @cached_property
    def fallback_locations(self):
        roots = []
        for raw in getattr(settings, FALLBACK_SETTING, ()) or ():
            path = os.path.abspath(str(raw))
            if path == self.location or path in roots:
                continue
            roots.append(path)
        return tuple(roots)

    def _clear_cached_properties(self, setting, **kwargs):
        super()._clear_cached_properties(setting, **kwargs)
        if setting in {"MEDIA_ROOT", FALLBACK_SETTING}:
            self.__dict__.pop("fallback_locations", None)

    def legacy_path(self, name):
        """Путь к файлу в прежнем каталоге, если он там ещё лежит."""
        if not name:
            return None
        for root in self.fallback_locations:
            try:
                candidate = safe_join(root, name)
            except (SuspiciousFileOperation, ValueError):
                continue
            if os.path.isfile(candidate):
                return candidate
        return None

    def resolved_path(self, name):
        """Где файл лежит на самом деле: актуальный каталог или прежний."""
        primary = super().path(name)
        if os.path.exists(primary):
            return primary
        return self.legacy_path(name)

    # ── Чтение: сначала актуальный каталог, потом прежние ───────────────────
    #
    # ``path()`` намеренно не переопределяем: его использует ``_save``, и подмена
    # пути увела бы новые загрузки обратно в старый каталог.

    def _open(self, name, mode="rb"):
        if "w" not in mode and "a" not in mode and "+" not in mode:
            legacy = self._legacy_only(name)
            if legacy:
                return File(open(legacy, mode))
        return super()._open(name, mode)

    def exists(self, name):
        # Учитываем прежние каталоги, чтобы новая загрузка не «затенила» файл,
        # на который всё ещё ссылаются старые записи в базе.
        return super().exists(name) or bool(self.legacy_path(name))

    def size(self, name):
        legacy = self._legacy_only(name)
        return os.path.getsize(legacy) if legacy else super().size(name)

    def get_accessed_time(self, name):
        return self._stat_time(name, "get_accessed_time", os.path.getatime)

    def get_created_time(self, name):
        return self._stat_time(name, "get_created_time", os.path.getctime)

    def get_modified_time(self, name):
        return self._stat_time(name, "get_modified_time", os.path.getmtime)

    def delete(self, name):
        if super().exists(name):
            super().delete(name)
        legacy = self.legacy_path(name)
        if legacy:
            try:
                os.remove(legacy)
            except FileNotFoundError:
                pass

    # ── Внутреннее ──────────────────────────────────────────────────────────
    def _legacy_only(self, name):
        """Путь в прежнем каталоге, только если в актуальном файла нет."""
        if os.path.exists(super().path(name)):
            return None
        return self.legacy_path(name)

    def _stat_time(self, name, method, getter):
        legacy = self._legacy_only(name)
        if not legacy:
            return getattr(super(), method)(name)
        timestamp = getter(legacy)
        if settings.USE_TZ:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return datetime.fromtimestamp(timestamp)
