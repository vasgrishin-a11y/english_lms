"""Bounded upload validation. Format checks are not a substitute for malware scanning."""

import codecs
import struct
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import mutagen
from django.conf import settings
from django.core.exceptions import ValidationError

AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "ogg", "aac"}
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
            if extension in AUDIO_EXTENSIONS:
                audio = mutagen.File(fileobj=file)
                allowed_types = {
                    "mp3": {"MP3"},
                    "wav": {"WAVE"},
                    "m4a": {"MP4"},
                    "aac": {"AAC"},
                    "ogg": {"OggVorbis", "OggOpus", "OggSpeex", "OggFLAC"},
                }
                valid = (
                    audio is not None
                    and type(audio).__name__ in allowed_types[extension]
                    and audio.info.length > 0
                )
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
        and Path(file.name).suffix.lower().lstrip(".") not in AUDIO_EXTENSIONS
    ):
        errors["file_answer"] = "Загрузите аудио: MP3, WAV, M4A, OGG или AAC."
    if errors:
        raise ValidationError(errors)
