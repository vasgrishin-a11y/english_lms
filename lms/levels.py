"""Порядок уровней CEFR и группировка библиотеки контента.

Библиотека шаблонов, заготовок и готовых курсов показывается учителю не в
порядке объявления в коде, а по уровням — от самых слабых к сильным, отдельно
от экзаменационных треков (ОГЭ, ЕГЭ, IELTS). Один модуль отвечает и за
сортировку, и за подписи групп, чтобы порядок был одинаковым во всех формах.
"""

from __future__ import annotations

import re

LEVEL_ORDER = ["A1", "A2", "B1", "B2", "C1", "C2"]

LEVEL_LABELS = {
    "A1": "A1 · Beginner",
    "A2": "A2 · Elementary",
    "B1": "B1 · Intermediate",
    "B2": "B2 · Upper-Intermediate",
    "C1": "C1 · Advanced",
    "C2": "C2 · Proficiency",
}

EXAM_ORDER = ["oge", "ege", "ielts"]

EXAM_LABELS = {
    "oge": "ОГЭ",
    "ege": "ЕГЭ",
    "ielts": "IELTS",
}

_EXAM_ALIASES = {
    "огэ": "oge",
    "oge": "oge",
    "егэ": "ege",
    "ege": "ege",
    "ielts": "ielts",
}

_LEVEL_PATTERN = re.compile(r"(?<![A-Za-z0-9])([abcABC][12])(?![0-9])")


def normalize_exam(value):
    """Привести метку экзамена к ключу ОГЭ/ЕГЭ/IELTS или вернуть ``None``."""
    if not value:
        return None
    return _EXAM_ALIASES.get(str(value).strip().lower())


def levels_in(value):
    """Все уровни CEFR, упомянутые в строке: «B1–B2» → ``["B1", "B2"]``."""
    if not value:
        return []
    found = []
    for match in _LEVEL_PATTERN.finditer(str(value)):
        level = match.group(1).upper()
        if level not in found:
            found.append(level)
    return found


def level_rank(value):
    """Место уровня в порядке «от слабых к сильным».

    Диапазон («B1–B2») сортируется по нижней границе: материал нужен раньше.
    Строки без уровня уходят в конец.
    """
    levels = levels_in(value)
    if not levels:
        return len(LEVEL_ORDER)
    return min(LEVEL_ORDER.index(level) for level in levels if level in LEVEL_ORDER)


def exam_rank(value):
    """Место экзаменационного трека: ОГЭ → ЕГЭ → IELTS."""
    key = normalize_exam(value)
    if key is None:
        return len(EXAM_ORDER)
    return EXAM_ORDER.index(key)


def level_label(value):
    """Подпись группы для уровня: «A2 · Elementary» либо исходная строка."""
    levels = levels_in(value)
    if not levels:
        return value or "Без уровня"
    first = levels[0]
    if len(levels) > 1:
        return f"{first}–{levels[-1]} · по возрастанию"
    return LEVEL_LABELS.get(first, first)


def sort_by_level(items, *, level_key="level", name_key="label"):
    """Отсортировать записи по уровню, затем по подписи."""
    return sorted(
        items,
        key=lambda item: (
            level_rank(item.get(level_key)),
            str(item.get(name_key) or ""),
        ),
    )


def exam_keys(item):
    """Нормализованные экзаменационные метки записи (поле ``exams`` или ``exam``)."""
    raw = item.get("exams")
    if raw is None:
        raw = [item.get("exam")] if item.get("exam") else []
    elif isinstance(raw, str):
        raw = [raw]
    result = []
    for value in raw:
        key = normalize_exam(value)
        if key and key not in result:
            result.append(key)
    return result


def group_items(items, *, level_key="level", name_key="label", with_exams=True):
    """Сгруппировать записи: сначала уровни A1→C2, затем экзаменационные треки.

    Возвращает список групп ``{"key", "label", "kind", "level", "items"}``.
    Пустые группы не попадают в результат, поэтому шаблон просто рисует то,
    что пришло, и порядок одинаков во всех формах.
    """
    levels_bucket: dict[str, list] = {level: [] for level in LEVEL_ORDER}
    exam_bucket: dict[tuple, list] = {}
    without: list = []

    for item in items:
        exams = exam_keys(item) if with_exams else []
        if exams:
            exam_bucket.setdefault(tuple(exams), []).append(item)
            continue
        value = item.get(level_key)
        ranks = levels_in(value)
        if ranks:
            levels_bucket[ranks[0]].append(item)
        else:
            without.append(item)

    groups = []
    for level in LEVEL_ORDER:
        bucket = levels_bucket[level]
        if not bucket:
            continue
        groups.append(
            {
                "key": level.lower(),
                "label": LEVEL_LABELS[level],
                "kind": "level",
                "level": level,
                "items": sorted(bucket, key=lambda item: str(item.get(name_key) or "")),
            }
        )
    for keys, bucket in sorted(
        exam_bucket.items(),
        key=lambda pair: (
            min(level_rank(item.get(level_key)) for item in pair[1]),
            tuple(exam_rank(key) for key in pair[0]),
        ),
    ):
        groups.append(
            {
                "key": "-".join(keys),
                "label": " и ".join(EXAM_LABELS.get(key, key.upper()) for key in keys),
                "kind": "exam",
                "level": "",
                "items": sorted(bucket, key=lambda item: str(item.get(name_key) or "")),
            }
        )
    if without:
        groups.append(
            {
                "key": "no-level",
                "label": "Все уровни",
                "kind": "level",
                "level": "",
                "items": sorted(without, key=lambda item: str(item.get(name_key) or "")),
            }
        )
    return groups


def level_chips(values):
    """Уникальные уровни для фильтра-чипов, в порядке от слабых к сильным."""
    seen = []
    for value in values:
        for level in levels_in(value):
            if level not in seen:
                seen.append(level)
    return sorted(seen, key=LEVEL_ORDER.index)
