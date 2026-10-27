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
from django.db.models import CharField, F, IntegerField, Q, Value

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

#: Только правила самого задания: четыре связи на весь список вместо
#: четырёх запросов на каждое задание.
ASSIGNMENT_OWN_PREFETCH = (
    "groups",
    "assigned_students",
    "excluded_groups",
    "excluded_students",
)

#: Уровни цепочки: модель, путь от Assignment и имя поля учеников.
LEVELS = (
    (Block, "topic__block", "students"),
    (Chapter, "topic__chapter", "students"),
    (Topic, "topic", "students"),
    (Assignment, None, "assigned_students"),
)

#: Правила аудитории темы вместе с правилами главы и класса: 12 связей вместо
#: такого же числа запросов на каждое место, где форма показывает доступ.
TOPIC_CHAIN_RELATIONS = ("groups", "students", "excluded_groups", "excluded_students")
TOPIC_CHAIN_PREFETCH = tuple(
    f"{prefix}{relation}"
    for prefix in ("", "chapter__", "block__")
    for relation in TOPIC_CHAIN_RELATIONS
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
    # «Общий» материал — только когда назначений нет вообще. Одни исключения не
    # превращают опустевшую ветку обратно в материал для всех.
    open_q = _open_q()
    allowed_q = Q(pk__in=[])

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


def _relation_columns(model, relation):
    """Через-таблица и имена колонок M2M-связи уровня аудитории."""
    field = model._meta.get_field(relation)
    through = field.remote_field.through
    object_column = through._meta.get_field(field.m2m_field_name()).attname
    target_column = through._meta.get_field(field.m2m_reverse_field_name()).attname
    return through, object_column, target_column


def user_group_ids(user):
    """Группы ученика одним запросом; у преподавателя групп нет."""
    if not hasattr(user, "student_groups"):
        return []
    return list(user.student_groups.values_list("pk", flat=True))


class AudienceRules:
    """Правила аудитории ученика для одного задания или целого списка — за 2 запроса.

    ``visibility_q`` на каждый уровень строит по два подзапроса на каждую группу
    ученика: ``4 уровня × 2 × |групп|`` вложенных выборок и десятки килобайт SQL,
    которые сначала собирает Python, а затем разбирает база. При проверке одного
    уже загруженного задания это чистая потеря времени: страница задания и каждый
    клик «Проверить» платили за фильтр всего каталога.

    Здесь одна UNION-выборка строк аудитории (назначения и исключения групп и
    учеников на четырёх уровнях) и — только когда ученику нигде ничего не
    назначено — одна проверка «цепочка без назначений, материал общий». Число
    запросов не зависит ни от количества проверяемых заданий, ни от числа групп
    ученика, ни от размера курса.
    """

    def __init__(self, user, assignments):
        self.user = user
        self.assignments = list(assignments)
        self.authenticated = bool(user is not None and getattr(user, "is_authenticated", False))
        self.group_ids = user_group_ids(user) if self.authenticated else []
        self.rows = {}
        self._open_ids = None
        if self.authenticated and self.assignments:
            self._load()

    def _level_ids(self):
        """Identifiers of the chain objects we need rules for, by level."""
        ids = {index: set() for index in range(len(LEVELS))}
        for assignment in self.assignments:
            topic = assignment.topic
            ids[0].add(topic.block_id)
            ids[1].add(topic.chapter_id)
            ids[2].add(topic.pk)
            ids[3].add(assignment.pk)
        return ids

    def _load(self):
        """Строки аудитории всех уровней одним UNION-запросом (по 4 ветки на уровень)."""
        ids = self._level_ids()
        parts = []
        for index, (model, _path, students_field) in enumerate(LEVELS):
            if not ids[index]:
                continue
            for kind, relation, members in (
                ("g", "groups", self.group_ids),
                ("e", "excluded_groups", self.group_ids),
                ("s", students_field, [self.user.pk]),
                ("x", "excluded_students", [self.user.pk]),
            ):
                if not members:
                    continue
                through, object_column, member_column = _relation_columns(model, relation)
                parts.append(
                    through.objects.filter(
                        **{f"{object_column}__in": ids[index], f"{member_column}__in": members}
                    )
                    .annotate(
                        kind=Value(kind, output_field=CharField(max_length=1)),
                        level=Value(index, output_field=IntegerField()),
                        owner=F(object_column),
                        member=F(member_column),
                    )
                    .values("kind", "level", "owner", "member")
                )
        if not parts:
            return
        for row in parts[0].union(*parts[1:]):
            bucket = self.rows.setdefault((row["level"], row["owner"]), {})
            bucket.setdefault(row["kind"], set()).add(row["member"])

    def _members(self, level, owner, kind):
        return self.rows.get((level, owner), {}).get(kind, ())

    def _open(self):
        """Задания, у которых во всей цепочке нет ни одного назначения — общие."""
        if self._open_ids is None:
            candidates = [item.pk for item in self.assignments if item.pk is not None]
            self._open_ids = (
                set(
                    Assignment.objects.filter(pk__in=candidates)
                    .filter(_open_q())
                    .values_list("pk", flat=True)
                )
                if candidates
                else set()
            )
        return self._open_ids

    def allows(self, assignment):
        """Видит ли ученик это задание с учётом наследования и исключений."""
        if not self.authenticated:
            return False
        if not assignment.is_visible:
            return False
        topic = assignment.topic
        if not (topic.is_active and topic.chapter.is_active and topic.block.is_active):
            return False
        chain_ids = [(0, topic.block_id), (1, topic.chapter_id), (2, topic.pk), (3, assignment.pk)]
        granted_to_me = any(
            self._members(level, owner, "g") or self._members(level, owner, "s")
            for level, owner in chain_ids
        )
        if not granted_to_me:
            # Ни одного назначения именно этому ученику: доступ возможен только
            # когда вся цепочка без назначений — тогда материал общий.
            return assignment.pk in self._open()

        student_allowed = False
        group_allowed = dict.fromkeys(self.group_ids, False)
        for level, owner in chain_ids:
            # Назначения текущего уровня применяются до исключений этого же уровня.
            if self._members(level, owner, "s"):
                student_allowed = True
            for group_id in self._members(level, owner, "g"):
                group_allowed[group_id] = True
            # Персональное исключение отменяет и личный, и групповой доступ.
            if self._members(level, owner, "x"):
                student_allowed = False
                group_allowed = dict.fromkeys(self.group_ids, False)
            for group_id in self._members(level, owner, "e"):
                group_allowed[group_id] = False
        return student_allowed or any(group_allowed.values())


def _open_q():
    """Фильтр «назначений нет ни на одном уровне»: материал видят все ученики."""
    open_q = Q()
    for model, path, students_field in LEVELS:
        key = _level_key(path)
        restricted = model.objects.filter(
            Q(groups__isnull=False) | Q(**{f"{students_field}__isnull": False})
        ).values("pk")
        open_q &= ~Q(**{key: restricted})
    return open_q


def assignment_is_visible_to(assignment, user):
    """Быстрая проверка одного задания без огромного ``visibility_q``.

    Результат совпадает с фильтром :func:`visibility_q`, но стоит два компактных
    запроса вместо десятков подзапросов — см. :class:`AudienceRules`.
    """
    return AudienceRules(user, [assignment]).allows(assignment)


DEFAULT_RELATED = ("topic__block", "topic__chapter__block")


def visible_assignment(user, pk, *, select_related=DEFAULT_RELATED, **filters):
    """Одно задание, видимое ученику: компактная проверка вместо ``visibility_q``.

    ``Assignment.objects.visible(user=...)`` фильтрует весь каталог огромным ``Q``;
    на странице задания это стоило десятки миллисекунд только на сборку SQL, хотя
    проверялась одна строка. Сначала выбирается сама строка, затем точечно
    проверяются публикация и правило аудитории.
    """
    assignment = Assignment.objects.filter(pk=pk, **filters).select_related(*select_related).first()
    if assignment is None or not assignment_is_visible_to(assignment, user):
        return None
    return assignment


def visible_assignment_or_404(user, pk, *, select_related=None, **filters):
    """Как ``get_object_or_404(Assignment.objects.visible(user=...), pk=pk)``, но быстро.

    Точечная проверка одного задания дешевле фильтрации всего каталога: страницы и
    AJAX-эндпоинты ученика («Проверить», «Сбросить», автосохранение черновика,
    тренажёр) обращаются к одному pk на каждый клик.
    """
    from django.http import Http404

    wanted = DEFAULT_RELATED if select_related is None else tuple(select_related)
    assignment = visible_assignment(user, pk, select_related=wanted, **filters)
    if assignment is None:
        raise Http404("Задание недоступно")
    return assignment


def filter_visible(queryset, user, *, select_related=DEFAULT_RELATED):
    """Ограничить небольшую выборку заданий доступными ученику.

    Для коротких выборок (соседние тренажёры темы, файлы одного задания)
    точечная проверка дешевле общего ``visibility_q``; строки аудитории
    читаются один раз на весь список.
    """
    items = list(queryset.select_related(*select_related))
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    rules = AudienceRules(user, items)
    return [item for item in items if rules.allows(item)]


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
    """Применить локальные назначения и затем исключения к аудитории родителя.

    Уровень без собственных назначений наследует родителя целиком: словарь
    копируется, а списки имён и подпись бейджа собираются заново только там, где
    назначения или исключения действительно заданы. На карте курса это экономит
    по пробегу по всем получателям класса на каждую главу, тему и задание.
    """
    rules = own_rules(obj)
    if parent is not None and not any(rules.values()):
        return {
            **parent,
            "has_own": False,
            "has_exclusions": False,
            "has_inherited": bool(parent["groups"] or parent["students"]),
        }
    groups = {group.pk: group for group in (parent["groups"] if parent else [])}
    students = {student.pk: student for student in (parent["students"] if parent else [])}
    inherited = {"groups": list(groups.values()), "students": list(students.values())}
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


#: Отношения аудитории на уровне цепочки: поле назначения учеников различается.
RULE_RELATIONS = ("groups", "students", "excluded_groups", "excluded_students")


def _level_ids_by_model(assignments):
    """Уровни цепочки списка заданий, сгруппированные по модели.

    Id берутся из ``select_related``-связей самого задания, поэтому классы, главы
    и темы не нужно загружать, чтобы узнать их идентификаторы.
    """
    ids = {model: set() for model in LEVEL_IDS_BY_MODEL}
    for assignment in assignments:
        ids[Assignment].add(assignment.pk)
        topic = assignment.topic
        ids[Topic].add(topic.pk)
        if topic.chapter_id:
            ids[Chapter].add(topic.chapter_id)
        if topic.block_id:
            ids[Block].add(topic.block_id)
    return ids


#: Модель -> порядок обхода цепочки (от класса к заданию).
LEVEL_IDS_BY_MODEL = (Block, Chapter, Topic, Assignment)


def students_field(model):
    """Поле персональных назначений уровня: у задания оно своё."""
    return "assigned_students" if model is Assignment else "students"


def _relation_maps(models_levels):
    """``{model: {relation: {level_pk: set(target_pk)}}`` — по одному запросу на связь.

    ``values_list`` вместо ``prefetch_related``: второй вариант собирает по всему
    курсу полноценные модели классов, глав, тем и групп. Для матрицы журнала,
    где нужны только идентификаторы, это дороже в разы.
    """
    maps = {}
    for model, level_ids in models_levels.items():
        if not level_ids:
            continue
        per_model = {}
        for relation in RULE_RELATIONS:
            field_name = students_field(model) if relation == "students" else relation
            through, object_column, target_column = _relation_columns(model, field_name)
            buckets = {}
            rows = through.objects.filter(**{f"{object_column}__in": level_ids}).values_list(
                object_column, target_column
            )
            for level_pk, target_pk in rows:
                buckets.setdefault(level_pk, set()).add(target_pk)
            per_model[relation] = buckets
        maps[model] = per_model
    return maps


def effective_ids_bulk(assignments, membership=None):
    """``{assignment.pk: set(id учеников) | None}`` без prefetch'ов аудитории.

    Правила читаются связующими таблицами (16 запросов на весь список), затем
    применяются в Python в том же порядке, что и :func:`_effective_ids`:
    назначения уровня, затем его исключения, снизу вверх по цепочке.
    """
    assignments = list(assignments)
    if not assignments:
        return {}
    maps = _relation_maps(_level_ids_by_model(assignments))
    membership = _group_membership() if membership is None else membership
    result = {}
    for assignment in assignments:
        topic = assignment.topic
        allowed = set()
        restricted = False
        for model, level_id in (
            (Block, topic.block_id),
            (Chapter, topic.chapter_id),
            (Topic, topic.pk),
            (Assignment, assignment.pk),
        ):
            if level_id is None:
                continue
            per_model = maps.get(model, {})
            groups = per_model.get("groups", {}).get(level_id)
            students = per_model.get("students", {}).get(level_id)
            if groups or students:
                restricted = True
            if groups:
                for group_id in groups:
                    allowed |= membership.get(group_id, set())
            if students:
                allowed |= students
            for group_id in per_model.get("excluded_groups", {}).get(level_id) or set():
                allowed -= membership.get(group_id, set())
            allowed -= per_model.get("excluded_students", {}).get(level_id) or set()
        result[assignment.pk] = allowed if restricted else None
    return result


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


#: Сколько получателей перечислять в подсказке аудитории перед счётчиком.
AUDIENCE_TOOLTIP_LIMIT = 8


def _names(items, limit):
    """Имена первых ``limit`` получателей и число тех, что не попали в список."""
    shown = []
    extra = 0
    for index, item in enumerate(items):
        if index >= limit:
            extra += 1
            continue
        shown.append(item)
    return shown, extra


def details(aud):
    """Список аудитории и локальных исключений для подсказки.

    Класс может быть назначен сотне учеников: перечислять их в `title` каждого
    бейджа — это десятки килобайт HTML на карте курса. Поэтому показываем
    несколько имён, а остальных сворачиваем в счётчик.
    """
    names = [group.name for group in aud["groups"]]
    names += [user_display_name(student) for student in aud["students"]]
    names = [name for name in names if name]
    shown, extra = _names(names, AUDIENCE_TOOLTIP_LIMIT)
    text = ", ".join(shown)
    if extra:
        text += f" и ещё {extra}"
    if not text:
        text = "никому" if not aud["open"] else ""
    excluded = aud.get("excluded") or {}
    excluded_names = [group.name for group in excluded.get("groups", [])]
    excluded_names += [user_display_name(student) for student in excluded.get("students", [])]
    excluded_shown, excluded_extra = _names([name for name in excluded_names if name], 4)
    if excluded_shown or excluded_extra:
        tail = f" и ещё {excluded_extra}" if excluded_extra else ""
        text += "; исключены: " + ", ".join(excluded_shown) + tail
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
