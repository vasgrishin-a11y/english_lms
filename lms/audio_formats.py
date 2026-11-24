"""Внебраузерные аудиоформаты: приём WMA/WMV/ASF/M4B и конвертация в MP3.

Браузеры не воспроизводят WMA/ASF, а модели не читают его содержимое. Раньше
такие файлы просто отсутствовали в списках допустимых форматов — и преподаватель
на странице ИИ-помощника видел «Формат .wma здесь не принимается», хотя это
обычная диктофонная запись.

Теперь путь один для любого такого файла: принять → проверить контейнер через
mutagen → перекодировать в MP3 (FFmpeg внутри колеса PyAV) → дальше система
работает с MP3, как с любым другим аудио. Перекодирование идёт на сервере, без
внешнего процесса и без shell: байты читаются из загруженного файла в память и
никогда не пишутся во временные файлы с именем от пользователя.

PyAV импортируется внутри функции: если зависимость почему-то не установлена,
сайт продолжает работать, а такой файл отклоняется с понятным сообщением.
"""

from __future__ import annotations

import io
import logging

from django.core.exceptions import ValidationError

from .validators import (
    ASF_HEADER,
    AUDIO_EXTENSIONS,
    BROWSER_AUDIO_EXTENSIONS,
    DOCUMENT_AUDIO_EXTENSIONS,
    audio_extension,
    validate_audio_content,
)

logger = logging.getLogger("lms.audio")

#: Целевой формат: MP3 моно 22,05 кГц, 64 кбит/с.
#:
#: Речь (а не музыка) на таком битрейте разборчива, браузер играет MP3 везде,
#: а размер — примерно в 40 раз меньше исходного WAV (≈ 480 КБ/мин). Моно
#: важен для диктофонов: стерео запись с одним микрофоном всё равно моно.
TARGET_EXTENSION = ".mp3"
TARGET_SAMPLE_RATE = 22050
TARGET_BIT_RATE = 64000
#: Запас на контейнер: даже многочасовая запись не должна расти бесконечно.
MAX_TARGET_BYTES = 30 * 1024 * 1024
#: Сколько секунд записи вообще принимаем после конвертации: защита от
#: «аудио-бомбы» (тянется сутками, а файл маленький).
MAX_DURATION_SECONDS = 2 * 60 * 60


class AudioConvertError(Exception):
    """Понятная пользователю причина, почему файл не удалось привести к MP3."""


#: Загружаемые расширения аудио: браузерные + внебраузерные.
AUDIO_UPLOAD_EXTENSIONS = AUDIO_EXTENSIONS | DOCUMENT_AUDIO_EXTENSIONS


def needs_conversion(filename, blob=None):
    """Нужно ли перекодирование в MP3.

    Перекодируем только аудио, которое браузер не играет (WMA, WMV, ASF, M4B), и
    ASF-контейнер под «непонятным» именем — например, диктофон сохранил файл без
    расширения. Документы, картинки и видео не трогаем ни при каких условиях.
    """
    extension = audio_extension(filename)
    if extension in DOCUMENT_AUDIO_EXTENSIONS:
        return True
    if blob is not None and bytes(blob[:16]) == ASF_HEADER:
        return extension not in BROWSER_AUDIO_EXTENSIONS
    return False


def convert_to_mp3(blob, *, filename="audio", validate=True):
    """Перекодировать аудио в MP3 для браузера и модели.

    ``validate=True`` проверяет содержимое входа тем же способом, что и
    остальные аудио-загрузки (ASF/WMA через mutagen): расширение — заявление
    пользователя, а контейнер — факт. После кодирования результат ещё раз
    проверяется, чтобы в хранилище и к модели не попал файл, который
    «настоящее аудио» не распознало.

    Ошибки низкоуровневого декодера не выходят наружу: ``AudioConvertError``
    объясняет пользователю, что делать, а подробности попадают в журнал.
    """
    if validate:
        try:
            validate_audio_content(blob, filename)
        except ValidationError as exc:
            # Понятная пользователю причина вместо 500: файл мог быть
            # переименован из текста или обрезан при скачивании.
            raise AudioConvertError(exc.messages[0]) from exc

    try:
        import av  # необязательная зависимость: сайт работает и без неё
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise AudioConvertError(
            "Запись в этом формате требует перекодирования, а модуль конвертации на "
            "сервере не установлен. Попросите администратора установить зависимость "
            "«av» (pip install av) или сохраните запись как MP3/WAV/M4A."
        ) from exc

    target = io.BytesIO()
    source = None
    try:
        # Контейнер определяет сама библиотека: так один и тот же код принимает
        # и WMA/ASF, и M4B. Принудительный "asf" ломал бы M4B.
        source = av.open(io.BytesIO(blob))
        if not source.streams.audio:
            raise AudioConvertError(
                "В файле нет звуковой дорожки — похоже, это видео без звука. "
                "Экспортируйте запись как MP3 или WAV."
            )
        stream = source.streams.audio[0]
        sink = av.open(target, "w", format="mp3")
        try:
            output = sink.add_stream("libmp3lame", rate=TARGET_SAMPLE_RATE)
            output.layout = "mono"
            output.bit_rate = TARGET_BIT_RATE
            resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_SAMPLE_RATE)
            seconds = 0.0
            for frame in source.decode(stream):
                seconds += frame.samples / float(frame.sample_rate or TARGET_SAMPLE_RATE)
                if seconds > MAX_DURATION_SECONDS:
                    raise AudioConvertError(
                        "Запись длиннее двух часов — разделите её на части или сохраните как MP3."
                    )
                for ready in resampler.resample(frame):
                    for packet in output.encode(ready):
                        sink.mux(packet)
                        if target.tell() > MAX_TARGET_BYTES:
                            raise AudioConvertError(
                                "Запись слишком большая даже после сжатия — "
                                "сократите материал или загрузите текст отдельно."
                            )
            for ready in resampler.resample(None):
                for packet in output.encode(ready):
                    sink.mux(packet)
            for packet in output.encode(None):
                sink.mux(packet)
        finally:
            sink.close()
    except AudioConvertError:
        raise
    except Exception as exc:  # битый контейнер, отключённый кодек, нехватка памяти
        logger.warning("audio_convert_failed name=%s", str(filename)[:120], exc_info=True)
        raise AudioConvertError(
            "Не удалось прочитать запись — файл повреждён или кодек в нём "
            "не поддерживается. Сохраните его как MP3 или WAV и попробуйте снова."
        ) from exc
    finally:
        if source is not None:
            source.close()

    data = target.getvalue()
    if not data:
        raise AudioConvertError("После перекодирования запись пустая — проверьте исходный файл.")
    try:
        if validate:
            validate_audio_content(data, TARGET_EXTENSION)
    except Exception as exc:  # результат обязан быть воспроизводимым
        logger.warning("audio_convert_output_invalid name=%s", str(filename)[:120])
        raise AudioConvertError(
            "Перекодированную запись не удалось проверить — попробуйте MP3 или WAV."
        ) from exc
    return data


def mp3_name(filename):
    """Имя для результата: исходное имя с расширением ``.mp3``."""
    base = str(filename or "audio").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return f"{(base[:dot] if dot > 0 else base) or 'audio'}{TARGET_EXTENSION}"


def convert_upload(upload):
    """Перекодировать загруженный файл, вернуть ``(имя .mp3, байты)``.

    Файл читается один раз и закрывается: вызывающий код дальше работает с
    обычными байтами, как будто пользователь и загрузил MP3.
    """
    name = getattr(upload, "name", "") or "audio"
    try:
        blob = upload.read()
    finally:
        close = getattr(upload, "close", None)
        if callable(close):
            try:
                close()
            except (OSError, ValueError):  # pragma: no cover - защитное закрытие
                pass
    return mp3_name(name), convert_to_mp3(blob, filename=name)


__all__ = [
    "AUDIO_UPLOAD_EXTENSIONS",
    "TARGET_EXTENSION",
    "AudioConvertError",
    "convert_to_mp3",
    "convert_upload",
    "mp3_name",
    "needs_conversion",
]
