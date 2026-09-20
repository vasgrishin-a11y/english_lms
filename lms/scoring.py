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
        lines.append(f"{index}. [{mark}] {prompt} — {detail.get('points', 0)}/{question.points}")
    return "\n".join(lines)[:20000]
