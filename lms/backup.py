"""Резервное копирование и восстановление учебных материалов курса.

Позволяет выгрузить все классы, главы, темы, задания с тестами, карточками,
навыками и реальными файлами материалов в единый переносимый ZIP-архив (или JSON),
а затем загрузить его обратно на сайт, восстановив полную структуру и файлы.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import slugify

from .forms import unique_slug
from .models import (
    Assignment,
    AssignmentAttachment,
    Block,
    Chapter,
    Choice,
    Flashcard,
    Group,
    Question,
    Skill,
    Topic,
)
from .skills import apply_default_skills, ensure_skill_catalog

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1
BACKUP_MANIFEST_NAMES = ("backup.json", "manifest.json", "curriculum.json")


# ── СЕРИАЛИЗАЦИЯ (ВЫГРУЗКА) ───────────────────────────────────────────────


def serialize_curriculum(blocks=None):
    """Собрать данные классов, глав, тем и заданий в JSON-совместимый словарь.

    Возвращает кортеж (manifest_data, files_to_export), где files_to_export —
    список кортежей (archive_path, file_field) для упаковки в ZIP-архив.
    """
    if blocks is None:
        blocks_qs = Block.objects.all().order_by("order", "pk")
    elif isinstance(blocks, Block):
        blocks_qs = Block.objects.filter(pk=blocks.pk)
    elif hasattr(blocks, "prefetch_related"):
        blocks_qs = blocks
    else:
        pks = [b.pk if isinstance(b, Block) else b for b in blocks]
        blocks_qs = Block.objects.filter(pk__in=pks).order_by("order", "pk")

    blocks_qs = blocks_qs.prefetch_related(
        "groups",
        "chapters__groups",
        "chapters__topics__groups",
        "chapters__topics__assignments__skills",
        "chapters__topics__assignments__groups",
        "chapters__topics__assignments__attachments",
        "chapters__topics__assignments__questions__choices",
        "chapters__topics__assignments__cards",
    )

    files_to_export: list[tuple[str, any]] = []
    blocks_data = []
    total_assignments = 0

    for block in blocks_qs:
        block_item = {
            "name": block.name,
            "slug": block.slug,
            "description": block.description,
            "cefr_level": block.cefr_level,
            "order": block.order,
            "is_active": block.is_active,
            "groups": [g.name for g in block.groups.all()],
            "chapters": [],
        }

        # Все темы класса, сгруппированные по главам
        for chapter in block.chapters.all().order_by("order", "pk"):
            chapter_item = {
                "title": chapter.title,
                "slug": chapter.slug,
                "description": chapter.description,
                "order": chapter.order,
                "is_active": chapter.is_active,
                "groups": [g.name for g in chapter.groups.all()],
                "topics": [],
            }

            for topic in chapter.topics.all().order_by("order", "pk"):
                topic_item = {
                    "title": topic.title,
                    "slug": topic.slug,
                    "description": topic.description,
                    "order": topic.order,
                    "is_active": topic.is_active,
                    "groups": [g.name for g in topic.groups.all()],
                    "assignments": [],
                }

                for assignment in topic.assignments.all().order_by("order", "pk"):
                    total_assignments += 1
                    assignment_item = {
                        "title": assignment.title,
                        "description": assignment.description,
                        "assignment_type": assignment.assignment_type,
                        "status": assignment.status,
                        "publish_at": (
                            assignment.publish_at.isoformat() if assignment.publish_at else None
                        ),
                        "deadline": (
                            assignment.deadline.isoformat() if assignment.deadline else None
                        ),
                        "max_points": assignment.max_points,
                        "max_tries": assignment.max_tries,
                        "allow_retake": assignment.allow_retake,
                        "exam_mode": assignment.exam_mode,
                        "recording_limit_seconds": assignment.recording_limit_seconds,
                        "order": assignment.order,
                        "is_active": assignment.is_active,
                        "skills": [s.slug for s in assignment.skills.all()],
                        "groups": [g.name for g in assignment.groups.all()],
                        "material_file": None,
                        "attachments": [],
                        "questions": [],
                        "cards": [],
                    }

                    # Файл материалов задания
                    if assignment.material_file and assignment.material_file.name:
                        try:
                            if assignment.material_file.storage.exists(
                                assignment.material_file.name
                            ):
                                orig_name = Path(assignment.material_file.name).name
                                safe_name = (
                                    slugify(orig_name, allow_unicode=True)
                                    or f"file_{assignment.pk}"
                                )
                                ext = Path(orig_name).suffix
                                if ext and not safe_name.endswith(ext):
                                    safe_name = f"{safe_name}{ext}"
                                arc_path = f"files/materials/assn_{assignment.pk}_{safe_name}"
                                files_to_export.append((arc_path, assignment.material_file))
                                assignment_item["material_file"] = {
                                    "original_name": orig_name,
                                    "archive_path": arc_path,
                                    "size": assignment.material_file.size,
                                }
                        except Exception as exc:
                            logger.warning(
                                "Could not export material_file for assignment %s: %s",
                                assignment.pk,
                                exc,
                            )

                    # Дополнительные вложения
                    for att in assignment.attachments.all().order_by("order", "pk"):
                        if att.file and att.file.name:
                            try:
                                if att.file.storage.exists(att.file.name):
                                    orig_name = Path(att.file.name).name
                                    safe_name = (
                                        slugify(orig_name, allow_unicode=True) or f"att_{att.pk}"
                                    )
                                    ext = Path(orig_name).suffix
                                    if ext and not safe_name.endswith(ext):
                                        safe_name = f"{safe_name}{ext}"
                                    arc_path = f"files/attachments/att_{att.pk}_{safe_name}"
                                    files_to_export.append((arc_path, att.file))
                                    assignment_item["attachments"].append(
                                        {
                                            "title": att.title,
                                            "order": att.order,
                                            "original_name": orig_name,
                                            "archive_path": arc_path,
                                            "size": att.file.size,
                                        }
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "Could not export attachment %s for assignment %s: %s",
                                    att.pk,
                                    assignment.pk,
                                    exc,
                                )

                    # Вопросы теста и варианты ответов
                    for question in assignment.questions.all().order_by("order", "pk"):
                        q_item = {
                            "kind": question.kind,
                            "text": question.text,
                            "explanation": question.explanation,
                            "points": question.points,
                            "recording_limit_seconds": question.recording_limit_seconds,
                            "order": question.order,
                            "choices": [
                                {
                                    "text": choice.text,
                                    "match_text": choice.match_text,
                                    "is_correct": choice.is_correct,
                                    "order": choice.order,
                                }
                                for choice in question.choices.all().order_by("order", "pk")
                            ],
                        }
                        assignment_item["questions"].append(q_item)

                    # Карточки-тренажёр
                    for card in assignment.cards.all().order_by("order", "pk"):
                        card_item = {
                            "front": card.front,
                            "back": card.back,
                            "example": card.example,
                            "order": card.order,
                        }
                        assignment_item["cards"].append(card_item)

                    topic_item["assignments"].append(assignment_item)

                chapter_item["topics"].append(topic_item)

            block_item["chapters"].append(chapter_item)

        blocks_data.append(block_item)

    manifest = {
        "version": BACKUP_VERSION,
        "app": "english_lms",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "blocks_count": len(blocks_data),
        "assignments_count": total_assignments,
        "blocks": blocks_data,
    }
    return manifest, files_to_export


def build_curriculum_zip(blocks=None) -> io.BytesIO:
    """Упаковать структуру и все связанные файлы в ZIP-архив в памяти."""
    manifest, files_to_export = serialize_curriculum(blocks)

    buf = io.BytesIO()
    with ZipFile(buf, "w") as zf:
        # Записываем манифест
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        zf.writestr("backup.json", manifest_bytes)

        # Записываем файлы материалов
        for arc_path, file_field in files_to_export:
            try:
                with file_field.open("rb") as f:
                    content = f.read()
                zf.writestr(arc_path, content)
            except Exception as exc:
                logger.warning("Failed to write %s to backup archive: %s", arc_path, exc)

    buf.seek(0)
    return buf


# ── ДЕСЕРИАЛИЗАЦИЯ И ЗАГРУЗКА (ИМПОРТ) ───────────────────────────────────


def load_backup_file(uploaded_file) -> tuple[dict, ZipFile | None]:
    """Проверить и распаковать загруженный файл бэкапа (ZIP или JSON).

    Возвращает (manifest_dict, zip_file_or_None).
    """
    if not uploaded_file:
        raise ValidationError("Файл не передан.")

    max_bytes = getattr(settings, "LMS_MAX_FILE_BYTES", 50 * 1024 * 1024)
    if uploaded_file.size > max_bytes:
        raise ValidationError(
            f"Файл слишком велик ({uploaded_file.size // (1024 * 1024)} МБ). "
            f"Максимальный размер: {max_bytes // (1024 * 1024)} МБ."
        )

    # Определяем формат: проверяем сигнатуру ZIP или расширение
    is_zip = False
    uploaded_file.seek(0)
    header = uploaded_file.read(4)
    uploaded_file.seek(0)

    if header.startswith(b"PK\x03\x04") or uploaded_file.name.lower().endswith(".zip"):
        is_zip = True

    if is_zip:
        try:
            zf = ZipFile(uploaded_file, "r")
        except BadZipFile as exc:
            raise ValidationError("Повреждённый ZIP-архив. Не удалось открыть файл.") from exc

        # Проверка безопасности имён в архиве
        for name in zf.namelist():
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                zf.close()
                raise ValidationError("Небезопасное имя файла в архиве (path traversal).")

        manifest_name = None
        for candidate in BACKUP_MANIFEST_NAMES:
            if candidate in zf.namelist():
                manifest_name = candidate
                break

        if not manifest_name:
            zf.close()
            raise ValidationError(
                "В архиве не найден файл структуры курса (backup.json или manifest.json)."
            )

        try:
            raw_json = zf.read(manifest_name).decode("utf-8")
            data = json.loads(raw_json)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            zf.close()
            raise ValidationError(f"Ошибка чтения данных JSON из архива: {exc}") from exc

        return data, zf

    # Иначе пробуем как чистый JSON
    try:
        raw_json = uploaded_file.read().decode("utf-8")
        data = json.loads(raw_json)
        return data, None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Файл не является корректным ZIP-архивом или JSON-файлом.") from exc


@transaction.atomic
def import_curriculum(
    data: dict,
    zip_file: ZipFile | None = None,
    *,
    strategy: str = "merge",
    duplicate_assignments: str = "skip",
    publication_status: str = "preserve",
) -> dict:
    """Восстановить курс из структуры данных и архива.

    Параметры:
    - strategy: 'merge' (дополнить существующие классы) или 'copy' (создавать новые копии)
    - duplicate_assignments: 'skip' (пропускать задание, если в теме уже есть с таким названием)
      или 'add' (добавлять задание в любом случае)
    - publication_status: 'preserve' (сохранить статус как в бэкапе) или 'draft' (сделать черновиками)

    Возвращает словарь со статистикой добавленных и сопоставленных элементов.
    """
    if not isinstance(data, dict) or "blocks" not in data or not isinstance(data["blocks"], list):
        raise ValidationError(
            "Некорректная структура файла бэкапа: отсутствует корневой список 'blocks'."
        )

    ensure_skill_catalog()

    stats = {
        "blocks_created": 0,
        "blocks_matched": 0,
        "chapters_created": 0,
        "chapters_matched": 0,
        "topics_created": 0,
        "topics_matched": 0,
        "assignments_created": 0,
        "assignments_skipped": 0,
        "questions_created": 0,
        "cards_created": 0,
        "attachments_created": 0,
        "materials_restored": 0,
    }

    for block_data in data["blocks"]:
        block_name = str(block_data.get("name") or "Класс").strip()
        block_slug = str(block_data.get("slug") or slugify(block_name, allow_unicode=True)).strip()

        block = None
        if strategy == "copy":
            candidate_name = (
                f"{block_name} (копия)"
                if Block.objects.filter(name=block_name).exists()
                else block_name
            )
            block = Block.objects.create(
                name=candidate_name,
                slug=unique_slug(Block, f"{block_slug}-copy"),
                description=block_data.get("description", ""),
                cefr_level=block_data.get("cefr_level", ""),
                order=Block.objects.count(),
                is_active=bool(block_data.get("is_active", True)),
            )
            stats["blocks_created"] += 1
        else:
            if block_slug:
                block = Block.objects.filter(slug=block_slug).first()
            if not block:
                block = Block.objects.filter(name__iexact=block_name).first()

            if block:
                stats["blocks_matched"] += 1
                updated = False
                if not block.description and block_data.get("description"):
                    block.description = block_data["description"]
                    updated = True
                if not block.cefr_level and block_data.get("cefr_level"):
                    block.cefr_level = block_data["cefr_level"]
                    updated = True
                if updated:
                    block.save()
            else:
                block = Block.objects.create(
                    name=block_name,
                    slug=unique_slug(Block, block_slug or block_name),
                    description=block_data.get("description", ""),
                    cefr_level=block_data.get("cefr_level", ""),
                    order=Block.objects.count(),
                    is_active=bool(block_data.get("is_active", True)),
                )
                stats["blocks_created"] += 1

        # Назначения групп на класс
        if block_data.get("groups"):
            groups = Group.objects.filter(name__in=block_data["groups"])
            if groups.exists():
                block.groups.add(*groups)

        # Главы класса
        for chapter_data in block_data.get("chapters", []):
            chap_title = str(chapter_data.get("title") or "Глава").strip()
            chap_slug = str(
                chapter_data.get("slug") or slugify(chap_title, allow_unicode=True)
            ).strip()

            chapter = None
            if strategy != "copy":
                if chap_slug:
                    chapter = block.chapters.filter(slug=chap_slug).first()
                if not chapter:
                    chapter = block.chapters.filter(title__iexact=chap_title).first()

            if chapter:
                stats["chapters_matched"] += 1
            else:
                chapter = Chapter.objects.create(
                    block=block,
                    title=chap_title,
                    slug=unique_slug(Chapter, chap_slug or chap_title, parent={"block": block}),
                    description=chapter_data.get("description", ""),
                    order=block.chapters.count(),
                    is_active=bool(chapter_data.get("is_active", True)),
                )
                stats["chapters_created"] += 1

            if chapter_data.get("groups"):
                groups = Group.objects.filter(name__in=chapter_data["groups"])
                if groups.exists():
                    chapter.groups.add(*groups)

            for topic_data in chapter_data.get("topics", []):
                _import_topic(
                    block=block,
                    chapter=chapter,
                    topic_data=topic_data,
                    strategy=strategy,
                    duplicate_assignments=duplicate_assignments,
                    publication_status=publication_status,
                    zip_file=zip_file,
                    stats=stats,
                )

        # Темы на уровне блока (legacy)
        for topic_data in block_data.get("topics", []):
            default_ch = block.default_chapter()
            _import_topic(
                block=block,
                chapter=default_ch,
                topic_data=topic_data,
                strategy=strategy,
                duplicate_assignments=duplicate_assignments,
                publication_status=publication_status,
                zip_file=zip_file,
                stats=stats,
            )

    logger.info("Curriculum imported: %s", stats)
    return stats


def _import_topic(
    block: Block,
    chapter: Chapter,
    topic_data: dict,
    strategy: str,
    duplicate_assignments: str,
    publication_status: str,
    zip_file: ZipFile | None,
    stats: dict,
):
    top_title = str(topic_data.get("title") or "Тема").strip()
    top_slug = str(topic_data.get("slug") or slugify(top_title, allow_unicode=True)).strip()

    topic = None
    if strategy != "copy":
        if top_slug:
            topic = block.topics.filter(chapter=chapter, slug=top_slug).first()
        if not topic:
            topic = block.topics.filter(chapter=chapter, title__iexact=top_title).first()
        if not topic and top_slug:
            topic = block.topics.filter(slug=top_slug).first()

    if topic:
        stats["topics_matched"] += 1
    else:
        topic = Topic.objects.create(
            block=block,
            chapter=chapter,
            title=top_title,
            slug=unique_slug(Topic, top_slug or top_title, parent={"block": block}),
            description=topic_data.get("description", ""),
            order=chapter.topics.count(),
            is_active=bool(topic_data.get("is_active", True)),
        )
        stats["topics_created"] += 1

    if topic_data.get("groups"):
        groups = Group.objects.filter(name__in=topic_data["groups"])
        if groups.exists():
            topic.groups.add(*groups)

    for a_data in topic_data.get("assignments", []):
        _import_assignment(
            topic=topic,
            a_data=a_data,
            duplicate_assignments=duplicate_assignments,
            publication_status=publication_status,
            zip_file=zip_file,
            stats=stats,
        )


def _import_assignment(
    topic: Topic,
    a_data: dict,
    duplicate_assignments: str,
    publication_status: str,
    zip_file: ZipFile | None,
    stats: dict,
):
    title = str(a_data.get("title") or "Задание").strip()[:200]

    if duplicate_assignments == "skip" and topic.assignments.filter(title__iexact=title).exists():
        stats["assignments_skipped"] += 1
        return

    if publication_status == "draft":
        status = Assignment.Publication.DRAFT
    elif publication_status == "preserve":
        status = a_data.get("status", Assignment.Publication.PUBLISHED)
        if status not in Assignment.Publication.values:
            status = Assignment.Publication.PUBLISHED
    else:
        status = Assignment.Publication.PUBLISHED

    assignment_type = a_data.get("assignment_type", Assignment.Type.TEXT)
    if assignment_type not in Assignment.Type.values:
        assignment_type = Assignment.Type.TEXT

    assignment = Assignment(
        topic=topic,
        title=title,
        description=a_data.get("description", ""),
        assignment_type=assignment_type,
        status=status,
        max_points=a_data.get("max_points", 100),
        max_tries=a_data.get("max_tries", 3),
        allow_retake=bool(a_data.get("allow_retake", False)),
        exam_mode=bool(a_data.get("exam_mode", False)),
        recording_limit_seconds=a_data.get("recording_limit_seconds"),
        order=topic.assignments.count(),
        is_active=bool(a_data.get("is_active", True)),
    )

    if a_data.get("deadline"):
        try:
            assignment.deadline = datetime.fromisoformat(a_data["deadline"])
        except (ValueError, TypeError):
            pass
    if a_data.get("publish_at"):
        try:
            assignment.publish_at = datetime.fromisoformat(a_data["publish_at"])
        except (ValueError, TypeError):
            pass

    # Восстановление файла материалов задания
    mat_info = a_data.get("material_file")
    if mat_info and zip_file and mat_info.get("archive_path") in zip_file.namelist():
        try:
            content = zip_file.read(mat_info["archive_path"])
            orig_name = mat_info.get("original_name") or "material.bin"
            assignment.material_file.save(orig_name, ContentFile(content), save=False)
            stats["materials_restored"] += 1
        except Exception as exc:
            logger.warning("Failed to restore material_file for '%s': %s", title, exc)

    assignment.save()
    stats["assignments_created"] += 1

    # Дополнительные файлы вложений
    for att_info in a_data.get("attachments", []):
        if zip_file and att_info.get("archive_path") in zip_file.namelist():
            try:
                content = zip_file.read(att_info["archive_path"])
                orig_name = att_info.get("original_name") or "attachment.bin"
                att = AssignmentAttachment(
                    assignment=assignment,
                    title=att_info.get("title", ""),
                    order=att_info.get("order", 0),
                )
                att.file.save(orig_name, ContentFile(content), save=True)
                stats["attachments_created"] += 1
            except Exception as exc:
                logger.warning("Failed to restore attachment for '%s': %s", title, exc)

    # Вопросы теста
    for q_data in a_data.get("questions", []):
        kind = q_data.get("kind", Question.Kind.MCQ)
        if kind not in Question.Kind.values:
            kind = Question.Kind.MCQ

        question = Question.objects.create(
            assignment=assignment,
            kind=kind,
            text=q_data.get("text", ""),
            explanation=q_data.get("explanation", ""),
            points=q_data.get("points", 1),
            recording_limit_seconds=q_data.get("recording_limit_seconds"),
            order=q_data.get("order", 0),
        )
        choices = [
            Choice(
                question=question,
                text=c.get("text", ""),
                match_text=c.get("match_text", ""),
                is_correct=bool(c.get("is_correct", False)),
                order=c.get("order", 0),
            )
            for c in q_data.get("choices", [])
        ]
        if choices:
            Choice.objects.bulk_create(choices)
        stats["questions_created"] += 1

    # Для тестов с автопроверкой синхронизируем max_points
    if assignment.is_quiz and assignment.questions.exists():
        assignment.max_points = assignment.total_question_points
        assignment.save(update_fields=["max_points", "updated_at"])

    # Карточки-тренажёр
    cards = [
        Flashcard(
            assignment=assignment,
            front=card.get("front", ""),
            back=card.get("back", ""),
            example=card.get("example", ""),
            order=card.get("order", 0),
        )
        for card in a_data.get("cards", [])
    ]
    if cards:
        Flashcard.objects.bulk_create(cards)
        stats["cards_created"] += len(cards)

    # Навыки
    skill_slugs = a_data.get("skills", [])
    if skill_slugs:
        found_skills = list(Skill.objects.filter(slug__in=skill_slugs))
        if found_skills:
            assignment.skills.set(found_skills)
        else:
            apply_default_skills(assignment)
    else:
        apply_default_skills(assignment)

    # Назначения групп
    if a_data.get("groups"):
        groups = Group.objects.filter(name__in=a_data["groups"])
        if groups.exists():
            assignment.groups.add(*groups)
