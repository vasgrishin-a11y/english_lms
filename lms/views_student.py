"""Экраны ученика: главная, карта курса, задание, тренажёр карточек, оценки."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .curriculum import (
    WAITING_STATUSES,
    annotate_student_states,
    state_of,
    visible_assignments,
)
from .decorators import student_required
from .forms import SubmissionForm
from .models import AnswerDraft, Assignment, FlashcardDeck, Submission
from .services import (
    RATING_CHOICES,
    ConflictError,
    RateLimitError,
    deck_stats,
    practice_queue,
    review_flashcard,
    save_answer_draft,
    submit_assignment,
    submit_quiz,
)
from .views import _add_validation_errors

SESSION_QUEUE_PREFIX = "trainer-queue:"


def _questions_with_choices(assignment):
    return list(assignment.questions.prefetch_related("choices").order_by("order", "pk"))


def _collect_quiz_answers(request, questions):
    """Собрать ответы теста из POST: одиночный выбор, несколько, пропуск, соответствие."""
    answers = {}
    for question in questions:
        key = f"q_{question.pk}"
        if question.kind == "multi":
            answers[str(question.pk)] = request.POST.getlist(key)
        elif question.kind == "match":
            mapping = {}
            for choice in question.choices.all():
                value = request.POST.get(f"{key}_{choice.pk}")
                if value:
                    mapping[str(choice.pk)] = value
            answers[str(question.pk)] = mapping
        else:
            answers[str(question.pk)] = request.POST.get(key, "")
    return answers


@student_required
@require_GET
def student_home(request):
    """Главная ученика: продолжить, сроки, прогресс, тренажёр, последние оценки."""
    student = request.user
    now = timezone.now()
    assignments = list(
        annotate_student_states(
            visible_assignments().select_related("topic__block"), student
        ).order_by("topic__block__order", "topic__block__name", "topic__order", "order", "pk")
    )
    drafts = {draft.assignment_id: draft for draft in AnswerDraft.objects.filter(student=student)}
    total = len(assignments)
    done = waiting = revision = graded = 0
    due_soon = []
    continue_candidates = []
    for assignment in assignments:
        state = state_of(assignment)
        assignment.state = state
        if state["status"] == Submission.Status.CHECKED:
            done += 1
            if state["grade"] is not None:
                graded += 1
        elif state["status"] == Submission.Status.NEEDS_REVISION:
            revision += 1
        elif state["status"]:
            waiting += 1
        if assignment.deadline and state["status"] != Submission.Status.CHECKED:
            hours_left = (assignment.deadline - now).total_seconds() / 3600
            if hours_left <= 72:
                due_soon.append((hours_left, assignment))
        unfinished = state["status"] in (None, Submission.Status.NEEDS_REVISION)
        if unfinished or assignment.pk in drafts:
            continue_candidates.append(assignment)
    due_soon.sort(key=lambda item: item[0])
    recent = list(
        Submission.objects.filter(student=student, feedback__isnull=False)
        .select_related("feedback", "assignment__topic__block")
        .order_by("-feedback__updated_at", "-pk")[:5]
    )
    decks = deck_stats(student)
    due_total = sum(deck.due_count for deck in decks)
    context = {
        "continue_item": continue_candidates[0] if continue_candidates else None,
        "continue_draft": drafts.get(continue_candidates[0].pk if continue_candidates else None),
        "due_soon": [item[1] for item in due_soon[:6]],
        "assignments": assignments,
        "totals": {
            "total": total,
            "done": done,
            "waiting": waiting,
            "revision": revision,
            "progress": int(round(100 * done / total)) if total else 0,
        },
        "recent": recent,
        "decks": decks,
        "due_total": due_total,
        "workspace": "home",
    }
    return render(request, "lms/student_home.html", context)


@student_required
@require_GET
def student_assignments(request):
    """Карта курса: страница заданий, сгруппированных по блокам и темам в шаблоне.

    Бюджет — шесть запросов независимо от размера курса: сессия, пользователь,
    роль, счётчик страниц, сама страница и одна агрегирующая сводка.
    """
    query = request.GET.get("q", "").strip()[:200]
    assignments = annotate_student_states(
        visible_assignments().select_related("topic__block"), request.user
    )
    if query:
        assignments = assignments.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(topic__title__icontains=query)
            | Q(topic__block__name__icontains=query)
        )
    assignments = assignments.order_by(
        "topic__block__order",
        "topic__block__name",
        "topic__order",
        "topic__title",
        "order",
        "pk",
    )
    page = Paginator(assignments, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    for assignment in page:
        assignment.state = state_of(assignment)
    summary = annotate_student_states(visible_assignments(), request.user).aggregate(
        total=Count("pk"),
        done=Count("pk", filter=Q(latest_status=Submission.Status.CHECKED)),
        waiting=Count("pk", filter=Q(latest_status__in=WAITING_STATUSES)),
        blocks=Count("topic__block_id", distinct=True),
        topics=Count("topic_id", distinct=True),
    )
    total = summary["total"] or 0
    summary["progress"] = int(round(100 * summary["done"] / total)) if total else 0
    mode = request.GET.get("mode", "map")
    if mode not in {"map", "list"}:
        mode = "map"
    return render(
        request,
        "lms/student_assignments.html",
        {
            "assignments": page,
            "totals": summary,
            "page_obj": page,
            "query": query,
            "mode": mode,
            "workspace": "curriculum",
        },
    )


@student_required
@require_http_methods(["GET", "POST"])
def assignment_detail(request, pk):
    """Страница задания: условия, ответ или тест, черновик, история попыток."""
    assignment = get_object_or_404(
        Assignment.objects.visible().select_related("topic__block"), pk=pk
    )
    attempts = (
        Submission.objects.filter(student=request.user, assignment=assignment)
        .select_related("feedback__teacher")
        .order_by("-version")
    )
    submission = attempts.first()
    questions = _questions_with_choices(assignment) if assignment.is_quiz else []
    quiz_result = getattr(submission, "quiz_attempt", None) if submission else None
    draft = (
        AnswerDraft.objects.filter(student=request.user, assignment=assignment).first()
        if not assignment.is_quiz
        else None
    )
    form = None
    status = 200
    if not assignment.is_quiz:
        form = SubmissionForm(
            request.POST if request.method == "POST" else None,
            request.FILES if request.method == "POST" else None,
            assignment=assignment,
            submission=submission,
            draft=draft,
        )

    if request.method == "POST":
        expected_version = _parse_int(request.POST.get("expected_version"), -1)
        if assignment.is_quiz:
            if not questions:
                messages.error(request, "В этом тесте пока нет вопросов. Напишите преподавателю.")
            else:
                try:
                    submit_quiz(
                        student=request.user,
                        assignment_id=assignment.pk,
                        expected_version=expected_version,
                        answers=_collect_quiz_answers(request, questions),
                    )
                except ConflictError as exc:
                    status = 409
                    messages.error(request, str(exc))
                except RateLimitError as exc:
                    status = 429
                    messages.error(request, str(exc))
                except ValidationError as exc:
                    messages.error(request, "; ".join(exc.messages))
                except PermissionDenied as exc:
                    raise Http404 from exc
                else:
                    messages.success(request, "Тест отправлен и проверен автоматически.")
                    return redirect("assignment_detail", pk=assignment.pk)
        elif form.is_valid():
            try:
                submit_assignment(
                    student=request.user, assignment_id=assignment.pk, **form.cleaned_data
                )
            except ConflictError as exc:
                form.add_error(None, str(exc))
                status = 409
            except RateLimitError as exc:
                form.add_error(None, str(exc))
                status = 429
            except ValidationError as exc:
                _add_validation_errors(form, exc)
            except PermissionDenied as exc:
                raise Http404 from exc
            else:
                messages.success(
                    request,
                    "Новая попытка отправлена на проверку. Предыдущие ответы сохранены в истории.",
                )
                return redirect("assignment_detail", pk=assignment.pk)

    history = Paginator(attempts, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    response = render(
        request,
        "lms/assignment_detail.html",
        {
            "assignment": assignment,
            "submission": submission,
            "form": form,
            "questions": questions,
            "quiz_result": quiz_result,
            "draft": draft,
            "page_obj": history,
            "conflict": status == 409,
            "workspace": "curriculum",
        },
        status=status,
    )
    if status == 429:
        response["Retry-After"] = "3600"
    return response


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@student_required
@require_POST
def save_draft(request, pk):
    """Автосохранение черновика ответа (htmx). Попытки при этом не создаются."""
    assignment = get_object_or_404(Assignment.objects.visible(), pk=pk)
    if assignment.is_quiz:
        return render(request, "lms/parts/draft_state.html", {"draft": None, "denied": True})
    try:
        draft = save_answer_draft(
            student=request.user, assignment_id=assignment.pk, text=request.POST.get("text", "")
        )
    except PermissionDenied:
        return render(request, "lms/parts/draft_state.html", {"draft": None, "denied": True})
    return render(request, "lms/parts/draft_state.html", {"draft": draft, "denied": False})


@student_required
@require_GET
def trainer(request):
    """Тренажёр карточек: наборы с числом карточек к повтору."""
    decks = deck_stats(request.user)
    return render(
        request,
        "lms/trainer.html",
        {
            "decks": decks,
            "due_total": sum(deck.due_count for deck in decks),
            "workspace": "trainer",
        },
    )


@student_required
@require_http_methods(["GET", "POST"])
def trainer_session(request, pk):
    deck = get_object_or_404(
        FlashcardDeck.objects.filter(
            is_active=True, topic__is_active=True, topic__block__is_active=True
        ).select_related("topic__block"),
        pk=pk,
    )
    key = f"{SESSION_QUEUE_PREFIX}{deck.pk}"
    if request.method == "POST" and request.POST.get("reset") == "1":
        request.session.pop(key, None)
        messages.success(request, "Новая сессия: очередь карточек собрана заново.")
        return redirect("trainer_session", pk=deck.pk)
    queue = request.session.get(key)
    if not queue:
        queue = [
            card.pk for card in practice_queue(student=request.user, deck=deck, limit=20)["queue"]
        ]
        request.session[key] = queue
    if not queue:
        return render(
            request,
            "lms/trainer_session.html",
            {"deck": deck, "card": None, "queue": [], "done": True, "workspace": "trainer"},
        )
    card = get_object_or_404(deck.cards, pk=queue[0])
    if request.method == "POST":
        rating = RATING_CHOICES.get(request.POST.get("rating", ""))
        if rating is None:
            messages.error(request, "Выберите оценку повторения.")
        else:
            review_flashcard(student=request.user, card_id=card.pk, rating=rating)
            queue = queue[1:]
            request.session[key] = queue
            if request.headers.get("HX-Request"):
                if not queue:
                    return render(
                        request,
                        "lms/parts/trainer_stage.html",
                        {"deck": deck, "card": None, "queue": []},
                    )
                card = get_object_or_404(deck.cards, pk=queue[0])
                return render(
                    request,
                    "lms/parts/trainer_stage.html",
                    {"deck": deck, "card": card, "queue": queue},
                )
            return redirect("trainer_session", pk=deck.pk)
    return render(
        request,
        "lms/trainer_session.html",
        {
            "deck": deck,
            "card": card,
            "queue": queue,
            "done": False,
            "workspace": "trainer",
        },
    )


@student_required
@require_GET
def student_grades(request):
    """Мои оценки: последние попытки с решением преподавателя."""
    attempts = (
        Submission.objects.filter(student=request.user)
        .latest_attempts()
        .select_related("feedback", "assignment__topic__block")
        .order_by("-submitted_at", "-pk")
    )
    page = Paginator(attempts, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    scored = [
        (attempt.feedback.grade, attempt.max_points_snapshot)
        for attempt in attempts
        if (feedback := getattr(attempt, "feedback", None)) and feedback.grade is not None
    ]
    graded = [grade for grade, _ in scored]
    maximums = [maximum for _, maximum in scored]
    average = int(round(100 * sum(graded) / sum(maximums))) if graded and sum(maximums) else None
    return render(
        request,
        "lms/student_grades.html",
        {
            "attempts": page,
            "page_obj": page,
            "average": average,
            "checked": len(graded),
            "workspace": "grades",
        },
    )


@student_required
@require_GET
def upcoming(request):
    """Ближайшие сроки — отдельный фокус-экран перед экзаменом."""
    now = timezone.now()
    assignments = list(
        annotate_student_states(
            visible_assignments().select_related("topic__block").filter(deadline__isnull=False),
            request.user,
        ).order_by("deadline")
    )
    open_items = []
    for assignment in assignments:
        state = state_of(assignment)
        if state["status"] == Submission.Status.CHECKED:
            continue
        assignment.state = state
        assignment.hours_left = (assignment.deadline - now).total_seconds() / 3600
        open_items.append(assignment)
    return render(
        request,
        "lms/student_upcoming.html",
        {
            "items": open_items[:100],
            "overdue": [item for item in open_items if item.hours_left < 0],
            "week": [item for item in open_items if 0 <= item.hours_left <= 168],
            "workspace": "curriculum",
        },
    )
