"""Bounded upload validation. Format checks are not a substitute for malware scanning."""

import codecs
import io
import struct
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import mutagen
from django.conf import settings
from django.core.exceptions import ValidationError

AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "ogg", "aac"}
#: Форматы, которые браузер не воспроизводит, но диктофоны и Windows Media
#: Player сохраняют по умолчанию. Сервер принимает их и перекодирует в MP3
#: (``lms.audio_formats``), поэтому искать конвертер не приходится.
#:
#: Список намеренно короткий: сюда попадает только то, что mutagen умеет
#: проверить как «настоящее аудио» (ASF и MP4), иначе валидатор отбраковывал бы
#: файлы, которые сам же и разрешил. AMR/MKA контейнеры mutagen не читает.
DOCUMENT_AUDIO_EXTENSIONS = {"wma", "wmv", "asf", "m4b"}
#: Всё аудио, которое принимает сайт — с уже приведённым к MP3 «внешним» звуком.
BROWSER_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS
UPLOAD_AUDIO_EXTENSIONS = BROWSER_AUDIO_EXTENSIONS | DOCUMENT_AUDIO_EXTENSIONS
#: Соответствие расширения аудио и классов mutagen. Единый источник правды для
#: проверки при загрузке (validate_upload) и при inline-превью (media_preview),
#: чтобы файл, принятый на входе, всегда можно было воспроизвести.
AUDIO_MUTAGEN_TYPES = {
    "mp3": {"MP3"},
    "wav": {"WAVE"},
    "m4a": {"MP4"},
    "aac": {"AAC"},
    "ogg": {"OggVorbis", "OggOpus", "OggSpeex", "OggFLAC"},
}
#: Контейнеры, которые mutagen разбирает, но браузер играть не умеет. Такой файл
#: валиден как аудио (проверяем заголовок контейнера, а не расширение), но перед
#: сохранением его обязательно перекодируют в MP3 (см. ``lms.audio_formats``).
DOCUMENT_AUDIO_MUTAGEN_TYPES = {
    "wma": {"ASF"},
    "wmv": {"ASF"},
    "asf": {"ASF"},
    "m4b": {"MP4"},
    # M4B — обычный MP4-контейнер, поэтому mutagen называет его MP4 и умеет
    # прочитать длительность; перекодируем его потому, что браузеры не всегда
    # открывают .m4b как аудио.
}
#: Типы mutagen для inline-превью: только то, что реально играет браузер.
INLINE_AUDIO_MUTAGEN_TYPES = dict(AUDIO_MUTAGEN_TYPES)
#: Начало любого ASF-файла (WMA/WMV). Расширение может и не быть ``.wma``:
#: диктофон мог сохранить ASF как ``.wmv`` или вовсе без понятного расширения.
ASF_HEADER = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
ALLOWED_FILE_EXTENSIONS = [
    "pdf",
    "doc",
    "docx",
    "txt",
    "rtf",
    "odt",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "mp3",
    "wav",
    "m4a",
    "ogg",
    "aac",
    "wma",
    "wmv",
    "asf",
    "m4b",
    "zip",
    "rar",
    "7z",
]


def _bounded_zip_directory(file):
    # Check the EOCD before ZipFile allocates an object per central-directory
    # entry. A small compressed upload must not create an unbounded metadata list.
    file.seek(max(0, file.size - 65557))
    tail = file.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 22 > len(tail):
        return False
    _, disk, directory_disk, disk_entries, entries, directory_size, _, comment_size = struct.unpack(
        "<4s4H2LH", tail[offset : offset + 22]
    )
    file.seek(0)
    return (
        disk == directory_disk == 0
        and disk_entries == entries
        and entries <= 4096
        and directory_size <= 512 * 1024
        and offset + 22 + comment_size == len(tail)
    )


def audio_accept_attribute():
    """Значение ``accept`` для полей аудио: браузерные форматы плюс WMA-семейство.

    Одна функция на весь проект: форма, тег шаблона и подсказка дропзоны не
    должны расходиться в том, что сервер реально принимает.
    """
    return ",".join(f".{ext}" for ext in sorted(UPLOAD_AUDIO_EXTENSIONS)) + ",audio/*"


def audio_extension(filename):
    """Расширение аудио без точки в нижнем регистре.

    В отличие от ``Path(...).suffix`` понимает и голое расширение (``".mp3"``):
    им пользуются внутренние вызовы, где имя файла уже не нужно.
    """
    name = str(filename or "").lower().rsplit("/", 1)[-1]
    if name.startswith(".") and "." not in name[1:]:
        return name[1:]
    dot = name.rfind(".")
    return name[dot + 1 :] if dot > -1 else ""


def audio_container_types(extension, header=b""):
    """Классы mutagen, которые считаются «настоящим аудио» для этого файла.

    ASF-контейнер (WMA, а также диктофонный ``.wmv``) узнаём по заголовку, а не
    по расширению: пользователь мог переименовать файл, а браузер его всё равно
    не проигрывает — такой файл перекодируется в MP3 на сервере.
    """
    if bytes(header[:16]) == ASF_HEADER:
        # Заголовок ASF однозначен: неважно, назвал файл .wma, .wmv или никак.
        return DOCUMENT_AUDIO_MUTAGEN_TYPES["wma"]
    return (
        AUDIO_MUTAGEN_TYPES.get(extension) or DOCUMENT_AUDIO_MUTAGEN_TYPES.get(extension) or set()
    )


def _audio_matches(audio, expected):
    """mutagen распознал контейнер как ожидаемый и у него есть длительность."""
    return (
        audio is not None
        and type(audio).__name__ in expected
        and getattr(audio.info, "length", 0) > 0
    )


def validate_audio_content(blob, filename):
    """Проверить, что байты — настоящее аудио ожидаемого контейнера.

    Общая проверка для ``lms.ai`` (файлы ИИ-помощника не проходят через
    ``validate_upload``) и для перекодирования в ``lms.audio_formats``: она
    работает с байтами в памяти, а не с ``UploadedFile``, поэтому её можно
    вызвать и до сохранения файла, и после.
    """
    extension = audio_extension(filename)
    if extension not in UPLOAD_AUDIO_EXTENSIONS:
        raise ValidationError("Это не аудиофайл.")
    expected = audio_container_types(extension, bytes(blob[:16]))
    if not expected:
        raise ValidationError("Это не аудиофайл.")
    try:
        audio = mutagen.File(fileobj=io.BytesIO(blob))
    except (OSError, ValueError, EOFError, mutagen.MutagenError) as exc:
        raise ValidationError(
            "Не удалось прочитать аудиофайл — загрузите неповреждённую запись."
        ) from exc
    if not _audio_matches(audio, expected):
        raise ValidationError("Содержимое файла не соответствует его формату или файл повреждён.")
    return audio


def validate_upload(file):
    if not file:
        return
    extension = Path(file.name).suffix.lower().lstrip(".")
    if extension not in ALLOWED_FILE_EXTENSIONS:
        raise ValidationError("Этот формат файла не разрешён.")
    try:
        if file.size > settings.LMS_MAX_FILE_BYTES:
            raise ValidationError(
                f"Максимальный размер файла: {settings.LMS_MAX_FILE_BYTES // (1024 * 1024)} MiB."
            )
        if file.size == 0:
            raise ValidationError("Пустой файл нельзя отправить.")
        file.open("rb")
        position = file.tell()
        try:
            file.seek(0)
            header = file.read(1024)
            file.seek(0)
            if extension in UPLOAD_AUDIO_EXTENSIONS or header[:16] == ASF_HEADER:
                # Сюда попадают и WMA/WMV/ASF/M4B: в ALLOWED они есть, и проверять
                # их содержимое нужно как аудио, а не по таблице сигнатур.
                expected = audio_container_types(extension, header)
                audio = mutagen.File(fileobj=file)
                valid = _audio_matches(audio, expected)
            elif extension == "txt":
                decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
                while chunk := file.read(64 * 1024):
                    if b"\x00" in chunk:
                        raise ValidationError(
                            "Текстовый файл должен быть в UTF-8 и не содержать бинарные данные."
                        )
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
                valid = True
            elif extension in {"zip", "docx", "xlsx", "pptx", "odt"}:
                if not _bounded_zip_directory(file):
                    raise ValidationError(
                        "Повреждённый архив или слишком большой каталог архива (ZIP64 не поддерживается)."
                    )
                with ZipFile(file) as archive:
                    names = set(archive.namelist())
                    markers = {
                        "docx": "word/document.xml",
                        "xlsx": "xl/workbook.xml",
                        "pptx": "ppt/presentation.xml",
                        "odt": "content.xml",
                    }
                    valid = bool(names) and (extension == "zip" or markers[extension] in names)
            elif extension in {"doc", "xls", "ppt"}:
                valid = header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            else:
                signatures = {
                    "pdf": header.startswith(b"%PDF-"),
                    "rtf": header.lstrip().startswith(b"{\\rtf"),
                    "jpg": header.startswith(b"\xff\xd8\xff"),
                    "jpeg": header.startswith(b"\xff\xd8\xff"),
                    "png": header.startswith(b"\x89PNG\r\n\x1a\n"),
                    "gif": header.startswith((b"GIF87a", b"GIF89a")),
                    "webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
                    "rar": header.startswith(b"Rar!\x1a\x07"),
                    "7z": header.startswith(b"7z\xbc\xaf\x27\x1c"),
                }
                valid = signatures.get(extension, False)
            if not valid:
                raise ValidationError(
                    "Содержимое файла не соответствует его формату или файл повреждён."
                )
        finally:
            file.seek(position)
    except (OSError, ValueError, EOFError, UnicodeError, BadZipFile, mutagen.MutagenError) as exc:
        raise ValidationError(
            "Не удалось прочитать файл. Загрузите неповреждённый файл правильного формата."
        ) from exc


def audio_content_matches(fileobj, extension):
    """Содержимое действительно является аудио заявленного расширения.

    Использует ту же проверку mutagen, что и ``validate_upload`` при приёме
    файла, поэтому любой корректно загруженный аудиофайл гарантированно
    проходит проверку при inline-превью (без хрупкого списка magic-байтов,
    который отбраковывал валидные MP3/контейнеры и отдавал ученику 404).
    Для превью годятся только форматы, которые играет браузер: WMA сюда не
    попадает и не превращается в плеер, который молча не работает.
    """
    extension = extension.lower().lstrip(".")
    expected = INLINE_AUDIO_MUTAGEN_TYPES.get(extension)
    if not expected:
        return False
    position = fileobj.tell()
    try:
        fileobj.seek(0)
        audio = mutagen.File(fileobj=fileobj)
    except (OSError, ValueError, EOFError, mutagen.MutagenError):
        return False
    finally:
        fileobj.seek(position)
    return _audio_matches(audio, expected)


#: Запас на округление кодеков и задержку остановки записи в браузере.
RECORDING_TOLERANCE_SECONDS = 2


def audio_duration(file):
    """Длительность аудиофайла в секундах или None, если её не удалось прочитать."""
    if not file:
        return None
    try:
        file.open("rb")
        position = file.tell()
        try:
            file.seek(0)
            audio = mutagen.File(fileobj=file)
        finally:
            file.seek(position)
    except (OSError, ValueError, EOFError, mutagen.MutagenError):
        return None
    if audio is None or not getattr(audio, "info", None):
        return None
    return float(audio.info.length or 0)


def validate_audio_upload(file):
    """Проверка аудио-загрузки с перекодированием внебраузерных форматов.

    WMA браузер не играет, поэтому файл сначала проверяется как обычно, а затем
    заменяется результатом серверной конвертации в MP3. Возвращает исходный
    ``file`` без изменений, если перекодирование не нужно, и новый
    ``SimpleUploadedFile`` (.mp3), если нужно.

    Живёт здесь, а не в ``lms.audio_formats``, чтобы не было цикла импортов:
    форматная часть не знает о Django, а валидаторы — знают.
    """
    if not file:
        return file
    from .audio_formats import AudioConvertError, convert_to_mp3, mp3_name

    extension = Path(file.name).suffix.lower().lstrip(".")
    if extension not in DOCUMENT_AUDIO_EXTENSIONS:
        return file
    validate_upload(file)
    position = file.tell() if hasattr(file, "tell") else 0
    try:
        blob = file.read()
    finally:
        if hasattr(file, "seek"):
            file.seek(position)
    if not blob:
        raise ValidationError("Пустой файл нельзя отправить.")
    try:
        blob = convert_to_mp3(blob, filename=file.name)
    except AudioConvertError as exc:
        raise ValidationError(str(exc)) from exc
    from django.core.files.uploadedfile import SimpleUploadedFile

    converted = SimpleUploadedFile(mp3_name(file.name), blob, content_type="audio/mpeg")
    # Длительность нужна лимиту записи: считаем её у уже чистого MP3, чтобы
    # не зависеть от поддержки контейнера в mutagen.
    converted.audio_duration = audio_duration(converted)
    return converted


def validate_recording_limit(file, limit_seconds):
    """Аудио длиннее лимита преподавателя отклоняется на сервере, а не только в браузере."""
    if not file or not limit_seconds:
        return
    if Path(file.name).suffix.lower().lstrip(".") not in AUDIO_EXTENSIONS:
        return
    duration = audio_duration(file)
    if duration is not None and duration > limit_seconds + RECORDING_TOLERANCE_SECONDS:
        from .models import format_duration

        raise ValidationError(
            f"Запись длиннее лимита: {format_duration(round(duration))} при лимите "
            f"{format_duration(limit_seconds)}. Сократите ответ и запишите заново."
        )


def validate_answer(assignment_type, text, file):
    errors = {}
    if assignment_type in {"text", "mixed"} and not text.strip():
        errors["text_answer"] = "Введите текстовый ответ."
    if assignment_type in {"file", "audio", "mixed"} and not file:
        errors["file_answer"] = "Загрузите файл. Нельзя удалить обязательный файл без замены."
    if assignment_type == "text" and file:
        errors["file_answer"] = "Для этого задания нужен только текстовый ответ."
    if (
        assignment_type == "audio"
        and file
        and Path(file.name).suffix.lower().lstrip(".") not in UPLOAD_AUDIO_EXTENSIONS
    ):
        # WMA и родственные контейнеры тоже принимаем: перед сохранением их
        # перекодирует ``validate_audio_upload``.
        errors["file_answer"] = "Загрузите аудио: MP3, WAV, M4A, OGG, AAC или WMA."
    if errors:
        raise ValidationError(errors)
