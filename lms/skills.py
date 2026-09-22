"""Навыки задания: каталог и подстановка по типу сдачи.

Преподаватель может заменить набор вручную; если ничего не выбрано,
подставляем разумный дефолт (письмо для текста, говорение для аудио и т.д.).
"""

from .models import Skill

DEFAULT_SKILLS = [
    ("Грамматика", Skill.Kind.GRAMMAR),
    ("Лексика", Skill.Kind.VOCABULARY),
    ("Аудирование", Skill.Kind.LISTENING),
    ("Говорение", Skill.Kind.SPEAKING),
    ("Письмо", Skill.Kind.WRITING),
    ("Чтение", Skill.Kind.READING),
]

TYPE_SKILL_KINDS = {
    "text": (Skill.Kind.WRITING,),
    "file": (Skill.Kind.WRITING,),
    "audio": (Skill.Kind.SPEAKING,),
    "mixed": (Skill.Kind.WRITING, Skill.Kind.SPEAKING),
    "quiz": (Skill.Kind.GRAMMAR,),
    "flashcards": (Skill.Kind.VOCABULARY,),
}


def ensure_skill_catalog():
    """Создать шесть базовых навыков, если их ещё нет. Идемпотентно."""
    for order, (name, kind) in enumerate(DEFAULT_SKILLS, start=1):
        Skill.objects.get_or_create(
            slug=kind,
            defaults={"name": name, "kind": kind, "order": order},
        )
    return {skill.kind: skill for skill in Skill.objects.all()}


def skills_for_type(assignment_type):
    """Навыки по умолчанию для типа задания, в стабильном порядке."""
    kinds = TYPE_SKILL_KINDS.get(assignment_type, ())
    if not kinds:
        return []
    found = {}
    for skill in Skill.objects.filter(kind__in=kinds).order_by("order", "pk"):
        found.setdefault(skill.kind, skill)
    missing = [kind for kind in kinds if kind not in found]
    if missing:
        catalog = ensure_skill_catalog()
        for kind in missing:
            skill = catalog.get(kind)
            if skill is not None:
                found[kind] = skill
    return [found[kind] for kind in kinds if kind in found]


def apply_default_skills(assignment, *, override=False):
    """Проставить навыки по типу, если преподаватель их не задал."""
    if assignment.pk is None:
        return []
    if not override and assignment.skills.exists():
        return list(assignment.skills.all())
    skills = skills_for_type(assignment.assignment_type)
    if skills:
        assignment.skills.set(skills)
    return skills


def type_skill_payload():
    """Словарь slug→id и type→[id] для подстановки в форме без лишних запросов."""
    by_kind = ensure_skill_catalog()
    catalog = {skill.slug: skill.pk for skill in Skill.objects.all()}
    mapping = {}
    for assignment_type, kinds in TYPE_SKILL_KINDS.items():
        mapping[assignment_type] = [by_kind[kind].pk for kind in kinds if kind in by_kind]
    return {"ids": catalog, "by_type": mapping}
