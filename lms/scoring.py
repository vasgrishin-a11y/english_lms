"""Автоматическая проверка тестов.

Чистые функции без обращений к БД: вход — объекты вопросов с загруженными
вариантами и словарь ответов, выход — баллы и разбор по каждому вопросу.
Частичный балл начисляется только там, где это методически честно
(несколько ответов и соответствие); одиночный выбор и пропуск — всё или ничего.
"""

import re
import unicodedata

PUNCTUATION = re.compile(r"[\s.,!?;:\"'’“”()\[\]]+")


def normalize_gap(value):
    """Сравнение вписанного ответа без учёта регистра, пунктуации и лишних пробелов."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.replace("ё", "е")
    text = PUNCTUATION.sub(" ", text).strip()
    return re.sub(r"\s+", " ", text)


def _choice_ids(question):
    return {str(choice.pk) for choice in question.choices.all()}


def score_question(question, answer):
    """Вернуть {'ratio', 'points', 'correct', 'expected', 'given'} для одного вопроса."""
    choices = list(question.choices.all())
    correct_ids = {str(choice.pk) for choice in choices if choice.is_correct}
    points = int(question.points or 0)
    expected = ""
    given = answer

    if question.kind == "mcq":
        expected = next((c.text for c in choices if str(c.pk) in correct_ids), "")
        ratio = 1.0 if str(answer or "") in correct_ids and correct_ids else 0.0
    elif question.kind == "multi":
        expected = ", ".join(c.text for c in choices if str(c.pk) in correct_ids)
        selected = {str(value) for value in (answer or []) if str(value)}
        selected &= _choice_ids(question)
        given = sorted(selected)
        if correct_ids:
            hits = len(selected & correct_ids)
            misses = len(selected - correct_ids)
            ratio = max(0.0, (hits - misses)) / len(correct_ids)
        else:
            ratio = 0.0
    elif question.kind == "gap":
        accepted = {normalize_gap(choice.text) for choice in choices if choice.is_correct}
        if not accepted:
            accepted = {normalize_gap(choice.text) for choice in choices}
        accepted.discard("")
        expected = " / ".join(sorted(accepted))
        ratio = 1.0 if normalize_gap(answer) in accepted else 0.0
    elif question.kind == "match":
        mapping = answer if isinstance(answer, dict) else {}
        pairs = [choice for choice in choices if choice.match_text]
        expected = ", ".join(f"{c.text} → {c.match_text}" for c in pairs)
        given = {str(key): str(value) for key, value in mapping.items()}
        if pairs:
            hits = sum(1 for choice in pairs if given.get(str(choice.pk)) == str(choice.pk))
            ratio = hits / len(pairs)
        else:
            ratio = 0.0
    elif question.kind == "order":
        # Эталон — порядок вариантов, заданный преподавателем; частичный балл
        # за каждое слово, стоящее на своём месте (как в позиционном сопоставлении).
        expected = " ".join(c.text for c in choices)
        given = [str(value) for value in (answer or []) if str(value)]
        given = [pk for pk in given if pk in _choice_ids(question)]
        if choices:
            hits = sum(
                1
                for position, choice in enumerate(choices)
                if position < len(given) and given[position] == str(choice.pk)
            )
            ratio = hits / len(choices)
        else:
            ratio = 0.0
    elif question.kind == "sort":
        mapping = {str(key): str(value) for key, value in (answer or {}).items()}
        pairs = [choice for choice in choices if choice.match_text]
        expected = ", ".join(f"{c.text} → {c.match_text}" for c in pairs)
        given = mapping
        if pairs:
            hits = sum(1 for choice in pairs if mapping.get(str(choice.pk)) == choice.match_text)
            ratio = hits / len(pairs)
        else:
            ratio = 0.0
    elif question.kind == "spell":
        accepted = {normalize_gap(choice.text) for choice in choices if choice.is_correct}
        if not accepted:
            accepted = {normalize_gap(choice.text) for choice in choices}
        accepted.discard("")
        expected = " / ".join(sorted(accepted))
        ratio = 1.0 if normalize_gap(answer) in accepted else 0.0
    else:  # неизвестный тип не даёт баллов, но и не роняет проверку
        ratio = 0.0

    awarded = int(round(points * ratio))
    return {
        "ratio": round(ratio, 4),
        "points": awarded,
        "correct": ratio >= 1.0,
        "partially": 0 < ratio < 1,
        "expected": expected,
        "given": given,
    }


def answer_parts(question, given):
    """Какие части составного ответа верны — для подсветки после неудачной попытки.

    Возвращает словарь ``{"rows": {choice_pk: bool}}`` для соответствия и
    сортировки, ``{"positions": [bool, ...]}`` для порядка слов и
    ``{"hits": n, "extra": m, "total": k}`` для выбора нескольких вариантов.
    Для вопросов «всё или ничего» частей нет — пустой словарь.
    """
    choices = list(question.choices.all())
    if question.kind == "match":
        mapping = given if isinstance(given, dict) else {}
        return {
            "rows": {
                str(c.pk): mapping.get(str(c.pk)) == str(c.pk) for c in choices if c.match_text
            }
        }
    if question.kind == "sort":
        mapping = given if isinstance(given, dict) else {}
        return {
            "rows": {
                str(c.pk): mapping.get(str(c.pk)) == c.match_text for c in choices if c.match_text
            }
        }
    if question.kind == "order":
        order = list(given or [])
        return {
            "positions": [
                index < len(order) and order[index] == str(choice.pk)
                for index, choice in enumerate(choices)
            ]
        }
    if question.kind == "multi":
        correct = {str(c.pk) for c in choices if c.is_correct}
        selected = {str(value) for value in (given or [])}
        return {
            "hits": len(selected & correct),
            "extra": len(selected - correct),
            "total": len(correct),
        }
    return {}


def describe_answer(question, given):
    """Ответ ученика словами — для истории попыток у преподавателя и ученика."""
    choices = {str(c.pk): c for c in question.choices.all()}
    if question.kind == "mcq":
        choice = choices.get(str(given or ""))
        return choice.text if choice else "—"
    if question.kind == "multi":
        texts = [choices[str(pk)].text for pk in (given or []) if str(pk) in choices]
        return ", ".join(texts) or "—"
    if question.kind == "match":
        mapping = given if isinstance(given, dict) else {}
        pairs = []
        for pk, choice in choices.items():
            if not choice.match_text:
                continue
            picked = choices.get(str(mapping.get(pk, "")))
            pairs.append(f"{choice.text} → {picked.match_text if picked else '—'}")
        return "; ".join(pairs) or "—"
    if question.kind == "sort":
        mapping = given if isinstance(given, dict) else {}
        pairs = [
            f"{choice.text} → {mapping.get(pk) or '—'}"
            for pk, choice in choices.items()
            if choice.match_text
        ]
        return "; ".join(pairs) or "—"
    if question.kind == "order":
        words = [choices[str(pk)].text for pk in (given or []) if str(pk) in choices]
        return " ".join(words) or "—"
    text = " ".join(str(given or "").split())
    return text[:500] or "—"


def is_blank_answer(question, given):
    """Пустой ответ не расходует попытку: ученик просто забыл заполнить пункт."""
    if question.kind in ("match", "sort"):
        return not any(str(value).strip() for value in (given or {}).values())
    if question.kind in ("multi", "order"):
        return not [value for value in (given or []) if str(value).strip()]
    return not str(given or "").strip()


def score_quiz(questions, answers):
    """Полный результат теста: баллы, максимум, число верных ответов и разбор."""
    details = {}
    score = 0
    max_score = 0
    correct_count = 0
    for question in questions:
        result = score_question(question, (answers or {}).get(str(question.pk)))
        details[str(question.pk)] = result
        score += result["points"]
        max_score += int(question.points or 0)
        correct_count += 1 if result["correct"] else 0
    return {
        "score": score,
        "max_score": max_score,
        "correct_count": correct_count,
        "total_count": len(details),
        "details": details,
    }


def answers_summary(questions, result):
    """Человекочитаемая сводка для неизменяемого текстового поля попытки."""
    lines = [
        "Автоматическая проверка теста.",
        f"Верных ответов: {result['correct_count']} из {result['total_count']}.",
        f"Баллы: {result['score']} из {result['max_score']}.",
        "",
    ]
    for index, question in enumerate(questions, start=1):
        detail = result["details"].get(str(question.pk), {})
        mark = "✓" if detail.get("correct") else "~" if detail.get("partially") else "✗"
        prompt = " ".join(str(question.text).split())[:120]
        if getattr(question, "is_manual", False):
            lines.append(f"{index}. [?] {prompt} — проверяет преподаватель, из {question.points}")
            continue
        lines.append(f"{index}. [{mark}] {prompt} — {detail.get('points', 0)}/{question.points}")
    return "\n".join(lines)[:20000]
