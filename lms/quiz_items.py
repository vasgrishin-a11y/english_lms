"""Состояние пунктов пошагового задания для экранов ученика и преподавателя.

Чистые функции: вход — вопросы с загруженными вариантами и ответы на пункты
(QuestionResponse), выход — словари, удобные шаблону. Никаких запросов к БД.
"""

from .models import QuestionResponse
from .scoring import score_question

State = QuestionResponse.State


def _selected(question, given):
    """Что показать выбранным в поле пункта после неудачной попытки."""
    if question.kind == "mcq":
        return {"choice": str(given or "")}
    if question.kind == "multi":
        return {"choices": [str(value) for value in (given or [])]}
    if question.kind in ("match", "sort"):
        return {"mapping": {str(k): str(v) for k, v in (given or {}).items()}}
    if question.kind in ("gap", "spell"):
        return {"text": str(given or "")}
    return {}


def _legacy_tries(detail):
    """Сдачи до пошаговой проверки: одна попытка из разбора QuizAttempt."""
    if not isinstance(detail, dict) or "given" not in detail:
        return [], "new"
    correct = bool(detail.get("correct"))
    return (
        [
            {
                "given": detail.get("given"),
                "display": "",
                "correct": correct,
                "ratio": detail.get("ratio", 1.0 if correct else 0.0),
                "parts": {},
            }
        ],
        State.CORRECT if correct else State.FAILED,
    )


def build_item(assignment, question, response, *, closed_round=False, legacy=None):
    tries = list(response.tries or []) if response else []
    state = response.state if response else "new"
    legacy_points = None
    if response is None and closed_round and legacy:
        detail = legacy.get(str(question.pk))
        tries, state = _legacy_tries(detail)
        legacy_points = (detail or {}).get("points", 0)
    if state == State.OPEN and not tries and not (response and response.text_answer):
        state = "new"
    max_tries = assignment.tries_per_item
    # Контрольная: итог пункта скрыт, пока задание не завершено (нет ✓/✗ и ответа).
    exam_hidden = (
        assignment.exam_mode
        and not closed_round
        and not question.is_manual
        and state in (State.CORRECT, State.FAILED)
    )
    if exam_hidden:
        state = State.ANSWERED
    graded = [item for item in tries if item.get("correct") is not None]
    last = graded[-1] if graded else None
    manual = question.is_manual
    closed = state in (State.CORRECT, State.FAILED) or (closed_round and not manual) or exam_hidden
    reveal = closed and not manual and not exam_hidden
    item = {
        "question": question,
        "file_field": f"q_{question.pk}_file",
        "response": response,
        "state": state,
        "manual": manual,
        "exam": assignment.exam_mode,
        "exam_hidden": exam_hidden,
        "tries": [] if exam_hidden else tries,
        "wrong_tries": []
        if exam_hidden
        else [entry for entry in graded if not entry.get("correct")],
        "tries_used": len(graded),
        "tries_left": max(0, max_tries - len(graded)),
        "max_tries": max_tries,
        "last": last,
        "just_wrong": not assignment.exam_mode
        and state == State.OPEN
        and bool(last)
        and not last.get("correct"),
        "editable": not closed_round
        and (state in ("new", State.OPEN) or (manual and state == State.ANSWERED)),
        "closed": closed or (manual and state == State.ANSWERED),
        "reveal": reveal,
        "expected": score_question(question, None)["expected"] if reveal else "",
        "selected": _selected(question, last.get("given")) if last and not exam_hidden else {},
        "wrong_choices": [] if exam_hidden else _wrong_choices(question, graded),
        "parts": (last or {}).get("parts") or {},
        "points": 0 if exam_hidden else (response.points if response else (legacy_points or 0)),
        "text_answer": response.text_answer if response else "",
        "file_answer": response.file_answer if response and response.file_answer else None,
        "teacher_points": response.teacher_points if response else None,
    }
    item["row_marks"] = item["parts"].get("rows", {}) if item["just_wrong"] else {}
    positions = item["parts"].get("positions") or []
    item["order_hits"] = sum(1 for flag in positions if flag)
    item["order_total"] = len(positions)
    return item


def _wrong_choices(question, graded):
    """Варианты, уже выбранные в неверных попытках одиночного выбора, — зачёркиваем."""
    if question.kind != "mcq":
        return []
    return [str(entry.get("given")) for entry in graded if not entry.get("correct")]


def build_items(assignment, questions, responses, *, closed_round=False, legacy=None):
    items = []
    for number, question in enumerate(questions, start=1):
        item = build_item(
            assignment,
            question,
            responses.get(question.pk),
            closed_round=closed_round,
            legacy=legacy,
        )
        item["number"] = number
        items.append(item)
    return items


def progress_of(items):
    total = len(items)
    correct = sum(1 for item in items if item["state"] == State.CORRECT)
    failed = sum(1 for item in items if item["state"] == State.FAILED)
    answered = sum(1 for item in items if item["manual"] and item["state"] == State.ANSWERED)
    # Контрольная: пункт с автопроверкой отвечен, но итог скрыт до завершения.
    sealed = sum(1 for item in items if item.get("exam_hidden"))
    done = correct + failed + answered + sealed
    first_try = sum(
        1 for item in items if item["state"] == State.CORRECT and item["tries_used"] == 1
    )
    manual = sum(1 for item in items if item["manual"])
    auto_points = sum(item["points"] for item in items if not item["manual"])
    auto_max = sum(int(item["question"].points or 0) for item in items if not item["manual"])
    return {
        "total": total,
        "manual": manual,
        "auto_total": total - manual,
        "auto_points": auto_points,
        "auto_max": auto_max,
        "done": done,
        "left": total - done,
        "correct": correct,
        "failed": failed,
        "answered": answered,
        "sealed": sealed,
        "first_try": first_try,
        "percent": int(round(100 * done / total)) if total else 0,
        "complete": bool(total) and done == total,
    }
