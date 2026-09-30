"""Комментарии преподавателя к фрагментам текстового ответа ученика.

Аналог рецензирования в Word: преподаватель выделяет часть текста и оставляет
к ней комментарий. Пока проверка не сохранена, выделения живут только в
браузере (см. ``lms.js``); на сервер они попадают одним JSON-полем формы и
превращаются в ``FeedbackHighlight`` только при сохранении решения — так
черновик выделений никогда не «протекает» ученику как законченная проверка.

Модуль отвечает за две вещи:
* рендер текста ответа с подсветкой (используется и для преподавателя, и для
  ученика — после того как проверка завершена);
* разбор данных о выделениях: из формы преподавателя и из ответа ИИ-проверки.
"""

from __future__ import annotations

import json

from django.utils.html import escape
from django.utils.safestring import mark_safe

MAX_HIGHLIGHTS_PER_SUBMISSION = 200
MAX_AI_HIGHLIGHTS = 12
MAX_QUOTE_CHARS = 600
MAX_COMMENT_CHARS = 2000


def _get(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def render_highlighted_html(text, highlights):
    """Текст ответа с безопасно экранированными ``<mark>`` вокруг выделений.

    ``highlights`` — любые объекты (модели или словари) с полями
    start/end/comment/source. Пересекающиеся или выходящие за границы текста
    диапазоны отбрасываются защитно — так опечатка в данных не ломает страницу.
    """
    text = text or ""
    length = len(text)
    ranges = []
    cursor_check = -1
    for item in sorted(highlights or [], key=lambda h: (_get(h, "start", 0), _get(h, "end", 0))):
        start = _get(item, "start", 0)
        end = _get(item, "end", 0)
        try:
            start = max(0, min(int(start), length))
            end = max(0, min(int(end), length))
        except (TypeError, ValueError):
            continue
        if end <= start or start < cursor_check:
            continue
        ranges.append((start, end, item))
        cursor_check = end

    if not ranges:
        return mark_safe(escape(text))

    parts = []
    cursor = 0
    for start, end, item in ranges:
        if start > cursor:
            parts.append(escape(text[cursor:start]))
        comment = escape(str(_get(item, "comment", "") or ""))
        source = _get(item, "source", "teacher") or "teacher"
        css_source = "ai" if source == "ai" else "teacher"
        highlight_id = _get(item, "pk", None) or _get(item, "id", "")
        parts.append(
            '<mark class="answer-highlight answer-highlight--{source}" '
            'data-highlight-id="{hid}" data-comment="{comment}" '
            'data-start="{start}" data-end="{end}" tabindex="0" role="note" '
            'aria-label="Комментарий преподавателя">{segment}</mark>'.format(
                source=css_source,
                hid=escape(str(highlight_id)),
                comment=comment,
                start=start,
                end=end,
                segment=escape(text[start:end]),
            )
        )
        cursor = end
    if cursor < length:
        parts.append(escape(text[cursor:]))
    return mark_safe("".join(parts))


def _find_free_occurrence(text, quote, taken):
    """Первое вхождение ``quote``, не пересекающееся с уже занятыми диапазонами."""
    search_from = 0
    while True:
        index = text.find(quote, search_from)
        if index == -1:
            return None
        end = index + len(quote)
        if not any(index < t_end and end > t_start for t_start, t_end in taken):
            return index
        search_from = index + 1


def resolve_ai_highlights(text, raw_highlights, *, limit=MAX_AI_HIGHLIGHTS):
    """Из ответа ИИ (список {"quote", "comment"}) — размещённые непересекающиеся выделения.

    Цитаты, которых нет дословно в тексте ответа, молча пропускаются: модель
    иногда слегка перефразирует — лучше потерять один комментарий, чем
    показать преподавателю фрагмент, которого ученик не писал.
    """
    text = text or ""
    results = []
    taken = []
    if not text or not raw_highlights:
        return results
    for item in raw_highlights:
        if len(results) >= limit or not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        comment = str(item.get("comment") or "").strip()
        if not quote or not comment or len(quote) > MAX_QUOTE_CHARS:
            continue
        start = _find_free_occurrence(text, quote, taken)
        if start is None:
            continue
        end = start + len(quote)
        taken.append((start, end))
        results.append(
            {
                "start": start,
                "end": end,
                "quote": text[start:end],
                "comment": comment[:MAX_COMMENT_CHARS],
                "source": "ai",
            }
        )
    results.sort(key=lambda h: h["start"])
    return results


def parse_submitted_highlights(text, raw, *, limit=MAX_HIGHLIGHTS_PER_SUBMISSION):
    """Выделения, присланные формой проверки (JSON) → безопасный список для сохранения.

    Цитата всегда пересчитывается из ``text`` по присланным смещениям — клиенту
    не доверяем, только диапазону. Пересекающиеся диапазоны отбрасываются.
    """
    text = text or ""
    length = len(text)
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []

    cleaned = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (TypeError, ValueError):
            continue
        start = max(0, min(start, length))
        end = max(0, min(end, length))
        if end <= start:
            continue
        comment = str(item.get("comment") or "").strip()[:MAX_COMMENT_CHARS]
        if not comment:
            continue
        source = "ai" if item.get("source") == "ai" else "teacher"
        cleaned.append({"start": start, "end": end, "comment": comment, "source": source})

    cleaned.sort(key=lambda h: (h["start"], h["end"]))
    result = []
    last_end = -1
    for item in cleaned:
        if item["start"] < last_end:
            continue
        item["quote"] = text[item["start"] : item["end"]]
        result.append(item)
        last_end = item["end"]
    return result


__all__ = [
    "MAX_AI_HIGHLIGHTS",
    "MAX_HIGHLIGHTS_PER_SUBMISSION",
    "parse_submitted_highlights",
    "render_highlighted_html",
    "resolve_ai_highlights",
]
