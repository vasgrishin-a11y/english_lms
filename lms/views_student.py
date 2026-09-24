"""Экраны ученика: главная, карта курса, задание, тренажёр карточек, оценки."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .curriculum import (
    WAITING_STATUSES,
    annotate_student_states,
    state_of,
    visible_assignments,
)
from .decorators import student_required
from .forms import DictionaryWordForm, SubmissionForm
from .models import (
    AnswerDraft,
    Assignment,
    CardReview,
    Flashcard,
    QuestionResponse,
    Skill,
    Submission,
)
from .quiz_items import build_items, progress_of
from .scoring import normalize_gap
from .services import (
    RATING_CHOICES,
    ConflictError,
    RateLimitError,
    add_dictionary_word,
    card_set_stats,
    check_item,
    finish_round,
    personal_cards,
    practice_queue,
    reset_item_answer,
    review_flashcard,
    round_state,
    save_answer_draft,
    save_item_answer,
    start_retake,
    submit_assignment,
    submit_quiz,
)
from .views import _add_validation_errors

SESSION_QUEUE_PREFIX = "trainer-queue:"


def _skill_context(assignment):
    """Контекст для боковой панели Sections: все навыки и выбранные."""
    # English labels for student
    eng_labels = {
        "grammar": "Grammar",
        "vocabulary": "Vocabulary",
        "listening": "Listening",
        "speaking": "Speaking",
        "writing": "Writing",
        "reading": "Reading",
    }
    all_kinds = [(k, eng_labels.get(k, k.title())) for k, _ in Skill.Kind.choices]
    assigned = set(assignment.skills.values_list("kind", flat=True)) if assignment.pk else set()
    return {"skill_kinds": all_kinds, "assigned_skill_kinds": assigned}


def _questions_with_choices(assignment):
    return list(assignment.questions.prefetch_related("choices").order_by("order", "pk"))


def _parse_order_answer(value, question):
    """Ответ «предложение из слов»: список id вариантов в выбранном порядке.

    Скрипт присылает id через запятую; без JavaScript форма отправляет слова
    через пробел — сопоставляем их с вариантами жадно, каждый вариант один раз.
    """
    raw = (value or "").strip()
    if not raw:
        return []
    if "," in raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    remaining = list(question.choices.all())
    ordered = []
    for token in raw.split():
        norm = normalize_gap(token)
        for choice in remaining:
            if normalize_gap(choice.text) == norm:
                ordered.append(str(choice.pk))
                remaining.remove(choice)
                break
    return ordered


def _collect_item_answer(request, question):
    """Ответ на один пункт из POST: выбор, пропуск, соответствие, порядок, сортировка."""
    key = f"q_{question.pk}"
    if question.kind == "multi":
        return request.POST.getlist(key)
    if question.kind in ("match", "sort"):
        mapping = {}
        for choice in question.choices.all():
            value = request.POST.get(f"{key}_{choice.pk}")
            if value:
                mapping[str(choice.pk)] = value
        return mapping
    if question.kind == "order":
        return _parse_order_answer(request.POST.get(key, ""), question)
    return request.POST.get(key, "")


def _collect_quiz_answers(request, questions):
    """Ответы сразу на все пункты — формат сервиса ``{str(question_id): значение}``."""
    return {str(question.pk): _collect_item_answer(request, question) for question in questions}


@student_required
@require_GET
def student_home(request):
    """Главная ученика: продолжить, сроки, прогресс, тренажёр, последние оценки."""
    student = request.user
    now = timezone.now()
    assignments = list(
        annotate_student_states(
            visible_assignments(student).select_related("topic__block", "topic__chapter"), student
        ).order_by(
            "topic__block__order",
            "topic__block__name",
            "topic__chapter__order",
            "topic__order",
            "order",
            "pk",
        )
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
        .select_related("feedback", "assignment__topic__block", "assignment__topic__chapter")
        .order_by("-feedback__updated_at", "-pk")[:5]
    )
    card_sets = card_set_stats(student)
    due_total = sum(item["due_count"] for item in card_sets)
    card_set_block_counts = {}
    for item in card_sets:
        assignment = item["assignment"]
        if assignment is not None and assignment.topic.block_id:
            block_id = assignment.topic.block_id
            card_set_block_counts[block_id] = card_set_block_counts.get(block_id, 0) + 1
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
        "card_sets": card_sets,
        "due_total": due_total,
        "card_set_block_counts": card_set_block_counts,
        "workspace": "home",
    }
    return render(request, "lms/student_home.html", context)


@student_required
@require_GET
def student_assignments(request):
    """Карта курса: страница заданий, сгруппированных по блокам и темам в шаблоне.

    Бюджет — фиксированное число запросов независимо от размера курса: сессия,
    пользователь, роль, счётчик страниц, сама страница и одна агрегирующая
    сводка. Задания с карточками приходят в общем списке с числом карточек.
    """
    query = request.GET.get("q", "").strip()[:200]
    assignments = annotate_student_states(
        visible_assignments(request.user).select_related("topic__block", "topic__chapter"),
        request.user,
    ).annotate(card_total=Count("cards"))
    if query:
        assignments = assignments.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(topic__title__icontains=query)
            | Q(topic__chapter__title__icontains=query)
            | Q(topic__block__name__icontains=query)
        )
    assignments = assignments.order_by(
        "topic__block__order",
        "topic__block__name",
        "topic__chapter__order",
        "topic__chapter__title",
        "topic__order",
        "topic__title",
        "order",
        "pk",
    )
    page = Paginator(assignments, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    for assignment in page:
        assignment.state = state_of(assignment)
        assignment.cards_count = assignment.card_total or 0
    summary = annotate_student_states(visible_assignments(request.user), request.user).aggregate(
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
        Assignment.objects.visible(user=request.user).select_related(
            "topic__block", "topic__chapter"
        ),
        pk=pk,
    )
    if assignment.is_flashcards or assignment.is_material:
        return _flashcards_detail(request, assignment)
    attempts = (
        Submission.objects.filter(student=request.user, assignment=assignment)
        .select_related("feedback__teacher")
        .order_by("-version")
    )
    submission = attempts.first()
    if assignment.is_quiz:
        return _quiz_detail(request, assignment, attempts, submission)
    draft = AnswerDraft.objects.filter(student=request.user, assignment=assignment).first()
    status = 200
    form = SubmissionForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        assignment=assignment,
        submission=submission,
        draft=draft,
    )

    if request.method == "POST" and form.is_valid():
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
    skill_ctx = _skill_context(assignment)
    response = render(
        request,
        "lms/assignment_detail.html",
        {
            "assignment": assignment,
            "submission": submission,
            "form": form,
            "questions": [],
            "quiz_result": None,
            "draft": draft,
            "page_obj": history,
            "conflict": status == 409,
            "trainer_cards": _topic_trainer_cards(assignment, request.user),
            "workspace": "curriculum",
            **skill_ctx,
        },
        status=status,
    )
    if status == 429:
        response["Retry-After"] = "3600"
    return response


def _quiz_context(request, assignment, attempts=None, submission=None):
    """Всё для экрана пошагового задания: пункты с состоянием, прогресс, итог."""
    if attempts is None:
        attempts = (
            Submission.objects.filter(student=request.user, assignment=assignment)
            .select_related("feedback__teacher")
            .order_by("-version")
        )
        submission = attempts.first()
    questions = _questions_with_choices(assignment)
    current, latest, accepting = round_state(request.user, assignment)
    if accepting:
        responses = QuestionResponse.objects.filter(
            student=request.user, assignment=assignment, round=current
        )
    else:
        responses = QuestionResponse.objects.filter(submission=submission)
    responses = {item.question_id: item for item in responses}
    quiz_result = getattr(submission, "quiz_attempt", None) if submission else None
    items = build_items(
        assignment,
        questions,
        responses,
        closed_round=not accepting,
        legacy=quiz_result.answers if quiz_result and not accepting else None,
    )
    return {
        "assignment": assignment,
        "submission": submission,
        "attempts": attempts,
        "items": items,
        "questions": questions,
        "quiz_round": current,
        "quiz_accepting": accepting,
        "quiz_progress": progress_of(items),
        "quiz_result": quiz_result if not accepting else None,
        "previous_result": quiz_result if accepting else None,
    }


def _quiz_detail(request, assignment, attempts, submission):
    status = 200
    quiz_error = ""
    if request.method == "POST":
        # Старая форма «сдать всё разом» (открытая до обновления вкладка, импорт):
        # каждый пункт получает одну окончательную попытку.
        try:
            submit_quiz(
                student=request.user,
                assignment_id=assignment.pk,
                expected_version=_parse_int(request.POST.get("expected_version"), -1),
                answers=_collect_quiz_answers(request, _questions_with_choices(assignment)),
                by_student=True,
            )
        except ConflictError as exc:
            quiz_error, status = str(exc), 409
        except RateLimitError as exc:
            quiz_error, status = str(exc), 429
        except ValidationError as exc:
            quiz_error = "; ".join(exc.messages)
        except PermissionDenied as exc:
            raise Http404 from exc
        else:
            messages.success(request, "Ответы приняты и проверены.")
            return redirect("assignment_detail", pk=assignment.pk)
        attempts = attempts.all()
        submission = attempts.first()
    context = _quiz_context(request, assignment, attempts, submission)
    history = Paginator(attempts, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    skill_ctx = _skill_context(assignment)
    context.update(
        {
            "form": None,
            "draft": None,
            "page_obj": history,
            "conflict": status == 409,
            "quiz_error": quiz_error,
            "trainer_cards": _topic_trainer_cards(assignment, request.user),
            "workspace": "curriculum",
            **skill_ctx,
        }
    )
    response = render(request, "lms/assignment_detail.html", context, status=status)
    if status == 429:
        response["Retry-After"] = "3600"
    return response


def _item_response(request, assignment, question_id, *, error="", status=200, flash=None):
    """Ответ на действие с пунктом: фрагмент для htmx или редирект к пункту без JS."""
    if not request.headers.get("HX-Request"):
        if error:
            messages.error(request, error)
        elif flash:
            messages.success(request, flash)
        return redirect(f"{reverse('assignment_detail', args=[assignment.pk])}#q-{question_id}")
    context = _quiz_context(request, assignment)
    item = next((entry for entry in context["items"] if entry["question"].pk == question_id), None)
    if item is None:
        raise Http404
    context.update({"item": item, "item_error": error, "oob": True})
    return render(request, "lms/parts/quiz_item_response.html", context, status=status)


def _item_action(request, pk, question_id, action):
    assignment = get_object_or_404(Assignment.objects.visible(user=request.user), pk=pk)
    if not assignment.is_quiz:
        raise Http404
    expected_round = _parse_int(request.POST.get("round"), None)
    try:
        flash = action(assignment, expected_round)
    except ConflictError as exc:
        return _item_response(request, assignment, question_id, error=str(exc), status=409)
    except RateLimitError as exc:
        return _item_response(request, assignment, question_id, error=str(exc), status=429)
    except ValidationError as exc:
        return _item_response(request, assignment, question_id, error="; ".join(exc.messages))
    except PermissionDenied as exc:
        raise Http404 from exc
    return _item_response(request, assignment, question_id, flash=flash)


@student_required
@require_POST
def item_check(request, pk, question_id):
    """«Принять» для пункта с автопроверкой: ✓ или ✗ и счётчик попыток."""

    def action(assignment, expected_round):
        question = get_object_or_404(
            assignment.questions.prefetch_related("choices"), pk=question_id
        )
        response = check_item(
            student=request.user,
            assignment_id=assignment.pk,
            question_id=question.pk,
            answer=_collect_item_answer(request, question),
            expected_round=expected_round,
        )
        if assignment.exam_mode:
            return "Ответ принят. Результат — после завершения задания."
        if response.state == QuestionResponse.State.CORRECT:
            return "Верно!"
        if response.state == QuestionResponse.State.FAILED:
            return "Попытки закончились — посмотрите правильный ответ."
        return f"Неверно. Осталось попыток: {assignment.tries_per_item - response.tries_used}."

    return _item_action(request, pk, question_id, action)


@student_required
@require_POST
def item_answer(request, pk, question_id):
    """«Принять» для свободного или голосового ответа: сохраняется без проверки."""

    def action(assignment, expected_round):
        save_item_answer(
            student=request.user,
            assignment_id=assignment.pk,
            question_id=question_id,
            text=request.POST.get(f"q_{question_id}", ""),
            file=request.FILES.get(f"q_{question_id}_file"),
            expected_round=expected_round,
        )
        return "Ответ принят — его проверит преподаватель."

    return _item_action(request, pk, question_id, action)


@student_required
@require_POST
def item_reset(request, pk, question_id):
    """Удалить принятый свободный/голосовой ответ и записать новый."""

    def action(assignment, expected_round):
        reset_item_answer(
            student=request.user,
            assignment_id=assignment.pk,
            question_id=question_id,
            expected_round=expected_round,
        )
        return "Ответ удалён — запишите новый."

    return _item_action(request, pk, question_id, action)


@student_required
@require_POST
def quiz_finish(request, pk):
    """Отправить работу со свободными/голосовыми пунктами преподавателю."""
    assignment = get_object_or_404(Assignment.objects.visible(user=request.user), pk=pk)
    try:
        finish_round(
            student=request.user,
            assignment_id=assignment.pk,
            expected_round=_parse_int(request.POST.get("round"), None),
        )
    except (ConflictError, RateLimitError, ValidationError) as exc:
        text = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, text)
    except PermissionDenied as exc:
        raise Http404 from exc
    else:
        messages.success(
            request, "Работа отправлена. Автопроверка готова, остальное проверит преподаватель."
        )
    return redirect("assignment_detail", pk=assignment.pk)


@student_required
@require_POST
def quiz_retake(request, pk):
    assignment = get_object_or_404(Assignment.objects.visible(user=request.user), pk=pk)
    try:
        start_retake(student=request.user, assignment_id=assignment.pk)
    except PermissionDenied as exc:
        raise Http404 from exc
    messages.success(request, "Новая попытка начата. Предыдущий результат сохранён в истории.")
    return redirect("assignment_detail", pk=assignment.pk)


def _topic_trainer_cards(assignment, student):
    """Соседние задания-тренажёры той же темы — для перехода из карточки задания."""
    return list(
        Assignment.objects.visible(user=student)
        .filter(
            topic_id=assignment.topic_id,
            assignment_type=Assignment.Type.FLASHCARDS,
            is_active=True,
        )
        .exclude(pk=assignment.pk)
        .order_by("order", "pk")
    )


def _flashcards_detail(request, assignment):
    """Задание с карточками или материалы: без сдачи, только просмотр и тренажёр.

    Сдач и оценок такое задание не предполагает — прогресс ведёт интервальное
    повторение, поэтому POST перенаправляет прямо в сессию тренажёра.
    Для материалов просто показываем файлы.
    """
    if request.method == "POST" and assignment.is_flashcards:
        return redirect("trainer_session", pk=assignment.pk)
    cards = list(assignment.cards.order_by("order", "pk")) if assignment.is_flashcards else []
    trainer_cards = _topic_trainer_cards(assignment, request.user)
    skill_ctx = _skill_context(assignment)
    attachments = list(assignment.attachments.all()) if hasattr(assignment, "attachments") else []
    return render(
        request,
        "lms/assignment_detail.html",
        {
            "assignment": assignment,
            "submission": None,
            "trainer_cards": trainer_cards,
            "form": None,
            "questions": [],
            "quiz_result": None,
            "draft": None,
            "page_obj": None,
            "conflict": False,
            "cards": cards,
            "attachments": attachments,
            "trainer_queue": practice_queue(student=request.user, cards=cards, limit=1)
            if cards
            else None,
            "workspace": "curriculum",
            **skill_ctx,
        },
    )


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@student_required
@require_POST
def save_draft(request, pk):
    """Автосохранение черновика ответа (htmx). Попытки при этом не создаются."""
    assignment = get_object_or_404(Assignment.objects.visible(user=request.user), pk=pk)
    if assignment.is_quiz or assignment.is_flashcards or assignment.is_material:
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
    """Тренажёр карточек: задания-тренажёры курса и личный словарь ученика."""
    card_sets = card_set_stats(request.user)
    return render(
        request,
        "lms/trainer.html",
        {
            "card_sets": card_sets,
            "due_total": sum(item["due_count"] for item in card_sets),
            "workspace": "trainer",
        },
    )


def _trainer_session(request, *, cards, session_key, back_url, title, subtitle, empty_context=None):
    """Общая логика сессии тренажёра: очередь в сессии, оценка повторения, htmx-ответ.

    Очередь хранится в сессии по ключу набора, поэтому обновление страницы не
    сбрасывает прогресс. Карточки берутся только из доступного ученику набора.
    """
    key = f"{SESSION_QUEUE_PREFIX}{session_key}"
    if request.method == "POST" and request.POST.get("reset") == "1":
        request.session.pop(key, None)
        messages.success(request, "Новая сессия: очередь карточек собрана заново.")
        return redirect(back_url)
    queue = request.session.get(key)
    available = {card.pk: card for card in cards}
    queue = [card_id for card_id in (queue or []) if card_id in available]
    if not queue:
        queue = [
            card.pk for card in practice_queue(student=request.user, cards=cards, limit=20)["queue"]
        ]
        request.session[key] = queue
    context = {
        "cards": cards,
        "title": title,
        "subtitle": subtitle,
        "session_url": back_url,
        "workspace": "trainer",
    }
    if not queue:
        return render(
            request,
            "lms/trainer_session.html",
            {**context, "card": None, "queue": [], "done": True},
        )
    card = available[queue[0]]
    if request.method == "POST":
        rating = RATING_CHOICES.get(request.POST.get("rating", ""))
        if rating is None:
            messages.error(request, "Выберите оценку повторения.")
        else:
            review_flashcard(student=request.user, card_id=card.pk, rating=rating)
            queue = queue[1:]
            request.session[key] = queue
            if request.headers.get("HX-Request"):
                next_card = available.get(queue[0]) if queue else None
                return render(
                    request,
                    "lms/parts/trainer_stage.html",
                    {**context, "card": next_card, "queue": queue},
                )
            return redirect(back_url)
    return render(
        request,
        "lms/trainer_session.html",
        {**context, "card": card, "queue": queue, "done": False},
    )


@student_required
@require_http_methods(["GET", "POST"])
def trainer_session(request, pk):
    """Сессия тренажёра по заданию с карточками."""
    assignment = get_object_or_404(
        Assignment.objects.visible(user=request.user)
        .filter(assignment_type=Assignment.Type.FLASHCARDS)
        .select_related("topic__block", "topic__chapter"),
        pk=pk,
    )
    if not (assignment.is_active and assignment.topic.is_reachable):
        raise Http404
    cards = list(assignment.cards.order_by("order", "pk"))
    return _trainer_session(
        request,
        cards=cards,
        session_key=f"assignment:{assignment.pk}",
        back_url=reverse("trainer_session", args=[assignment.pk]),
        title=assignment.title,
        subtitle=(
            f"{assignment.topic.block.name} · {assignment.topic.chapter.title} · "
            f"{assignment.topic.title}"
        ),
    )


@student_required
@require_http_methods(["GET", "POST"])
def dictionary_session(request):
    """Сессия тренажёра по личному словарю ученика."""
    cards = list(personal_cards(request.user).order_by("order", "pk"))
    return _trainer_session(
        request,
        cards=cards,
        session_key="personal",
        back_url=reverse("student_dictionary_session"),
        title="Мой словарь",
        subtitle="Личный словарь ученика",
    )


@student_required
@require_http_methods(["GET", "POST"])
def student_dictionary(request):
    """Личный словарь ученика: слова, статистика повторений и добавление новых слов."""
    form = DictionaryWordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _, created = add_dictionary_word(
            student=request.user,
            term=form.cleaned_data["term"],
            translation=form.cleaned_data["translation"],
            example=form.cleaned_data["example"],
        )
        messages.success(
            request,
            "Слово добавлено в личный словарь."
            if created
            else "Такое слово уже было — перевод обновлён.",
        )
        return redirect("student_dictionary")
    words = list(personal_cards(request.user).prefetch_related("reviews").order_by("order", "pk"))
    for card in words:
        card.review = next(
            (review for review in card.reviews.all() if review.student_id == request.user.pk),
            None,
        )
    queue = practice_queue(student=request.user, cards=words, limit=1)
    reviews = CardReview.objects.filter(student=request.user, card__in=words).aggregate(
        studied=Count("pk"), lapses=Sum("lapses"), repetitions=Sum("repetitions")
    )
    return render(
        request,
        "lms/student_dictionary.html",
        {
            "words": words,
            "form": form,
            "queue": queue,
            "reviews": reviews,
            "workspace": "dictionary",
        },
    )


@student_required
@require_POST
def dictionary_word_delete(request, pk):
    card = get_object_or_404(Flashcard, pk=pk, owner=request.user, assignment__isnull=True)
    card.delete()
    messages.success(request, "Слово удалено из словаря.")
    return redirect("student_dictionary")


@student_required
@require_GET
def student_grades(request):
    """Мои оценки: последние попытки с решением преподавателя."""
    attempts = (
        Submission.objects.filter(student=request.user)
        .latest_attempts()
        .select_related("feedback", "assignment__topic__block", "assignment__topic__chapter")
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
            visible_assignments(request.user)
            .select_related("topic__block", "topic__chapter")
            .filter(deadline__isnull=False),
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
