"""Контекстные подсказки поиска: только объекты, которые уже есть в БД."""

from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse

from .models import Assignment, Block, Chapter, Group, Profile, Topic

User = get_user_model()

LIMIT = 8


def _label(user):
    return user.get_full_name() or user.username


def teacher_suggest(scope, query):
    query = (query or "").strip()[:80]
    if len(query) < 1:
        return []
    if scope == "students":
        return _students(query)
    if scope == "groups":
        return _groups(query)
    if scope == "curriculum":
        return _curriculum(query, teacher=True)
    if scope == "review":
        return _students(query, limit=4) + _assignments(query, teacher=True, limit=4)
    if scope == "topics":
        return _topics(query, teacher=True)
    return []


def student_suggest(scope, query, user):
    query = (query or "").strip()[:80]
    if len(query) < 1:
        return []
    if scope in {"curriculum", "course"}:
        return _curriculum(query, teacher=False, student=user)
    return []


def _students(query, limit=LIMIT):
    people = (
        User.objects.filter(profile__role=Profile.Role.STUDENT, is_active=True)
        .filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(profile__telegram__icontains=query)
        )
        .select_related("profile")
        .order_by("last_name", "first_name", "username")[:limit]
    )
    items = []
    for user in people:
        telegram = user.profile.telegram if getattr(user, "profile", None) else ""
        hint = f"@{user.username}"
        if telegram:
            hint = f"{hint} · {telegram}"
        items.append(
            {
                "type": "student",
                "label": _label(user),
                "hint": hint,
                "url": reverse("teacher_student_detail", args=[user.pk]),
                "value": str(user.pk),
            }
        )
    return items


def _groups(query, limit=LIMIT):
    groups = Group.objects.filter(
        Q(name__icontains=query) | Q(description__icontains=query) | Q(slug__icontains=query)
    ).order_by("name")[:limit]
    return [
        {
            "type": "group",
            "label": group.name,
            "hint": group.get_cefr_level_display() if group.cefr_level else "группа",
            "url": reverse("teacher_group_edit", args=[group.pk]),
            "value": str(group.pk),
        }
        for group in groups
    ]


def _chapters(query, *, teacher=True, student=None, limit=LIMIT):
    chapters = Chapter.objects.select_related("block").filter(
        Q(title__icontains=query) | Q(block__name__icontains=query)
    )
    if teacher:
        chapters = chapters.filter(is_active=True, block__is_active=True)
    else:
        visible = Assignment.objects.visible(user=student).values_list(
            "topic__chapter_id", flat=True
        )
        chapters = chapters.filter(pk__in=visible, is_active=True, block__is_active=True)
    chapters = chapters.order_by("block__order", "order", "title")[:limit]
    items = []
    for chapter in chapters:
        url = (
            reverse("teacher_curriculum") + f"#chapter-{chapter.pk}"
            if teacher
            else reverse("student_assignments") + "?" + urlencode({"q": chapter.title})
        )
        items.append(
            {
                "type": "chapter",
                "label": chapter.title,
                "hint": f"{chapter.block.name} · глава",
                "url": url,
                "value": str(chapter.pk),
            }
        )
    return items


def _topics(query, *, teacher=True, student=None, limit=LIMIT):
    topics = Topic.objects.select_related("block", "chapter").filter(
        Q(title__icontains=query)
        | Q(chapter__title__icontains=query)
        | Q(block__name__icontains=query)
    )
    if teacher:
        topics = topics.filter(is_active=True, chapter__is_active=True, block__is_active=True)
    else:
        visible = Assignment.objects.visible(user=student).values_list("topic_id", flat=True)
        topics = topics.filter(
            pk__in=visible, is_active=True, chapter__is_active=True, block__is_active=True
        )
    topics = topics.order_by("block__order", "chapter__order", "order", "title")[:limit]
    items = []
    for topic in topics:
        url = (
            reverse("teacher_topic_edit", args=[topic.pk])
            if teacher
            else reverse("student_assignments") + "?" + urlencode({"q": topic.title})
        )
        items.append(
            {
                "type": "topic",
                "label": topic.title,
                "hint": f"{topic.block.name} · {topic.chapter.title}",
                "url": url,
                "value": str(topic.pk),
            }
        )
    return items


def _assignments(query, *, teacher, student=None, limit=LIMIT):
    assignments = Assignment.objects.select_related("topic__block", "topic__chapter")
    if teacher:
        assignments = assignments.filter(is_active=True)
    else:
        assignments = assignments.visible(user=student)
    assignments = assignments.filter(
        Q(title__icontains=query)
        | Q(topic__title__icontains=query)
        | Q(topic__chapter__title__icontains=query)
        | Q(topic__block__name__icontains=query)
    ).order_by("topic__block__order", "topic__chapter__order", "topic__order", "order", "title")[
        :limit
    ]
    items = []
    for assignment in assignments:
        url = (
            reverse("teacher_assignment_form", args=[assignment.pk])
            if teacher
            else reverse("assignment_detail", args=[assignment.pk])
        )
        items.append(
            {
                "type": "assignment",
                "label": assignment.title,
                "hint": (
                    f"{assignment.topic.block.name} · {assignment.topic.chapter.title} · "
                    f"{assignment.topic.title}"
                ),
                "url": url,
                "value": str(assignment.pk),
            }
        )
    return items


def _blocks(query, *, teacher, student=None, limit=4):
    blocks = Block.objects.filter(name__icontains=query)
    if teacher:
        blocks = blocks.filter(is_active=True)
    else:
        visible = Assignment.objects.visible(user=student).values_list("topic__block_id", flat=True)
        blocks = blocks.filter(pk__in=visible, is_active=True)
    blocks = blocks.order_by("order", "name")[:limit]
    items = []
    for block in blocks:
        url = (
            reverse("teacher_curriculum") + "?" + urlencode({"q": block.name})
            if teacher
            else reverse("student_assignments") + "?" + urlencode({"q": block.name})
        )
        items.append(
            {
                "type": "block",
                "label": block.name,
                "hint": block.get_cefr_level_display() if block.cefr_level else "класс",
                "url": url,
                "value": str(block.pk),
            }
        )
    return items


def _curriculum(query, *, teacher, student=None):
    return (
        _blocks(query, teacher=teacher, student=student, limit=2)
        + _chapters(query, teacher=teacher, student=student, limit=2)
        + _topics(query, teacher=teacher, student=student, limit=3)
        + _assignments(query, teacher=teacher, student=student, limit=4)
    )[:LIMIT]
