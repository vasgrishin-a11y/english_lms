"""Построение карты курса и агрегатов прогресса.

Все функции читают данные фиксированным числом запросов. Состояние работ
ученика добавляется аннотациями к одному запросу по заданиям, а не отдельным
словарём попыток: каталог остаётся в бюджете ≤6 SQL (проверяется в CI).
"""

from django.db.models import Avg, Count, OuterRef, Q, Subquery
from django.utils import timezone

from . import audience
from .models import Assignment, Block, Chapter, Submission, Topic

WAITING_STATUSES = [Submission.Status.SUBMITTED, Submission.Status.IN_REVIEW]


def visible_assignments(student=None, at=None):
    """Задания, которые видит ученик: активные, опубликованные, назначенные ему (или общие)."""
    return Assignment.objects.visible(user=student, at=at)


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


def ordered_chapters(active_only=False):
    chapters = Chapter.objects.select_related("block")
    if active_only:
        chapters = chapters.filter(is_active=True, block__is_active=True)
    return chapters.order_by("block__order", "block__name", "order", "title", "pk")


def ordered_topics(active_only=False):
    topics = Topic.objects.select_related("block", "chapter")
    if active_only:
        topics = topics.filter(is_active=True, chapter__is_active=True, block__is_active=True)
    return topics.order_by(
        "block__order", "block__name", "chapter__order", "chapter__title", "order", "title", "pk"
    )


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


def course_tree(
    *,
    student=None,
    query=None,
    teacher_view=False,
    include_archived=False,
    include_skills=False,
    at=None,
):
    """Единое дерево курса для учителя и ученика.

    Ученик: только опубликованное, с состоянием своих работ одним аннотированным запросом.
    Учитель: активный курс и черновики, со счётчиками работ по каждому заданию.
    Архив намеренно вынесен в отдельный экран и не смешивается с рабочей картой.
    """
    show_active_only = not teacher_view or not include_archived
    blocks_queryset = ordered_blocks(active_only=show_active_only)
    chapters_queryset = ordered_chapters(active_only=show_active_only)
    topics_queryset = ordered_topics(active_only=show_active_only)
    assignments_queryset = Assignment.objects.select_related("topic")
    if teacher_view:
        # Бейджи «кому доступно» на карте курса — без лишних запросов.
        blocks_queryset = blocks_queryset.prefetch_related("groups", "students")
        chapters_queryset = chapters_queryset.prefetch_related("groups", "students")
        topics_queryset = topics_queryset.prefetch_related("groups", "students")
        assignments_queryset = assignments_queryset.prefetch_related("groups", "assigned_students")
    blocks = list(blocks_queryset)
    chapters = list(chapters_queryset)
    topics = list(topics_queryset)
    if student is not None:
        assignments_queryset = annotate_student_states(
            Assignment.objects.visible(user=student, at=at).select_related("topic"), student
        )
    elif not teacher_view or not include_archived:
        assignments_queryset = assignments_queryset.filter(
            is_active=True,
            topic__is_active=True,
            topic__chapter__is_active=True,
            topic__block__is_active=True,
        )
    if include_skills:
        assignments_queryset = assignments_queryset.prefetch_related("skills")
    assignments = list(
        assignments_queryset.annotate(card_total=Count("cards")).order_by(
            "topic__order", "order", "pk"
        )
    )
    stats = teacher_assignment_stats() if teacher_view else None
    return _assemble(
        blocks,
        chapters,
        topics,
        assignments,
        stats=stats,
        student_view=student is not None,
        query=query,
        with_audience=teacher_view,
    )


def _matches(query, *values):
    if not query:
        return True
    needle = query.casefold()
    return any(needle in str(value or "").casefold() for value in values)


def _topic_entry(topic, assignments, *, stats, student_view, query, parent_audience=None):
    """Собрать узел темы: задания с состоянием/статистикой и счётчики темы."""
    entries = []
    topic_total = topic_done = topic_waiting = topic_revision = 0
    flashcard_count = 0
    topic_audience = (
        audience.accumulate(topic, parent_audience) if parent_audience is not None else None
    )
    for assignment in assignments:
        if not _matches(query, assignment.title, assignment.description):
            continue
        entry = {"assignment": assignment, "state": None, "stats": None, "audience": None}
        if topic_audience is not None:
            entry["audience"] = audience.accumulate(assignment, topic_audience)
        if assignment.is_flashcards:
            flashcard_count += 1
        if student_view and not assignment.is_flashcards:
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
    return {
        "topic": topic,
        "audience": topic_audience,
        "assignments": entries,
        "flashcards": flashcard_count,
        "total": topic_total,
        "done": topic_done,
        "waiting": topic_waiting,
        "revision": topic_revision,
        "progress": _percent(topic_done, topic_total),
    }


def _chapter_entry(chapter, topic_items, chapter_audience=None):
    """Узел главы: темы и суммы по ним (для карты курса и прогресса ученика)."""
    total = sum(item["total"] for item in topic_items)
    done = sum(item["done"] for item in topic_items)
    return {
        "chapter": chapter,
        "audience": chapter_audience,
        "topics": topic_items,
        "assignments": sum(len(item["assignments"]) for item in topic_items),
        "flashcards": sum(item["flashcards"] for item in topic_items),
        "total": total,
        "done": done,
        "waiting": sum(item["waiting"] for item in topic_items),
        "revision": sum(item["revision"] for item in topic_items),
        "progress": _percent(done, total),
    }


def _assemble(
    blocks, chapters, topics, assignments, *, stats, student_view, query, with_audience=False
):
    """Класс → главы → темы → задания.

    У каждого класса два представления одних и тех же тем: ``chapters`` —
    вложенное (карта курса учителя), ``topics`` — плоский список в порядке
    глав (прогресс ученика, счётчики, старые шаблоны). Тема без активной
    главы наружу не попадает — как и тема без активного класса.
    """
    chapters_by_block = {}
    for chapter in chapters:
        chapters_by_block.setdefault(chapter.block_id, []).append(chapter)
    topics_by_chapter = {}
    for topic in topics:
        topics_by_chapter.setdefault(topic.chapter_id, []).append(topic)
    assignments_by_topic = {}
    for assignment in assignments:
        assignments_by_topic.setdefault(assignment.topic_id, []).append(assignment)

    result = []
    for block in blocks:
        block_chapters = []
        block_topics = []
        block_audience = audience.accumulate(block) if with_audience else None
        for chapter in chapters_by_block.get(block.pk, []):
            topic_items = []
            chapter_audience = (
                audience.accumulate(chapter, block_audience) if with_audience else None
            )
            for topic in topics_by_chapter.get(chapter.pk, []):
                item = _topic_entry(
                    topic,
                    assignments_by_topic.get(topic.pk, []),
                    stats=stats,
                    student_view=student_view,
                    query=query,
                    parent_audience=chapter_audience,
                )
                if (
                    query
                    and not item["assignments"]
                    and not _matches(query, topic.title, chapter.title, block.name)
                ):
                    continue
                topic_items.append(item)
            if query and not topic_items and not _matches(query, chapter.title, block.name):
                continue
            block_chapters.append(_chapter_entry(chapter, topic_items, chapter_audience))
            block_topics.extend(topic_items)
        if query and not block_chapters:
            continue
        block_total = sum(item["total"] for item in block_topics)
        block_done = sum(item["done"] for item in block_topics)
        result.append(
            {
                "block": block,
                "audience": block_audience,
                "chapters": block_chapters,
                "topics": block_topics,
                "total": block_total,
                "done": block_done,
                "waiting": sum(item["waiting"] for item in block_topics),
                "revision": sum(item["revision"] for item in block_topics),
                "assignments": sum(len(item["assignments"]) for item in block_topics),
                "flashcards": sum(item["flashcards"] for item in block_topics),
                "progress": _percent(block_done, block_total),
            }
        )
    totals = {
        "blocks": len(result),
        "chapters": sum(len(item["chapters"]) for item in result),
        "topics": sum(len(item["topics"]) for item in result),
        "assignments": sum(item["assignments"] for item in result),
        "flashcards": sum(item["flashcards"] for item in result),
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
        chapters=Count("topic__chapter", distinct=True),
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

    assignments = (
        visible_assignments()
        .select_related("topic__block", "topic__chapter")
        .prefetch_related(*audience.ASSIGNMENT_PREFETCH)
    )
    if topic:
        assignments = assignments.filter(topic=topic)
    if block:
        assignments = assignments.filter(topic__block=block)
    assignments = list(
        assignments.order_by(
            "topic__block__order", "topic__chapter__order", "topic__order", "order", "pk"
        )
    )
    expected = audience.expected_ids_map(assignments)
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
        graded = possible = submitted = checked = assigned_total = 0
        for assignment in assignments:
            attempt = cells.get((student.pk, assignment.pk))
            allowed = expected[assignment.pk]
            assigned = allowed is None or student.pk in allowed or attempt is not None
            if assigned:
                assigned_total += 1
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
                    "assigned": assigned,
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
                "total": assigned_total,
            }
        )
    topics = Topic.objects.select_related("block", "chapter").order_by(
        "block__order", "chapter__order", "order", "title"
    )
    return {
        "assignments": assignments,
        "rows": rows,
        "blocks": list(ordered_blocks()),
        "topics": list(topics),
    }
