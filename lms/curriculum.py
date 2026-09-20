"""Построение карты курса и агрегатов прогресса.

Все функции читают данные фиксированным числом запросов. Состояние работ
ученика добавляется аннотациями к одному запросу по заданиям, а не отдельным
словарём попыток: каталог остаётся в бюджете ≤6 SQL (проверяется в CI).
"""

from django.db.models import Avg, Count, OuterRef, Q, Subquery
from django.utils import timezone

from .models import Assignment, Block, FlashcardDeck, Submission, Topic

WAITING_STATUSES = [Submission.Status.SUBMITTED, Submission.Status.IN_REVIEW]


def visible_assignments(at=None):
    """Задания, которые видит ученик: активные, опубликованные, срок публикации наступил."""
    return Assignment.objects.visible(at)


def annotate_student_states(queryset, student):
    """Последняя попытка ученика по каждому заданию — подзапросы в одном SELECT.

    Сначала одним подзапросом находим id свежей попытки, затем забираем из неё
    нужные поля: вложенная корреляция через OuterRef остаётся однозначной, а
    число запросов не зависит от размера курса.
    """
    latest_pk = Subquery(
        Submission.objects.filter(student=student, assignment=OuterRef("pk"))
        .order_by("-version")
        .values("pk")[:1]
    )
    attempt = Submission.objects.filter(pk=OuterRef("latest_attempt_id"))
    return queryset.annotate(latest_attempt_id=latest_pk).annotate(
        latest_status=Subquery(attempt.values("status")[:1]),
        latest_grade=Subquery(attempt.values("feedback__grade")[:1]),
        latest_version=Subquery(attempt.values("version")[:1]),
        latest_max_points=Subquery(attempt.values("max_points_snapshot")[:1]),
        latest_submitted_at=Subquery(attempt.values("submitted_at")[:1]),
        latest_deadline=Subquery(attempt.values("deadline_snapshot")[:1]),
    )


def state_of(assignment):
    """Состояние аннотированного задания в виде простого словаря."""
    status = getattr(assignment, "latest_status", None)
    submitted_at = getattr(assignment, "latest_submitted_at", None)
    deadline = getattr(assignment, "latest_deadline", None)
    return {
        "status": status,
        "grade": getattr(assignment, "latest_grade", None),
        "version": getattr(assignment, "latest_version", None) or 0,
        "max_points": getattr(assignment, "latest_max_points", None) or assignment.max_points,
        "is_late": bool(deadline and submitted_at and submitted_at > deadline),
        "attempt_id": getattr(assignment, "latest_attempt_id", None),
    }


def ordered_blocks(active_only=False):
    blocks = Block.objects.all()
    if active_only:
        blocks = blocks.filter(is_active=True)
    return blocks.order_by("order", "name", "pk")


def ordered_topics(active_only=False):
    topics = Topic.objects.select_related("block")
    if active_only:
        topics = topics.filter(is_active=True, block__is_active=True)
    return topics.order_by("block__order", "block__name", "order", "title", "pk")


def teacher_assignment_stats():
    """Один запрос: сколько работ ждёт, на доработке, проверено, средний балл по заданию."""
    rows = (
        Submission.objects.latest_attempts()
        .values("assignment_id")
        .annotate(
            waiting=Count("pk", filter=Q(status__in=WAITING_STATUSES)),
            revision=Count("pk", filter=Q(status=Submission.Status.NEEDS_REVISION)),
            checked=Count("pk", filter=Q(status=Submission.Status.CHECKED)),
            students=Count("student_id", distinct=True),
            average=Avg("feedback__grade"),
        )
    )
    return {row["assignment_id"]: row for row in rows}


def _percent(done, total):
    return int(round(100 * done / total)) if total else 0


def course_tree(*, student=None, query=None, teacher_view=False, include_skills=False, at=None):
    """Единое дерево курса для учителя и ученика.

    Ученик: только опубликованное, с состоянием своих работ одним аннотированным запросом.
    Учитель: всё, включая черновики, со счётчиками работ по каждому заданию.
    """
    blocks = list(ordered_blocks(active_only=not teacher_view))
    topics = list(ordered_topics(active_only=not teacher_view))
    assignments_queryset = Assignment.objects.select_related("topic")
    if student is not None:
        assignments_queryset = annotate_student_states(
            Assignment.objects.visible(at).select_related("topic"), student
        )
    elif not teacher_view:
        assignments_queryset = assignments_queryset.visible(at)
    if include_skills:
        assignments_queryset = assignments_queryset.prefetch_related("skills")
    assignments = list(assignments_queryset.order_by("topic__order", "order", "pk"))
    decks = FlashcardDeck.objects.select_related("topic__block")
    if not teacher_view:
        decks = decks.filter(is_active=True, topic__is_active=True, topic__block__is_active=True)
    decks = list(decks.order_by("topic__order", "order", "pk"))
    stats = teacher_assignment_stats() if teacher_view else None
    return _assemble(
        blocks,
        topics,
        assignments,
        decks,
        stats=stats,
        student_view=student is not None,
        query=query,
    )


def _matches(query, *values):
    if not query:
        return True
    needle = query.casefold()
    return any(needle in str(value or "").casefold() for value in values)


def _assemble(blocks, topics, assignments, decks, *, stats, student_view, query):
    topics_by_block = {}
    for topic in topics:
        topics_by_block.setdefault(topic.block_id, []).append(topic)
    assignments_by_topic = {}
    for assignment in assignments:
        assignments_by_topic.setdefault(assignment.topic_id, []).append(assignment)
    decks_by_topic = {}
    for deck in decks:
        decks_by_topic.setdefault(deck.topic_id, []).append(deck)

    result = []
    for block in blocks:
        block_topics = []
        block_total = block_done = block_waiting = block_count = block_revision = 0
        for topic in topics_by_block.get(block.pk, []):
            entries = []
            topic_total = topic_done = topic_waiting = topic_revision = 0
            for assignment in assignments_by_topic.get(topic.pk, []):
                if not _matches(query, assignment.title, assignment.description):
                    continue
                entry = {"assignment": assignment, "state": None, "stats": None}
                if student_view:
                    state = state_of(assignment)
                    entry["state"] = state
                    topic_total += 1
                    if state["status"] == Submission.Status.CHECKED:
                        topic_done += 1
                    elif state["status"] == Submission.Status.NEEDS_REVISION:
                        topic_revision += 1
                if stats is not None:
                    row = stats.get(assignment.pk, {})
                    entry["stats"] = {
                        "waiting": row.get("waiting", 0),
                        "revision": row.get("revision", 0),
                        "checked": row.get("checked", 0),
                        "students": row.get("students", 0),
                        "average": row.get("average"),
                    }
                    topic_waiting += row.get("waiting", 0)
                    topic_revision += row.get("revision", 0)
                entries.append(entry)
            topic_decks = decks_by_topic.get(topic.pk, [])
            if query and not entries and not _matches(query, topic.title, block.name):
                continue
            if not entries and not topic_decks and query:
                continue
            block_total += topic_total
            block_done += topic_done
            block_waiting += topic_waiting
            block_revision += topic_revision
            block_count += len(entries)
            block_topics.append(
                {
                    "topic": topic,
                    "assignments": entries,
                    "decks": topic_decks,
                    "total": topic_total,
                    "done": topic_done,
                    "waiting": topic_waiting,
                    "revision": topic_revision,
                    "progress": _percent(topic_done, topic_total),
                }
            )
        if query and not block_topics:
            continue
        result.append(
            {
                "block": block,
                "topics": block_topics,
                "total": block_total,
                "done": block_done,
                "waiting": block_waiting,
                "revision": block_revision,
                "assignments": block_count,
                "progress": _percent(block_done, block_total),
            }
        )
    totals = {
        "blocks": len(result),
        "topics": sum(len(item["topics"]) for item in result),
        "assignments": sum(item["assignments"] for item in result),
        "done": sum(item["done"] for item in result),
        "total": sum(item["total"] for item in result),
        "waiting": sum(item["waiting"] for item in result),
        "revision": sum(item["revision"] for item in result),
        "progress": _percent(
            sum(item["done"] for item in result), sum(item["total"] for item in result)
        ),
    }
    if not student_view:
        totals["drafts"] = sum(
            1
            for item in result
            for topic_item in item["topics"]
            for entry in topic_item["assignments"]
            if not entry["assignment"].is_visible
        )
    return result, totals


def queue_counts():
    """Счётчики для вкладок очереди проверки — один агрегирующий запрос."""
    return Submission.objects.latest_attempts().aggregate(
        waiting=Count("pk", filter=Q(status__in=WAITING_STATUSES)),
        revision=Count("pk", filter=Q(status=Submission.Status.NEEDS_REVISION)),
        checked=Count("pk", filter=Q(status=Submission.Status.CHECKED)),
        total=Count("pk"),
    )


def teacher_overview():
    """Сводка для домашней страницы преподавателя: 4 запроса."""
    counts = queue_counts()
    students = Submission.objects.values("student_id").distinct().count()
    curriculum = Assignment.objects.aggregate(
        total=Count("pk"),
        drafts=Count("pk", filter=Q(status=Assignment.Publication.DRAFT)),
        quizzes=Count("pk", filter=Q(assignment_type=Assignment.Type.QUIZ)),
        overdue=Count("pk", filter=Q(deadline__lt=timezone.now(), is_active=True)),
        topics=Count("topic", distinct=True),
        blocks=Count("topic__block", distinct=True),
    )
    averages = Submission.objects.latest_attempts().aggregate(
        avg=Avg("feedback__grade"), graded=Count("feedback__grade")
    )
    return {
        "queue": counts,
        "students": students,
        "curriculum": curriculum,
        "average": averages["avg"],
        "graded": averages["graded"],
    }


def gradebook(block=None, topic=None):
    """Матрица журнала «ученики × задания»: 4 запроса независимо от размера."""
    from django.contrib.auth import get_user_model

    from .models import Profile, Topic

    assignments = visible_assignments().select_related("topic__block")
    if topic:
        assignments = assignments.filter(topic=topic)
    if block:
        assignments = assignments.filter(topic__block=block)
    assignments = list(assignments.order_by("topic__block__order", "topic__order", "order", "pk"))
    User = get_user_model()
    students = list(
        User.objects.filter(profile__role=Profile.Role.STUDENT, is_active=True)
        .select_related("profile")
        .order_by("last_name", "first_name", "username")
    )
    cells = {}
    attempts = (
        Submission.objects.filter(student__in=students, assignment__in=assignments)
        .latest_attempts()
        .select_related("feedback")
        .order_by()
    )
    for attempt in attempts:
        cells[(attempt.student_id, attempt.assignment_id)] = attempt
    rows = []
    for student in students:
        row_cells = []
        graded = possible = submitted = checked = 0
        for assignment in assignments:
            attempt = cells.get((student.pk, assignment.pk))
            feedback = getattr(attempt, "feedback", None) if attempt else None
            grade = feedback.grade if feedback else None
            maximum = attempt.max_points_snapshot if attempt else assignment.max_points
            if attempt:
                submitted += 1
            if attempt and attempt.status == Submission.Status.CHECKED:
                checked += 1
            if grade is not None:
                graded += grade
                possible += maximum or 0
            row_cells.append(
                {
                    "assignment": assignment,
                    "attempt": attempt,
                    "grade": grade,
                    "maximum": maximum,
                    "status": attempt.status if attempt else None,
                    "percent": _percent(grade, maximum) if grade is not None else None,
                }
            )
        rows.append(
            {
                "student": student,
                "cells": row_cells,
                "scored": graded,
                "possible": possible,
                "percent": _percent(graded, possible),
                "submitted": submitted,
                "checked": checked,
                "total": len(assignments),
            }
        )
    topics = Topic.objects.select_related("block").order_by("block__order", "order", "title")
    return {
        "assignments": assignments,
        "rows": rows,
        "blocks": list(ordered_blocks()),
        "topics": list(topics),
    }
