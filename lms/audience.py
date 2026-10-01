"""Аудитория материалов курса: кому назначены класс, глава, тема, задание.

Правило последовательно применяется по иерархии:

* назначения родителя наследуются содержимым;
* текущий уровень может исключить унаследованную группу или ученика, а следующий
  уровень — назначить их снова;
* персональное исключение отменяет в том числе доступ через унаследованную группу;
* если назначений нет во всей цепочке, материал общий; если последнее назначение
  исключено, ветка остаётся ограниченной и не становится случайно общей.

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
    "excluded_groups",
    "excluded_students",
    "topic__groups",
    "topic__students",
    "topic__excluded_groups",
    "topic__excluded_students",
    "topic__chapter__groups",
    "topic__chapter__students",
    "topic__chapter__excluded_groups",
    "topic__chapter__excluded_students",
    "topic__block__groups",
    "topic__block__students",
    "topic__block__excluded_groups",
    "topic__block__excluded_students",
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


def _ids(model, **filters):
    return model.objects.filter(**filters).values("pk")


def _level_key(path):
    return f"{path}__in" if path else "pk__in"


def visibility_q(user):
    """Доступ с переопределениями: нижнее исключение отменяет верхнее назначение.

    Для каждого источника доступа проверяем, что ниже по цепочке нет более позднего
    исключения. Новое назначение после исключения снова открывает доступ. Отдельное
    исключение ученика отменяет и персональное, и групповое наследование.
    """
    user_group_ids = list(
        user.student_groups.values_list("pk", flat=True) if hasattr(user, "student_groups") else []
    )
    open_q = Q()
    allowed_q = Q(pk__in=[])

    # «Общий» материал — только когда назначений нет вообще. Одни исключения не
    # превращают опустевшую ветку обратно в материал для всех.
    for model, path, students_field in LEVELS:
        key = _level_key(path)
        restricted = model.objects.filter(
            Q(groups__isnull=False) | Q(**{f"{students_field}__isnull": False})
        ).values("pk")
        open_q &= ~Q(**{key: restricted})

    for index, (model, path, students_field) in enumerate(LEVELS):
        key = _level_key(path)
        later = LEVELS[index:]

        student_term = Q(**{key: _ids(model, **{students_field: user})})
        for later_model, later_path, _later_students in later:
            later_key = _level_key(later_path)
            student_term &= ~Q(**{later_key: _ids(later_model, excluded_students=user)})
        allowed_q |= student_term

        for group_id in user_group_ids:
            group_term = Q(**{key: _ids(model, groups=group_id)})
            for later_model, later_path, _later_students in later:
                later_key = _level_key(later_path)
                group_term &= ~Q(**{later_key: _ids(later_model, excluded_groups=group_id)})
                group_term &= ~Q(**{later_key: _ids(later_model, excluded_students=user)})
            allowed_q |= group_term

    return open_q | allowed_q


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


def own_rules(obj):
    """Назначения и исключения самого объекта (без родителей)."""
    students_manager = obj.assigned_students if isinstance(obj, Assignment) else obj.students
    return {
        "groups": list(obj.groups.all()),
        "students": list(students_manager.all()),
        "excluded_groups": list(obj.excluded_groups.all()),
        "excluded_students": list(obj.excluded_students.all()),
    }


def own_grants(obj):
    """Обратная совместимость: только положительные назначения объекта."""
    rules = own_rules(obj)
    return {"groups": rules["groups"], "students": rules["students"]}


def accumulate(obj, parent=None):
    """Применить локальные назначения и затем исключения к аудитории родителя."""
    groups = {group.pk: group for group in (parent["groups"] if parent else [])}
    students = {student.pk: student for student in (parent["students"] if parent else [])}
    inherited = {"groups": list(groups.values()), "students": list(students.values())}
    rules = own_rules(obj)
    for group in rules["groups"]:
        groups[group.pk] = group
    for student in rules["students"]:
        students[student.pk] = student
    # Исключения текущего уровня применяются после назначений этого же уровня:
    # так можно назначить группу «кроме Victoria». Назначение потомка снова откроет доступ.
    for group in rules["excluded_groups"]:
        groups.pop(group.pk, None)
    for student in rules["excluded_students"]:
        students.pop(student.pk, None)

    restricted = bool(parent and parent.get("restricted")) or bool(
        rules["groups"] or rules["students"]
    )
    has_rules = any(rules.values())
    result = {
        "open": not restricted,
        "restricted": restricted,
        "groups": list(groups.values()),
        "students": list(students.values()),
        "own": {"groups": rules["groups"], "students": rules["students"]},
        "excluded": {
            "groups": rules["excluded_groups"],
            "students": rules["excluded_students"],
        },
        "inherited": inherited,
        "has_own": has_rules,
        "has_exclusions": bool(rules["excluded_groups"] or rules["excluded_students"]),
        "has_inherited": bool(inherited["groups"] or inherited["students"]),
    }
    result["label"] = label(result)
    result["details"] = details(result)
    return result


def effective(obj):
    """Итоговая аудитория объекта с учётом всех родителей (см. ``accumulate``)."""
    result = None
    for level in chain(obj):
        result = accumulate(level, result)
    return result


def _effective_ids(assignment, membership):
    """Итоговые ID учеников; ``None`` означает общий материал."""
    allowed = set()
    restricted = False
    for level in chain(assignment):
        rules = own_rules(level)
        if rules["groups"] or rules["students"]:
            restricted = True
        for group in rules["groups"]:
            allowed |= membership.get(group.pk, set())
        allowed |= {student.pk for student in rules["students"]}
        for group in rules["excluded_groups"]:
            allowed -= membership.get(group.pk, set())
        allowed -= {student.pk for student in rules["excluded_students"]}
    return allowed if restricted else None


def _group_membership():
    membership = {}
    for group_id, user_id in Group.students.through.objects.values_list("group_id", "user_id"):
        membership.setdefault(group_id, set()).add(user_id)
    return membership


def expected_students(assignment):
    """Активные ученики, от которых ждём ответ: все — для общего задания, иначе аудитория."""
    students = User.objects.filter(profile__role=Profile.Role.STUDENT, is_active=True)
    ids = _effective_ids(assignment, _group_membership())
    return students if ids is None else students.filter(pk__in=ids)


def expected_ids_map(assignments):
    """``{assignment.pk: set(id учеников) | None}`` — ``None`` значит «все» (общее задание).

    Состав групп читается одним запросом; задания должны быть загружены с
    ``ASSIGNMENT_PREFETCH``, иначе на каждое уйдёт до восьми запросов.
    """
    membership = _group_membership()
    result = {}
    for assignment in assignments:
        result[assignment.pk] = _effective_ids(assignment, membership)
    return result


def user_display_name(user):
    """Имя ученика для подсказок: «Имя Фамилия», без имени — логин.

    Если указана только часть имени, показывается она (например, только имя).
    """
    if user is None:
        return ""
    return (user.get_full_name() or user.username or "").strip()


def details(aud):
    """Полный список аудитории и локальных исключений для подсказки."""
    names = [group.name for group in aud["groups"]]
    names += [user_display_name(student) for student in aud["students"]]
    text = ", ".join(name for name in names if name) or ("никому" if not aud["open"] else "")
    excluded = aud.get("excluded") or {}
    excluded_names = [group.name for group in excluded.get("groups", [])]
    excluded_names += [user_display_name(student) for student in excluded.get("students", [])]
    if excluded_names:
        text += "; исключены: " + ", ".join(excluded_names)
    return text


def label(audience, *, max_groups=2):
    """Короткая подпись для бейджа: «ОГЭ-А, ОГЭ-Б · +3 ученика» или «Все ученики»."""
    if audience["open"]:
        return "Все ученики"
    parts = []
    groups = audience["groups"]
    if not groups and not audience["students"]:
        return "Никому"
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
