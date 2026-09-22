"""ИИ-помощник преподавателя: файл или текст → структура курса черновиками.

Режим работы гибридный (решение продукта):

* **online** — задан ключ провайдера (по умолчанию Gemini): материал отправляется
  в модель вместе с фото/PDF/видео, ответ разбирается в структуру курса;
* **offline** — ключа нет: работаем без сети, извлекаем текст из файла
  (DOCX, XLSX, PDF, TXT/MD/CSV) и раскладываем его по структуре эвристиками;
* **off** — помощник выключен переменной окружения ``LMS_AI_ENABLED=0``.

Гарантии: ИИ ничего не публикует сам — импорт всегда создаёт **черновики**;
ответ модели валидируется и обрезается по лимитам; содержимое файлов не
логируется (в журнал попадают только счётчики).

Внешних зависимостей нет: запрос к REST API выполняется через ``urllib``,
архивы Office разбираются стандартным ``zipfile``. Это важно для сборки
Amvera: ``requirements.txt`` с хешами не меняется.
"""

from __future__ import annotations

import base64
import json
import logging
import re
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
    ("mixed", "Структура целиком: блок → темы → задания"),
    ("assignment", "Одно задание"),
    ("quiz", "Тест с вопросами"),
    ("cards", "Набор карточек"),
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


class AiError(Exception):
    """Понятная преподавателю ошибка помощника."""


# ── Настройки и режим ──────────────────────────────────────────────────────
def _setting(name, default):
    return getattr(settings, name, default)


def ai_enabled():
    return bool(_setting("LMS_AI_ENABLED", True))


def ai_api_key():
    return str(_setting("LMS_AI_API_KEY", "") or "").strip()


def ai_mode():
    """Режим помощника: off, offline или online."""
    if not ai_enabled():
        return "off"
    return "online" if ai_api_key() else "offline"


def ai_mode_label(mode=None):
    mode = mode or ai_mode()
    if mode == "off":
        return "ИИ-помощник выключен администратором"
    if mode == "online":
        provider = _setting("LMS_AI_PROVIDER", "gemini")
        model = _setting("LMS_AI_MODEL", "gemini-2.0-flash")
        return f"ИИ подключён: {provider} · {model}"
    return "Офлайн-разбор: ключа нет, работаем без сети"


def max_upload_bytes():
    return int(_setting("LMS_AI_MAX_FILE_BYTES", 10 * 1024 * 1024))


def _limit(name, default):
    return int(_setting(name, default))


def limits():
    return {
        "blocks": _limit("LMS_AI_MAX_BLOCKS", 5),
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
        return "Офлайн-режим не распознаёт текст на картинках — нужен ключ ИИ."
    if kind in {"video", "audio"}:
        return "Офлайн-режим не смотрит видео и не слушает аудио — нужен ключ ИИ."
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
    """Сборка структуры из текста: заголовки, вопросы и карточки по строкам."""

    source: str = ""
    blocks: list = field(default_factory=list)
    _block: dict | None = None
    _topic: dict | None = None
    _assignment: dict | None = None
    _question: dict | None = None
    _card_title: str = ""
    _card_notes: list = field(default_factory=list)

    def ensure_block(self, name=None):
        if self._block is None:
            self._block = {
                "name": (name or self.source or "Блок из материала")[:150],
                "cefr_level": "",
                "description": "",
                "topics": [],
            }
            self.blocks.append(self._block)
        return self._block

    def ensure_topic(self, title=None):
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
    """Офлайн-разбор: заголовки → блок/тема/задание, строки → вопросы и карточки."""
    builder = _OfflineBuilder(source=source)
    pending_cards = []
    saw_heading = False

    def flush_cards():
        nonlocal pending_cards
        if len(pending_cards) >= 2:
            builder.add_cards(pending_cards)
        pending_cards = []

    for raw_line in (text or "").splitlines():
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
            title = _strip_markers(heading.group(2))
            if level == 1:
                builder._topic = None
                builder._block = None
                builder.ensure_block(title)
            elif level == 2:
                builder._topic = None
                builder.ensure_topic(title)
            else:
                assignment_type = _marker_value(
                    heading.group(2), TYPE_MARKERS, Assignment.Type.TEXT
                )
                if assignment_type == Assignment.Type.FLASHCARDS:
                    builder._card_title = title
                else:
                    builder._assignment = None
                    builder.ensure_assignment(title, assignment_type)
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
        else:
            builder.ensure_topic()["description"] = stripped

    flush_cards()
    if not saw_heading:
        builder.blocks.clear()
        builder._block = builder._topic = builder._assignment = None
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
PROMPT_SCHEMA = """Верни строго JSON без пояснений в формате:
{"title": "Название материала",
 "blocks": [{"name": "Блок", "cefr_level": "A1|A2|B1|B2|C1|C2 или пусто",
   "description": "1–2 предложения",
   "topics": [{"title": "Тема", "description": "1–2 предложения",
     "assignments": [{"type": "text|file|audio|mixed|quiz", "title": "Название",
        "description": "Условие для ученика: что сделать, объём, критерии",
        "max_points": 10, "skills": ["grammar|vocabulary|listening|speaking|writing|reading"],
        "questions": [{"kind": "mcq|multi|gap|match|order|sort|spell", "text": "вопрос",
            "points": 1, "explanation": "",
            "choices": [{"text": "вариант", "correct": true, "match_text": ""}]}]}],
     "cards": [{"title": "Карточки: тема", "description": "",
        "cards": [{"front": "слово", "back": "перевод", "example": "пример"}]}]}]}]}
Правила: для mcq/order ровно один верный вариант; для multi — от двух; для match/sort
в choices используй пары text ↔ match_text; для gap/spell приведи принимаемые ответы
как верные варианты; questions и cards не выдумывай, если их нет в материале."""

TARGET_PROMPTS = {
    "mixed": "Собери из материала блок курса с темами, заданиями, тестом и карточками.",
    "assignment": "Собери одно задание с условием; если в материале есть упражнения — оформи их тестом.",
    "quiz": "Собери одно задание-тест с вопросами по материалу.",
    "cards": "Собери набор карточек: слово → перевод и пример употребления.",
}


def build_prompt(text, *, prompt="", target="mixed", filename="", kind="text"):
    parts = [TARGET_PROMPTS.get(target, TARGET_PROMPTS["mixed"])]
    if prompt.strip():
        parts.append(f"Пожелания преподавателя: {prompt.strip()}")
    if filename:
        parts.append(f"Исходный файл: {filename}. Тип файла: {kind}.")
    if text.strip():
        parts.append("Текст материала:\n" + text.strip()[: limits()["text_chars"]])
    elif kind in {"image", "video", "audio", "pdf"}:
        parts.append("Разбери приложенный файл.")
    parts.append(PROMPT_SCHEMA)
    return "\n\n".join(parts)


def _inline_part(filename, blob):
    kind = upload_kind(filename)
    if kind == "image":
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(extension_of(filename), "image/jpeg")
    elif kind == "pdf":
        mime = "application/pdf"
    elif kind == "video":
        mime = {"…mov": "video/quicktime", ".webm": "video/webm"}.get(
            extension_of(filename), "video/mp4"
        )
    elif kind == "audio":
        mime = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
            ".flac": "audio/flac",
        }.get(extension_of(filename), "audio/mp4")
    else:
        return None
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(blob).decode("ascii")}}


def _gemini_material(parts):
    model = _setting("LMS_AI_MODEL", "gemini-2.0-flash")
    endpoint = _setting(
        "LMS_AI_ENDPOINT",
        "https://generativelanguage.googleapis.com/v1beta/models",
    )
    url = f"{endpoint}/{model}:generateContent?key={ai_api_key()}"
    if not url.startswith(("https://", "http://")):
        raise AiError("Адрес провайдера ИИ настроен неверно — нужен http(s).")
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = int(_setting("LMS_AI_TIMEOUT", 60))
    try:
        # Схема адреса проверена выше; endpoint задаётся администратором в настройках.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
        except Exception:  # pragma: no cover - защита от нечитаемого тела ответа
            detail = ""
        logger.warning("ai_http_error status=%s", exc.code)
        if exc.code in {401, 403}:
            raise AiError("Ключ ИИ отклонён провайдером — проверьте LMS_AI_API_KEY.") from exc
        if exc.code == 429:
            raise AiError("Лимит запросов бесплатного тарифа исчерпан. Попробуйте позже.") from exc
        raise AiError(f"Провайдер ИИ вернул ошибку {exc.code}. {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AiError("Не удалось связаться с ИИ — сработал офлайн-разбор.") from exc

    try:
        chunks = payload["candidates"][0]["content"]["parts"]
        raw = "".join(part.get("text", "") for part in chunks)
    except (KeyError, IndexError, TypeError) as exc:
        raise AiError("ИИ вернул пустой ответ — попробуйте ещё раз.") from exc
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AiError("Не удалось разобрать ответ ИИ — попробуйте ещё раз.") from exc


# ── Нормализация и валидация ──────────────────────────────────────────────
def _clean(value, limit=200):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_text(value, limit=4000):
    return str(value or "").strip()[:limit]


def normalise(payload, *, source=""):
    """Привести ответ ИИ или офлайн-разбор к безопасной структуре курса."""
    caps = limits()
    if not isinstance(payload, dict):
        raise AiError("Материал не распознан: ожидался объект с блоками.")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise AiError("В материале не нашлось ни одного блока — уточните текст или запрос.")

    result = {"title": _clean(payload.get("title") or source, 150) or "Материал ИИ-помощника"}
    clean_blocks = []
    topics_total = 0
    assignments_total = 0
    cards_total = 0
    for block_data in blocks[: caps["blocks"]]:
        if not isinstance(block_data, dict):
            continue
        block = {
            "name": _clean(block_data.get("name") or source, 150) or "Новый блок",
            "cefr_level": _clean(block_data.get("cefr_level"), 2).upper(),
            "description": _clean_text(block_data.get("description")),
            "topics": [],
        }
        if block["cefr_level"] not in AI_CEFR_LEVELS:
            block["cefr_level"] = ""
        for topic_data in block_data.get("topics") or []:
            if not isinstance(topic_data, dict) or topics_total >= caps["topics"]:
                continue
            topic = {
                "title": _clean(topic_data.get("title"), 200) or "Новая тема",
                "description": _clean_text(topic_data.get("description")),
                "assignments": [],
                "cards": [],
            }
            for item in topic_data.get("assignments") or []:
                if not isinstance(item, dict) or assignments_total >= caps["assignments"]:
                    continue
                assignment = _normalise_assignment(item, caps)
                if assignment:
                    topic["assignments"].append(assignment)
                    assignments_total += 1
            for card_set in topic_data.get("cards") or []:
                if not isinstance(card_set, dict):
                    continue
                cards = []
                for card in card_set.get("cards") or []:
                    if cards_total + len(cards) >= caps["cards"] or not isinstance(card, dict):
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
                            "title": _clean(card_set.get("title"), 200)
                            or f"Карточки: {topic['title']}",
                            "description": _clean_text(card_set.get("description")),
                            "cards": cards,
                        }
                    )
                    cards_total += len(cards)
            if topic["assignments"] or topic["cards"]:
                block["topics"].append(topic)
                topics_total += 1
        if block["topics"]:
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
        if not choices and kind not in {Question.Kind.GAP, Question.Kind.SPELL}:
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
    if not title or not description:
        return None
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
    """Счётчики для превью: сколько чего создаст импорт."""
    blocks = material.get("blocks") or []
    topics = [topic for block in blocks for topic in block["topics"]]
    assignments = [assignment for topic in topics for assignment in topic.get("assignments", [])]
    card_sets = [item for topic in topics for item in topic.get("cards", [])]
    return {
        "blocks": len(blocks),
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
        parts = [
            {"text": build_prompt(text, prompt=prompt, target=target, filename=filename, kind=kind)}
        ]
        inline = _inline_part(filename, blob) if blob else None
        if inline and len(blob) <= max_upload_bytes():
            parts.append(inline)
        try:
            payload = _gemini_material(parts)
            material = normalise(payload, source=filename or "Материал ИИ-помощника")
            meta["result"] = "online"
            return material, meta
        except AiError as exc:
            meta["notes"].append(f"{exc} Материал разобран офлайн.")

    if not text:
        text = prompt.strip()
    if not text:
        raise AiError("Офлайн-режим не смог прочитать файл — вставьте текст вручную.")
    material = normalise(
        parse_text(text, source=(filename.rsplit(".", 1)[0] if filename else "") or "Материал"),
        source=filename or "Материал ИИ-помощника",
    )
    meta.setdefault("result", "offline")
    return material, meta


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
    добавляются в неё, а структура блоков/тем из материала игнорируется.
    """
    ensure_skill_catalog()
    created = {
        "blocks": 0,
        "topics": 0,
        "assignments": 0,
        "questions": 0,
        "cards": 0,
        "skipped": 0,
    }
    for block_data in material.get("blocks", []):
        if target_topic is not None:
            topic = target_topic
        else:
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
            topic = None
        for topic_data in block_data["topics"]:
            if target_topic is None:
                topic = block.topics.filter(title=topic_data["title"]).first()
                if topic is None:
                    topic = Topic.objects.create(
                        block=block,
                        slug=_unique_slug(Topic, topic_data["title"], block=block),
                        title=topic_data["title"],
                        description=topic_data["description"],
                        order=block.topics.count(),
                    )
                    created["topics"] += 1
            created["assignments"] += _import_assignments(topic, topic_data, created)
            created["cards"] += _import_cards(topic, topic_data, created)
    logger.info(
        "ai_import blocks=%s topics=%s assignments=%s questions=%s cards=%s",
        created["blocks"],
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


__all__ = [
    "AiError",
    "AI_CEFR_LEVELS",
    "AI_QUESTION_KINDS",
    "TARGETS",
    "UPLOAD_EXTENSIONS",
    "ai_enabled",
    "ai_mode",
    "ai_mode_label",
    "build_material",
    "build_prompt",
    "extension_of",
    "extract_text",
    "import_material",
    "limits",
    "material_summary",
    "max_upload_bytes",
    "normalise",
    "offline_notes",
    "parse_text",
    "upload_kind",
]
