"""Аудитория материалов курса: кому назначены класс, глава, тема, задание.

Правило одно на всех уровнях и складывается по иерархии («объединение»):

* назначение на любом уровне открывает всё, что внутри, назначенным группам
  и ученикам;
* ученик видит задание, если назначен хотя бы на одном уровне его цепочки
  (класс → глава → тема → задание);
* если ни на одном уровне цепочки ничего не выбрано — материал общий, его
  видят все ученики (так вели себя задания без группы и раньше).

Модуль не трогает публикацию/архив: это отдельные фильтры в
``Assignment.objects.visible()``; здесь — только «кому».
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Assignment, Block, Chapter, Group, Profile, Topic

User = get_user_model()

#: prefetch_related для списка заданий, чтобы ``effective()`` не ходил в базу.
ASSIGNMENT_PREFETCH = (
    "groups",
    "assigned_students",
    "topic__groups",
    "topic__students",
    "topic__chapter__groups",
    "topic__chapter__students",
    "topic__block__groups",
    "topic__block__students",
)

#: Уровни цепочки: модель, путь от Assignment и имя поля учеников.
LEVELS = (
    (Block, "topic__block", "students"),
    (Chapter, "topic__chapter", "students"),
    (Topic, "topic", "students"),
    (Assignment, None, "assigned_students"),
)

LEVEL_TITLES = {
    Block: "класс",
    Chapter: "глава",
    Topic: "тема",
    Assignment: "задание",
}


def _restricted(model, students_field):
    """Объекты уровня, у которых есть хоть одно назначение."""
    return model.objects.filter(
        Q(groups__isnull=False) | Q(**{f"{students_field}__isnull": False})
    ).values("pk")


def _granted(model, students_field, user, user_groups):
    """Объекты уровня, где ученик назначен (через группу или лично)."""
    return model.objects.filter(Q(groups__in=user_groups) | Q(**{students_field: user})).values(
        "pk"
    )


def visibility_q(user):
    """Q для ``Assignment``: общий материал ИЛИ ученик назначен хоть на одном уровне."""
    user_groups = user.student_groups.all() if hasattr(user, "student_groups") else []
    open_q = Q()
    granted_q = Q()
    for model, path, students_field in LEVELS:
        key = f"{path}__in" if path else "pk__in"
        open_q &= ~Q(**{key: _restricted(model, students_field)})
        granted_q |= Q(**{key: _granted(model, students_field, user, user_groups)})
    return open_q | granted_q


def chain(obj):
    """Цепочка от класса к самому объекту: [Block, Chapter?, Topic?, Assignment?]."""
    if isinstance(obj, Assignment):
        topic = obj.topic
        return [topic.block, topic.chapter, topic, obj]
    if isinstance(obj, Topic):
        return [obj.block, obj.chapter, obj]
    if isinstance(obj, Chapter):
        return [obj.block, obj]
    return [obj]


def own_grants(obj):
    """Собственные назначения объекта: группы и ученики (списки, без запросов при prefetch)."""
    students_manager = obj.assigned_students if isinstance(obj, Assignment) else obj.students
    return {
        "groups": list(obj.groups.all()),
        "students": list(students_manager.all()),
    }


def accumulate(obj, parent=None):
    """Аудитория объекта поверх аудитории родителя (``parent`` — результат этой же функции).

    Возвращает словарь: ``open`` — общий материал (нигде ничего не назначено),
    ``groups``/``students`` — объединение назначений по цепочке (без дублей),
    ``own`` — собственные назначения объекта, ``inherited`` — унаследованные,
    ``label`` — короткая подпись для бейджа.
    """
    groups = {group.pk: group for group in (parent["groups"] if parent else [])}
    students = {student.pk: student for student in (parent["students"] if parent else [])}
    inherited = {"groups": list(groups.values()), "students": list(students.values())}
    own = own_grants(obj)
    for group in own["groups"]:
        groups.setdefault(group.pk, group)
    for student in own["students"]:
        students.setdefault(student.pk, student)
    result = {
        "open": not groups and not students,
        "groups": list(groups.values()),
        "students": list(students.values()),
        "own": own,
        "inherited": inherited,
        "has_own": bool(own["groups"] or own["students"]),
        "has_inherited": bool(inherited["groups"] or inherited["students"]),
    }
    result["label"] = label(result)
    return result


def effective(obj):
    """Итоговая аудитория объекта с учётом всех родителей (см. ``accumulate``)."""
    result = None
    for level in chain(obj):
        result = accumulate(level, result)
    return result


def expected_students(assignment):
    """Активные ученики, от которых ждём ответ: все — для общего задания, иначе аудитория."""
    students = User.objects.filter(profile__role=Profile.Role.STUDENT, is_active=True)
    audience = effective(assignment)
    if audience["open"]:
        return students
    group_ids = [group.pk for group in audience["groups"]]
    student_ids = [student.pk for student in audience["students"]]
    return students.filter(Q(student_groups__in=group_ids) | Q(pk__in=student_ids)).distinct()


def expected_ids_map(assignments):
    """``{assignment.pk: set(id учеников) | None}`` — ``None`` значит «все» (общее задание).

    Состав групп читается одним запросом; задания должны быть загружены с
    ``ASSIGNMENT_PREFETCH``, иначе на каждое уйдёт до восьми запросов.
    """
    membership = {}
    for group_id, user_id in Group.students.through.objects.values_list("group_id", "user_id"):
        membership.setdefault(group_id, set()).add(user_id)
    result = {}
    for assignment in assignments:
        item = effective(assignment)
        if item["open"]:
            result[assignment.pk] = None
            continue
        ids = {student.pk for student in item["students"]}
        for group in item["groups"]:
            ids |= membership.get(group.pk, set())
        result[assignment.pk] = ids
    return result


def label(audience, *, max_groups=2):
    """Короткая подпись для бейджа: «ОГЭ-А, ОГЭ-Б · +3 ученика» или «Все ученики»."""
    if audience["open"]:
        return "Все ученики"
    parts = []
    groups = audience["groups"]
    if groups:
        names = [group.name for group in groups[:max_groups]]
        rest = len(groups) - len(names)
        parts.append(", ".join(names) + (f" +{rest}" if rest > 0 else ""))
    count = len(audience["students"])
    if count:
        parts.append(_plural_students(count) if groups else _plural_students(count, only=True))
    return " · ".join(parts)


def _plural_students(count, *, only=False):
    tail = count % 10
    if count % 100 in range(11, 15) or tail == 0 or tail >= 5:
        word = "учеников"
    elif tail == 1:
        word = "ученик"
    else:
        word = "ученика"
    prefix = "" if only else "+"
    return f"{prefix}{count} {word}"


def describe(obj):
    """Аудитория объекта одним словарём для шаблонов (``audience`` в контексте)."""
    return effective(obj)
