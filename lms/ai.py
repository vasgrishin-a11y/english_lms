"""ИИ-помощник преподавателя: файл или текст → структура курса черновиками.

Помощник создаёт универсальные задания для заранее неизвестного ученика: в запрос
не передаются имя, профиль, история попыток или ответы ученика. Конкретную группу
или ученика преподаватель назначает после проверки черновика.

Помощник работает на **локальной модели** — ничего не уходит в облако:

* ``ollama`` (по умолчанию) — Ollama на http://localhost:11434;
* ``lmstudio`` — LM Studio на http://localhost:1234;
* ``openai`` — свой OpenAI-совместимый шлюз через ``LMS_AI_ENDPOINT``.

Фото страницы учебника читает vision-модель (qwen3-vl, qwen2.5vl, gemma3,
minicpm-v, llava) — картинка уходит в запрос data-url. Текст из DOCX, XLSX,
PDF и TXT извлекается офлайн и уходит промптом, поэтому модель нужна только
для «понять материал и собрать структуру».

Режимы:

* **online** — модель включена (``LMS_AI_LOCAL=1`` для локальной или
  непустой ``LMS_AI_API_KEY`` для своего шлюза);
* **offline** — модель не включена: работаем без сети, извлекаем текст из
  файла и раскладываем его по структуре эвристиками;
* **off** — помощник выключен переменной окружения ``LMS_AI_ENABLED=0``.

Гарантии: ИИ ничего не публикует сам — импорт всегда создаёт **черновики**;
ответ модели валидируется и обрезается по лимитам; содержимое файлов не
логируется (в журнал попадают только счётчики).

Внешних зависимостей нет: запрос к модели идёт через ``urllib``, архивы
Office разбираются стандартным ``zipfile``. Это важно для сборки Amvera:
``requirements.txt`` с хешами не меняется.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from io import BytesIO

from django.conf import settings
from django.db import transaction
from django.utils.text import slugify

from .library import _create_questions
from .models import Assignment, Block, CefrLevel, Flashcard, Question, Skill, Topic
from .skills import apply_default_skills, ensure_skill_catalog

logger = logging.getLogger("lms.ai")

# ── Лимиты и словари ───────────────────────────────────────────────────────
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".rtf"}
DOCX_EXTENSIONS = {".docx"}
XLSX_EXTENSIONS = {".xlsx", ".xlsm"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac"}
OFFICE_EXTENSIONS = DOCX_EXTENSIONS | XLSX_EXTENSIONS
UPLOAD_EXTENSIONS = (
    TEXT_EXTENSIONS
    | OFFICE_EXTENSIONS
    | PDF_EXTENSIONS
    | IMAGE_EXTENSIONS
    | VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
)

AI_ASSIGNMENT_TYPES = {value for value, _ in Assignment.Type.choices}
AI_QUESTION_KINDS = {value for value, _ in Question.Kind.choices}
AI_CEFR_LEVELS = {value for value, _ in CefrLevel.choices}

TARGETS = (
    ("mixed", "Класс → главы → темы → задания"),
    ("assignment", "Задание"),
)

TYPE_MARKERS = {
    "текст": Assignment.Type.TEXT,
    "text": Assignment.Type.TEXT,
    "письмо": Assignment.Type.TEXT,
    "файл": Assignment.Type.FILE,
    "file": Assignment.Type.FILE,
    "аудио": Assignment.Type.AUDIO,
    "audio": Assignment.Type.AUDIO,
    "mixed": Assignment.Type.MIXED,
    "смешанное": Assignment.Type.MIXED,
    "тест": Assignment.Type.QUIZ,
    "quiz": Assignment.Type.QUIZ,
    "карточки": Assignment.Type.FLASHCARDS,
    "cards": Assignment.Type.FLASHCARDS,
    "flashcards": Assignment.Type.FLASHCARDS,
}
KIND_MARKERS = {
    "mcq": Question.Kind.MCQ,
    "выбор": Question.Kind.MCQ,
    "multi": Question.Kind.MULTI,
    "несколько": Question.Kind.MULTI,
    "gap": Question.Kind.GAP,
    "пропуск": Question.Kind.GAP,
    "match": Question.Kind.MATCH,
    "соответствие": Question.Kind.MATCH,
    "order": Question.Kind.ORDER,
    "порядок": Question.Kind.ORDER,
    "sort": Question.Kind.SORT,
    "сортировка": Question.Kind.SORT,
    "spell": Question.Kind.SPELL,
    "буквы": Question.Kind.SPELL,
}
CHAPTER_MARKERS = {
    "глава": "chapter",
    "chapter": "chapter",
    "раздел": "chapter",
}
TOPIC_MARKERS = {
    "тема": "topic",
    "topic": "topic",
    "юнит": "topic",
    "unit": "topic",
}


class AiError(Exception):
    """Понятная преподавателю ошибка помощника."""


# ── Провайдеры ─────────────────────────────────────────────────────────────
# Локальные модели: ни ключа, ни облака, материалы не покидают сервер.
#   ollama    — Ollama (http://localhost:11434), по умолчанию;
#   lmstudio  — LM Studio (http://localhost:1234);
#   openai    — свой OpenAI-совместимый шлюз через LMS_AI_ENDPOINT.
# Адаптер у всех один: POST {endpoint}/chat/completions. Vision умеют модели
# qwen3-vl, qwen2.5vl, gemma3, minicpm-v, llava — фото уходят как data-url.
def _openai_provider(
    label, endpoint, *, model="", uploads=("image",), keyless=False, json_mode=False, hint=""
):
    return {
        "kind": "openai",
        "label": label,
        "model": model,
        "endpoint": endpoint,
        "uploads": tuple(uploads),
        "keyless": keyless,
        "json_mode": json_mode,
        "hint": hint,
    }


PROVIDERS = {
    "ollama": _openai_provider(
        "Ollama (локальная модель)",
        "http://localhost:11434/v1",
        model="qwen3-vl:8b",
        keyless=True,
        hint=(
            "Модель работает на вашем сервере: ollama pull qwen3-vl:8b. "
            "Для фото нужна vision-модель (qwen3-vl, qwen2.5vl, gemma3, minicpm-v); "
            "для слабых машин — gemma3:4b или moondream."
        ),
    ),
    "lmstudio": _openai_provider(
        "LM Studio (локальная модель)",
        "http://localhost:1234/v1",
        keyless=True,
        hint=(
            "В LM Studio запустите сервер (Developer → Start Server) и загрузите "
            "vision-модель (Qwen3-VL, Gemma, MiniCPM-V). Идентификатор модели "
            "скопируйте в LMS_AI_MODEL."
        ),
    ),
    "openai": _openai_provider(
        "Свой OpenAI-совместимый шлюз",
        "",
        keyless=False,
        json_mode=True,
        hint="Задайте LMS_AI_ENDPOINT — подойдёт любой шлюз с /chat/completions.",
    ),
}

DEFAULT_PROVIDER = "ollama"
UPLOAD_KINDS = ("image",)
UPLOAD_KIND_LABELS = {
    "image": "фото и картинки",
    "pdf": "PDF",
    "video": "видео",
    "audio": "аудио",
    "other": "файлы такого формата",
}
# Модели прошлых версий: если в .env осталось значение зарубежного провайдера,
# берём модель выбранного, иначе Ollama попытается скачать чужое имя.
KNOWN_MODELS = {"gemini-2.0-flash", "GigaChat-2", "gpt://<folder_id>/yandexgpt-5.1"}


# ── Настройки и режим ──────────────────────────────────────────────────────
def _setting(name, default):
    return getattr(settings, name, default)


def ai_enabled():
    return bool(_setting("LMS_AI_ENABLED", True))


def ai_api_key():
    return str(_setting("LMS_AI_API_KEY", "") or "").strip()


def ai_provider():
    name = str(_setting("LMS_AI_PROVIDER", DEFAULT_PROVIDER) or "").strip().lower()
    return name or DEFAULT_PROVIDER


def provider_spec(name=None):
    """Описание провайдера: адаптер, подпись, модель и адрес по умолчанию.

    Незнакомое имя не ошибка: такой провайдер считается OpenAI-совместимым,
    поэтому новый шлюз можно подключить одной переменной окружения.
    """
    key = (name or ai_provider()).strip().lower() or DEFAULT_PROVIDER
    spec = dict(PROVIDERS.get(key) or _openai_provider(f"{key} (OpenAI-совместимый)", ""))
    spec["key"] = key
    spec["uploads"] = tuple(spec.get("uploads") or ())
    return spec


def _resolve_default(configured, spec, field, known):
    """Значение из окружения с защитой от имён удалённых провайдеров.

    Подмена происходит, только если у выбранного провайдера есть своё значение
    этого поля: иначе (свой шлюз без значений по умолчанию) имя модели или адрес
    из окружения остались бы пустыми, и помощник уходил бы в офлайн-режим.
    """
    value = str(configured or "").strip()
    if not value:
        return spec[field]
    if spec[field] and value in known and value != spec[field]:
        logger.warning("ai_settings_mismatch provider=%s field=%s", spec["key"], field)
        return spec[field]
    return value


def ai_model(name=None):
    """Модель: значение из окружения, иначе — модель провайдера по умолчанию."""
    spec = provider_spec(name)
    configured = _resolve_default(
        _setting("LMS_AI_MODEL", ""),
        spec,
        "model",
        {item["model"] for item in PROVIDERS.values() if item.get("model")} | KNOWN_MODELS,
    )
    if not configured:
        return spec["model"]
    return configured


def ai_endpoint(name=None):
    """Базовый адрес API: переопределяется переменной ``LMS_AI_ENDPOINT``."""
    spec = provider_spec(name)
    configured = _resolve_default(
        _setting("LMS_AI_ENDPOINT", ""),
        spec,
        "endpoint",
        {item["endpoint"] for item in PROVIDERS.values() if item.get("endpoint")},
    )
    return configured.rstrip("/") or spec["endpoint"].rstrip("/")


def provider_uploads(spec=None):
    """Какие файлы провайдер читает сам. ``LMS_AI_UPLOAD_KINDS`` переопределяет."""
    spec = spec or provider_spec()
    configured = str(_setting("LMS_AI_UPLOAD_KINDS", "") or "").strip().lower()
    if not configured:
        return spec["uploads"]
    if configured in {"none", "-", "0"}:
        return ()
    kinds = tuple(
        kind for kind in (item.strip() for item in configured.split(",")) if kind in UPLOAD_KINDS
    )
    if not kinds:  # опечатка в списке не должна незаметно отключать вложения
        logger.warning("ai_upload_kinds_unknown")
        return spec["uploads"]
    return kinds


def provider_hint(spec=None):
    return (spec or provider_spec()).get("hint") or ""


def ai_local():
    """Локальная модель: ключ не нужен, включение — явным LMS_AI_LOCAL=1.

    Без флага помощник остаётся офлайн: иначе каждая страница ждала бы ответа
    от localhost, которого может и не быть.
    """
    spec = provider_spec()
    return bool(_setting("LMS_AI_LOCAL", False)) and bool(spec.get("keyless"))


def ai_mode():
    """Режим помощника: off, offline или online."""
    if not ai_enabled():
        return "off"
    if ai_api_key() or ai_local():
        return "online"
    return "offline"


def ai_mode_label(mode=None):
    mode = mode or ai_mode()
    if mode == "off":
        return "ИИ-помощник выключен администратором"
    if mode == "online":
        spec = provider_spec()
        suffix = " · локально, без ключа" if ai_local() and not ai_api_key() else ""
        return f"ИИ подключён: {spec['label']} · {ai_model(spec['key'])}{suffix}"
    if ai_enabled() and provider_spec().get("keyless"):
        return "Офлайн-разбор: локальная модель не включена (LMS_AI_LOCAL=1)"
    return "Офлайн-разбор: модель не подключена, работаем без сети"


def unsupported_upload_note(filename, spec=None):
    """Честно объясняем, чего выбранный провайдер не умеет с этим файлом."""
    kind = upload_kind(filename)
    if kind == "other":
        return "Такой формат не разбирается — вставьте текст вручную."
    if kind in {"text", "docx", "xlsx"}:
        return ""
    label = UPLOAD_KIND_LABELS.get(kind, "этот формат")
    spec = spec or provider_spec()
    return (
        f"{spec['label']} не читает {label}: текст из документа разберём офлайн, "
        f"а для фото нужна vision-модель (qwen3-vl, qwen2.5vl, gemma3, minicpm-v)."
    )


def max_upload_bytes():
    return int(_setting("LMS_AI_MAX_FILE_BYTES", 10 * 1024 * 1024))


def _limit(name, default):
    return int(_setting(name, default))


def limits():
    return {
        "blocks": _limit("LMS_AI_MAX_BLOCKS", 5),
        "chapters": _limit("LMS_AI_MAX_CHAPTERS", 20),
        "topics": _limit("LMS_AI_MAX_TOPICS", 30),
        "assignments": _limit("LMS_AI_MAX_ASSIGNMENTS", 60),
        "questions": _limit("LMS_AI_MAX_QUESTIONS", 20),
        "cards": _limit("LMS_AI_MAX_CARDS", 300),
        "text_chars": _limit("LMS_AI_MAX_TEXT_CHARS", 12000),
    }


def extension_of(filename):
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot > -1 else ""


def upload_kind(filename):
    """Категория файла: text, docx, xlsx, pdf, image, video, audio или other."""
    ext = extension_of(filename)
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in DOCX_EXTENSIONS:
        return "docx"
    if ext in XLSX_EXTENSIONS:
        return "xlsx"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return "other"


def offline_notes(filename):
    """Чего офлайн-режим сделать не может — показываем честно."""
    kind = upload_kind(filename)
    if kind == "image":
        return "Офлайн-режим не распознаёт текст на картинках — включите локальную модель (LMS_AI_LOCAL=1)."
    if kind in {"video", "audio"}:
        return "Офлайн-режим не смотрит видео и не слушает аудио — включите локальную модель (LMS_AI_LOCAL=1)."
    if kind == "other":
        return "Такой формат офлайн не разбирается — вставьте текст вручную."
    return ""


# ── Извлечение текста (офлайн) ─────────────────────────────────────────────
def extract_text(filename, blob):
    """Текст из файла без внешних библиотек. Пустая строка — нечего разобрать."""
    kind = upload_kind(filename)
    if kind == "text":
        return _decode(blob)
    if kind == "docx":
        return _docx_text(blob)
    if kind == "xlsx":
        return _xlsx_text(blob)
    if kind == "pdf":
        return _pdf_text(blob)
    return ""


def _decode(blob, limit=None):
    limit = limit or limits()["text_chars"]
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return blob.decode(encoding)[:limit].strip()
        except (UnicodeDecodeError, AttributeError):
            continue
    return ""


def _zip_blob(blob):
    try:
        return zipfile.ZipFile(BytesIO(blob))
    except (zipfile.BadZipFile, OSError) as exc:
        raise AiError("Файл повреждён или это не документ Office.") from exc


def _xml_text(xml_bytes):
    text = xml_bytes.decode("utf-8", errors="ignore")
    text = re.sub(r"<w:p[ >]", "\n<w:p ", text)
    text = re.sub(r"<a:p[ >]", "\n<a:p ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _docx_text(blob):
    archive = _zip_blob(blob)
    try:
        raw = archive.read("word/document.xml")
    except KeyError as exc:
        raise AiError("В документе Word нет основного текста.") from exc
    return _xml_text(raw)[: limits()["text_chars"]]


def _xlsx_text(blob):
    archive = _zip_blob(blob)
    names = set(archive.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        raw = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="ignore")
        for item in re.findall(r"<si>(.*?)</si>", raw, flags=re.DOTALL):
            shared.append(re.sub(r"<[^>]+>", "", item).strip())
    rows = []
    for name in sorted(item for item in names if re.match(r"xl/worksheets/sheet\d+\.xml$", item)):
        sheet = archive.read(name).decode("utf-8", errors="ignore")
        for row in re.findall(r"<row[^>]*>(.*?)</row>", sheet, flags=re.DOTALL):
            cells = []
            for match in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row, flags=re.DOTALL):
                attributes, body = match.group(1) or "", match.group(2) or ""
                cell_type = (re.search(r't="([^"]+)"', attributes) or [None, ""])[1]
                value = re.search(r"<v>(.*?)</v>", body, flags=re.DOTALL)
                if value is None:
                    cells.append("")
                elif cell_type == "s":
                    index = int(value.group(1) or 0)
                    cells.append(shared[index] if 0 <= index < len(shared) else "")
                elif cell_type in {"inlineStr", "str"}:
                    cells.append(re.sub(r"<[^>]+>", "", body).strip())
                else:
                    cells.append(re.sub(r"<[^>]+>", "", value.group(1)).strip())
            line = " | ".join(cells).strip(" |")
            if line:
                rows.append(line)
    if not rows:
        raise AiError("В таблице не нашлось текста.")
    return "\n".join(rows)[: limits()["text_chars"]]


def _pdf_text(blob):
    """Грубое извлечение текста PDF: распаковываем потоки и собираем строки."""
    parts = []
    for chunk in re.findall(rb"stream\r?\n(.*?)endstream", blob, flags=re.DOTALL):
        try:
            data = zlib.decompress(chunk)
        except zlib.error:
            data = chunk
        if b"Tj" not in data and b"TJ" not in data:
            continue
        for line in re.findall(rb"\((?:\\.|[^\\()])*\)", data):
            parts.append(line[1:-1])
    raw = b" ".join(parts).replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
    text = re.sub(r"\s+", " ", raw.decode("latin-1", errors="ignore")).strip()
    if len(text) < 20:
        raise AiError(
            "Из PDF не удалось извлечь текст (скан или защита). Загрузите фото страницы "
            "или вставьте текст вручную."
        )
    return text[: limits()["text_chars"]]


# ── Офлайн-разбор в структуру курса ────────────────────────────────────────
@dataclass
class _OfflineBuilder:
    """Сборка структуры из текста: заголовки, вопросы и карточки по строкам.

    Иерархия курса: Класс → Глава → Тема → Задание.
    Для совместимости поддерживаются оба формата:
    - старый: # Блок, ## Тема, ### Задание
    - новый:  # Блок, ## Глава, ### Тема, #### Задание
    Определяется автоматически: если в тексте есть заголовки 4 уровня (####),
    то уровень 2 считается главой, иначе — темой (как раньше).
    Также работают маркеры [глава], [тема], [задание] в заголовке.
    """

    source: str = ""
    blocks: list = field(default_factory=list)
    _block: dict | None = None
    _chapter: dict | None = None
    _topic: dict | None = None
    _assignment: dict | None = None
    _question: dict | None = None
    _card_title: str = ""
    _card_notes: list = field(default_factory=list)

    def ensure_block(self, name=None):
        if self._block is None:
            self._block = {
                "name": (name or self.source or "Класс из материала")[:150],
                "cefr_level": "",
                "description": "",
                "chapters": [],
                "topics": [],  # legacy: темы без главы попадут в «Общее» при импорте
            }
            self.blocks.append(self._block)
        return self._block

    def ensure_chapter(self, title=None):
        block = self.ensure_block()
        if self._chapter is None:
            self._chapter = {
                "title": (title or "Общее")[:200],
                "description": "",
                "topics": [],
            }
            block["chapters"].append(self._chapter)
        return self._chapter

    def ensure_topic(self, title=None):
        # Тема внутри главы, если глава есть, иначе — напрямую в блоке (legacy)
        if self._chapter is not None:
            if self._topic is None:
                self._topic = {
                    "title": (title or "Материал")[:200],
                    "description": "",
                    "assignments": [],
                    "cards": [],
                }
                self._chapter["topics"].append(self._topic)
        else:
            block = self.ensure_block()
            if self._topic is None:
                self._topic = {
                    "title": (title or "Материал")[:200],
                    "description": "",
                    "assignments": [],
                    "cards": [],
                }
                block["topics"].append(self._topic)
        return self._topic

    def ensure_assignment(self, title=None, assignment_type=Assignment.Type.TEXT):
        topic = self.ensure_topic()
        if self._assignment is None:
            self._assignment = {
                "type": assignment_type,
                "title": (title or "Задание из материала")[:200],
                "description": "",
                "max_points": 10,
                "questions": [],
                "skills": [],
            }
            topic["assignments"].append(self._assignment)
        return self._assignment

    def close_question(self):
        self._question = None

    def add_cards(self, cards):
        if not cards:
            return
        topic = self.ensure_topic()
        title = self._card_title or f"Карточки: {topic['title']}"
        description = " ".join(self._card_notes) or "Набор для интервального повторения."
        topic["cards"].append(
            {"title": title[:200], "description": description[:4000], "cards": cards}
        )
        self._card_title = ""
        self._card_notes = []

    def add_question(self, text, kind=Question.Kind.MCQ):
        assignment = self.ensure_assignment()
        assignment["type"] = Assignment.Type.QUIZ
        question = {
            "kind": kind if kind in AI_QUESTION_KINDS else Question.Kind.MCQ,
            "text": text[:2000],
            "points": 1,
            "explanation": "",
            "choices": [],
        }
        assignment["questions"].append(question)
        self._question = question
        return question

    def add_choice(self, text, correct):
        if self._question is None or not text.strip():
            return
        self._question["choices"].append({"text": text[:500], "correct": bool(correct)})


CARD_SPLIT = re.compile(r"\s*(?:\||—|–|::|=>)\s*")
HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
MARKER = re.compile(r"\[([^\]]+)\]")
NUMBERED = re.compile(r"^\d+[.)]\s+(.+)$")


def _marker_value(text, mapping, default=None):
    for marker in MARKER.findall(text):
        key = marker.strip().lower()
        if key in mapping:
            return mapping[key]
    return default


def _strip_markers(text):
    return MARKER.sub("", text).strip(" :—-")


def parse_text(text, *, source=""):
    """Офлайн-разбор: заголовки → блок/глава/тема/задание, строки → вопросы и карточки.

    Поддерживает два формата:
    - старый: # Блок, ## Тема, ### Задание
    - новый:  # Блок, ## Глава, ### Тема, #### Задание
    Определяется автоматически: если в блоке есть заголовки уровня 4 (####),
    то в этом блоке уровень 2 = глава. Маркеры [глава], [тема], [chapter], [topic]
    переопределяют уровень в любом формате.
    """
    builder = _OfflineBuilder(source=source)
    pending_cards = []
    saw_heading = False

    # Предварительный проход по блокам: есть ли #### внутри каждого блока?
    lines = (text or "").splitlines()
    block_has_level4 = []  # список (block_start_index, has_level4)
    current_has = False
    current_block_start = 0
    found_block = False
    for idx, raw in enumerate(lines):
        m = HEADING.match(raw.strip())
        if not m:
            continue
        lvl = len(m.group(1))
        if lvl == 1:
            if found_block:
                block_has_level4.append((current_block_start, current_has))
            found_block = True
            current_block_start = idx
            current_has = False
        elif lvl >= 4:
            current_has = True
    if found_block:
        block_has_level4.append((current_block_start, current_has))
    # Если нет явных блоков (#), считаем весь текст одним блоком
    if not block_has_level4:
        has_any_l4 = any(
            HEADING.match((ln or "").strip())
            and len(HEADING.match((ln or "").strip()).group(1)) >= 4
            for ln in lines
        )
        block_has_level4 = [(0, has_any_l4)]

    def current_block_has_level4(line_idx):
        # найти последний блок, начавшийся до line_idx
        has_flag = False
        for start, has_l4 in block_has_level4:
            if start <= line_idx:
                has_flag = has_l4
            else:
                break
        return has_flag

    def flush_cards():
        nonlocal pending_cards
        if len(pending_cards) >= 2:
            builder.add_cards(pending_cards)
        pending_cards = []

    for line_idx, raw_line in enumerate((text or "").splitlines()):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        heading = HEADING.match(line.strip())
        if heading:
            saw_heading = True
            flush_cards()
            builder._assignment = None
            builder._card_title = ""
            builder.close_question()
            level = len(heading.group(1))
            raw_title = heading.group(2)
            title = _strip_markers(raw_title)

            # Маркеры [глава]/[тема]/[задание] имеют приоритет над уровнем
            chapter_marker = _marker_value(raw_title, CHAPTER_MARKERS, None)
            topic_marker = _marker_value(raw_title, TOPIC_MARKERS, None)
            assignment_marker = _marker_value(raw_title, TYPE_MARKERS, None)

            if level == 1:
                builder._chapter = None
                builder._topic = None
                builder._block = None
                builder.ensure_block(title)
            elif chapter_marker is not None:
                builder._topic = None
                builder._chapter = None
                builder.ensure_chapter(title)
            elif topic_marker is not None:
                builder._assignment = None
                builder._topic = None
                builder.ensure_topic(title)
            elif assignment_marker is not None:
                if assignment_marker == Assignment.Type.FLASHCARDS:
                    builder._card_title = title
                else:
                    builder._assignment = None
                    builder.ensure_assignment(title, assignment_marker)
            else:
                # Без маркеров — по уровню с учётом наличия #### в текущем блоке
                has_l4 = current_block_has_level4(line_idx)
                if has_l4:
                    if level == 2:
                        builder._topic = None
                        builder._chapter = None
                        builder.ensure_chapter(title)
                    elif level == 3:
                        builder._assignment = None
                        builder._topic = None
                        builder.ensure_topic(title)
                    else:  # 4+
                        atype = _marker_value(raw_title, TYPE_MARKERS, Assignment.Type.TEXT)
                        if atype == Assignment.Type.FLASHCARDS:
                            builder._card_title = title
                        else:
                            builder._assignment = None
                            builder.ensure_assignment(title, atype)
                else:
                    # Старый формат: 2=тема, 3+=задание
                    if level == 2:
                        builder._chapter = None
                        builder._topic = None
                        builder.ensure_topic(title)
                    else:
                        atype = _marker_value(raw_title, TYPE_MARKERS, Assignment.Type.TEXT)
                        if atype == Assignment.Type.FLASHCARDS:
                            builder._card_title = title
                        else:
                            builder._assignment = None
                            builder.ensure_assignment(title, atype)
            continue

        stripped = line.strip()
        if stripped.startswith("?"):
            flush_cards()
            kind = _marker_value(stripped, KIND_MARKERS, Question.Kind.MCQ)
            builder.add_question(_strip_markers(stripped.lstrip("?").strip()), kind)
            continue
        if stripped.startswith(("*", "+")) and builder._question is not None:
            builder.add_choice(_strip_markers(stripped[1:].strip()), correct=True)
            continue
        if stripped.startswith("-") and builder._question is not None:
            body = _strip_markers(stripped[1:].strip())
            parts = CARD_SPLIT.split(body)
            if (
                builder._question["kind"] in {Question.Kind.MATCH, Question.Kind.SORT}
                and len(parts) >= 2
            ):
                builder._question["choices"].append(
                    {"text": parts[0][:500], "match_text": parts[1][:500], "correct": True}
                )
            else:
                builder.add_choice(body, correct=False)
            continue

        numbered = NUMBERED.match(stripped)
        if stripped.startswith("-") or numbered:
            body = numbered.group(1) if numbered else stripped[1:].strip()
            card = _card_from_line(body)
            if card:
                pending_cards.append(card)
                continue
            assignment = builder._assignment
            if assignment is not None:
                assignment["description"] = f"{assignment['description']}\n• {body}".strip()
            elif builder._card_title:
                builder._card_notes.append(body)
            elif builder._chapter is not None and builder._topic is None:
                builder._chapter["description"] = (
                    f"{builder._chapter.get('description', '')}\n{body}".strip()
                )
            continue

        if "|" in stripped or "—" in stripped:
            card = _card_from_line(stripped)
            if card:
                pending_cards.append(card)
                continue
        flush_cards()
        if builder._card_title and builder._assignment is None:
            builder._card_notes.append(stripped)
        elif builder._assignment is not None:
            builder._assignment["description"] = (
                f"{builder._assignment['description']}\n{stripped}".strip()
            )
        elif builder._topic is not None:
            builder._topic["description"] = f"{builder._topic['description']}\n{stripped}".strip()
        elif builder._chapter is not None:
            builder._chapter["description"] = (
                f"{builder._chapter.get('description', '')}\n{stripped}".strip()
            )
        else:
            builder.ensure_topic()["description"] = stripped

    flush_cards()
    if not saw_heading:
        builder.blocks.clear()
        builder._block = builder._chapter = builder._topic = builder._assignment = None
        builder.ensure_block()
        builder.ensure_topic("Материал")
        body = (text or "").strip()
        cards = [_card_from_line(line) for line in body.splitlines()]
        cards = [card for card in cards if card]
        content_lines = [line for line in body.splitlines() if line.strip()]
        if len(cards) >= 3 and len(cards) * 3 >= len(content_lines):
            builder.add_cards(cards[: limits()["cards"]])
        else:
            assignment = builder.ensure_assignment(
                source or "Задание из материала", Assignment.Type.TEXT
            )
            assignment["description"] = body[:4000] or "Материал без текста."
        builder.ensure_topic()
    return {"title": source or "Материал ИИ-помощника", "blocks": builder.blocks}


def _card_from_line(line):
    parts = [part.strip(" «»\"'") for part in CARD_SPLIT.split(line)]
    parts = [part for part in parts if part]
    if len(parts) < 2 and " - " in line:
        head, _, tail = line.partition(" - ")
        if len(head.strip()) <= 40 and not head.strip().endswith((".", "!", "?")):
            parts = [head.strip(" «»\"'"), tail.strip(" «»\"'")]
    if len(parts) < 2:
        return None
    front, back = parts[0], parts[1]
    if not front or not back or len(front) > 200 or len(back) > 500:
        return None
    return {"front": front, "back": back, "example": parts[2] if len(parts) > 2 else ""}


# ── Онлайн-разбор через провайдера ─────────────────────────────────────────
AI_AUTHORING_CONTEXT = """Ты — профессиональный преподаватель английского языка и методист.
Ты помогаешь преподавателю English LMS создавать педагогически обоснованные задания и
учебные материалы. Используй современный коммуникативный подход, уровни CEFR A1–C2,
естественный английский язык и понятные инструкции для ученика.

Ученик заранее неизвестен. В запросе нет и не будет профиля конкретного ученика:
имени, возраста, родного языка, биографии, индивидуальных ошибок, истории попыток,
оценок или предыдущих ответов. Поэтому:
- не обращайся к ученику по имени и не придумывай персональные сведения;
- не пиши «как ты уже умеешь», «исправь свои прошлые ошибки» и другие предположения
  о конкретном человеке;
- создавай универсальное переиспользуемое задание для аудитории, обозначенной только
  явными параметрами запроса: уровнем, темой, навыком, возрастом/группой или целью;
- если такие параметры не указаны, не выдумывай профиль ученика: опирайся на материал,
  оставляй cefr_level пустым, а формулировки делай нейтральными;
- обращайся к ученику нейтрально («прочитайте», «выберите», «напишите») и не включай
  персональные данные в результат.

Проверяй доступность материалов для выполнения задания:
- Материал обязан быть доступен ученику в самом задании, если он должен прочитать текст,
  посмотреть картинку, прослушать аудио или изучить файл;
- используй полный материал из исходного запроса, встроив его в description под явным
  заголовком «Материал для выполнения», либо ссылайся только на файл, который действительно
  приложен к заданию и доступен ученику;
- не создавай формулировки «прочитайте текст и ответьте на вопросы», «посмотрите картинку»
  или «изучите приложенный файл», если после такой инструкции нет самого текста, картинки
  или файла;
- загруженный файл, переданный модели для разбора, является исходным материалом и не
  становится вложением задания автоматически. Если его нельзя передать ученику как
  вложение, включи нужный текст и контекст в description или создай другое, самодостаточное
  задание. Не оставляй скрытых ссылок на материал, которого ученик не увидит.

В этой LMS учитель сначала получает содержание как черновик и сам проверяет его,
а назначает его группе или отдельным ученикам позже. Не добавляй в текст задания
имена, идентификаторы пользователей, группы, сроки или персональные назначения —
они настраиваются в интерфейсе после импорта.

Учитывай модель данных проекта:
- строгая иерархия курса: Класс (Block) → Глава (Chapter) → Тема (Topic) → Задание
  (Assignment); темы без главы при импорте попадут в служебную главу «Общее»;
- у Класса есть название, описание и необязательный уровень CEFR;
- Задание бывает типов text, file, audio, mixed, quiz, flashcards или material;
  карточки — это разновидность задания flashcards, а не отдельная сущность курса;
- навык задания выбирается только из grammar, vocabulary, listening, speaking, writing,
  reading;
- у quiz допустимы пункты mcq, multi, gap, match, order, sort, spell, text и voice;
- Импорт создаёт только черновики: ничего не публикуй и не утверждай за преподавателя.
"""

PROMPT_SCHEMA = """Верни строго JSON без пояснений в формате:
{"title": "Название материала",
 "blocks": [{"name": "Класс", "cefr_level": "A1|A2|B1|B2|C1|C2 или пусто",
   "description": "1–2 предложения",
   "chapters": [{"title": "Глава", "description": "1–2 предложения",
     "topics": [{"title": "Тема", "description": "1–2 предложения",
       "assignments": [{"type": "text|file|audio|mixed|quiz|flashcards|material", "title": "Название",
          "description": "Условие для заранее неизвестного ученика: цель, шаги, объём и критерии",
          "max_points": 10, "skills": ["grammar|vocabulary|listening|speaking|writing|reading"],
          "questions": [{"kind": "mcq|multi|gap|match|order|sort|spell|text|voice", "text": "вопрос",
              "points": 1, "explanation": "",
              "choices": [{"text": "вариант", "correct": true, "match_text": ""}]}]}],
       "cards": [{"title": "Карточки: тема", "description": "",
          "cards": [{"front": "слово", "back": "перевод", "example": "пример"}]}]}]}],
   "topics": []
  }]}

Правила:
- Соблюдай иерархию Класс → Глава → Тема → Задание. Если в материале есть главы,
  разложи темы по chapters; если глав нет, оставь темы на уровне блока в topics —
  при импорте они попадут в «Общее».
- Создавай задания только из доступного материала и явных пожеланий преподавателя.
  Не выдумывай факты, имена, личный контекст ученика или историю его обучения.
- Пиши описание задания как готовую инструкцию для любого ученика указанного уровня:
  цель, что сделать, формат/объём ответа и критерии успеха. Не используй имя ученика.
- Проверяй самодостаточность задания: если для ответа нужно читать, смотреть, слушать или
  изучать материал, помести полный материал в description или явно используй реально доступное
  ученику вложение. Нельзя оставлять инструкцию «прочитайте текст и ответьте на вопросы»,
  если в задании нет этого текста или вложения. Исходный файл запроса не считается вложением
  задания автоматически.
- Для mcq и order укажи ровно один верный вариант; для multi — минимум два; для
  match и sort используй пары text ↔ match_text; для gap и spell приведи принимаемые
  ответы как верные варианты; для text и voice choices не нужны.
- Если выбран quiz, вопросы должны проверять только материал задания. Если в исходном
  материале нет упражнений, можно составить проверочные вопросы только по фактам и
  языку этого материала. Не создавай персональную адаптацию.
- questions и cards не добавляй без педагогической причины; для типов, которым они не
  нужны, возвращай пустые списки. Для flashcards создай минимум две карточки.
- Не добавляй поля, которых нет в схеме. Лимиты проекта: не более 5 классов, 10 глав
  на класс, 30 тем, 60 заданий, 20 вопросов и 300 карточек.
"""

TARGET_PROMPTS = {
    "mixed": "Собери из материала переиспользуемую структуру курса: Класс → Главы → Темы → Задания (тесты, карточки, материалы). Ученик заранее неизвестен, поэтому не персонализируй контент. Если главы явно выделены в материале — используй их, иначе оставь темы в главе «Общее».",
    "assignment": "Собери одно универсальное задание с понятным условием для заранее неизвестного ученика; если в материале есть упражнения, оформи их в подходящий тип задания.",
    "quiz": "Собери одно универсальное задание-тест с вопросами только по материалу. Не привязывай тест к конкретному ученику или его прошлым результатам.",
    "cards": "Собери универсальный набор карточек: слово → перевод и пример употребления. Не добавляй персональный контекст ученика.",
}


def build_prompt(text, *, prompt="", target="mixed", filename="", kind="text"):
    parts = [AI_AUTHORING_CONTEXT, TARGET_PROMPTS.get(target, TARGET_PROMPTS["mixed"])]
    if prompt.strip():
        parts.append(f"Пожелания преподавателя: {prompt.strip()}")
    if filename:
        parts.append(
            f"Исходный файл для разбора: {filename}. Тип файла: {kind}. "
            "Он доступен модели как источник, но не считается автоматически вложенным "
            "в итоговое задание для ученика."
        )
    if text.strip():
        parts.append(
            "Текст материала (если задание будет на него ссылаться, включи нужный полный "
            "текст в description, потому что исходный текст сам по себе не прикрепляется "
            "к итоговому заданию):\n" + text.strip()[: limits()["text_chars"]]
        )
    elif kind in {"image", "video", "audio", "pdf"}:
        parts.append(
            "Разбери приложенный файл. Если итоговое задание требует, чтобы ученик его "
            "прочитал, посмотрел или прослушал, обеспечь доступ к нему в самом задании "
            "или создай самодостаточный текстовый материал в description."
        )
    else:
        parts.append(
            "В запросе нет отдельного материала, на который можно ссылаться в условии задания."
        )
    parts.append(PROMPT_SCHEMA)
    return "\n\n".join(parts)


def build_material_revision_prompt(material, instruction):
    """Промпт правки текущего предпросмотра до импорта в курс."""
    current = json.dumps(material, ensure_ascii=False, indent=1)
    return "\n\n".join(
        [
            AI_AUTHORING_CONTEXT,
            "Ты редактируешь текущий предпросмотр материала ИИ до его импорта в LMS. "
            "Верни полную новую версию материала в той же структуре: не описывай отличия "
            "и не удаляй элементы, которые не затронуты пожеланием преподавателя.",
            f"Пожелания преподавателя к текущей версии: {instruction.strip()}",
            "Текущая версия материала (JSON):\n" + current,
            PROMPT_SCHEMA,
        ]
    )


def _image_part(blob, mime):
    """Картинка для vision-модели: base64 в data-url (Ollama и LM Studio так умеют)."""
    return base64.b64encode(blob).decode("ascii"), mime


def _image_mime(filename):
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(extension_of(filename), "image/jpeg")


def _ssl_context():
    """Контекст TLS для запросов к модели.

    Локальные Ollama и LM Studio работают по http и сертификата не требуют.
    Для своего шлюза с внутренним УЦ укажите ``LMS_AI_CA_BUNDLE``;
    ``LMS_AI_VERIFY_SSL=0`` отключает проверку целиком — крайняя мера.
    """
    if not bool(_setting("LMS_AI_VERIFY_SSL", True)):
        logger.warning("ai_tls_verification_disabled")
        return ssl._create_unverified_context()  # nosec B323 - осознанный выбор администратора
    bundle = str(_setting("LMS_AI_CA_BUNDLE", "") or "").strip()
    if not bundle:
        return None
    try:
        return ssl.create_default_context(cafile=bundle)
    except (OSError, ssl.SSLError) as exc:
        raise AiError(
            f"Не удалось прочитать сертификат из LMS_AI_CA_BUNDLE ({bundle}): {exc}"
        ) from exc


def _http_json(request, *, provider="", timeout=None):
    """Один HTTP-запрос через urllib: JSON-ответ или понятная ошибка."""
    timeout = timeout or int(_setting("LMS_AI_TIMEOUT", 60))
    context = _ssl_context()
    try:
        # Схема адреса проверена в ai_endpoint; endpoint задаёт администратор.
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # nosec B310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
        except Exception:  # pragma: no cover - защита от нечитаемого тела ответа
            detail = ""
        logger.warning("ai_http_error provider=%s status=%s", provider or "?", exc.code)
        if exc.code in {401, 403}:
            raise AiError("Модель отклонила ключ — проверьте LMS_AI_API_KEY.") from exc
        if exc.code == 404:
            raise AiError(
                f"Модель «{ai_model()}» не найдена. Проверьте имя модели "
                "(для Ollama: ollama list) — оно должно совпадать с LMS_AI_MODEL."
            ) from exc
        if exc.code in {400, 422}:
            raise AiError(
                "Модель отклонила запрос — обычно это значит, что она не понимает "
                "изображения или не поддерживает JSON-ответ (LMS_AI_JSON_MODE=0)."
                f" Ответ: {detail}"
            ) from exc
        raise AiError(f"Модель вернула ошибку {exc.code}. {detail}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(
            reason
        ):
            logger.warning("ai_tls_error provider=%s", provider or "?")
            raise AiError(
                "TLS-сертификат модели не удалось проверить: для своего шлюза задайте "
                "LMS_AI_CA_BUNDLE или отключите проверку через LMS_AI_VERIFY_SSL=0."
            ) from exc
        raise AiError(
            f"Нет связи с моделью по адресу {ai_endpoint() or 'не задан'}. "
            "Запущен ли Ollama или LM Studio и тот ли адрес в LMS_AI_ENDPOINT? "
            "Материал разобран офлайн."
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise AiError(
            "Модель не ответила за отведённое время — увеличьте LMS_AI_TIMEOUT "
            "или возьмите модель поменьше. Материал разобран офлайн."
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AiError("Модель вернула не JSON — попробуйте ещё раз.") from exc


def _json_from_text(raw):
    """Ответ модели → JSON материала: снимаем ```-обёртки и текст вокруг объекта."""
    text = re.sub(r"^```(?:json)?|```$", "", str(raw or "").strip(), flags=re.MULTILINE).strip()
    if not text:
        raise AiError("Модель вернула пустой ответ — попробуйте ещё раз.")
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start > -1 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AiError(
            "Не удалось разобрать ответ модели: возможно, она вернула текст вместо JSON. "
            "Попробуйте модель побольше или повторите запрос."
        ) from exc


def _chat_text(payload):
    """Текст ответа из формата OpenAI (``choices[0].message.content``)."""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AiError("Модель вернула пустой ответ — попробуйте ещё раз.") from exc
    if isinstance(content, list):  # некоторые локальные серверы отдают список частей
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


# ── Адаптер локальной модели ──────────────────────────────────────────────
def _json_mode(spec):
    """response_format=json_object: по умолчанию выключен, локальные модели его не любят."""
    configured = _setting("LMS_AI_JSON_MODE", None)
    if configured is None:
        return bool(spec.get("json_mode"))
    return bool(configured)


def _provider_material(spec, prompt_text, *, filename="", blob=b"", raw_text=False):
    """Отправить материал локальной модели (Ollama, LM Studio, свой шлюз).

    Единый транспорт: ``POST {endpoint}/chat/completions``. Фото уходит
    data-url в content, как того ждут vision-модели; остальные файлы
    (DOCX, XLSX, PDF, TXT) к этому моменту уже превращены в текст.
    """
    endpoint = ai_endpoint()
    if not endpoint:
        raise AiError(
            "Задайте LMS_AI_ENDPOINT — адрес OpenAI-совместимого сервера модели "
            "(например, http://localhost:11434/v1)."
        )
    if not endpoint.startswith(("http://", "https://")):
        raise AiError("Адрес модели настроен неверно — нужен http(s).")
    model = ai_model()
    if not model:
        raise AiError(
            "Не выбрана модель: укажите LMS_AI_MODEL (для LM Studio — идентификатор "
            "загруженной модели, для Ollama — имя из `ollama list`)."
        )
    content = prompt_text
    if blob:
        data, mime = _image_part(blob, _image_mime(filename))
        content = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
        ]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
    }
    if not raw_text and _json_mode(spec):
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if ai_api_key():  # локальные Ollama и LM Studio ключа не требуют
        headers["Authorization"] = f"Bearer {ai_api_key()}"
    request = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    text = _chat_text(_http_json(request, provider=spec["label"]))
    return text if raw_text else _json_from_text(text)


# ── Нормализация и валидация ──────────────────────────────────────────────
def _clean(value, limit=200):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_text(value, limit=4000):
    return str(value or "").strip()[:limit]


def _normalise_topic(topic_data, caps, counters):
    """Тема из JSON → чистая тема с заданиями и карточками."""
    if not isinstance(topic_data, dict):
        return None
    if counters["topics"] >= caps["topics"]:
        return None
    topic = {
        "title": _clean(topic_data.get("title"), 200) or "Новая тема",
        "description": _clean_text(topic_data.get("description")),
        "assignments": [],
        "cards": [],
    }
    for item in topic_data.get("assignments") or []:
        if not isinstance(item, dict) or counters["assignments"] >= caps["assignments"]:
            continue
        assignment = _normalise_assignment(item, caps)
        if assignment:
            topic["assignments"].append(assignment)
            counters["assignments"] += 1
    for card_set in topic_data.get("cards") or []:
        if not isinstance(card_set, dict):
            continue
        cards = []
        for card in card_set.get("cards") or []:
            if counters["cards"] + len(cards) >= caps["cards"] or not isinstance(card, dict):
                break
            front = _clean(card.get("front"), 200)
            back = _clean(card.get("back"), 500)
            if not front or not back:
                continue
            cards.append(
                {
                    "front": front,
                    "back": back,
                    "example": _clean(card.get("example"), 500),
                }
            )
        if len(cards) >= 2:
            topic["cards"].append(
                {
                    "title": _clean(card_set.get("title"), 200) or f"Карточки: {topic['title']}",
                    "description": _clean_text(card_set.get("description")),
                    "cards": cards,
                }
            )
            counters["cards"] += len(cards)
    if topic["assignments"] or topic["cards"]:
        counters["topics"] += 1
        return topic
    return None


def normalise(payload, *, source=""):
    """Привести ответ ИИ или офлайн-разбор к безопасной структуре курса.

    Поддерживает иерархию Класс → Глава → Тема → Задание.
    Для совместимости: если блок содержит topics без chapters — они попадут
    в главу «Общее» при импорте.
    """
    caps = limits()
    if not isinstance(payload, dict):
        raise AiError("Материал не распознан: ожидался объект с блоками.")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise AiError("В материале не нашлось ни одного блока — уточните текст или запрос.")

    result = {"title": _clean(payload.get("title") or source, 150) or "Материал ИИ-помощника"}
    clean_blocks = []
    counters = {"topics": 0, "assignments": 0, "cards": 0, "chapters": 0}
    for block_data in blocks[: caps["blocks"]]:
        if not isinstance(block_data, dict):
            continue
        block = {
            "name": _clean(block_data.get("name") or source, 150) or "Новый класс",
            "cefr_level": _clean(block_data.get("cefr_level"), 2).upper(),
            "description": _clean_text(block_data.get("description")),
            "chapters": [],
            "topics": [],
        }
        if block["cefr_level"] not in AI_CEFR_LEVELS:
            block["cefr_level"] = ""
        # Главы (новый формат)
        for chapter_data in block_data.get("chapters") or []:
            if not isinstance(chapter_data, dict):
                continue
            if counters["chapters"] >= caps["chapters"]:
                continue
            chapter = {
                "title": _clean(chapter_data.get("title"), 200) or "Новая глава",
                "description": _clean_text(chapter_data.get("description")),
                "topics": [],
            }
            for topic_data in chapter_data.get("topics") or []:
                topic = _normalise_topic(topic_data, caps, counters)
                if topic:
                    chapter["topics"].append(topic)
            if chapter["topics"]:
                block["chapters"].append(chapter)
                counters["chapters"] += 1
        # Темы без главы (legacy и офлайн-разбор)
        for topic_data in block_data.get("topics") or []:
            topic = _normalise_topic(topic_data, caps, counters)
            if topic:
                block["topics"].append(topic)
        if block["chapters"] or block["topics"]:
            clean_blocks.append(block)
    if not clean_blocks:
        raise AiError("В материале не нашлось ни заданий, ни карточек.")
    result["blocks"] = clean_blocks
    return result


def _normalise_assignment(item, caps):
    assignment_type = _clean(item.get("type"), 20).lower()
    if assignment_type not in AI_ASSIGNMENT_TYPES:
        assignment_type = Assignment.Type.TEXT
    questions = []
    for question in item.get("questions") or []:
        if len(questions) >= caps["questions"] or not isinstance(question, dict):
            break
        kind = _clean(question.get("kind"), 10).lower()
        if kind not in AI_QUESTION_KINDS:
            kind = Question.Kind.MCQ
        text = _clean_text(question.get("text"), 2000)
        if not text:
            continue
        choices = []
        for choice in question.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            label = _clean(choice.get("text"), 500)
            if not label:
                continue
            choices.append(
                {
                    "text": label,
                    "match_text": _clean(choice.get("match_text"), 500),
                    "correct": bool(choice.get("correct")),
                }
            )
        if kind in {
            Question.Kind.MCQ,
            Question.Kind.MULTI,
            Question.Kind.MATCH,
            Question.Kind.SORT,
        } and not any(choice["correct"] for choice in choices):
            if choices:
                choices[0]["correct"] = True
            else:
                continue
        if kind in Question.MANUAL_KINDS:
            choices = []
        elif not choices and kind not in {Question.Kind.GAP, Question.Kind.SPELL}:
            continue
        try:
            points = int(question.get("points", 1))
        except (TypeError, ValueError):
            points = 1
        questions.append(
            {
                "kind": kind,
                "text": text,
                "points": max(1, min(100, points)),
                "explanation": _clean_text(question.get("explanation"), 1000),
                "choices": choices,
            }
        )
    if questions:
        assignment_type = Assignment.Type.QUIZ
    title = _clean(item.get("title"), 200)
    description = _clean_text(item.get("description"))
    if not title:
        return None
    if not description and not questions:
        return None
    if not description and questions:
        description = f"{title}. Выполните тест."
    skills = [
        _clean(skill, 20).lower()
        for skill in item.get("skills") or []
        if _clean(skill, 20).lower() in {value for value, _ in Skill.Kind.choices}
    ]
    try:
        max_points = int(item.get("max_points", 10))
    except (TypeError, ValueError):
        max_points = 10
    return {
        "type": assignment_type,
        "title": title,
        "description": description,
        "max_points": max(1, min(1000, max_points)),
        "questions": questions,
        "skills": skills,
    }


def material_summary(material):
    """Счётчики для превью: сколько чего создаст импорт (с учётом глав)."""
    blocks = material.get("blocks") or []
    chapters = [ch for block in blocks for ch in block.get("chapters", [])]
    topics_from_chapters = [topic for ch in chapters for topic in ch.get("topics", [])]
    topics_direct = [topic for block in blocks for topic in block.get("topics", [])]
    topics = topics_from_chapters + topics_direct
    assignments = [assignment for topic in topics for assignment in topic.get("assignments", [])]
    card_sets = [item for topic in topics for item in topic.get("cards", [])]
    return {
        "blocks": len(blocks),
        "chapters": len(chapters),
        "topics": len(topics),
        "assignments": len(assignments),
        "questions": sum(len(item["questions"]) for item in assignments),
        "quizzes": len([item for item in assignments if item["questions"]]),
        "card_sets": len(card_sets),
        "cards": sum(len(item["cards"]) for item in card_sets),
    }


# ── Точка входа: файл/текст → материал ────────────────────────────────────
def build_material(
    *,
    text="",
    prompt="",
    target="mixed",
    upload=None,
    filename="",
):
    """Собрать материал: онлайн через ИИ, при неудаче — офлайн-эвристики.

    Возвращает ``(material, meta)``: ``meta`` описывает режим, источник и
    предупреждения для интерфейса.
    """
    mode = ai_mode()
    if mode == "off":
        raise AiError("ИИ-помощник выключен администратором (LMS_AI_ENABLED=0).")
    text = (text or "").strip()[: limits()["text_chars"]]
    blob = b""
    if upload is not None:
        blob = upload.read()
        filename = filename or getattr(upload, "name", "")
    kind = upload_kind(filename) if filename else "text"
    notes = []

    if blob and len(blob) > max_upload_bytes():
        raise AiError(
            f"Файл больше {max_upload_bytes() // (1024 * 1024)} МБ — загрузите часть материала."
        )
    if not text and blob:
        try:
            text = extract_text(filename, blob)
        except AiError as exc:
            notes.append(str(exc))
    if not text and not blob and not prompt.strip():
        raise AiError("Приложите файл, вставьте текст или опишите, что нужно собрать.")

    meta = {"mode": mode, "kind": kind, "filename": filename, "notes": notes}
    if mode == "online":
        spec = provider_spec()
        meta["provider"] = spec["key"]
        meta["provider_label"] = spec["label"]
        attachable = bool(blob) and kind in provider_uploads(spec)
        if blob and not attachable:
            notes.append(unsupported_upload_note(filename, spec))
        if text or prompt.strip() or attachable:
            try:
                payload = _provider_material(
                    spec,
                    build_prompt(text, prompt=prompt, target=target, filename=filename, kind=kind),
                    filename=filename if attachable else "",
                    blob=blob if attachable else b"",
                )
                material = normalise(payload, source=filename or "Материал ИИ-помощника")
                meta["result"] = "online"
                return material, meta
            except AiError as exc:
                meta["notes"].append(f"{exc} Материал разобран офлайн.")
        else:
            notes.append(
                "Онлайн-разбор недоступен: этот файл провайдер не читает, "
                "а текста в нём нет — вставьте текст вручную."
            )
    elif blob:
        note = offline_notes(filename)
        if note:
            notes.append(note)

    if not text:
        text = prompt.strip()
    if not text:
        hints = " ".join(note for note in notes if note)
        raise AiError(
            "Офлайн-режим не смог прочитать файл — вставьте текст вручную."
            + (f" {hints}" if hints else "")
        )
    material = normalise(
        parse_text(text, source=(filename.rsplit(".", 1)[0] if filename else "") or "Материал"),
        source=filename or "Материал ИИ-помощника",
    )
    meta.setdefault("result", "offline")
    return material, meta


def revise_material(material, instruction):
    """Обновить предпросмотр материала, не создавая записи в базе данных."""
    mode = ai_mode()
    if mode == "off":
        raise AiError("ИИ-помощник выключен администратором (LMS_AI_ENABLED=0).")
    if mode != "online":
        raise AiError(
            "Правка текущей версии требует работающей модели. Подключите локальную модель "
            "(LMS_AI_LOCAL=1) или задайте ключ LMS_AI_API_KEY и попробуйте снова."
        )
    instruction = (instruction or "").strip()[:4000]
    if not instruction:
        raise AiError("Опишите, что изменить в текущей версии материала.")
    if not isinstance(material, dict) or not material.get("blocks"):
        raise AiError("Текущая версия материала не найдена — соберите материал заново.")

    spec = provider_spec()
    payload = _provider_material(spec, build_material_revision_prompt(material, instruction))
    revised = normalise(payload, source=material.get("title", "Материал ИИ-помощника"))
    meta = {
        "mode": mode,
        "provider": spec["key"],
        "provider_label": spec["label"],
        "result": "online",
        "revision_instruction": instruction,
    }
    return revised, meta


# ── Импорт черновиками ────────────────────────────────────────────────────
def _unique_slug(model, base, **filters):
    slug = slugify(base, allow_unicode=False)[:50] or "material"
    candidate = slug
    index = 2
    lookup = {**filters, "slug": candidate}
    while model.objects.filter(**lookup).exists():
        candidate = f"{slug}-{index}"[:50]
        index += 1
        lookup["slug"] = candidate
    return candidate


@transaction.atomic
def import_material(material, *, target_topic=None):
    """Создать материалы черновиками. Ничего не публикуется и не меняется.

    ``target_topic`` — существующая тема: тогда новые задания и карточки
    добавляются в неё, а структура блоков/тем/глав из материала игнорируется.
    Иначе создаётся иерархия Класс → Глава → Тема → Задание.
    Темы без главы попадают в главу «Общее» блока (логика Topic.save).
    """
    from .models import Chapter

    ensure_skill_catalog()
    created = {
        "blocks": 0,
        "chapters": 0,
        "topics": 0,
        "assignments": 0,
        "questions": 0,
        "cards": 0,
        "skipped": 0,
    }
    for block_data in material.get("blocks", []):
        if target_topic is not None:
            # При импорте в существующую тему — главы/блоки из материала игнорируются
            topic = target_topic
            for topic_data in block_data.get("topics", []) + [
                t for ch in block_data.get("chapters", []) for t in ch.get("topics", [])
            ]:
                created["assignments"] += _import_assignments(topic, topic_data, created)
                created["cards"] += _import_cards(topic, topic_data, created)
            continue

        block = Block.objects.filter(name=block_data["name"]).first()
        if block is None:
            block = Block.objects.create(
                slug=_unique_slug(Block, block_data["name"]),
                name=block_data["name"],
                description=block_data["description"],
                cefr_level=block_data["cefr_level"],
                order=Block.objects.count(),
            )
            created["blocks"] += 1

        # Главы (новый формат)
        for chapter_data in block_data.get("chapters", []) or []:
            chapter = block.chapters.filter(title=chapter_data["title"]).first()
            if chapter is None:
                chapter = Chapter.objects.create(
                    block=block,
                    slug=_unique_slug(Chapter, chapter_data["title"], block=block),
                    title=chapter_data["title"][:200],
                    description=chapter_data.get("description", "")[:4000],
                    order=block.chapters.count(),
                )
                created["chapters"] += 1
            for topic_data in chapter_data.get("topics", []) or []:
                topic = block.topics.filter(title=topic_data["title"], chapter=chapter).first()
                if topic is None:
                    topic = Topic.objects.create(
                        block=block,
                        chapter=chapter,
                        slug=_unique_slug(Topic, topic_data["title"], block=block),
                        title=topic_data["title"],
                        description=topic_data["description"],
                        order=chapter.topics.count(),
                    )
                    created["topics"] += 1
                created["assignments"] += _import_assignments(topic, topic_data, created)
                created["cards"] += _import_cards(topic, topic_data, created)

        # Темы без главы (legacy)
        for topic_data in block_data.get("topics", []) or []:
            topic = block.topics.filter(title=topic_data["title"]).first()
            if topic is None:
                # Без главы — попадёт в «Общее» через Topic.save, но создадим явно в default_chapter для ясности
                default_ch = block.default_chapter()
                topic = Topic.objects.create(
                    block=block,
                    chapter=default_ch,
                    slug=_unique_slug(Topic, topic_data["title"], block=block),
                    title=topic_data["title"],
                    description=topic_data["description"],
                    order=default_ch.topics.count(),
                )
                created["topics"] += 1
            created["assignments"] += _import_assignments(topic, topic_data, created)
            created["cards"] += _import_cards(topic, topic_data, created)

    logger.info(
        "ai_import blocks=%s chapters=%s topics=%s assignments=%s questions=%s cards=%s",
        created["blocks"],
        created.get("chapters", 0),
        created["topics"],
        created["assignments"],
        created["questions"],
        created["cards"],
    )
    return created


def _import_assignments(topic, topic_data, created):
    order = topic.assignments.count()
    count = 0
    for item in topic_data.get("assignments", []):
        if topic.assignments.filter(title=item["title"][:200]).exists():
            created["skipped"] += 1
            continue
        assignment = Assignment.objects.create(
            topic=topic,
            title=item["title"][:200],
            description=item["description"],
            assignment_type=item["type"],
            max_points=item["max_points"],
            status=Assignment.Publication.DRAFT,
            order=order,
        )
        order += 1
        count += 1
        if item["questions"]:
            _create_questions(assignment, item["questions"])
            created["questions"] += len(item["questions"])
            assignment.max_points = assignment.total_question_points
            assignment.save(update_fields=["max_points", "updated_at"])
        skills = list(Skill.objects.filter(slug__in=item["skills"]))
        if skills:
            assignment.skills.set(skills)
        else:
            apply_default_skills(assignment)
    return count


def _import_cards(topic, topic_data, created):
    order = topic.assignments.count()
    total = 0
    for card_set in topic_data.get("cards", []):
        if topic.assignments.filter(title=card_set["title"][:200]).exists():
            created["skipped"] += 1
            continue
        assignment = Assignment.objects.create(
            topic=topic,
            title=card_set["title"][:200],
            description=card_set["description"],
            assignment_type=Assignment.Type.FLASHCARDS,
            max_points=0,
            status=Assignment.Publication.DRAFT,
            order=order,
        )
        order += 1
        Flashcard.objects.bulk_create(
            [
                Flashcard(
                    assignment=assignment,
                    front=card["front"],
                    back=card["back"],
                    example=card["example"],
                    order=position,
                )
                for position, card in enumerate(card_set["cards"], start=1)
            ]
        )
        apply_default_skills(assignment)
        total += len(card_set["cards"])
    return total


# ── Правка существующего задания ──────────────────────────────────────────
REVISION_SCHEMA = """Верни строго JSON без пояснений в формате:
{"title": "Название задания",
 "description": "Условие для заранее неизвестного ученика: цель, шаги, объём и критерии",
 "max_points": 10, "skills": ["grammar|vocabulary|listening|speaking|writing|reading"],
 "questions": [{"kind": "mcq|multi|gap|match|order|sort|spell|text|voice", "text": "вопрос",
    "points": 1, "explanation": "",
    "choices": [{"text": "вариант", "correct": true, "match_text": ""}]}],
 "cards": [{"front": "слово", "back": "перевод", "example": "пример"}]}
Правила: верни задание ЦЕЛИКОМ, уже с правкой — не описывай отличия. Сохраняй язык,
уровень и тип исходного задания; не добавляй имя, профиль, прошлые ошибки или другую
персонализацию ученика. Описание должно быть пригодно для любого ученика, которому
позже назначат это задание. Если новая версия требует прочитать текст, посмотреть
картинку, прослушать аудио или открыть файл, полный материал должен быть в description
либо среди реально доступных ученику вложений; не оставляй ссылку на скрытый или
несуществующий материал. Для mcq/order ровно один верный вариант, для multi — от
двух; для match/sort в choices пары text ↔ match_text; для gap/spell принимаемые
ответы как верные варианты; text и voice — без choices."""


def assignment_payload(assignment):
    """Текущее задание и его место в курсе — контекст для модели.

    В payload намеренно нет ученика и его попыток: правка создаётся для
    переиспользуемого задания, а аудиторию преподаватель назначает отдельно.
    """
    payload = {
        "type": assignment.assignment_type,
        "title": assignment.title,
        "description": assignment.description,
        "max_points": assignment.max_points,
        "skills": list(assignment.skills.order_by("order", "name").values_list("slug", flat=True)),
    }
    topic = getattr(assignment, "topic", None)
    if topic is not None:
        block = getattr(topic, "block", None)
        chapter = getattr(topic, "chapter", None)
        payload["course_context"] = {
            "block": block.name if block is not None else "",
            "cefr_level": block.cefr_level if block is not None else "",
            "chapter": chapter.title if chapter is not None else "",
            "topic": topic.title,
        }
    if assignment.is_quiz:
        payload["questions"] = [
            {
                "kind": question.kind,
                "text": question.text,
                "points": question.points,
                "explanation": question.explanation,
                "choices": [
                    {
                        "text": choice.text,
                        "correct": choice.is_correct,
                        "match_text": choice.match_text,
                    }
                    for choice in question.choices.all()
                ],
            }
            for question in assignment.questions.prefetch_related("choices").order_by("order", "pk")
        ]
    if assignment.is_flashcards:
        payload["cards"] = [
            {"front": card.front, "back": card.back, "example": card.example}
            for card in assignment.cards.all().order_by("order", "pk")
        ]
    return payload


def build_revision_prompt(assignment, instruction):
    """Промпт правки: текущая версия задания + что изменить + схема ответа."""
    payload = assignment_payload(assignment)
    assignment_type = payload["type"]
    caps = limits()
    parts = [
        AI_AUTHORING_CONTEXT,
        "Ты обновляешь существующее задание, не создаёшь профиль ученика и не меняешь "
        "аудиторию назначения. Верни универсальную версию для любого ученика, которому "
        "преподаватель позже назначит это задание.",
        f"Правка преподавателя: {instruction.strip()}",
        "Текущая версия задания и его место в курсе (JSON). Поле course_context — "
        "контекст Класса/Главы/Темы, а не профиль ученика:\n"
        + json.dumps(payload, ensure_ascii=False, indent=1),
    ]
    if assignment_type == Assignment.Type.QUIZ:
        parts.append(
            "Это тест: верни полный список questions с учётом правки "
            f"(не больше {caps['questions']}), cards верни пустым списком."
        )
    elif assignment_type == Assignment.Type.FLASHCARDS:
        parts.append(
            "Это тренажёр карточек: верни полный список cards с учётом правки "
            f"(не больше {caps['cards']}), questions верни пустым списком."
        )
    else:
        parts.append(
            "Тип задания менять нельзя: правь только title и description, "
            "questions и cards верни пустыми списками."
        )
    parts.append(REVISION_SCHEMA)
    return "\n\n".join(parts)


def revise_assignment(assignment, instruction):
    """Попросить модель переписать задание. Возвращает ``(revision, meta)``."""
    mode = ai_mode()
    if mode == "off":
        raise AiError("ИИ-помощник выключен администратором (LMS_AI_ENABLED=0).")
    if mode != "online":
        raise AiError(
            "Правка с ИИ требует работающей модели: офлайн-разбор не умеет "
            "переписывать готовое задание. Подключите локальную модель "
            "(LMS_AI_LOCAL=1) или задайте ключ LMS_AI_API_KEY и попробуйте снова."
        )
    instruction = (instruction or "").strip()[: limits()["text_chars"]]
    if not instruction:
        raise AiError("Опишите, что изменить: например, «сделай вопросы сложнее».")
    spec = provider_spec()
    payload = _provider_material(spec, build_revision_prompt(assignment, instruction))
    revision = normalise_revision(payload, assignment_type=assignment.assignment_type)
    meta = {
        "mode": mode,
        "provider": spec["key"],
        "provider_label": spec["label"],
        "result": "online",
    }
    return revision, meta


def normalise_revision(payload, *, assignment_type):
    """Ответ модели → безопасная правка задания того же типа, что и исходное."""
    if not isinstance(payload, dict):
        raise AiError("Модель вернула не JSON-объект задания — попробуйте ещё раз.")
    revision = _normalise_assignment({**payload, "type": assignment_type}, limits())
    if revision is None:
        raise AiError("В ответе модели нет названия или условия — попробуйте ещё раз.")
    revision["type"] = assignment_type  # вопросы не превращают задание в тест: тип фиксирован
    if assignment_type == Assignment.Type.QUIZ:
        if not revision["questions"]:
            raise AiError("Модель не вернула вопросов теста — попробуйте ещё раз.")
    else:
        revision["questions"] = []
    if assignment_type == Assignment.Type.FLASHCARDS:
        cards = []
        for card in payload.get("cards") or []:
            if len(cards) >= limits()["cards"] or not isinstance(card, dict):
                break
            front = _clean(card.get("front"), 200)
            back = _clean(card.get("back"), 500)
            if not front or not back:
                continue
            cards.append(
                {"front": front, "back": back, "example": _clean(card.get("example"), 500)}
            )
        if len(cards) < 2:
            raise AiError(
                "Модель вернула слишком мало карточек (нужно хотя бы две) — попробуйте ещё раз."
            )
        revision["cards"] = cards
    else:
        revision["cards"] = []
    return revision


@transaction.atomic
def apply_revision(assignment, revision):
    """Заменить содержимое задания версией ИИ. Статус, дедлайн, вложение сохраняются."""
    summary = {"questions": 0, "cards": 0}
    assignment.title = revision["title"][:200]
    assignment.description = revision["description"]
    if assignment.is_quiz:
        assignment.questions.all().delete()
        _create_questions(assignment, revision["questions"])
        assignment.max_points = assignment.total_question_points
        summary["questions"] = len(revision["questions"])
    elif assignment.is_flashcards:
        assignment.cards.all().delete()
        Flashcard.objects.bulk_create(
            [
                Flashcard(
                    assignment=assignment,
                    front=card["front"],
                    back=card["back"],
                    example=card["example"],
                    order=position,
                )
                for position, card in enumerate(revision["cards"], start=1)
            ]
        )
        summary["cards"] = len(revision["cards"])
    else:
        assignment.max_points = max(1, min(1000, revision["max_points"]))
    skills = list(Skill.objects.filter(slug__in=revision.get("skills") or []))
    if skills:
        assignment.skills.set(skills)
    assignment.save(update_fields=["title", "description", "max_points", "updated_at"])
    logger.info(
        "ai_revision_applied assignment=%s questions=%s cards=%s",
        assignment.pk,
        summary["questions"],
        summary["cards"],
    )
    return summary


__all__ = [
    "AiError",
    "AI_CEFR_LEVELS",
    "AI_QUESTION_KINDS",
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "TARGETS",
    "UPLOAD_EXTENSIONS",
    "UPLOAD_KINDS",
    "ai_enabled",
    "ai_endpoint",
    "ai_mode",
    "ai_mode_label",
    "ai_model",
    "ai_provider",
    "apply_revision",
    "assignment_payload",
    "build_material",
    "build_material_revision_prompt",
    "build_prompt",
    "build_revision_prompt",
    "extension_of",
    "extract_text",
    "import_material",
    "limits",
    "material_summary",
    "max_upload_bytes",
    "normalise",
    "normalise_revision",
    "offline_notes",
    "parse_text",
    "provider_hint",
    "provider_spec",
    "provider_uploads",
    "revise_assignment",
    "revise_material",
    "unsupported_upload_note",
    "upload_kind",
]
