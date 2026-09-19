import logging
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import OuterRef, Q, Subquery
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_safe

from .decorators import get_user_role, student_required, teacher_required
from .forms import ReviewForm, SubmissionForm
from .models import Assignment, Profile, Submission
from .services import ConflictError, RateLimitError, review_submission, submit_assignment

logger = logging.getLogger(__name__)


def _add_validation_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


@login_required
@require_safe
def dashboard(request):
    return redirect(
        "teacher_submissions"
        if get_user_role(request.user) == Profile.Role.TEACHER
        else "student_assignments"
    )


@student_required
@require_safe
def student_assignments(request):
    latest = Submission.objects.filter(student=request.user, assignment=OuterRef("pk")).order_by(
        "-version"
    )
    query = request.GET.get("q", "").strip()[:200]
    assignments = (
        Assignment.objects.filter(
            is_active=True, topic__is_active=True, topic__block__is_active=True
        )
        .select_related("topic__block")
        .annotate(
            latest_status=Subquery(latest.values("status")[:1]),
            latest_grade=Subquery(latest.values("feedback__grade")[:1]),
        )
        .order_by(
            "topic__block__order",
            "topic__block__name",
            "topic__order",
            "topic__title",
            "order",
            "pk",
        )
    )
    if query:
        assignments = assignments.filter(
            Q(title__icontains=query)
            | Q(topic__title__icontains=query)
            | Q(topic__block__name__icontains=query)
        )
    page = Paginator(assignments, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    blocks_data = []
    for assignment in page:
        if not blocks_data or blocks_data[-1]["block"].pk != assignment.topic.block_id:
            blocks_data.append({"block": assignment.topic.block, "topics": []})
        topics = blocks_data[-1]["topics"]
        if not topics or topics[-1]["topic"].pk != assignment.topic_id:
            topics.append({"topic": assignment.topic, "assignments": []})
        topics[-1]["assignments"].append(assignment)
    return render(
        request,
        "lms/student_assignments.html",
        {"blocks_data": blocks_data, "page_obj": page, "query": query},
    )


@student_required
@require_http_methods(["GET", "POST"])
def assignment_detail(request, pk):
    assignment = get_object_or_404(
        Assignment.objects.select_related("topic__block"),
        pk=pk,
        is_active=True,
        topic__is_active=True,
        topic__block__is_active=True,
    )
    attempts = (
        Submission.objects.filter(student=request.user, assignment=assignment)
        .select_related("feedback__teacher")
        .order_by("-version")
    )
    submission = attempts.first()
    form = SubmissionForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        assignment=assignment,
        submission=submission,
    )
    status = 200
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
            "page_obj": history,
            "conflict": status == 409,
        },
        status=status,
    )
    if status == 429:
        response["Retry-After"] = "3600"
    return response


QUEUE_CHOICES = [
    ("pending", "На проверке"),
    ("revision", "Ожидают доработки"),
    ("checked", "Проверенные"),
    ("all", "Все последние попытки"),
]


@teacher_required
@require_safe
def teacher_submissions(request):
    queue = request.GET.get("status", "pending")
    if queue not in dict(QUEUE_CHOICES):
        queue = "pending"
    submissions = (
        Submission.objects.latest_attempts()
        .select_related("student", "assignment__topic__block", "feedback")
        .order_by("-submitted_at", "-pk")
    )
    filters = {
        "pending": [Submission.Status.SUBMITTED, Submission.Status.IN_REVIEW],
        "revision": [Submission.Status.NEEDS_REVISION],
        "checked": [Submission.Status.CHECKED],
    }
    if queue in filters:
        submissions = submissions.filter(status__in=filters[queue])
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
    page = Paginator(submissions, settings.LMS_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "lms/teacher_submissions.html",
        {
            "submissions": page,
            "page_obj": page,
            "query": query,
            "queue": queue,
            "queue_choices": QUEUE_CHOICES,
        },
    )


@teacher_required
@require_http_methods(["GET", "POST"])
def teacher_submission_review(request, pk):
    submission = get_object_or_404(
        Submission.objects.select_related(
            "student", "assignment__topic__block", "feedback__teacher"
        ),
        pk=pk,
    )
    attempts = (
        Submission.objects.filter(student=submission.student, assignment=submission.assignment)
        .select_related("feedback__teacher")
        .order_by("-version")
    )
    latest = attempts.first()
    is_latest = latest.pk == submission.pk
    feedback = getattr(submission, "feedback", None)
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
            messages.success(request, "Проверка сохранена для выбранной попытки.")
            return redirect("teacher_submission_review", pk=submission.pk)
    return render(
        request,
        "lms/teacher_submission_review.html",
        {
            "submission": submission,
            "feedback": feedback,
            "form": form,
            "is_latest": is_latest,
            "latest": latest,
            "conflict": status == 409,
            "page_obj": Paginator(attempts, settings.LMS_PAGE_SIZE).get_page(
                request.GET.get("page")
            ),
            "events": submission.events.select_related("actor").order_by("-created_at", "-pk")[:20],
        },
        status=status,
    )


@login_required
@require_safe
@never_cache
def private_file(request, name):
    """Resolve only DB-referenced names, then authorize; never join raw URL paths."""
    teacher = get_user_role(request.user) == Profile.Role.TEACHER
    materials = Assignment.objects.filter(material_file=name)
    attempts = Submission.objects.filter(file_answer=name)
    if not teacher:
        materials = materials.filter(
            is_active=True, topic__is_active=True, topic__block__is_active=True
        )
        attempts = attempts.filter(
            student=request.user,
            assignment__is_active=True,
            assignment__topic__is_active=True,
            assignment__topic__block__is_active=True,
        )
    material = materials.first()
    attempt = None if material else attempts.first()
    file = material.material_file if material else attempt.file_answer if attempt else None
    if not file:
        raise Http404
    try:
        response = FileResponse(
            file.open("rb"),
            as_attachment=True,
            filename=Path(file.name).name,
            content_type="application/octet-stream",
        )
    except FileNotFoundError as exc:
        raise Http404 from exc
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@never_cache
def health_live(request):
    return JsonResponse({"status": "ok"})


@require_GET
@never_cache
def health_ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        Submission.objects.exists()
        with tempfile.TemporaryFile(dir=settings.MEDIA_ROOT):
            pass
    except Exception:
        logger.exception("health.readiness_failed")
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
