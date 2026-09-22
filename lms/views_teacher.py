"""Единая консоль преподавателя.

Один интерфейс для двух пространств: «Проверка» (очередь, оценивание) и «Курс»
(блоки, темы, задания, тесты, карточки), плюс «Ученики», «Аналитика» и
«Настройки». Системное администрирование (пользователи, права, журнал Django)
остаётся в Django Admin и доступно из настроек консоли.

Запись в учебный контент выполняется только преподавателем; сдачи и проверки
при этом идут через существующие сервисы с блокировками и контролем версий.
"""

import csv
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Max, ProtectedError, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .curriculum import course_tree, gradebook, queue_counts, teacher_overview
from .decorators import get_user_role, teacher_required
from .forms import (
    AssignmentForm,
    BlockForm,
    CommentSnippetForm,
    FlashcardBulkForm,
    FlashcardDeckForm,
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
    ASSIGNMENT_PRESETS,
    BLOCK_SUGGESTIONS,
    DECK_LEVELS,
    DECK_PRESETS,
    TOPIC_SUGGESTIONS,
    create_cards_from_preset,
    deck_preset_topics,
    get_deck_preset,
    import_course_pack,
    packs_with_state,
)
from .models import (
    Assignment,
    Block,
    CardReview,
    Choice,
    CommentSnippet,
    Flashcard,
    FlashcardDeck,
    Group,
    Profile,
    Question,
    Submission,
    Topic,
)
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


# ── Главная консоли ────────────────────────────────────────────────────────
@teacher_required
@require_GET
def console_home(request):
    overview = teacher_overview()
    waiting = (
        Submission.objects.latest_attempts()
        .filter(status__in=QUEUE_FILTERS["pending"])
        .select_related("student", "assignment__topic__block")
        .order_by("submitted_at", "pk")[:6]
    )
    drafts = Assignment.objects.filter(status=Assignment.Publication.DRAFT).select_related(
        "topic__block"
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
        .select_related("student", "assignment__topic__block", "feedback")
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
    form = ReviewForm(
        request.POST if request.method == "POST" else None, submission=submission, feedback=feedback
    )
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
            "questions": list(
                submission.assignment.questions.prefetch_related("choices").order_by("order", "pk")
            )
            if submission.assignment.is_quiz
            else [],
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
    """Карта курса: блоки → темы → задания и наборы карточек со счётчиками."""
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
        {"entries": packs_with_state(), "workspace": "curriculum"},
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
        f"блоков {created['blocks']}",
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
            topic_total=Count("topics", distinct=True),
            assignment_total=Count("topics__assignments", distinct=True),
        )
        .order_by("order", "name", "pk")
    )
    archived_topics = (
        Topic.objects.filter(is_active=False, block__is_active=True)
        .select_related("block")
        .annotate(assignment_total=Count("assignments"))
        .order_by("block__order", "order", "title", "pk")
    )
    archived_assignments = (
        Assignment.objects.filter(
            is_active=False,
            topic__is_active=True,
            topic__block__is_active=True,
        )
        .select_related("topic__block", "topic")
        .annotate(submission_total=Count("submissions"))
        .order_by("topic__block__order", "topic__order", "order", "title", "pk")
    )
    return render(
        request,
        "lms/teacher_archive.html",
        {
            "archived_blocks": archived_blocks,
            "archived_topics": archived_topics,
            "archived_assignments": archived_assignments,
            "archive_total": (
                archived_blocks.count() + archived_topics.count() + archived_assignments.count()
            ),
            "workspace": "archive",
        },
    )


def _archive_queryset(kind, pk):
    models = {"block": Block, "topic": Topic, "assignment": Assignment}
    model = models.get(kind)
    if model is None:
        raise Http404("Неизвестный тип элемента архива")
    return get_object_or_404(model, pk=pk)


def _set_archived(item, archived, restore_tree=False):
    """Скрыть/восстановить элемент и, при необходимости, его дочерние узлы."""
    now = timezone.now()
    active = not archived
    if isinstance(item, Block):
        item.is_active = active
        item.save(update_fields=["is_active", "updated_at"])
        if archived or restore_tree:
            Topic.objects.filter(block=item).update(is_active=active, updated_at=now)
            Assignment.objects.filter(topic__block=item).update(is_active=active, updated_at=now)
    elif isinstance(item, Topic):
        item.is_active = active
        item.save(update_fields=["is_active", "updated_at"])
        if archived or restore_tree:
            Assignment.objects.filter(topic=item).update(is_active=active, updated_at=now)
    else:
        item.is_active = active
        item.save(update_fields=["is_active", "updated_at"])


@teacher_required
@require_POST
@transaction.atomic
def archive_item(request, kind, pk):
    """Переместить блок, тему или задание в архив либо восстановить его."""
    item = _archive_queryset(kind, pk)
    restore = request.POST.get("action") == "restore"
    restore_tree = request.POST.get("restore_tree") == "1"
    _set_archived(item, archived=not restore, restore_tree=restore_tree)
    label = "Блок" if kind == "block" else "Тема" if kind == "topic" else "Задание"
    title = str(item)
    if restore:
        message = (
            f"{label} «{title}» восстановлен вместе с содержимым."
            if restore_tree and kind in {"block", "topic"}
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
    """
    if not assignment.pk:
        return None
    expected = User.objects.filter(profile__role=Profile.Role.STUDENT, is_active=True)
    if assignment.group_id:
        expected = expected.filter(student_groups=assignment.group_id)
    expected_ids = set(expected.values_list("pk", flat=True))
    expected_ids |= set(
        assignment.assigned_students.filter(is_active=True).values_list("pk", flat=True)
    )
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


@teacher_required
@require_http_methods(["GET", "POST"])
def block_form(request, pk=None):
    block = get_object_or_404(Block, pk=pk) if pk else None
    form = BlockForm(request.POST or None, instance=block)
    if request.method == "POST" and form.is_valid():
        instance = form.save()
        messages.success(request, f"Блок сохранён: {instance.name}")
        return redirect("teacher_curriculum")
    return render(
        request,
        "lms/teacher_block_form.html",
        {
            "form": form,
            "block": block,
            "suggestions": BLOCK_SUGGESTIONS,
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def block_delete(request, pk):
    block = get_object_or_404(Block, pk=pk)
    return _delete(
        request,
        block,
        "teacher_archive" if request.POST.get("from_archive") == "1" else "teacher_curriculum",
        "Блок нельзя удалить: в нём есть темы или сдачи работ. Сначала удалите пустые дочерние элементы или используйте архив.",
        blocked=block.topics.exists(),
    )


@teacher_required
@require_POST
def block_move(request, pk):
    """Переместить блок в дорожной карте: порядок виден и ученикам, и в журнале."""
    block = get_object_or_404(Block, pk=pk)
    if _reorder(Block.objects.all(), block, request.POST.get("direction")):
        messages.success(request, f"Порядок блока изменён: {block.name}")
    else:
        messages.info(request, "Крайний блок: перемещать некуда.")
    return redirect("teacher_curriculum")


@teacher_required
@require_http_methods(["GET", "POST"])
def topic_form(request, pk=None):
    topic = get_object_or_404(Topic.objects.select_related("block"), pk=pk) if pk else None
    initial = {}
    if request.GET.get("block"):
        initial["block"] = request.GET["block"]
    form = TopicForm(request.POST or None, instance=topic, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        instance = form.save()
        messages.success(request, f"Тема сохранена: {instance.title}")
        return redirect("teacher_curriculum")
    return render(
        request,
        "lms/teacher_topic_form.html",
        {
            "form": form,
            "topic": topic,
            "blocks": Block.objects.order_by("order", "name"),
            "suggestions": TOPIC_SUGGESTIONS,
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def topic_delete(request, pk):
    topic = get_object_or_404(Topic, pk=pk)
    return _delete(
        request,
        topic,
        "teacher_archive" if request.POST.get("from_archive") == "1" else "teacher_curriculum",
        "Тему нельзя удалить: в ней есть задания или сдачи работ. Сначала удалите пустые дочерние элементы или используйте архив.",
        blocked=topic.assignments.exists(),
    )


@teacher_required
@require_POST
def topic_move(request, pk):
    """Переместить тему внутри блока."""
    topic = get_object_or_404(Topic.objects.select_related("block"), pk=pk)
    if _reorder(Topic.objects.filter(block=topic.block), topic, request.POST.get("direction")):
        messages.success(request, f"Порядок темы изменён: {topic.title}")
    else:
        messages.info(request, "Крайняя тема в блоке: перемещать некуда.")
    return redirect("teacher_curriculum")


@teacher_required
@require_http_methods(["GET", "POST"])
def assignment_form(request, pk=None):
    assignment = (
        get_object_or_404(Assignment.objects.select_related("topic__block"), pk=pk) if pk else None
    )
    initial = {}
    if request.GET.get("topic"):
        initial["topic"] = request.GET["topic"]
    if pk is None and request.method != "POST":
        initial["skills"] = [skill.pk for skill in skills_for_type(Assignment.Type.TEXT)]
    form = AssignmentForm(
        request.POST or None, request.FILES or None, instance=assignment, initial=initial or None
    )
    if request.method == "POST" and form.is_valid():
        is_new = assignment is None
        instance = form.save()
        messages.success(
            request,
            "Задание сохранено как черновик — ученикам пока не видно."
            if instance.status == Assignment.Publication.DRAFT
            else f"Задание сохранено: {instance.title}",
        )
        if request.POST.get("_save_questions") or (is_new and instance.is_quiz):
            return redirect("teacher_questions", pk=instance.pk)
        return redirect("teacher_curriculum")
    skill_payload = type_skill_payload()
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
            "topics": Topic.objects.select_related("block").order_by(
                "block__order", "order", "title"
            ),
            "groups": Group.objects.filter(is_active=True).order_by("name"),
            "presets": ASSIGNMENT_PRESETS,
            "skill_ids": skill_payload["ids"],
            "skills_by_type": skill_payload["by_type"],
            "progress": _submission_progress(assignment) if assignment else None,
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
def assignment_publish(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    publish = request.POST.get("publish") == "1"
    assignment.status = (
        Assignment.Publication.PUBLISHED if publish else Assignment.Publication.DRAFT
    )
    if publish and assignment.publish_at and assignment.publish_at > timezone.now():
        assignment.publish_at = None
    assignment.save(update_fields=["status", "publish_at", "updated_at"])
    messages.success(
        request,
        f"Задание опубликовано: {assignment.title}"
        if publish
        else f"Задание возвращено в черновики: {assignment.title}",
    )
    return redirect(request.POST.get("next") or "teacher_curriculum")


@teacher_required
@require_POST
def block_publish(request, pk):
    block = get_object_or_404(Block, pk=pk)
    active = request.POST.get("active") == "1"
    block.is_active = active
    block.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request, f"Блок «{block.name}» опубликован." if active else f"Блок «{block.name}» скрыт."
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
    assignment = get_object_or_404(Assignment.objects.select_related("topic__block"), pk=pk)
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
@require_http_methods(["GET", "POST"])
def questions(request, pk):
    """Редактор вопросов теста. Баллы задания синхронизируются с суммой вопросов."""
    assignment = get_object_or_404(Assignment.objects.select_related("topic__block"), pk=pk)
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
        form.save()
        form.save_choices(question)
        _sync_quiz_points(question.assignment)
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


# ── Квизлеты: наборы карточек как задания тренажёрного типа ────────────────
@teacher_required
@require_http_methods(["GET", "POST"])
def deck_form(request, pk=None):
    """Квизлет — задание тренажёрного типа: шаблон подставляет название, описание и карточки."""
    deck = (
        get_object_or_404(FlashcardDeck.objects.select_related("topic__block"), pk=pk)
        if pk
        else None
    )
    initial = {}
    if request.GET.get("topic"):
        initial["topic"] = request.GET["topic"]
    preset_id = request.GET.get("preset") or request.POST.get("preset_id") or ""
    preset = get_deck_preset(preset_id) if preset_id else None
    if preset and not deck and request.method == "GET":
        initial.update(
            {
                "title": preset["fields"]["title"],
                "description": preset["fields"]["description"],
            }
        )
    form = FlashcardDeckForm(request.POST or None, instance=deck, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        is_new = deck is None
        instance = form.save()
        added = 0
        if preset_id:
            try:
                added = create_cards_from_preset(instance, preset_id)
            except LookupError:
                preset = None
        if is_new and added:
            messages.success(
                request,
                f"Квизлет «{instance.title}» создан из шаблона: добавлено карточек: {added}.",
            )
        else:
            messages.success(request, f"Набор карточек сохранён: {instance.title}")
        return redirect("teacher_deck_cards", pk=instance.pk)
    return render(
        request,
        "lms/teacher_deck_form.html",
        {
            "form": form,
            "deck": deck,
            "topics": Topic.objects.select_related("block").order_by(
                "block__order", "order", "title"
            ),
            "presets": DECK_PRESETS,
            "preset_topics": deck_preset_topics(),
            "preset_levels": DECK_LEVELS,
            "active_preset": preset["id"] if preset else "",
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def deck_cards(request, pk):
    """Карточки набора: одиночное добавление, массовый импорт и шаблоны библиотеки."""
    deck = get_object_or_404(
        FlashcardDeck.objects.select_related("topic__block").prefetch_related("cards"), pk=pk
    )
    card_form = FlashcardForm(request.POST or None, prefix="card")
    bulk_form = FlashcardBulkForm(request.POST or None, prefix="bulk")
    if request.method == "POST":
        preset_id = request.POST.get("preset_id", "").strip()
        if preset_id and "_preset" in request.POST:
            try:
                added = create_cards_from_preset(
                    deck, preset_id, replace=bool(request.POST.get("preset_replace"))
                )
            except LookupError:
                messages.error(request, "Неизвестный шаблон квизлета.")
            else:
                preset = get_deck_preset(preset_id)
                messages.success(
                    request,
                    f"Из шаблона «{preset['label']}» добавлено карточек: {added}.",
                )
            return redirect("teacher_deck_cards", pk=deck.pk)
        if "bulk-cards_text" in request.POST and bulk_form.is_valid():
            if bulk_form.cleaned_data["replace"]:
                deck.cards.all().delete()
                CardReview.objects.filter(card__deck=deck).delete()
            start = deck.cards.aggregate(last=Max("order"))["last"] or 0
            Flashcard.objects.bulk_create(
                [
                    Flashcard(
                        deck=deck,
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
            return redirect("teacher_deck_cards", pk=deck.pk)
        if card_form.is_valid():
            card = card_form.save(commit=False)
            card.deck = deck
            if not card.order:
                card.order = (deck.cards.aggregate(last=Max("order"))["last"] or 0) + 1
            card.save()
            messages.success(request, f"Карточка добавлена: {card.front}")
            return redirect("teacher_deck_cards", pk=deck.pk)
    return render(
        request,
        "lms/teacher_deck_cards.html",
        {
            "deck": deck,
            "cards": list(deck.cards.all()),
            "card_form": card_form,
            "bulk_form": bulk_form,
            "presets": DECK_PRESETS,
            "preset_topics": deck_preset_topics(),
            "preset_levels": DECK_LEVELS,
            "learners": CardReview.objects.filter(card__deck=deck)
            .values("student_id")
            .distinct()
            .count(),
            "workspace": "curriculum",
        },
    )


@teacher_required
@require_POST
def card_delete(request, pk):
    card = get_object_or_404(Flashcard, pk=pk)
    deck_id = card.deck_id
    card.delete()
    messages.success(request, "Карточка удалена.")
    return redirect("teacher_deck_cards", pk=deck_id)


@teacher_required
@require_POST
def deck_delete(request, pk):
    deck = get_object_or_404(FlashcardDeck, pk=pk)
    return _delete(
        request,
        deck,
        "teacher_curriculum",
        "Набор нельзя удалить: ученики уже тренировали карточки. Скройте его флагом «Активен».",
        blocked=CardReview.objects.filter(card__deck=deck).exists(),
    )


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


@teacher_required
@require_GET
def student_detail(request, pk):
    student = get_object_or_404(
        User.objects.select_related("profile"), pk=pk, profile__role=Profile.Role.STUDENT
    )
    blocks_data, totals = course_tree(student=student)
    attempts = (
        Submission.objects.filter(student=student)
        .select_related("feedback", "assignment__topic__block")
        .order_by("-submitted_at", "-pk")[:30]
    )
    skill_rows = (
        Submission.objects.filter(student=student)
        .latest_attempts()
        .values("assignment__skills__name", "feedback__grade", "max_points_snapshot")
    )
    skills = {}
    for row in skill_rows:
        name = row["assignment__skills__name"]
        grade = row["feedback__grade"]
        maximum = row["max_points_snapshot"]
        if not name or grade is None or not maximum:
            continue
        bucket = skills.setdefault(name, {"scored": 0, "possible": 0, "count": 0})
        bucket["scored"] += grade
        bucket["possible"] += maximum
        bucket["count"] += 1
    for bucket in skills.values():
        bucket["percent"] = (
            int(round(100 * bucket["scored"] / bucket["possible"])) if bucket["possible"] else 0
        )
    cards_due = CardReview.objects.filter(student=student, due_at__lte=timezone.now()).count()
    return render(
        request,
        "lms/teacher_student_detail.html",
        {
            "student": student,
            "blocks_data": blocks_data,
            "totals": totals,
            "attempts": list(attempts),
            "skills": sorted(skills.items(), key=lambda item: item[1]["percent"]),
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
    summary = Submission.objects.latest_attempts().aggregate(
        average=Avg("feedback__grade"),
        graded=Count("feedback__grade"),
        waiting=Count("pk", filter=Q(status__in=QUEUE_FILTERS["pending"])),
        revision=Count("pk", filter=Q(status=Submission.Status.NEEDS_REVISION)),
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
    """Выгрузка журнала в CSV (Excel-совместимая: BOM и точка с запятой)."""
    block = request.GET.get("block", "")
    topic = request.GET.get("topic", "")
    data = gradebook(
        block=int(block) if block.isdigit() else None,
        topic=int(topic) if topic.isdigit() else None,
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="journal.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(
        ["Ученик", "Прогресс, %", "Сдано", "Проверено"]
        + [
            f"{item.topic.block.name} / {item.topic.title} / {item.title}"
            for item in data["assignments"]
        ]
    )
    for row in data["rows"]:
        writer.writerow(
            [
                row["student"].get_full_name() or row["student"].username,
                row["percent"],
                row["submitted"],
                row["checked"],
            ]
            + [
                (f"{cell['grade']}/{cell['maximum']}" if cell["grade"] is not None else "")
                for cell in row["cells"]
            ]
        )
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
                f"Ученик «{user.first_name} {user.last_name}» успешно создан. "
                f"Логин: {user.username}, Пароль: {form.saved_password}. "
                "Сохраните пароль и передайте его ученику!",
            )
            return redirect("teacher_students")
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
                request, f"Данные ученика «{student.first_name} {student.last_name}» обновлены."
            )
            return redirect("teacher_students")
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

    messages.success(
        request,
        f"Пароль ученика «{student.get_full_name() or student.username}» сброшен. "
        f"Новый пароль: {new_password}",
    )
    return redirect("teacher_students")
