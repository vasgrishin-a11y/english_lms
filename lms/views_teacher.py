"""Единая консоль преподавателя.

Один интерфейс для двух пространств: «Проверка» (очередь, оценивание) и «Курс»
(блоки, темы, задания, тесты, карточки), плюс «Ученики», «Аналитика» и
«Настройки». Системное администрирование (пользователи, права, журнал Django)
остаётся в Django Admin и доступно из настроек консоли.

Запись в учебный контент выполняется только преподавателем; сдачи и проверки
при этом идут через существующие сервисы с блокировками и контролем версий.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Max, ProtectedError, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import audience, xlsx
from .curriculum import course_tree, gradebook, queue_counts, teacher_overview
from .decorators import get_user_role, teacher_required
from .forms import (
    AssignmentForm,
    AssignmentQuickForm,
    BlockForm,
    ChapterForm,
    CommentSnippetForm,
    FlashcardBulkForm,
    FlashcardForm,
    GroupForm,
    QuestionForm,
    ReviewForm,
    StudentCreateForm,
    StudentEditForm,
    TeacherProfileForm,
    TopicForm,
)
from .library import (
    CARD_LEVELS,
    assignment_preset_groups,
    block_suggestion_groups,
    card_preset_groups,
    course_pack_groups,
    create_cards_from_preset,
    get_card_preset,
    import_course_pack,
    topic_suggestion_groups,
)
from .models import (
    Assignment,
    Block,
    CardReview,
    Chapter,
    Choice,
    CommentSnippet,
    Flashcard,
    Group,
    Profile,
    Question,
    QuestionResponse,
    Submission,
    Topic,
)
from .quiz_items import build_items, progress_of
from .services import ConflictError, review_submission
from .skills import skills_for_type, type_skill_payload
from .views import _add_validation_errors

logger = logging.getLogger(__name__)
User = get_user_model()

QUEUE_CHOICES = [
    ("pending", "Ждут проверки"),
    ("revision", "Ожидают доработки"),
    ("checked", "Проверенные"),
    ("all", "Все последние попытки"),
]
QUEUE_FILTERS = {
    "pending": [Submission.Status.SUBMITTED, Submission.Status.IN_REVIEW],
    "revision": [Submission.Status.NEEDS_REVISION],
    "checked": [Submission.Status.CHECKED],
}
ORDER_CHOICES = {
    "fifo": ("submitted_at", "Сначала старые"),
    "new": ("-submitted_at", "Сначала новые"),
}


def _visible_snippets(user):
    return CommentSnippet.objects.filter(Q(is_shared=True) | Q(author=user)).order_by("title", "pk")


def _delete(request, obj, redirect_to, protected_message, blocked=False):
    """Удаление с двумя предохранителями: явный запрет и защита внешних ключей.

    Каскад Django молча удалил бы темы и задания вместе с блоком, поэтому
    содержимое проверяется до удаления, а вместо удаления предлагается скрыть.
    """
    if blocked:
        messages.error(request, protected_message)
        return redirect(redirect_to)
    try:
        obj.delete()
    except ProtectedError:
        messages.error(request, protected_message)
    else:
        messages.success(request, f"Удалено: {obj}")
    return redirect(redirect_to)


def block_tree_stats(block):
    """Сколько вложенного уйдёт вместе с классом: главы, темы, задания, вопросы, карточки."""
    assignments = Assignment.objects.filter(topic__block=block)
    return {
        "chapters": block.chapters.count(),
        "topics": block.topics.count(),
        "assignments": assignments.count(),
        "questions": Question.objects.filter(assignment__in=assignments).count(),
        "cards": Flashcard.objects.filter(assignment__in=assignments).count(),
        "submissions": Submission.objects.filter(assignment__in=assignments).count(),
    }


def chapter_tree_stats(chapter):
    """Сколько вложенного уйдёт вместе с главой: темы, задания, вопросы, карточки."""
    assignments = Assignment.objects.filter(topic__chapter=chapter)
    return {
        "chapters": 1,
        "topics": chapter.topics.count(),
        "assignments": assignments.count(),
        "questions": Question.objects.filter(assignment__in=assignments).count(),
        "cards": Flashcard.objects.filter(assignment__in=assignments).count(),
        "submissions": Submission.objects.filter(assignment__in=assignments).count(),
    }


def topic_tree_stats(topic):
    assignments = topic.assignments.all()
    return {
        "chapters": 0,
        "topics": 1,
        "assignments": assignments.count(),
        "questions": Question.objects.filter(assignment__in=assignments).count(),
        "cards": Flashcard.objects.filter(assignment__in=assignments).count(),
        "submissions": Submission.objects.filter(assignment__in=assignments).count(),
    }


def _delete_cascade(request, obj, redirect_to, kind, stats):
    """Удалить класс, главу или тему целиком: вместе с темами, заданиями и карточками.

    Работы учеников неприкосновенны: если в поддереве есть сдачи, удаление
    запрещено и предлагается архив — иначе пропали бы оценки и история попыток.
    Требуется явное подтверждение ``confirm``: одна кнопка не должна сносить
    полкурса по случайному нажатию.
    """
    if request.POST.get("confirm") != "1":
        messages.error(
            request,
            f"Удаление {kind} не подтверждено: откройте подтверждение и повторите действие.",
        )
        return redirect(redirect_to)
    if stats["submissions"]:
        messages.error(
            request,
            f"{kind.capitalize()} «{obj}» нельзя удалить: есть работы учеников "
            f"({stats['submissions']}). Используйте архив — история и оценки сохранятся.",
        )
        return redirect(redirect_to)
    label, pk = str(obj), obj.pk
    try:
        obj.delete()
    except ProtectedError:
        messages.error(
            request,
            f"{kind.capitalize()} «{label}» нельзя удалить: есть связанные данные. "
            "Используйте архив.",
        )
        return redirect(redirect_to)
    parts = [
        f"тем {stats['topics']}",
        f"заданий {stats['assignments']}",
        f"вопросов {stats['questions']}",
        f"карточек {stats['cards']}",
    ]
    if stats.get("chapters"):
        parts.insert(0, f"глав {stats['chapters']}")
    logger.info(
        "curriculum_delete kind=%s pk=%s chapters=%s topics=%s assignments=%s questions=%s cards=%s",
        kind,
        pk,
        stats.get("chapters", 0),
        stats["topics"],
        stats["assignments"],
        stats["questions"],
        stats["cards"],
    )
    messages.success(
        request,
        f"Удалено: {kind} «{label}» вместе с содержимым — " + ", ".join(parts) + ".",
    )
    return redirect(redirect_to)


# ── Главная консоли ────────────────────────────────────────────────────────
@teacher_required
@require_GET
def console_home(request):
    overview = teacher_overview()
    waiting = (
        Submission.objects.latest_attempts()
        .exclude(assignment__assignment_type__in=Assignment.NO_SUBMISSION_TYPES)
        .filter(status__in=QUEUE_FILTERS["pending"])
        .select_related("student", "assignment__topic__block", "assignment__topic__chapter")
        .order_by("submitted_at", "pk")[:6]
    )
    drafts = Assignment.objects.filter(status=Assignment.Publication.DRAFT).select_related(
        "topic__block", "topic__chapter"
    )[:5]
    blocks_data, _ = course_tree(query="", teacher_view=True)
    request.session["_queue_counts"] = {"ts": timezone.now().timestamp(), **overview["queue"]}
    return render(
        request,
        "lms/teacher_home.html",
        {
            "overview": overview,
            "waiting": list(waiting),
            "drafts": list(drafts),
            "blocks_data": blocks_data,
            "workspace": "home",
        },
    )


# ── Пространство «Проверка» ────────────────────────────────────────────────
def _queue_queryset(request):
    queue = request.GET.get("status", "pending")
    if queue not in dict(QUEUE_CHOICES):
        queue = "pending"
    order = request.GET.get("order", "fifo")
    if order not in ORDER_CHOICES:
        order = "fifo"
    submissions = (
        Submission.objects.latest_attempts()
        # Карточки и материалы для занятий не сдаются и не проверяются.
        .exclude(assignment__assignment_type__in=Assignment.NO_SUBMISSION_TYPES)
        .select_related(
            "student", "assignment__topic__block", "assignment__topic__chapter", "feedback"
        )
        .order_by(ORDER_CHOICES[order][0], "pk")
    )
    if queue in QUEUE_FILTERS:
        submissions = submissions.filter(status__in=QUEUE_FILTERS[queue])
    query = request.GET.get("q", "").strip()[:200]
    if query:
        submissions = submissions.filter(
            Q(student__username__icontains=query)
            | Q(student__first_name__icontains=query)
            | Q(student__last_name__icontains=query)
            | Q(assignment__title__icontains=query)
            | Q(assignment__topic__title__icontains=query)
            | Q(assignment__topic__block__name__icontains=query)
        )
    student_id = request.GET.get("student")
    if student_id and student_id.isdigit():
        submissions = submissions.filter(student_id=int(student_id))
    assignment_id = request.GET.get("assignment")
    if assignment_id and assignment_id.isdigit():
        submissions = submissions.filter(assignment_id=int(assignment_id))
    return submissions, queue, order, query


@teacher_required
@require_GET
def review_queue(request):
    """Очередь проверки: счётчики, фильтры, сортировка FIFO, поиск."""
    submissions, queue, order, query = _queue_queryset(request)
    counts = queue_counts()
    # Только в атрибут запроса: запись в сессию добавила бы три запроса на экран.
    request.lms_queue_counts = counts
    page = Paginator(submissions, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "lms/teacher_submissions.html",
        {
            "submissions": page,
            "page_obj": page,
            "query": query,
            "queue": queue,
            "order": order,
            "order_choices": ORDER_CHOICES,
            "queue_choices": QUEUE_CHOICES,
            "counts": counts,
            "workspace": "review",
        },
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def review_detail(request, pk):
    """Разбор одной работы: ответ слева, решение справа, переход к следующей."""
    submission = get_object_or_404(
        Submission.objects.select_related(
            "student", "assignment__topic__block", "feedback__teacher"
        ),
        pk=pk,
    )
    attempts = list(
        Submission.objects.filter(student=submission.student, assignment=submission.assignment)
        .select_related("feedback__teacher")
        .order_by("-version")
    )
    latest = attempts[0]
    is_latest = latest.pk == submission.pk
    feedback = getattr(submission, "feedback", None)
    quiz = getattr(submission, "quiz_attempt", None)
    questions = (
        list(submission.assignment.questions.prefetch_related("choices").order_by("order", "pk"))
        if submission.assignment.is_quiz
        else []
    )
    responses = {
        item.question_id: item
        for item in QuestionResponse.objects.filter(submission=submission).select_related(
            "question"
        )
    }
    items = build_items(
        submission.assignment,
        questions,
        responses,
        closed_round=True,
        legacy=quiz.answers if quiz else None,
    )
    manual_responses = []
    for item in items:
        if item["manual"] and item["response"] is not None:
            item["response"].number = item["number"]
            manual_responses.append(item["response"])
    form = ReviewForm(
        request.POST if request.method == "POST" else None,
        submission=submission,
        feedback=feedback,
        manual_responses=manual_responses if is_latest else (),
    )
    for item in items:
        if item["manual"] and item["response"] is not None and is_latest:
            item["points_field"] = form.item_field(item["response"].pk)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            review_submission(
                teacher=request.user, submission_id=submission.pk, **form.cleaned_data
            )
        except ConflictError as exc:
            form.add_error(None, str(exc))
            status = 409
        except ValidationError as exc:
            _add_validation_errors(form, exc)
        else:
            snippet_id = request.POST.get("snippet_used")
            if snippet_id and snippet_id.isdigit():
                CommentSnippet.objects.filter(pk=int(snippet_id)).update(
                    usage_count=F("usage_count") + 1
                )
            messages.success(request, "Проверка сохранена для выбранной попытки.")
            if request.POST.get("next") == "1":
                nxt = _neighbour(request, submission, direction=1)
                if nxt:
                    return redirect("teacher_submission_review", pk=nxt.pk)
            return redirect("teacher_submission_review", pk=submission.pk)

    queue_ids, position = _queue_positions(request, submission)
    return render(
        request,
        "lms/teacher_submission_review.html",
        {
            "submission": submission,
            "feedback": feedback,
            "form": form,
            "is_latest": is_latest,
            "latest": latest,
            "attempts": attempts,
            "quiz": quiz,
            "conflict": status == 409,
            "page_obj": Paginator(attempts, settings.LMS_PAGE_SIZE).get_page(
                request.GET.get("page")
            ),
            "events": submission.events.select_related("actor").order_by("-created_at", "-pk")[:20],
            "snippets": _visible_snippets(request.user),
            "nav": {
                "prev": queue_ids[0] if queue_ids else None,
                "next": queue_ids[1] if queue_ids else None,
                "position": position,
            },
            "questions": questions,
            "items": items,
            "items_progress": progress_of(items) if items else None,
            "workspace": "review",
        },
        status=status,
    )


def _filtered_queue(request, exclude_pk=None):
    submissions, _, _, _ = _queue_queryset(request)
    return list(submissions.values_list("pk", flat=True))


def _queue_positions(request, submission):
    """Соседи по текущему фильтру очереди: (prev_pk, next_pk), позиция."""
    ids = _filtered_queue(request)
    if not ids:
        ids = list(
            Submission.objects.latest_attempts()
            .exclude(assignment__assignment_type__in=Assignment.NO_SUBMISSION_TYPES)
            .order_by("submitted_at", "pk")
            .values_list("pk", flat=True)
        )
    if submission.pk not in ids:
        return (None, None), 0
    index = ids.index(submission.pk)
    previous = ids[index - 1] if index > 0 else None
    following = ids[index + 1] if index < len(ids) - 1 else None
    return (previous, following), index + 1


def _neighbour(request, submission, direction):
    """Сосед по текущему фильтру очереди.

    После сохранения проверенная работа обычно выпадает из фильтра «ждут»,
    поэтому продолжаем с первой оставшейся — так очередь разбирается до конца.
    """
    ids = _filtered_queue(request)
    if submission.pk in ids:
        index = ids.index(submission.pk)
        neighbours = ids[index - 1 : index] if direction < 0 else ids[index + 1 : index + 2]
    else:
        remaining = [pk for pk in ids if pk != submission.pk]
        neighbours = remaining[-1:] if direction < 0 else remaining[:1]
    return Submission.objects.filter(pk=neighbours[0]).first() if neighbours else None


# ── Пространство «Курс» ────────────────────────────────────────────────────
@teacher_required
@require_GET
def curriculum(request):
    """Карта курса: классы → главы → темы → задания и наборы карточек со счётчиками."""
    query = request.GET.get("q", "").strip()[:200]
    blocks_data, totals = course_tree(query=query, teacher_view=True)
    return render(
        request,
        "lms/teacher_curriculum.html",
        {
            "blocks_data": blocks_data,
            "totals": totals,
            "query": query,
            "workspace": "curriculum",
        },
    )


def _wants_json(request):
    """Быстрые действия доски отвечают JSON'ом, когда их вызывает lms.js."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _renumber(items, *, force_pks=()):
    """Перенумеровать список в его текущей последовательности шагом 1."""
    for position, item in enumerate(items, start=1):
        if item.order != position or item.pk in force_pks:
            item.order = position
            item.save(update_fields=["order", "updated_at"])


def _place_assignment(assignment, target_topic, before):
    """Поставить задание на точное место на доске (перетаскивание).

    ``target_topic`` — тема-приёмник (может совпадать с текущей), ``before`` —
    задание, перед которым вставить; ``None`` ставит задание в конец темы.
    """
    moved_across = target_topic.pk != assignment.topic_id
    with transaction.atomic():
        source = [
            item
            for item in assignment.topic.assignments.order_by("order", "pk")
            if item.pk != assignment.pk
        ]
        siblings = (
            list(target_topic.assignments.order_by("order", "pk")) if moved_across else source
        )
        index = len(siblings)
        if before is not None:
            index = next(
                (position for position, item in enumerate(siblings) if item.pk == before.pk),
                len(siblings),
            )
        siblings.insert(index, assignment)
        if moved_across:
            assignment.topic = target_topic
            assignment.save(update_fields=["topic", "updated_at"])
        _renumber(siblings, force_pks={assignment.pk})
        if moved_across:
            _renumber(source)
    return index


def _place_topic(topic, before, target_chapter=None):
    """Поставить тему на точное место внутри главы (перетаскивание).

    ``target_chapter`` — глава-приёмник того же класса (может совпадать с
    текущей); ``before`` — тема, перед которой вставить; ``None`` — в конец.
    """
    target_chapter = target_chapter or topic.chapter
    moved_across = target_chapter.pk != topic.chapter_id
    with transaction.atomic():
        source = [
            item for item in topic.chapter.topics.order_by("order", "pk") if item.pk != topic.pk
        ]
        siblings = list(target_chapter.topics.order_by("order", "pk")) if moved_across else source
        index = len(siblings)
        if before is not None:
            index = next(
                (position for position, item in enumerate(siblings) if item.pk == before.pk),
                len(siblings),
            )
        siblings.insert(index, topic)
        if moved_across:
            topic.chapter = target_chapter
            topic.block_id = target_chapter.block_id
            topic.save(update_fields=["chapter", "block", "updated_at"])
        _renumber(siblings, force_pks={topic.pk})
        if moved_across:
            _renumber(source)
    return index


def _place_chapter(chapter, before):
    """Поставить главу на точное место внутри её класса (перетаскивание)."""
    siblings = [
        item for item in chapter.block.chapters.order_by("order", "pk") if item.pk != chapter.pk
    ]
    index = len(siblings)
    if before is not None:
        index = next(
            (position for position, item in enumerate(siblings) if item.pk == before.pk),
            len(siblings),
        )
    siblings.insert(index, chapter)
    with transaction.atomic():
        _renumber(siblings, force_pks={chapter.pk})
    return index


def _generate_unique_topic_slug(block, base_slug):
    """Сгенерировать уникальный slug темы внутри блока."""
    base = (base_slug or "tema").strip()[:80] or "tema"
    slug = base
    counter = 2
    while Topic.objects.filter(block=block, slug=slug).exists():
        suffix = f"-{counter}"
        slug = (
            (base[: 80 - len(suffix)] + suffix) if len(base) + len(suffix) > 80 else base + suffix
        )
        counter += 1
        if counter > 1000:
            slug = f"{base[:70]}-{counter}"
            break
    return slug


def _clone_assignment_full(source, target_topic, order=None):
    """Клонировать задание со всеми вложениями, карточками, вопросами."""
    from .models import AssignmentAttachment, Choice, Flashcard, Question

    new_order = order if order is not None else source.order
    copy = Assignment(
        topic=target_topic,
        title=source.title,
        description=source.description,
        assignment_type=source.assignment_type,
        material_file=source.material_file,
        deadline=source.deadline,
        max_points=source.max_points,
        max_tries=source.max_tries,
        allow_retake=source.allow_retake,
        exam_mode=source.exam_mode,
        recording_limit_seconds=source.recording_limit_seconds,
        order=new_order,
        is_active=source.is_active,
        status=Assignment.Publication.DRAFT,
        publish_at=None,
    )
    copy.save()
    copy.skills.set(source.skills.all())
    copy.groups.set(source.groups.all())
    copy.assigned_students.set(source.assigned_students.all())
    # attachments
    for att in source.attachments.order_by("order", "pk"):
        AssignmentAttachment.objects.create(assignment=copy, file=att.file, order=att.order)
    # flashcards
    for card in source.cards.order_by("order", "pk"):
        Flashcard.objects.create(
            assignment=copy,
            front=card.front,
            back=card.back,
            example=card.example,
            order=card.order,
        )
    # questions
    for question in source.questions.prefetch_related("choices").order_by("order", "pk"):
        new_q = Question.objects.create(
            assignment=copy,
            kind=question.kind,
            text=question.text,
            explanation=question.explanation,
            points=question.points,
            order=question.order,
            recording_limit_seconds=question.recording_limit_seconds,
        )
        Choice.objects.bulk_create(
            [
                Choice(
                    question=new_q,
                    text=choice.text,
                    match_text=choice.match_text,
                    is_correct=choice.is_correct,
                    order=choice.order,
                )
                for choice in question.choices.all()
            ]
        )
    return copy


def _clone_topic_to_block(source_topic, target_block, before=None, target_chapter=None):
    """Скопировать тему в другой класс вместе со всеми заданиями (оригинал не удаляется).

    Копия попадает в ``target_chapter`` (глава целевого класса) или, если глава
    не указана, — в главу «Общее» целевого класса.
    """
    with transaction.atomic():
        if target_chapter is None or target_chapter.block_id != target_block.pk:
            target_chapter = target_block.default_chapter()
        max_order = target_chapter.topics.aggregate(m=Max("order"))["m"] or 0
        new_slug = _generate_unique_topic_slug(target_block, source_topic.slug)
        # если в целевом классе уже есть тема с таким же title, добавим (копия)
        title = source_topic.title
        if target_block.topics.filter(title=title).exists():
            title = f"{title} (копия)"
            if len(title) > 200:
                title = title[:200]
        new_topic = Topic.objects.create(
            block=target_block,
            chapter=target_chapter,
            title=title,
            slug=new_slug,
            description=source_topic.description,
            order=max_order + 1,
            is_active=source_topic.is_active,
        )
        new_topic.groups.set(source_topic.groups.all())
        new_topic.students.set(source_topic.students.all())
        for assignment in source_topic.assignments.order_by("order", "pk"):
            _clone_assignment_full(assignment, new_topic)
        if before is not None and before.chapter_id == target_chapter.pk:
            _place_topic(new_topic, before)
        else:
            _renumber(list(target_chapter.topics.order_by("order", "pk")), force_pks={new_topic.pk})
        return new_topic


def _quick_outcome(request, done, success, failure):
    """Ответ быстрого действия: JSON для fetch, сообщение+редирект без JS."""
    if _wants_json(request):
        payload = {"ok": done} if done else {"ok": False, "error": failure}
        return JsonResponse(payload, status=200 if done else 400)
    if done:
        messages.success(request, success)
    else:
        messages.info(request, failure)
    return redirect(request.POST.get("next") or "teacher_curriculum")


def _reorder(siblings, obj, direction):
    """Поменять объект местами с соседом по порядку.

    Если порядок совпадает (все нули), сначала перенумеровываем список — иначе
    обмен был бы невидимым. Один запрос на чтение и два на запись.
    """
    items = list(siblings.order_by("order", "pk"))
    if len(items) < 2 or direction not in {"up", "down"}:
        return False
    index = next((position for position, item in enumerate(items) if item.pk == obj.pk), None)
    if index is None:
        return False
    if len({item.order for item in items}) != len(items):
        for position, item in enumerate(items, start=1):
            if item.order != position:
                item.order = position
                item.save(update_fields=["order", "updated_at"])
        items = list(siblings.order_by("order", "pk"))
        index = next(position for position, item in enumerate(items) if item.pk == obj.pk)
    target = index - 1 if direction == "up" else index + 1
    if not 0 <= target < len(items):
        return False
    first, second = items[index], items[target]
    first.order, second.order = second.order, first.order
    first.save(update_fields=["order", "updated_at"])
    second.save(update_fields=["order", "updated_at"])
    return True


@teacher_required
@require_GET
def library(request):
    """Библиотека готовых курсов: наборы «блоки → темы → задания» одним действием."""
    return render(
        request,
        "lms/teacher_library.html",
        {"pack_groups": course_pack_groups(), "workspace": "curriculum"},
    )


@teacher_required
@require_POST
def library_import(request, slug):
    """Скопировать набор в курс. Задания попадают в черновики, дубликаты пропускаются."""
    try:
        result = import_course_pack(slug)
    except LookupError:
        raise Http404("Неизвестный набор библиотеки")
    created, skipped = result["created"], result["skipped"]
    parts = [
        f"классов {created['blocks']}",
        f"тем {created['topics']}",
        f"заданий {created['assignments']}",
    ]
    if created["questions"]:
        parts.append(f"вопросов {created['questions']}")
    if created["cards"]:
        parts.append(f"карточек {created['cards']}")
    message = (
        "Добавлено в курс: "
        + ", ".join(parts)
        + ". Задания созданы черновиками — проверьте и опубликуйте."
    )
    skipped_total = sum(skipped.values())
    if skipped_total:
        message += f" Пропущено дубликатов: {skipped_total}."
    messages.success(request, message)
    logger.info("Library pack %s imported by user %s: %s", slug, request.user.pk, created)
    return redirect("teacher_curriculum")


@teacher_required
@require_GET
def archive(request):
    """Отдельная папка архива контента.

    Архивирование — это мягкое удаление: история сдач и ссылки на материалы
    сохраняются, а ученики больше не видят элемент. Для блока и темы архив
    распространяется на вложенное содержимое, чтобы не оставить «висящие»
    активные задания.
    """
    archived_blocks = (
        Block.objects.filter(is_active=False)
        .annotate(
            chapter_total=Count("chapters", distinct=True),
            topic_total=Count("topics", distinct=True),
            assignment_total=Count("topics__assignments", distinct=True),
        )
        .order_by("order", "name", "pk")
    )
    archived_chapters = (
        Chapter.objects.filter(is_active=False, block__is_active=True)
        .select_related("block")
        .annotate(
            topic_total=Count("topics", distinct=True),
            assignment_total=Count("topics__assignments", distinct=True),
        )
        .order_by("block__order", "order", "title", "pk")
    )
    archived_topics = (
        Topic.objects.filter(is_active=False, chapter__is_active=True, block__is_active=True)
        .select_related("block", "chapter")
        .annotate(assignment_total=Count("assignments"))
        .order_by("block__order", "chapter__order", "order", "title", "pk")
    )
    archived_assignments = (
        Assignment.objects.filter(
            is_active=False,
            topic__is_active=True,
            topic__chapter__is_active=True,
            topic__block__is_active=True,
        )
        .select_related("topic__block", "topic__chapter", "topic")
        .annotate(submission_total=Count("submissions"))
        .order_by(
            "topic__block__order", "topic__chapter__order", "topic__order", "order", "title", "pk"
        )
    )
    return render(
        request,
        "lms/teacher_archive.html",
        {
            "archived_blocks": archived_blocks,
            "archived_chapters": archived_chapters,
            "archived_topics": archived_topics,
            "archived_assignments": archived_assignments,
            "archive_total": (
                archived_blocks.count()
                + archived_chapters.count()
                + archived_topics.count()
                + archived_assignments.count()
            ),
            "workspace": "archive",
        },
    )


ARCHIVE_KINDS = {"block": Block, "chapter": Chapter, "topic": Topic, "assignment": Assignment}
ARCHIVE_LABELS = {"block": "Класс", "chapter": "Глава", "topic": "Тема", "assignment": "Задание"}


def _archive_queryset(kind, pk):
    model = ARCHIVE_KINDS.get(kind)
    if model is None:
        raise Http404("Неизвестный тип элемента архива")
    return get_object_or_404(model, pk=pk)


def _set_archived(item, archived, restore_tree=False):
    """Скрыть/восстановить элемент и, при необходимости, его дочерние узлы."""
    now = timezone.now()
    active = not archived
    item.is_active = active
    item.save(update_fields=["is_active", "updated_at"])
    if not (archived or restore_tree):
        return
    if isinstance(item, Block):
        Chapter.objects.filter(block=item).update(is_active=active, updated_at=now)
        Topic.objects.filter(block=item).update(is_active=active, updated_at=now)
        Assignment.objects.filter(topic__block=item).update(is_active=active, updated_at=now)
    elif isinstance(item, Chapter):
        Topic.objects.filter(chapter=item).update(is_active=active, updated_at=now)
        Assignment.objects.filter(topic__chapter=item).update(is_active=active, updated_at=now)
    elif isinstance(item, Topic):
        Assignment.objects.filter(topic=item).update(is_active=active, updated_at=now)


@teacher_required
@require_POST
@transaction.atomic
def archive_item(request, kind, pk):
    """Переместить класс, главу, тему или задание в архив либо восстановить."""
    item = _archive_queryset(kind, pk)
    restore = request.POST.get("action") == "restore"
    restore_tree = request.POST.get("restore_tree") == "1"
    _set_archived(item, archived=not restore, restore_tree=restore_tree)
    label = ARCHIVE_LABELS[kind]
    title = str(item)
    if restore:
        message = (
            f"{label} «{title}» восстановлен вместе с содержимым."
            if restore_tree and kind in {"block", "chapter", "topic"}
            else f"{label} «{title}» восстановлен."
        )
    else:
        message = f"{label} «{title}» перемещён в архив. История сдач сохранена."
    messages.success(request, message)
    return redirect("teacher_archive" if restore else "teacher_curriculum")


@login_required
@require_POST
def teacher_impersonate_start(request, pk):
    """Открыть режим ученика без передачи учителю прав ученика навсегда.

    Реальный пользователь в сессии временно меняется на ученика; исходный
    преподаватель хранится в подписанной Django-сессии и восстанавливается
    отдельной кнопкой в заметном баннере.
    """
    if request.session.get("impersonating_teacher_id"):
        return redirect("student_home")
    if get_user_role(request.user) != Profile.Role.TEACHER:
        raise PermissionDenied
    student = get_object_or_404(User, pk=pk, profile__role=Profile.Role.STUDENT, is_active=True)
    teacher = request.user
    login(request, student, backend="django.contrib.auth.backends.ModelBackend")
    # login() flushes a session belonging to another authenticated user, so
    # the return marker must be written after switching to the student.
    request.session["impersonating_teacher_id"] = teacher.pk
    request.session["impersonating_teacher_name"] = teacher.get_full_name() or teacher.username
    request.session["impersonating_student_id"] = student.pk
    messages.info(
        request,
        f"Вы смотрите приложение глазами ученика «{student.get_full_name() or student.username}».",
    )
    return redirect("student_home")


@login_required
@require_POST
def teacher_impersonate_stop(request):
    """Завершить просмотр и вернуть исходную учётную запись преподавателя."""
    teacher_id = request.session.get("impersonating_teacher_id")
    student_id = request.session.get("impersonating_student_id")
    if not teacher_id:
        raise PermissionDenied
    teacher = User.objects.filter(
        pk=teacher_id, profile__role=Profile.Role.TEACHER, is_active=True
    ).first()
    if teacher is None:
        logout(request)
        return redirect("login")
    login(request, teacher, backend="django.contrib.auth.backends.ModelBackend")
    for key in (
        "impersonating_teacher_id",
        "impersonating_teacher_name",
        "impersonating_student_id",
    ):
        request.session.pop(key, None)
    messages.success(request, "Вы вернулись в режим преподавателя.")
    if student_id:
        return redirect("teacher_student_detail", pk=student_id)
    return redirect("teacher_home")


def _submission_progress(assignment):
    """Кто из ожидаемых учеников уже сдал задание, а кто нет.

    Ожидаемые — активные ученики группы задания (или все ученики, если группа
    не выбрана) плюс назначенные персонально. Используется в боковой панели
    формы задания, чтобы преподаватель видел «не сдали» без перехода в очередь.
    Для нового задания возвращает None: сдавать ещё нечего.
    Для заданий без сдачи (карточки, материалы) тоже None: «не сдавших» нет.
    """
    if not assignment.pk or assignment.is_no_submission:
        return None
    expected_ids = set(audience.expected_students(assignment).values_list("pk", flat=True))
    submitted_ids = set(
        Submission.objects.filter(assignment=assignment).values_list("student_id", flat=True)
    )
    students = list(
        User.objects.filter(pk__in=expected_ids).order_by("last_name", "first_name", "username")
    )
    pending = [student for student in students if student.pk not in submitted_ids]
    return {
        "total": len(students),
        "submitted": len(students) - len(pending),
        "pending": pending[:8],
        "pending_more": max(len(pending) - 8, 0),
    }


def _flat_presets(groups):
    """Плоский список шаблонов в порядке групп: его читает lms.js при подстановке."""
    return [item for group in groups for item in group["items"]]


def _copy_topics_into_block(request, block, ids):
    """Скопировать темы ``ids`` из других классов в ``block``; вернуть число копий.

    Ошибки выбора (пусто, слишком много, список устарел) сообщаются через
    ``messages`` — вызывающему остаётся только решить, куда перенаправить.
    """
    if not ids:
        messages.error(request, "Выберите хотя бы одну тему.")
        return 0
    if len(ids) > 100 or any(not str(value).isdigit() for value in ids):
        messages.error(request, "Выберите не более 100 тем из списка.")
        return 0
    ids = set(map(int, ids))
    with transaction.atomic():
        Block.objects.select_for_update().get(pk=block.pk)
        sources = list(
            Topic.objects.filter(pk__in=ids, is_active=True)
            .exclude(block=block)
            .select_related("block")
            .order_by("block__order", "order", "pk")
        )
        if len(sources) != len(ids):
            messages.error(request, "Список тем изменился. Выберите темы из других классов заново.")
            return 0
        for source in sources:
            _clone_topic_to_block(source, block)
    messages.success(request, f"Скопировано тем: {len(sources)}. Задания сохранены как черновики.")
    return len(sources)


def _copyable_topics(block=None):
    """Активные темы других классов — кандидаты на копирование в ``block``."""
    qs = Topic.objects.filter(is_active=True)
    if block is not None:
        qs = qs.exclude(block=block)
    return (
        qs.select_related("block", "chapter")
        .annotate(assignment_count=Count("assignments"))
        .order_by("block__order", "chapter__order", "order", "title")[:100]
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def block_form(request, pk=None):
    block = get_object_or_404(Block, pk=pk) if pk else None
    # Accept the legacy single-copy field as well as checkbox selections.
    if request.method == "POST" and (
        request.POST.get("copy_topics") or request.POST.get("copy_topic_id")
    ):
        if not block:
            messages.error(request, "Сначала сохраните класс, затем копируйте темы.")
            return redirect("teacher_block_new")
        ids = request.POST.getlist("copy_topic_ids") or request.POST.getlist("copy_topic_id")
        _copy_topics_into_block(request, block, ids)
        return redirect("teacher_block_edit", pk=block.pk)
    form = BlockForm(request.POST or None, instance=block)
    if request.method == "POST" and form.is_valid():
        instance = form.save()
        messages.success(request, f"Класс сохранён: {instance.name}")
        # При создании класса темы других классов выбираются сразу в той же форме:
        # не нужно сохранять пустой класс и возвращаться за копированием.
        if block is None and request.POST.getlist("copy_topic_ids"):
            _copy_topics_into_block(request, instance, request.POST.getlist("copy_topic_ids"))
        return redirect("teacher_curriculum")
    groups = block_suggestion_groups()
    other_topics = _copyable_topics(block)
    if block:
        all_blocks = Block.objects.exclude(pk=block.pk).order_by("order", "name")
    else:
        all_blocks = Block.objects.order_by("order", "name")
    return render(
        request,
        "lms/teacher_block_form.html",
        {
            "form": form,
            "block": block,
            "suggestion_groups": groups,
            "suggestion_payload": _flat_presets(groups),
            "other_topics": other_topics,
            "all_blocks": all_blocks,
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def block_delete(request, pk):
    """Удалить класс целиком: главы, темы, задания, вопросы теста и карточки — вместе с ним."""
    block = get_object_or_404(Block, pk=pk)
    target = "teacher_archive" if request.POST.get("from_archive") == "1" else "teacher_curriculum"
    if not block.topics.exists():  # пустой класс: прежнее простое удаление
        return _delete(request, block, target, "Класс нельзя удалить: есть связанные данные.")
    return _delete_cascade(request, block, target, "класс", block_tree_stats(block))


@teacher_required
@require_POST
def block_move(request, pk):
    """Переместить класс в дорожной карте: порядок виден и ученикам, и в журнале."""
    block = get_object_or_404(Block, pk=pk)
    if _reorder(Block.objects.all(), block, request.POST.get("direction")):
        messages.success(request, f"Порядок класса изменён: {block.name}")
    else:
        messages.info(request, "Крайний класс: перемещать некуда.")
    return redirect("teacher_curriculum")


# ── Главы ──────────────────────────────────────────────────────────────────
@teacher_required
@require_http_methods(["GET", "POST"])
def chapter_form(request, pk=None):
    """Создать или изменить главу — подход тот же, что у темы, уровнем выше."""
    chapter = get_object_or_404(Chapter.objects.select_related("block"), pk=pk) if pk else None
    initial = {}
    if request.GET.get("block", "").isdigit():
        initial["block"] = request.GET["block"]
    form = ChapterForm(request.POST or None, instance=chapter, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        instance = form.save()
        messages.success(request, f"Глава сохранена: {instance.title}")
        return redirect(reverse("teacher_curriculum") + f"#block-{instance.block_id}")
    parent = chapter.block if chapter else _block_from_query(request)
    return render(
        request,
        "lms/teacher_chapter_form.html",
        {
            "form": form,
            "chapter": chapter,
            "blocks": Block.objects.order_by("order", "name"),
            "inherited_audience": audience.describe(parent) if parent else None,
            "workspace": "curriculum",
        },
    )


def _block_from_query(request):
    raw = request.GET.get("block", "")
    return Block.objects.filter(pk=int(raw)).first() if raw.isdigit() else None


@teacher_required
@require_POST
def chapter_delete(request, pk):
    """Удалить главу целиком вместе с темами, заданиями, вопросами и карточками."""
    chapter = get_object_or_404(Chapter, pk=pk)
    target = "teacher_archive" if request.POST.get("from_archive") == "1" else "teacher_curriculum"
    if not chapter.topics.exists():
        return _delete(request, chapter, target, "Главу нельзя удалить: есть связанные данные.")
    return _delete_cascade(request, chapter, target, "главу", chapter_tree_stats(chapter))


@teacher_required
@require_POST
def chapter_move(request, pk):
    """Порядок глав внутри класса: кнопками (direction) или перетаскиванием (before)."""
    chapter = get_object_or_404(Chapter.objects.select_related("block"), pk=pk)
    if "before" in request.POST:
        before = None
        if request.POST.get("before"):
            before = get_object_or_404(Chapter, pk=request.POST["before"], block=chapter.block)
        _place_chapter(chapter, before)
        return _quick_outcome(request, True, f"Порядок главы изменён: {chapter.title}", "")
    done = _reorder(
        Chapter.objects.filter(block=chapter.block), chapter, request.POST.get("direction")
    )
    return _quick_outcome(
        request,
        done,
        f"Порядок главы изменён: {chapter.title}",
        "Крайняя глава в классе: перемещать некуда.",
    )


@teacher_required
@require_POST
def chapter_publish(request, pk):
    chapter = get_object_or_404(Chapter, pk=pk)
    active = request.POST.get("active") == "1"
    chapter.is_active = active
    chapter.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"Глава «{chapter.title}» опубликована." if active else f"Глава «{chapter.title}» скрыта.",
    )
    return redirect(request.POST.get("next") or "teacher_curriculum")


@teacher_required
@require_POST
def chapter_assignments_publish(request, pk):
    """Скопом: статус всех заданий главы (все её темы разом)."""
    chapter = get_object_or_404(Chapter, pk=pk)
    return _bulk_publish_assignments(
        request, Assignment.objects.filter(topic__chapter=chapter), f"главы «{chapter.title}»"
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def topic_form(request, pk=None):
    topic = (
        get_object_or_404(Topic.objects.select_related("block", "chapter"), pk=pk) if pk else None
    )
    initial = {}
    if request.GET.get("chapter", "").isdigit():
        chapter = Chapter.objects.filter(pk=int(request.GET["chapter"])).first()
        if chapter is not None:
            initial["chapter"] = chapter.pk
            initial["block"] = chapter.block_id
    if request.GET.get("block", "").isdigit():
        initial["block"] = request.GET["block"]
    form = TopicForm(request.POST or None, instance=topic, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        instance = form.save()
        messages.success(request, f"Тема сохранена: {instance.title}")
        return redirect(reverse("teacher_curriculum") + f"#block-{instance.block_id}")
    groups = topic_suggestion_groups()
    if topic is not None:
        parent = topic.chapter
    else:
        parent = None
        if request.GET.get("chapter", "").isdigit():
            parent = Chapter.objects.filter(pk=int(request.GET["chapter"])).first()
        parent = parent or _block_from_query(request)
    return render(
        request,
        "lms/teacher_topic_form.html",
        {
            "form": form,
            "topic": topic,
            "blocks": Block.objects.order_by("order", "name"),
            "suggestion_groups": groups,
            "suggestion_payload": _flat_presets(groups),
            "inherited_audience": audience.describe(parent) if parent else None,
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def topic_delete(request, pk):
    """Удалить тему целиком вместе с её заданиями, вопросами и карточками."""
    topic = get_object_or_404(Topic, pk=pk)
    target = "teacher_archive" if request.POST.get("from_archive") == "1" else "teacher_curriculum"
    if not topic.assignments.exists():
        return _delete(request, topic, target, "Тему нельзя удалить: есть связанные данные.")
    return _delete_cascade(request, topic, target, "тему", topic_tree_stats(topic))


def _target_chapter(request, block):
    """Глава-приёмник из запроса (``chapter``/``target_chapter``) — только из ``block``."""
    raw = request.POST.get("target_chapter") or request.POST.get("chapter") or ""
    if not str(raw).isdigit():
        return None
    return Chapter.objects.filter(pk=int(raw), block=block).first()


@teacher_required
@require_POST
def topic_move(request, pk):
    """Переместить тему: порядок в главе, перенос между главами класса, копия в другой класс.

    Внутри класса тема именно переезжает (в ту же или другую главу); в другой
    класс — копируется, оригинал остаётся, как и раньше.
    """
    topic = get_object_or_404(Topic.objects.select_related("block", "chapter"), pk=pk)
    target_block_id = request.POST.get("target_block") or request.POST.get("block")
    if target_block_id and str(target_block_id).isdigit():
        target_block = get_object_or_404(Block, pk=int(target_block_id))
        if target_block.pk != topic.block_id:
            target_chapter = _target_chapter(request, target_block)
            before = None
            if str(request.POST.get("before", "")).isdigit():
                before = Topic.objects.filter(
                    pk=int(request.POST["before"]), block=target_block
                ).first()
            if before is not None and target_chapter is None:
                target_chapter = before.chapter
            new_topic = _clone_topic_to_block(
                topic, target_block, before=before, target_chapter=target_chapter
            )
            msg = (
                f"Тема скопирована в класс «{target_block.name}»: {new_topic.title} "
                f"({new_topic.assignments.count()} заданий)"
            )
            logger.info(
                "topic_copy src=%s -> block=%s new=%s", topic.pk, target_block.pk, new_topic.pk
            )
            return _quick_outcome(request, True, msg, "")
        # target_block совпадает с исходным — перенос внутри класса (ниже)

    target_chapter = _target_chapter(request, topic.block)
    if "before" in request.POST or target_chapter is not None:
        before = None
        if request.POST.get("before"):
            before = get_object_or_404(Topic, pk=request.POST["before"], block=topic.block)
            if target_chapter is None:
                target_chapter = before.chapter
        target_chapter = target_chapter or topic.chapter
        if before is not None and before.chapter_id != target_chapter.pk:
            return _quick_outcome(request, False, "", "Тема-ориентир из другой главы.")
        moved = target_chapter.pk != topic.chapter_id
        _place_topic(topic, before, target_chapter=target_chapter)
        return _quick_outcome(
            request,
            True,
            f"Тема перенесена в главу «{target_chapter.title}»: {topic.title}"
            if moved
            else f"Порядок темы изменён: {topic.title}",
            "",
        )
    done = _reorder(
        Topic.objects.filter(chapter=topic.chapter), topic, request.POST.get("direction")
    )
    return _quick_outcome(
        request,
        done,
        f"Порядок темы изменён: {topic.title}",
        "Крайняя тема в главе: перемещать некуда.",
    )


@teacher_required
@require_POST
def topic_copy(request, pk):
    """Скопировать тему в другой класс: оригинал остаётся, копия — черновиками."""
    topic = get_object_or_404(Topic.objects.select_related("block", "chapter"), pk=pk)
    target_block_id = request.POST.get("target_block") or request.POST.get("block")
    if not target_block_id or not str(target_block_id).isdigit():
        messages.error(request, "Не выбран целевой класс для копирования темы.")
        return redirect(request.POST.get("next") or "teacher_curriculum")
    target_block = get_object_or_404(Block, pk=int(target_block_id))
    before = None
    if request.POST.get("before") and str(request.POST["before"]).isdigit():
        before = Topic.objects.filter(pk=int(request.POST["before"]), block=target_block).first()
    new_topic = _clone_topic_to_block(
        topic, target_block, before=before, target_chapter=_target_chapter(request, target_block)
    )
    messages.success(
        request,
        f"Тема «{topic.title}» скопирована в класс «{target_block.name}» как «{new_topic.title}». "
        f"Скопировано заданий: {new_topic.assignments.count()}. Копии — черновики.",
    )
    nxt = request.POST.get("next")
    if nxt:
        return redirect(nxt)
    return redirect("teacher_curriculum")


@teacher_required
@require_http_methods(["GET", "POST"])
def assignment_form(request, pk=None):
    assignment = (
        get_object_or_404(
            Assignment.objects.select_related("topic__block", "topic__chapter"), pk=pk
        )
        if pk
        else None
    )
    initial = {}
    if request.GET.get("topic"):
        initial["topic"] = request.GET["topic"]
    requested_type = request.GET.get("type")
    if requested_type in Assignment.Type.values and pk is None:
        initial["assignment_type"] = requested_type
    if pk is None and request.method != "POST":
        initial["skills"] = [
            skill.pk for skill in skills_for_type(requested_type or Assignment.Type.TEXT)
        ]
    form = AssignmentForm(
        request.POST or None, request.FILES or None, instance=assignment, initial=initial or None
    )
    if request.method == "POST" and form.is_valid():
        is_new = assignment is None
        instance = form.save()
        # Handle multiple attachments
        new_files = request.FILES.getlist("new_attachments")
        if not new_files:
            # fallback single field name from widget
            single = request.FILES.get("new_attachments")
            if single:
                new_files = [single]
        if new_files:
            from django.db.models import Max

            from .models import AssignmentAttachment

            last_order = instance.attachments.aggregate(m=Max("order"))["m"] or 0
            for idx, f in enumerate(new_files, start=1):
                AssignmentAttachment.objects.create(
                    assignment=instance, file=f, order=last_order + idx
                )
        # Handle deletion of existing attachments
        delete_ids = request.POST.getlist("delete_attachments")
        if delete_ids:
            from .models import AssignmentAttachment

            instance.attachments.filter(pk__in=[i for i in delete_ids if i.isdigit()]).delete()

        messages.success(
            request,
            "Задание сохранено как черновик — ученикам пока не видно."
            if instance.status == Assignment.Publication.DRAFT
            else f"Задание сохранено: {instance.title}",
        )
        if request.POST.get("_save_questions") or (is_new and instance.is_quiz):
            return redirect("teacher_questions", pk=instance.pk)
        if instance.is_flashcards:
            messages.info(request, "Добавьте карточки: вручную, списком или из шаблона.")
            return redirect("teacher_assignment_cards", pk=instance.pk)
        # If topic board was origin, go back there
        if request.GET.get("from_topic"):
            return redirect("teacher_topic_board", pk=instance.topic_id)
        return redirect("teacher_curriculum")
    skill_payload = type_skill_payload()
    preset_groups = assignment_preset_groups()
    question_items = []
    question_form = None
    if assignment and assignment.is_quiz:
        question_items = list(
            assignment.questions.prefetch_related("choices").order_by("order", "pk")
        )
        question_form = QuestionForm()
    return render(
        request,
        "lms/teacher_assignment_form.html",
        {
            "form": form,
            "assignment": assignment,
            "topics": Topic.objects.select_related("block", "chapter").order_by(
                "block__order", "chapter__order", "order", "title"
            ),
            "groups": Group.objects.filter(is_active=True).order_by("name"),
            "preset_groups": preset_groups,
            "preset_payload": _flat_presets(preset_groups),
            "skill_ids": skill_payload["ids"],
            "skills_by_type": skill_payload["by_type"],
            "progress": _submission_progress(assignment) if assignment else None,
            "inherited_audience": audience.describe(assignment.topic) if assignment else None,
            "questions": question_items,
            "question_form": question_form,
            "total_points": sum(item.points for item in question_items),
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def assignment_duplicate(request, pk):
    source = get_object_or_404(Assignment, pk=pk)
    copy = Assignment(
        topic=source.topic,
        title=f"{source.title} (копия)",
        description=source.description,
        assignment_type=source.assignment_type,
        material_file=source.material_file,
        deadline=source.deadline,
        max_points=source.max_points,
        max_tries=source.max_tries,
        allow_retake=source.allow_retake,
        exam_mode=source.exam_mode,
        recording_limit_seconds=source.recording_limit_seconds,
        order=source.order + 1,
        is_active=source.is_active,
        status=Assignment.Publication.DRAFT,
    )
    copy.save()
    copy.skills.set(source.skills.all())
    for question in source.questions.prefetch_related("choices").all():
        new_question = Question.objects.create(
            assignment=copy,
            kind=question.kind,
            text=question.text,
            explanation=question.explanation,
            points=question.points,
            order=question.order,
            recording_limit_seconds=question.recording_limit_seconds,
        )
        Choice.objects.bulk_create(
            [
                Choice(
                    question=new_question,
                    text=choice.text,
                    match_text=choice.match_text,
                    is_correct=choice.is_correct,
                    order=choice.order,
                )
                for choice in question.choices.all()
            ]
        )
    messages.success(request, f"Создан черновик-копия: {copy.title}")
    return redirect("teacher_assignment_form", pk=copy.pk)


@teacher_required
@require_POST
def assignment_rename(request, pk):
    """Быстрое переименование прямо на доске курса."""
    assignment = get_object_or_404(Assignment, pk=pk)
    title = request.POST.get("title", "").strip()[:200]
    if not title:
        if _wants_json(request):
            return JsonResponse(
                {"ok": False, "error": "Название не может быть пустым."}, status=400
            )
        messages.error(request, "Название задания не может быть пустым.")
        return redirect(request.POST.get("next") or "teacher_curriculum")
    assignment.title = title
    assignment.save(update_fields=["title", "updated_at"])
    if _wants_json(request):
        return JsonResponse({"ok": True, "title": assignment.title})
    messages.success(request, f"Задание переименовано: {assignment.title}")
    return redirect(request.POST.get("next") or "teacher_curriculum")


@teacher_required
@require_POST
def assignment_quick_edit(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    form = AssignmentQuickForm(
        request.POST, request.FILES, instance=assignment, auto_id=f"assignment-{pk}-%s"
    )
    question_forms = [
        QuestionForm(request.POST, instance=q, prefix=f"question-{q.pk}")
        for q in assignment.questions.prefetch_related("choices")
    ]
    valid = form.is_valid()
    for question_form in question_forms:
        valid = question_form.is_valid() and valid
    from .models import AssignmentAttachment

    attachments = []
    for upload in request.FILES.getlist("new_attachments"):
        attachment = AssignmentAttachment(assignment=assignment, file=upload)
        try:
            attachment.full_clean()
        except ValidationError as error:
            form.add_error(None, error.messages)
            valid = False
        attachments.append(attachment)
    if not valid:
        return _render_topic_board(
            request, assignment.topic_id, edit_errors=(assignment.pk, form, question_forms)
        )
    with transaction.atomic():
        form.save()
        for question_form in question_forms:
            question = question_form.save()
            question_form.save_choices(question)
        if question_forms:
            _sync_quiz_points(assignment)
            from .services import regrade_assignment

            regrade_assignment(assignment)
        last_order = assignment.attachments.aggregate(m=Max("order"))["m"] or 0
        for index, attachment in enumerate(attachments, 1):
            attachment.order = last_order + index
            attachment.save()
        assignment.attachments.filter(
            pk__in=[
                value for value in request.POST.getlist("delete_attachments") if value.isdigit()
            ]
        ).delete()
    messages.success(request, "Задание сохранено. Автоматические результаты пересчитаны.")
    return redirect(
        reverse("teacher_topic_board", args=[assignment.topic_id]) + f"#assignment-{assignment.pk}"
    )


@teacher_required
@require_POST
def assignment_move(request, pk):
    """Порядок задания на доске: точное место (перетаскивание) или шаг ↑/↓."""
    assignment = get_object_or_404(Assignment.objects.select_related("topic"), pk=pk)
    if "before" in request.POST or "topic" in request.POST:
        target_topic = assignment.topic
        if request.POST.get("topic"):
            target_topic = get_object_or_404(Topic, pk=request.POST["topic"])
        before = None
        if request.POST.get("before"):
            before = get_object_or_404(
                Assignment.objects.exclude(pk=assignment.pk),
                pk=request.POST["before"],
                topic=target_topic,
            )
        _place_assignment(assignment, target_topic, before)
        return _quick_outcome(request, True, f"Задание перемещено: {assignment.title}", "")
    done = _reorder(assignment.topic.assignments.all(), assignment, request.POST.get("direction"))
    return _quick_outcome(
        request,
        done,
        f"Порядок задания изменён: {assignment.title}",
        "Крайнее задание в теме: перемещать некуда.",
    )


@teacher_required
@require_POST
def assignment_publish(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    publish = request.POST.get("publish") == "1"
    assignment.status = (
        Assignment.Publication.PUBLISHED if publish else Assignment.Publication.DRAFT
    )
    if publish and assignment.publish_at and assignment.publish_at > timezone.now():
        assignment.publish_at = None
    assignment.save(update_fields=["status", "publish_at", "updated_at"])
    if _wants_json(request):
        return JsonResponse({"ok": True, "status": assignment.status, "published": publish})
    messages.success(
        request,
        f"Задание опубликовано: {assignment.title}"
        if publish
        else f"Задание возвращено в черновики: {assignment.title}",
    )
    return redirect(request.POST.get("next") or "teacher_curriculum")


def _bulk_publish_assignments(request, queryset, scope_label):
    """Опубликовать или вернуть в черновики все задания области разом."""
    publish = request.POST.get("publish") == "1"
    new_status = Assignment.Publication.PUBLISHED if publish else Assignment.Publication.DRAFT
    pending = queryset.exclude(status=new_status)
    count = pending.count()
    if not count:
        messages.info(request, f"Менять нечего: все задания {scope_label} уже в нужном статусе.")
        return redirect(request.POST.get("next") or "teacher_curriculum")
    if publish:
        pending.filter(publish_at__gt=timezone.now()).update(publish_at=None)
    pending.update(status=new_status, updated_at=timezone.now())
    messages.success(
        request,
        f"Опубликовано заданий {scope_label}: {count}."
        if publish
        else f"Заданий {scope_label} переведено в черновики: {count}.",
    )
    return redirect(request.POST.get("next") or "teacher_curriculum")


@teacher_required
@require_POST
def topic_assignments_publish(request, pk):
    """Скопом: статус всех заданий темы."""
    topic = get_object_or_404(Topic.objects.select_related("block"), pk=pk)
    return _bulk_publish_assignments(request, topic.assignments.all(), f"темы «{topic.title}»")


@teacher_required
@require_POST
def block_assignments_publish(request, pk):
    """Скопом: статус всех заданий класса (все его главы и темы разом)."""
    block = get_object_or_404(Block, pk=pk)
    return _bulk_publish_assignments(
        request, Assignment.objects.filter(topic__block=block), f"класса «{block.name}»"
    )


@teacher_required
@require_POST
def block_publish(request, pk):
    block = get_object_or_404(Block, pk=pk)
    active = request.POST.get("active") == "1"
    block.is_active = active
    block.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request, f"Класс «{block.name}» опубликован." if active else f"Класс «{block.name}» скрыт."
    )
    return redirect(request.POST.get("next") or "teacher_curriculum")


@teacher_required
@require_POST
def topic_publish(request, pk):
    topic = get_object_or_404(Topic, pk=pk)
    active = request.POST.get("active") == "1"
    topic.is_active = active
    topic.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request,
        f"Тема «{topic.title}» опубликована." if active else f"Тема «{topic.title}» скрыта.",
    )
    return redirect(request.POST.get("next") or "teacher_curriculum")


@teacher_required
@require_POST
def assignment_delete(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    return _delete(
        request,
        assignment,
        "teacher_archive" if request.POST.get("from_archive") == "1" else "teacher_curriculum",
        "Задание нельзя удалить: есть сдачи работ. Используйте архив — история сохранится.",
    )


@teacher_required
@require_GET
def assignment_preview(request, pk):
    """Предпросмотр глазами ученика: условия, требования, тест без ответов."""
    assignment = get_object_or_404(
        Assignment.objects.select_related("topic__block", "topic__chapter"), pk=pk
    )
    questions = list(assignment.questions.prefetch_related("choices").order_by("order", "pk"))
    return render(
        request,
        "lms/teacher_assignment_preview.html",
        {
            "assignment": assignment,
            "questions": questions,
            "reveal": request.GET.get("answers") == "1",
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_GET
def topic_board(request, pk):
    return _render_topic_board(request, pk)


def _render_topic_board(request, pk, edit_errors=None):
    """Раздел Задания по Теме: все задания темы со всем содержимым для правок учителя.

    Открывается при клике на тему в разделе Курс. Справа — навигация по навыкам на английском.
    Не добавляется в главное меню, выход — кнопка Назад в Курс.
    """
    topic = get_object_or_404(Topic.objects.select_related("block", "chapter"), pk=pk)
    assignments = list(
        Assignment.objects.filter(topic=topic)
        .select_related("topic__block", "topic__chapter")
        .prefetch_related(
            "skills", "questions__choices", "cards", "attachments", "groups", "assigned_students"
        )
        .order_by("order", "pk")
    )
    topic_audience = audience.describe(topic)
    for assignment in assignments:
        assignment.audience = audience.accumulate(assignment, topic_audience)
        assignment.quick_form = AssignmentQuickForm(
            instance=assignment, auto_id=f"assignment-{assignment.pk}-%s"
        )
        assignment.question_forms = [
            QuestionForm(instance=q, prefix=f"question-{q.pk}") for q in assignment.questions.all()
        ]
        if edit_errors and assignment.pk == edit_errors[0]:
            assignment.quick_form, assignment.question_forms = edit_errors[1:]
            assignment.edit_open = True
    # Собираем навыки присутствующие в теме
    # Для фильтра
    selected_skill = request.GET.get("skill", "").strip().lower()
    if selected_skill:
        filtered = []
        for a in assignments:
            slugs = [s.slug for s in a.skills.all()]
            kinds = [s.kind for s in a.skills.all()]
            # match by slug or kind or english name lower
            if (
                selected_skill in slugs
                or selected_skill in kinds
                or any(selected_skill == (s.name or "").lower() for s in a.skills.all())
            ):
                filtered.append(a)
            # also match english names
            eng_map = {
                "reading": "reading",
                "listening": "listening",
                "speaking": "speaking",
                "writing": "writing",
                "grammar": "grammar",
                "vocabulary": "vocabulary",
            }
            if selected_skill in eng_map:
                if eng_map[selected_skill] in kinds or eng_map[selected_skill] in slugs:
                    if a not in filtered:
                        filtered.append(a)
        # if filter by english but no match via slug, try kind
        if not filtered and selected_skill in [
            "reading",
            "listening",
            "speaking",
            "writing",
            "grammar",
            "vocabulary",
        ]:
            filtered = [
                a for a in assignments if any(s.kind == selected_skill for s in a.skills.all())
            ]
        assignments_filtered = filtered
    else:
        assignments_filtered = assignments

    # Полный список навыков для меню (английские названия)
    skill_menu = [
        {"slug": "all", "label": "All", "count": len(assignments), "kind": "all"},
        {
            "slug": "reading",
            "label": "Reading",
            "count": len(
                [a for a in assignments if any(s.kind == "reading" for s in a.skills.all())]
            ),
            "kind": "reading",
        },
        {
            "slug": "listening",
            "label": "Listening",
            "count": len(
                [a for a in assignments if any(s.kind == "listening" for s in a.skills.all())]
            ),
            "kind": "listening",
        },
        {
            "slug": "speaking",
            "label": "Speaking",
            "count": len(
                [a for a in assignments if any(s.kind == "speaking" for s in a.skills.all())]
            ),
            "kind": "speaking",
        },
        {
            "slug": "writing",
            "label": "Writing",
            "count": len(
                [a for a in assignments if any(s.kind == "writing" for s in a.skills.all())]
            ),
            "kind": "writing",
        },
        {
            "slug": "grammar",
            "label": "Grammar",
            "count": len(
                [a for a in assignments if any(s.kind == "grammar" for s in a.skills.all())]
            ),
            "kind": "grammar",
        },
        {
            "slug": "vocabulary",
            "label": "Vocabulary",
            "count": len(
                [a for a in assignments if any(s.kind == "vocabulary" for s in a.skills.all())]
            ),
            "kind": "vocabulary",
        },
    ]
    # Only show skills that have at least 1 assignment, plus All
    skill_menu_visible = [item for item in skill_menu if item["slug"] == "all" or item["count"] > 0]

    return render(
        request,
        "lms/teacher_topic_board.html",
        {
            "topic": topic,
            "block": topic.block,
            "topic_audience": topic_audience,
            "assignments": assignments_filtered,
            "all_assignments": assignments,
            "skill_menu": skill_menu_visible,
            "selected_skill": selected_skill or "all",
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def attachment_delete(request, pk):
    """Удалить одно вложение задания."""
    from .models import AssignmentAttachment

    att = get_object_or_404(AssignmentAttachment, pk=pk)
    assignment_id = att.assignment_id
    topic_id = att.assignment.topic_id
    att.delete()
    messages.success(request, "Файл удалён.")
    if request.POST.get("from") == "board":
        return redirect("teacher_topic_board", pk=topic_id)
    return redirect("teacher_assignment_form", pk=assignment_id)


@teacher_required
@require_POST
def ai_extract_text(request):
    """ИИ-извлечение текста с картинки/файла в окно Условия задания.

    Принимает файл, извлекает текст офлайн (docx/xlsx/pdf/txt) или через vision-модель если доступна,
    возвращает JSON с текстом.
    """
    from . import ai
    from .models import AssignmentAttachment
    from .validators import validate_upload

    for field in ("attachment_id", "material_assignment_id"):
        if request.POST.get(field) and not request.POST[field].isdigit():
            return JsonResponse({"ok": False, "error": "Некорректный файл."}, status=400)
    upload = request.FILES.get("file")
    if not upload and request.POST.get("attachment_id"):
        upload = get_object_or_404(AssignmentAttachment, pk=request.POST["attachment_id"]).file
    if not upload and request.POST.get("material_assignment_id"):
        upload = get_object_or_404(
            Assignment, pk=request.POST["material_assignment_id"]
        ).material_file
    if not upload:
        return JsonResponse({"ok": False, "error": "Выберите файл для распознавания."}, status=400)
    try:
        if upload.size > ai.max_upload_bytes():
            raise ValidationError("Файл слишком большой для распознавания.")
        validate_upload(upload)
        upload.open("rb")
        blob = upload.read(ai.max_upload_bytes() + 1)
        upload.close()
        filename = upload.name
        text = ai.extract_text(filename, blob)
        if not text and ai.upload_kind(filename) == "image":
            spec = ai.provider_spec()
            if ai.ai_mode() != "online" or "image" not in ai.provider_uploads(spec):
                raise ai.AiError(
                    "Для распознавания картинки подключите ИИ-модель с поддержкой изображений."
                )
            text = ai._provider_material(
                spec,
                "Перепиши весь видимый текст изображения дословно, сохраняя абзацы. "
                "Не решай задания, не добавляй пояснений или новых заданий. Верни только распознанный текст.",
                filename=filename,
                blob=blob,
                raw_text=True,
            )
        if not text.strip():
            raise ai.AiError(
                "Не удалось извлечь текст. Попробуйте более чёткое изображение или вставьте текст вручную."
            )
        return JsonResponse({"ok": True, "text": text[:10000]})
    except (ValidationError, ai.AiError) as exc:
        error = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({"ok": False, "error": error}, status=400)
    except (FileNotFoundError, OSError):
        return JsonResponse(
            {"ok": False, "error": "Файл недоступен. Загрузите его повторно."}, status=400
        )


@teacher_required
@require_http_methods(["GET", "POST"])
def questions(request, pk):
    """Редактор вопросов теста. Баллы задания синхронизируются с суммой вопросов."""
    assignment = get_object_or_404(
        Assignment.objects.select_related("topic__block", "topic__chapter"), pk=pk
    )
    form = QuestionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        question = form.save(commit=False)
        question.assignment = assignment
        if not question.order:
            last = assignment.questions.aggregate(last=Max("order"))["last"] or 0
            question.order = last + 1
        question.save()
        form.save_choices(question)
        _sync_quiz_points(assignment)
        messages.success(request, "Вопрос добавлен.")
        if request.headers.get("HX-Request"):
            form = QuestionForm()
        else:
            return redirect("teacher_questions", pk=assignment.pk)
    items = list(assignment.questions.prefetch_related("choices").order_by("order", "pk"))
    context = {
        "assignment": assignment,
        "questions": items,
        "form": form,
        "total_points": sum(item.points for item in items),
        "workspace": "curriculum",
    }
    template = (
        "lms/parts/quiz_questions.html"
        if request.headers.get("HX-Request")
        else "lms/teacher_questions.html"
    )
    return render(request, template, context)


def _item_results_data(assignment):
    """Последняя попытка каждого ученика по пунктам + статистика по столбцам."""
    items = list(assignment.questions.order_by("order", "pk"))
    latest = {}
    for submission in (
        Submission.objects.filter(assignment=assignment)
        .select_related("student", "feedback")
        .order_by("student_id", "-version")
    ):
        latest.setdefault(submission.student_id, submission)
    responses = {}
    for response in QuestionResponse.objects.filter(
        submission__in=[submission.pk for submission in latest.values()]
    ):
        responses[(response.submission_id, response.question_id)] = response
    rows = []
    stats = {item.pk: {"correct": 0, "answered": 0, "first": 0} for item in items}
    for submission in sorted(
        latest.values(),
        key=lambda entry: (entry.student.get_full_name() or entry.student.username).lower(),
    ):
        cells = []
        for item in items:
            response = responses.get((submission.pk, item.pk))
            cells.append(response)
            if response is None or item.is_manual:
                continue
            stats[item.pk]["answered"] += 1
            if response.state == QuestionResponse.State.CORRECT:
                stats[item.pk]["correct"] += 1
                if response.tries_used == 1:
                    stats[item.pk]["first"] += 1
        rows.append({"submission": submission, "cells": cells})
    columns = []
    for number, item in enumerate(items, start=1):
        stat = stats[item.pk]
        rate = round(100 * stat["correct"] / stat["answered"]) if stat["answered"] else None
        columns.append({"number": number, "question": item, "rate": rate, **stat})
    return columns, rows


def _item_cell_text(question, response):
    """Ячейка XLSX: баллы и отметка, понятные без легенды."""
    if response is None:
        return ""
    if question.is_manual:
        if response.teacher_points is None:
            return "ждёт проверки"
        return f"{response.teacher_points}/{question.points}"
    if response.state == QuestionResponse.State.CORRECT:
        tries = response.tries_used
        return f"✓ {response.points}/{question.points}" + (
            f" (попытка {tries})" if tries > 1 else ""
        )
    if response.state == QuestionResponse.State.FAILED:
        return f"✗ {response.points}/{question.points}"
    return ""


@teacher_required
@require_GET
def item_results(request, pk):
    """Матрица «ученик × пункт» по последним прохождениям задания.

    В ячейке — ✓ (с какой попытки), ✗ или отметка ручной проверки; сверху —
    доля верных по каждому пункту, чтобы сразу видеть трудные места.
    """
    assignment = get_object_or_404(
        Assignment.objects.select_related("topic__block", "topic__chapter"), pk=pk
    )
    columns, rows = _item_results_data(assignment)
    return render(
        request,
        "lms/teacher_item_results.html",
        {
            "assignment": assignment,
            "columns": columns,
            "rows": rows,
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_GET
def item_results_export(request, pk):
    """Та же матрица в XLSX: строка на ученика, столбец на пункт, внизу — доля верных."""
    assignment = get_object_or_404(Assignment, pk=pk)
    columns, rows = _item_results_data(assignment)
    header = ["Ученик"]
    for column in columns:
        text = " ".join(str(column["question"].text).split())
        header.append(f"{column['number']}. {text[:60]}")
    header += ["Итог", "Максимум", "Статус"]
    table = [header]
    for row in rows:
        submission = row["submission"]
        feedback = getattr(submission, "feedback", None)
        grade = feedback.grade if feedback and feedback.grade is not None else ""
        table.append(
            [
                submission.student.get_full_name() or submission.student.username,
                *[
                    _item_cell_text(column["question"], cell)
                    for column, cell in zip(columns, row["cells"], strict=True)
                ],
                grade,
                submission.max_points_snapshot,
                submission.get_status_display(),
            ]
        )
    table.append(
        [
            "Доля верных, %",
            *[column["rate"] if column["rate"] is not None else "" for column in columns],
            "",
            "",
            "",
        ]
    )
    payload = xlsx.build_xlsx(table, sheet_name="По пунктам")
    response = HttpResponse(payload, content_type=xlsx.CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="items-{assignment.pk}.xlsx"'
    response["Content-Length"] = str(len(payload))
    return response


def _sync_quiz_points(assignment):
    total = assignment.questions.aggregate(total=Count("pk"))["total"]
    points = sum(assignment.questions.values_list("points", flat=True))
    fields = []
    if assignment.assignment_type != Assignment.Type.QUIZ and total:
        assignment.assignment_type = Assignment.Type.QUIZ
        fields.append("assignment_type")
    if total and assignment.max_points != points:
        assignment.max_points = points
        fields.append("max_points")
    if fields:
        fields.append("updated_at")
        assignment.save(update_fields=fields)
    return points


@teacher_required
@require_http_methods(["GET", "POST"])
def question_form(request, pk):
    question = get_object_or_404(Question.objects.select_related("assignment"), pk=pk)
    form = QuestionForm(request.POST or None, instance=question)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            form.save_choices(question)
            _sync_quiz_points(question.assignment)
            from .services import regrade_assignment

            regrade_assignment(question.assignment)
        messages.success(request, "Вопрос обновлён.")
        return redirect("teacher_questions", pk=question.assignment_id)
    return render(
        request,
        "lms/teacher_question_form.html",
        {
            "form": form,
            "question": question,
            "assignment": question.assignment,
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def question_delete(request, pk):
    question = get_object_or_404(Question, pk=pk)
    assignment_id = question.assignment_id
    question.delete()
    assignment = Assignment.objects.get(pk=assignment_id)
    _sync_quiz_points(assignment)
    messages.success(request, "Вопрос удалён.")
    return redirect("teacher_questions", pk=assignment_id)


# ── Карточки-тренажёр: разновидность задания, а не отдельная сущность ──────
def _flashcard_assignment(pk):
    return get_object_or_404(
        Assignment.objects.select_related("topic__block", "topic__chapter"),
        pk=pk,
        assignment_type=Assignment.Type.FLASHCARDS,
    )


def _cards_context(assignment, cards):
    """Контекст страницы карточек: наборы шаблонов по уровням и число учеников."""
    groups = card_preset_groups()
    return {
        "assignment": assignment,
        "cards": cards,
        "preset_groups": groups,
        "preset_payload": _flat_presets(groups),
        "preset_levels": CARD_LEVELS,
        "learners": CardReview.objects.filter(card__assignment=assignment)
        .values("student_id")
        .distinct()
        .count(),
        "workspace": "curriculum",
    }


@teacher_required
@require_http_methods(["GET", "POST"])
def assignment_cards(request, pk):
    """Карточки задания-тренажёра: одиночное добавление, массовый импорт, шаблоны."""
    assignment = _flashcard_assignment(pk)
    card_form = FlashcardForm(request.POST or None, prefix="card")
    bulk_form = FlashcardBulkForm(request.POST or None, prefix="bulk")
    if request.method == "POST":
        preset_id = request.POST.get("preset_id", "").strip()
        if preset_id and "_preset" in request.POST:
            try:
                added = create_cards_from_preset(
                    assignment, preset_id, replace=bool(request.POST.get("preset_replace"))
                )
            except LookupError:
                messages.error(request, "Неизвестный шаблон карточек.")
            else:
                preset = get_card_preset(preset_id)
                messages.success(
                    request,
                    f"Из шаблона «{preset['label']}» добавлено карточек: {added}.",
                )
            return redirect("teacher_assignment_cards", pk=assignment.pk)
        if "bulk-cards_text" in request.POST and bulk_form.is_valid():
            if bulk_form.cleaned_data["replace"]:
                CardReview.objects.filter(card__assignment=assignment).delete()
                assignment.cards.all().delete()
            start = assignment.cards.aggregate(last=Max("order"))["last"] or 0
            Flashcard.objects.bulk_create(
                [
                    Flashcard(
                        assignment=assignment,
                        front=item["front"],
                        back=item["back"],
                        example=item["example"],
                        order=start + index,
                    )
                    for index, item in enumerate(bulk_form.cleaned_data["cards_text"], start=1)
                ]
            )
            messages.success(
                request, f"Добавлено карточек: {len(bulk_form.cleaned_data['cards_text'])}"
            )
            return redirect("teacher_assignment_cards", pk=assignment.pk)
        if card_form.is_valid():
            card = card_form.save(commit=False)
            card.assignment = assignment
            if not card.order:
                card.order = (assignment.cards.aggregate(last=Max("order"))["last"] or 0) + 1
            card.save()
            messages.success(request, f"Карточка добавлена: {card.front}")
            return redirect("teacher_assignment_cards", pk=assignment.pk)
    return render(
        request,
        "lms/teacher_assignment_cards.html",
        {
            **_cards_context(assignment, list(assignment.cards.all())),
            "card_form": card_form,
            "bulk_form": bulk_form,
        },
    )


@teacher_required
@require_POST
def card_delete(request, pk):
    card = get_object_or_404(Flashcard, pk=pk)
    assignment_id = card.assignment_id
    card.delete()
    messages.success(request, "Карточка удалена.")
    if assignment_id:
        return redirect("teacher_assignment_cards", pk=assignment_id)
    return redirect("teacher_curriculum")


# ── Прежние адреса наборов карточек: объясняем и ведём к новому месту ─────
@teacher_required
@require_GET
def deck_new_redirect(request):
    """Старая ссылка «Новый набор карточек» → создание задания с карточками."""
    return redirect(f"{reverse('teacher_assignment_new')}?type={Assignment.Type.FLASHCARDS}")


@teacher_required
@require_GET
def deck_legacy_redirect(request, pk):
    """Старые ссылки на набор карточек: наборы стали заданиями с карточками."""
    messages.info(
        request,
        "Наборы карточек теперь задания типа «Карточки-тренажёр»: "
        "откройте задание в теме курса и нажмите «Карточки».",
    )
    return redirect("teacher_curriculum")


# ── Пространство «Ученики» ─────────────────────────────────────────────────
@teacher_required
@require_GET
def students_list(request):
    query = request.GET.get("q", "").strip()[:200]
    students = User.objects.filter(
        profile__role=Profile.Role.STUDENT, is_active=True
    ).select_related("profile")
    if query:
        students = students.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(profile__telegram__icontains=query)
        )
    students = list(students.order_by("last_name", "first_name", "username"))
    stats = {
        row["student_id"]: row
        for row in Submission.objects.latest_attempts()
        .values("student_id")
        .annotate(
            attempts=Count("pk"),
            waiting=Count("pk", filter=Q(status__in=QUEUE_FILTERS["pending"])),
            revision=Count("pk", filter=Q(status=Submission.Status.NEEDS_REVISION)),
            checked=Count("pk", filter=Q(status=Submission.Status.CHECKED)),
            average=Avg("feedback__grade"),
            last=Max("submitted_at"),
        )
    }
    total_assignments = Assignment.objects.visible().count()
    for student in students:
        row = stats.get(student.pk, {})
        student.stats = {
            "attempts": row.get("attempts", 0),
            "waiting": row.get("waiting", 0),
            "revision": row.get("revision", 0),
            "checked": row.get("checked", 0),
            "average": row.get("average"),
            "last": row.get("last"),
            "progress": int(round(100 * row.get("checked", 0) / total_assignments))
            if total_assignments
            else 0,
        }
    return render(
        request,
        "lms/teacher_students.html",
        {
            "students": students,
            "query": query,
            "total_assignments": total_assignments,
            "workspace": "students",
        },
    )


#: Полосы вердикта по % освоения навыка: (нижняя граница, вердикт, класс полоски).
SKILL_BANDS = (
    (85, "Отлично", "progress-success"),
    (70, "Хорошо", ""),
    (50, "Средне", "progress-warning"),
    (0, "Слабо", "progress-danger"),
)


def _skill_summary(student, blocks_data):
    """Сводный анализ по навыкам: % освоения, охват и очередь проверки.

    Единица измерения — последнее сданное задание, видимое ученику
    (тренажёр и материалы без сдачи исключены). Каждое оценённое задание
    даёт каждому своему навыку один голос ``grade / max_points``; %
    навыка — среднее по голосам, каждая работа весит одинаково, иначе
    одна большая работа перекроет всю картину. Сданное без оценки
    (очередь проверки, свободная часть теста) в % не входит и
    показывается отдельно. Два запроса: навыки заданий и последние
    попытки ученика.
    """
    visible_ids = [
        entry["assignment"].pk
        for block_item in blocks_data
        for chapter_item in block_item["chapters"]
        for topic_item in chapter_item["topics"]
        for entry in topic_item["assignments"]
        if entry["assignment"].assignment_type not in Assignment.NO_SUBMISSION_TYPES
    ]
    skill_names = {}
    through = Assignment.skills.through
    for link in through.objects.filter(assignment_id__in=visible_ids).select_related("skill"):
        skill_names.setdefault(link.assignment_id, []).append(link.skill.name)
    buckets = {}
    for assignment_id in visible_ids:
        for name in skill_names.get(assignment_id, []):
            bucket = buckets.setdefault(
                name, {"total": 0, "graded": 0, "percent_sum": 0, "pending": 0}
            )
            bucket["total"] += 1
    rows = (
        Submission.objects.filter(student=student, assignment_id__in=visible_ids)
        .latest_attempts()
        .values("assignment_id", "feedback__grade", "max_points_snapshot")
    )
    for row in rows:
        names = skill_names.get(row["assignment_id"], [])
        if not names:
            continue
        grade = row["feedback__grade"]
        maximum = row["max_points_snapshot"]
        for name in names:
            bucket = buckets[name]
            if grade is None or not maximum:
                bucket["pending"] += 1
            else:
                bucket["graded"] += 1
                bucket["percent_sum"] += 100 * grade / maximum
    skills = []
    for name, bucket in buckets.items():
        if not bucket["graded"]:
            skills.append(
                {
                    "name": name,
                    "percent": None,
                    "verdict": "Нет оценок",
                    "bar_class": "progress-sand",
                    "graded": 0,
                    "total": bucket["total"],
                    "pending": bucket["pending"],
                }
            )
            continue
        percent = int(round(bucket["percent_sum"] / bucket["graded"]))
        verdict, bar_class = next(
            (verdict, bar_class) for bound, verdict, bar_class in SKILL_BANDS if percent >= bound
        )
        skills.append(
            {
                "name": name,
                "percent": percent,
                "verdict": verdict,
                "bar_class": bar_class,
                "graded": bucket["graded"],
                "total": bucket["total"],
                "pending": bucket["pending"],
            }
        )
    skills.sort(key=lambda item: (item["percent"] is None, item["percent"] or 0))
    summary = None
    rated = [skill for skill in skills if skill["percent"] is not None]
    if rated:
        summary = {
            "average": int(round(sum(skill["percent"] for skill in rated) / len(rated))),
            "strongest": max(rated, key=lambda skill: skill["percent"])["name"],
            "weakest": min(rated, key=lambda skill: skill["percent"])["name"],
        }
    return skills, summary


def _attach_progress_details(blocks_data):
    """Сдачи ученика и разбор теста по пунктам прямо в дереве прогресса.

    Фиксированные +3 запроса: сдачи, вопросы с вариантами, ответы по пунктам.
    Каждой записи задания добавляются ``attempt`` (последняя сдача или None)
    и ``items`` (разбор пунктов теста, для остальных типов — пусто).
    """
    entries = [
        entry
        for block_item in blocks_data
        for chapter_item in block_item["chapters"]
        for topic_item in chapter_item["topics"]
        for entry in topic_item["assignments"]
    ]
    for entry in entries:
        entry["attempt"] = None
        entry["items"] = []
    attempt_ids = [
        entry["state"]["attempt_id"]
        for entry in entries
        if entry["state"] and entry["state"]["attempt_id"]
    ]
    if not attempt_ids:
        return
    attempts = {
        attempt.pk: attempt
        for attempt in Submission.objects.filter(pk__in=attempt_ids).select_related(
            "feedback", "quiz_attempt"
        )
    }
    for entry in entries:
        state = entry["state"]
        if state and state["attempt_id"]:
            entry["attempt"] = attempts.get(state["attempt_id"])
    quiz_entries = [
        entry for entry in entries if entry["attempt"] is not None and entry["assignment"].is_quiz
    ]
    if not quiz_entries:
        return
    questions_by_assignment = {}
    questions = (
        Question.objects.filter(
            assignment_id__in=[entry["assignment"].pk for entry in quiz_entries]
        )
        .prefetch_related("choices")
        .order_by("assignment_id", "order", "pk")
    )
    for question in questions:
        questions_by_assignment.setdefault(question.assignment_id, []).append(question)
    responses_by_submission = {}
    responses = QuestionResponse.objects.filter(
        submission_id__in=[entry["attempt"].pk for entry in quiz_entries]
    )
    for response in responses:
        responses_by_submission.setdefault(response.submission_id, {})[response.question_id] = (
            response
        )
    for entry in quiz_entries:
        attempt = entry["attempt"]
        quiz = getattr(attempt, "quiz_attempt", None)
        entry["items"] = build_items(
            entry["assignment"],
            questions_by_assignment.get(entry["assignment"].pk, []),
            responses_by_submission.get(attempt.pk, {}),
            closed_round=True,
            legacy=quiz.answers if quiz else None,
        )


@teacher_required
@require_GET
def student_detail(request, pk):
    student = get_object_or_404(
        User.objects.select_related("profile"), pk=pk, profile__role=Profile.Role.STUDENT
    )
    blocks_data, totals = course_tree(student=student)
    _attach_progress_details(blocks_data)
    attempts = (
        Submission.objects.filter(student=student)
        .exclude(assignment__assignment_type__in=Assignment.NO_SUBMISSION_TYPES)
        .select_related("feedback", "assignment__topic__block", "assignment__topic__chapter")
        .order_by("-submitted_at", "-pk")[:30]
    )
    skills, skill_summary = _skill_summary(student, blocks_data)
    cards_due = CardReview.objects.filter(student=student, due_at__lte=timezone.now()).count()
    issued_password = student.profile.reveal_password()
    return render(
        request,
        "lms/teacher_student_detail.html",
        {
            "student": student,
            "issued_password": issued_password,
            "password_changed_by_student": not student.profile.issued_password,
            "blocks_data": blocks_data,
            "totals": totals,
            "attempts": list(attempts),
            "skills": skills,
            "skill_summary": skill_summary,
            "cards_due": cards_due,
            "workspace": "students",
        },
    )


# ── Пространство «Аналитика» ───────────────────────────────────────────────
@teacher_required
@require_GET
def analytics(request):
    block = request.GET.get("block")
    topic = request.GET.get("topic")
    data = gradebook(
        block=block if block and block.isdigit() else None,
        topic=topic if topic and topic.isdigit() else None,
    )
    summary = (
        Submission.objects.latest_attempts()
        .exclude(assignment__assignment_type__in=Assignment.NO_SUBMISSION_TYPES)
        .aggregate(
            average=Avg("feedback__grade"),
            graded=Count("feedback__grade"),
            waiting=Count("pk", filter=Q(status__in=QUEUE_FILTERS["pending"])),
            revision=Count("pk", filter=Q(status=Submission.Status.NEEDS_REVISION)),
        )
    )
    return render(
        request,
        "lms/teacher_analytics.html",
        {
            **data,
            "summary": summary,
            "selected_block": block or "",
            "selected_topic": topic or "",
            "workspace": "analytics",
        },
    )


@teacher_required
@require_GET
def analytics_export(request):
    """Выгрузка журнала в XLSX: одна книга, шапка закреплена, автофильтр включён."""
    block = request.GET.get("block", "")
    topic = request.GET.get("topic", "")
    data = gradebook(
        block=int(block) if block.isdigit() else None,
        topic=int(topic) if topic.isdigit() else None,
    )
    assignments = [
        f"{item.topic.block.name} / {item.topic.title} / {item.title}"
        for item in data["assignments"]
    ]
    rows = [["Ученик", "Прогресс, %", "Сдано", "Проверено", *assignments, "Итого баллов"]]
    for row in data["rows"]:
        grades = [cell["grade"] for cell in row["cells"] if cell["grade"] is not None]
        rows.append(
            [
                row["student"].get_full_name() or row["student"].username,
                row["percent"],
                row["submitted"],
                row["checked"],
                *[
                    (f"{cell['grade']}/{cell['maximum']}" if cell["grade"] is not None else "")
                    for cell in row["cells"]
                ],
                sum(grades),
            ]
        )
    payload = xlsx.build_xlsx(rows, sheet_name="Журнал")
    response = HttpResponse(payload, content_type=xlsx.CONTENT_TYPE)
    response["Content-Disposition"] = 'attachment; filename="journal.xlsx"'
    response["Content-Length"] = str(len(payload))
    return response


# ── Пространство «Настройки» ───────────────────────────────────────────────
@teacher_required
@require_http_methods(["GET", "POST"])
def console_settings(request):
    if request.method == "POST":
        if "density" in request.POST or "hotkeys" in request.POST:
            density = request.POST.get("density", "comfortable")
            request.session["ui_density"] = (
                density if density in {"comfortable", "compact"} else "comfortable"
            )
            request.session["ui_hotkeys"] = request.POST.get("hotkeys") == "on"
            messages.success(request, "Настройки интерфейса сохранены.")
            return redirect("teacher_settings")
        form = TeacherProfileForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Профиль обновлён.")
            return redirect("teacher_settings")
    else:
        form = TeacherProfileForm(user=request.user)
    return render(
        request,
        "lms/teacher_settings.html",
        {
            "form": form,
            "snippets": _visible_snippets(request.user),
            "workspace": "settings",
        },
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def snippet_form(request, pk=None):
    snippet = get_object_or_404(CommentSnippet, pk=pk) if pk else None
    if (
        snippet
        and snippet.author_id
        and snippet.author_id != request.user.id
        and not request.user.is_superuser
    ):
        raise PermissionDenied
    form = CommentSnippetForm(request.POST or None, instance=snippet)
    if request.method == "POST" and form.is_valid():
        instance = form.save(commit=False)
        if not instance.pk:
            instance.author = request.user
        try:
            instance.save()
        except ValidationError as exc:
            _add_validation_errors(form, exc)
        else:
            messages.success(request, f"Шаблон комментария сохранён: {instance.title}")
            return redirect("teacher_settings")
    return render(
        request,
        "lms/teacher_snippet_form.html",
        {"form": form, "snippet": snippet, "workspace": "settings"},
    )


@teacher_required
@require_POST
def snippet_delete(request, pk):
    snippet = get_object_or_404(CommentSnippet, pk=pk)
    if snippet.author_id and snippet.author_id != request.user.id and not request.user.is_superuser:
        raise Http404
    snippet.delete()
    messages.success(request, "Шаблон комментария удалён.")
    return redirect("teacher_settings")


# ── Управление группами ───────────────────────────────────────────────────────


@teacher_required
@require_GET
def groups_list(request):
    """Список всех групп учителя."""
    query = request.GET.get("q", "").strip()[:200]
    groups = (
        Group.objects.filter(Q(teacher=request.user) | Q(teacher__isnull=True))
        .select_related("teacher")
        .prefetch_related("students")
        .order_by("name")
    )

    if query:
        groups = groups.filter(
            Q(name__icontains=query) | Q(description__icontains=query) | Q(slug__icontains=query)
        )

    # Считаем количество учеников и заданий в каждой группе
    for group in groups:
        group.student_count = group.students.count()
        group.assignment_count = group.assignments.count()

    return render(
        request,
        "lms/teacher_groups.html",
        {
            "groups": groups,
            "query": query,
            "workspace": "groups",
        },
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def group_form(request, pk=None):
    """Создание или редактирование группы."""
    group = get_object_or_404(Group, pk=pk) if pk else None

    # Проверка прав: учитель может редактировать только свои группы
    if (
        group
        and group.teacher_id
        and group.teacher_id != request.user.id
        and not request.user.is_superuser
    ):
        raise PermissionDenied

    if request.method == "POST":
        form = GroupForm(request.POST, instance=group)
        if form.is_valid():
            obj = form.save(commit=False)
            # Если группа создается и учитель не указан, ставим текущего
            if not obj.pk and not obj.teacher_id:
                obj.teacher = request.user
            obj.save()
            form.save_m2m()  # Сохраняем связи ManyToMany формы
            if "student_ids" in form.cleaned_data:
                obj.students.set(form.cleaned_data["student_ids"])

            action = "создана" if not pk else "обновлена"
            messages.success(request, f"Группа «{obj.name}» {action}.")
            return redirect("teacher_groups")
    else:
        form = GroupForm(instance=group)

    return render(
        request,
        "lms/teacher_group_form.html",
        {
            "form": form,
            "group": group,
            "title": "Редактировать группу" if group else "Создать группу",
            "workspace": "groups",
        },
    )


@teacher_required
@require_POST
def group_delete(request, pk):
    """Удаление группы."""
    group = get_object_or_404(Group, pk=pk)

    # Проверка прав
    if group.teacher_id and group.teacher_id != request.user.id and not request.user.is_superuser:
        raise PermissionDenied

    try:
        name = group.name
        group.delete()
        messages.success(request, f"Группа «{name}» удалена.")
    except ProtectedError:
        messages.error(request, "Нельзя удалить группу, к которой привязаны задания.")

    return redirect("teacher_groups")


# ── Управление учениками ──────────────────────────────────────────────────────


@teacher_required
@require_http_methods(["GET", "POST"])
def student_create(request):
    """Создание нового ученика с генерацией пароля."""
    if request.method == "POST":
        form = StudentCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(
                request,
                f"Ученик «{user.get_full_name() or user.username}» создан. "
                f"Логин: {user.username}. Пароль показан на странице ученика — "
                "передайте его ученику.",
            )
            return redirect("teacher_student_detail", pk=user.pk)
    else:
        form = StudentCreateForm()

    return render(
        request,
        "lms/teacher_student_form.html",
        {
            "form": form,
            "student": None,
            "title": "Создать ученика",
            "workspace": "students",
        },
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def student_edit(request, pk):
    """Редактирование ученика."""
    student = get_object_or_404(User, pk=pk, profile__role=Profile.Role.STUDENT)

    if request.method == "POST":
        form = StudentEditForm(request.POST, instance=student)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                f"Данные ученика «{student.get_full_name() or student.username}» обновлены.",
            )
            return redirect("teacher_student_detail", pk=student.pk)
    else:
        form = StudentEditForm(instance=student)

    return render(
        request,
        "lms/teacher_student_form.html",
        {
            "form": form,
            "student": student,
            "title": "Редактировать ученика",
            "workspace": "students",
        },
    )


@teacher_required
@require_POST
def student_delete(request, pk):
    """Удаление ученика."""
    student = get_object_or_404(User, pk=pk, profile__role=Profile.Role.STUDENT)

    # Нельзя удалить самого себя
    if student.pk == request.user.pk:
        messages.error(request, "Нельзя удалить самого себя.")
        return redirect("teacher_students")

    try:
        name = student.get_full_name() or student.username
        student.delete()
        messages.success(request, f"Ученик «{name}» удалён.")
    except ProtectedError:
        messages.error(request, "Нельзя удалить ученика, у которого есть сдачи работ.")

    return redirect("teacher_students")


@teacher_required
@require_POST
def student_reset_password(request, pk):
    """Сброс пароля ученика с генерацией нового."""
    import secrets

    student = get_object_or_404(User, pk=pk, profile__role=Profile.Role.STUDENT)

    new_password = secrets.token_urlsafe(12)
    student.set_password(new_password)
    student.save()
    profile, _ = Profile.objects.get_or_create(user=student)
    profile.remember_password(new_password)

    messages.success(
        request,
        f"Пароль ученика «{student.get_full_name() or student.username}» сброшен. "
        "Новый пароль показан на странице ученика.",
    )
    return redirect("teacher_student_detail", pk=student.pk)
