"""Текст с пропусками: разметка, проверка и подготовка к показу.

Пункт этого типа — не вопрос с отдельными полями, а цельный текст, внутри
которого стоят пропуски. Ученик вписывает ответ прямо в текст, на месте
пропуска: контекст предложения остаётся перед глазами, а не теряется в
списке «вопрос — поле ответа».

Разметка авторская и намеренно простая:

* ``[[1]]``, ``[[2]]`` — пропуск с явным номером;
* ``___`` (три и более подчёркивания) — пропуск, номер по порядку.

Номера нормализуются по позиции в тексте: первый пропуск — всегда 1. Явные
номера нужны только для читаемости черновика; если они идут не по порядку,
форма правки честно скажет об этом.

Принятые ответы хранятся вариантами ``Choice``, у которых ``order`` — номер
пропуска, а ``text`` — один из допустимых вариантов (``don't`` / ``do not``).
Так формат не требует новых таблиц, и резервные копии, копирование задания
и импорт из библиотеки работают без доработок.

Модуль сознательно не знает про модели и базу: сюда приходят текст, список
принятых ответов и ответ ученика. Это позволяет проверять логику в чистых
тестах и переиспользовать разметку в формах, предпросмотре и ИИ-импорте.
"""

import re

from .scoring import normalize_gap

#: Пропуск: явная разметка ``[[n]]`` или три и более подчёркивания.
GAP = re.compile(r"\[\[\s*(\d{1,2})\s*\]\]|_{3,}")
#: Больше 40 пропусков в одном тексте — уже не упражнение, а простыня.
MAX_GAPS = 40
#: Потолок вариантов ответа на весь пункт (каждый вариант — строка Choice).
MAX_VARIANTS = 200


def parse_cloze(text):
    """Текст с пропусками → сегменты ``{"type": "text"|"gap", ...}``.

    У пропуска есть ``number`` (номер по порядку в тексте) и ``explicit`` —
    номер, который написал преподаватель (или ``None`` для ``___``).
    """
    source = str(text or "")
    segments = []
    position = 0
    number = 0
    for match in GAP.finditer(source):
        if match.start() > position:
            segments.append({"type": "text", "text": source[position : match.start()]})
        number += 1
        explicit = match.group(1)
        segments.append(
            {
                "type": "gap",
                "number": number,
                "explicit": int(explicit) if explicit is not None else None,
            }
        )
        position = match.end()
    if position < len(source):
        segments.append({"type": "text", "text": source[position:]})
    return segments


def gap_count(text):
    """Сколько пропусков в тексте."""
    return sum(1 for segment in parse_cloze(text) if segment["type"] == "gap")


def normalize_cloze_text(text):
    """Перечислить пропуски по порядку: ``___`` → ``[[n]]``, чужие номера — тоже.

    Импорт из ИИ и старые черновики приходят с разной разметкой. Нормализация
    делает текст предсказуемым: первый пропуск всегда ``[[1]]``, второй
    ``[[2]]``. Уже правильная разметка остаётся без изменений.
    """
    parts = []
    number = 0
    for segment in parse_cloze(text):
        if segment["type"] == "text":
            parts.append(segment["text"])
        else:
            number += 1
            parts.append(f"[[{number}]]")
    return "".join(parts)


def layout_error(text):
    """Понятная преподавателю причина, почему разметка текста не годится."""
    gaps = [segment for segment in parse_cloze(text) if segment["type"] == "gap"]
    if not gaps:
        return "Добавьте хотя бы один пропуск: [[1]] или ___ (три подчёркивания)."
    for segment in gaps:
        explicit = segment["explicit"]
        if explicit is not None and explicit != segment["number"]:
            return (
                "Номера пропусков [[n]] должны идти по порядку: первый — [[1]], "
                f"второй — [[2]] и так далее. Сейчас на месте {segment['number']} "
                f"стоит [[{explicit}]]."
            )
    if len(gaps) > MAX_GAPS:
        return f"Слишком много пропусков: {len(gaps)}, максимум {MAX_GAPS}."
    return ""


def accepted_by_gap(choices, count):
    """Принятые ответы по номерам пропусков: список списков вариантов."""
    grouped = {}
    for choice in choices:
        text = str(getattr(choice, "text", "") or "").strip()
        if not text:
            continue
        number = int(getattr(choice, "order", 0) or 0)
        if 1 <= number <= max(count, 0):
            grouped.setdefault(number, []).append(text)
    return [grouped.get(number, []) for number in range(1, count + 1)]


def answer_key(accepted):
    """Принятые ответы в виде, удобном шаблону: [{number, answers}]."""
    return [
        {"number": number, "answers": list(options)}
        for number, options in enumerate(accepted, start=1)
    ]


def score_cloze(given, accepted, count=None):
    """Разбор ответа: сколько пропусков заполнено верно и что вписал ученик.

    ``given`` — словарь ``{str(номер): вписанный текст}`` из формы; ``accepted``
    — список допустимых вариантов по пропускам. Пустой пропуск — ошибка:
    пункт считается недоделанным, но не роняет проверку.
    """
    total = count if count is not None else len(accepted)
    supplied = given if isinstance(given, dict) else {}
    normalized = {}
    marks = {}
    hits = 0
    for number in range(1, total + 1):
        value = str(supplied.get(str(number), "") or "")[:500]
        normalized[str(number)] = value
        options = {normalize_gap(item) for item in accepted[number - 1] if normalize_gap(item)}
        options.discard("")
        ok = bool(options) and normalize_gap(value) in options
        marks[str(number)] = ok
        hits += 1 if ok else 0
    ratio = hits / total if total else 0.0
    return {
        "ratio": ratio,
        "hits": hits,
        "total": total,
        "gaps": marks,
        "given": normalized,
    }


def filled_text(text, accepted, given=None, *, use_given=False):
    """Текст с ответами вместо пропусков: эталон (первый вариант) или ответ ученика."""
    supplied = given if isinstance(given, dict) else {}
    parts = []
    for segment in parse_cloze(text):
        if segment["type"] == "text":
            parts.append(segment["text"])
            continue
        number = segment["number"]
        options = accepted[number - 1] if number <= len(accepted) else []
        if use_given:
            value = str(supplied.get(str(number), "") or "").strip()
            parts.append(value or "______")
        else:
            parts.append(options[0] if options else "______")
    return "".join(parts)


def display_text(text):
    """Текст без служебной разметки: пропуски заменены на «______».

    Для списков, заголовков таблиц и выгрузок, где нужен обычный текст, а не
    интерактивная форма: ``Yesterday Anna [[1]] to the park`` → «Yesterday
    Anna ______ to the park».
    """
    return filled_text(text, [[] for _ in range(gap_count(text))])


def build_segments(text, accepted, given=None, *, show_answers=False):
    """Сегменты для шаблона: текст и пропуски с ответом ученика и отметкой.

    ``show_answers`` включает проверку: у каждого пропуска появляется статус
    ``ok`` / ``bad`` / ``empty`` и подсказка с правильным ответом. Пока пункт
    открыт, статусы не считаются — иначе подсказка выдала бы решение.
    """
    supplied = given if isinstance(given, dict) else {}
    marks = score_cloze(supplied, accepted)["gaps"] if show_answers else {}
    segments = []
    for segment in parse_cloze(text):
        if segment["type"] == "text":
            segments.append(segment)
            continue
        number = segment["number"]
        value = str(supplied.get(str(number), "") or "")[:500]
        options = accepted[number - 1] if number <= len(accepted) else []
        if not show_answers:
            status, title = "", ""
        elif marks.get(str(number)):
            status, title = "ok", ""
        else:
            status = "empty" if not value.strip() else "bad"
            title = "Правильно: " + " / ".join(options) if options else "Ответ не задан"
        segments.append(
            {
                "type": "gap",
                "number": number,
                "given": value,
                "status": status,
                "title": title,
                "answers": list(options),
            }
        )
    return segments
