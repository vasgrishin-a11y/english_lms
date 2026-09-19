from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .decorators import get_user_role, student_required, teacher_required
from .forms import ReviewForm, SubmissionForm
from .models import (
    Assignment,
    Block,
    Feedback,
    Profile,
    Submission,
    Topic,
)


@login_required
def dashboard(request):
    role = get_user_role(request.user)

    if role == Profile.Role.TEACHER:
        return redirect("teacher_submissions")

    return redirect("student_assignments")


@student_required
def student_assignments(request):
    blocks_data = []

    blocks = Block.objects.filter(is_active=True).order_by("order", "name")

    for block in blocks:
        topics_data = []

        topics = block.topics.filter(is_active=True).order_by("order", "title")

        for topic in topics:
            assignments = topic.assignments.filter(is_active=True).order_by(
                "order",
                "created_at",
            )

            if assignments.exists():
                topics_data.append(
                    {
                        "topic": topic,
                        "assignments": assignments,
                    }
                )

        if topics_data:
            blocks_data.append(
                {
                    "block": block,
                    "topics": topics_data,
                }
            )

    return render(
        request,
        "lms/student_assignments.html",
        {"blocks_data": blocks_data},
    )


@student_required
def assignment_detail(request, pk):
    assignment = get_object_or_404(
        Assignment.objects.select_related("topic__block"),
        pk=pk,
        is_active=True,
        topic__is_active=True,
        topic__block__is_active=True,
    )

    submission = (
        Submission.objects.filter(student=request.user, assignment=assignment)
        .select_related("feedback")
        .first()
    )

    feedback = None
    if submission:
        feedback = getattr(submission, "feedback", None)

    if request.method == "POST":
        form = SubmissionForm(
            request.POST,
            request.FILES,
            instance=submission,
            assignment=assignment,
        )

        if form.is_valid():
            obj = form.save(commit=False)
            obj.student = request.user
            obj.assignment = assignment
            obj.status = Submission.Status.SUBMITTED
            obj.save()

            messages.success(request, "Задание отправлено на проверку.")
            return redirect("student_assignments")
    else:
        form = SubmissionForm(instance=submission, assignment=assignment)

    return render(
        request,
        "lms/assignment_detail.html",
        {
            "assignment": assignment,
            "submission": submission,
            "feedback": feedback,
            "form": form,
        },
    )


@teacher_required
def teacher_submissions(request):
    submissions = (
        Submission.objects.select_related(
            "student",
            "assignment__topic__block",
        )
        .exclude(status=Submission.Status.CHECKED)
        .order_by("-submitted_at")
    )

    query = request.GET.get("q", "").strip()

    if query:
        submissions = submissions.filter(
            Q(student__username__icontains=query)
            | Q(student__first_name__icontains=query)
            | Q(student__last_name__icontains=query)
            | Q(assignment__title__icontains=query)
            | Q(assignment__topic__title__icontains=query)
            | Q(assignment__topic__block__name__icontains=query)
        )

    return render(
        request,
        "lms/teacher_submissions.html",
        {
            "submissions": submissions,
            "query": query,
        },
    )


@teacher_required
def teacher_submission_review(request, pk):
    submission = get_object_or_404(
        Submission.objects.select_related(
            "student",
            "assignment__topic__block",
            "feedback",
        ),
        pk=pk,
    )

    feedback = getattr(submission, "feedback", None)

    if request.method == "POST":
        form = ReviewForm(
            request.POST,
            assignment=submission.assignment,
            feedback=feedback,
            submission=submission,
        )

        if form.is_valid():
            decision = form.cleaned_data["decision"]
            grade = form.cleaned_data.get("grade") or 0
            comment = form.cleaned_data.get("comment", "")

            if decision == ReviewForm.DECISION_CHECKED:
                Feedback.objects.update_or_create(
                    submission=submission,
                    defaults={
                        "teacher": request.user,
                        "grade": grade,
                        "comment": comment,
                    },
                )

                submission.status = Submission.Status.CHECKED
                submission.save(update_fields=["status", "updated_at"])

                messages.success(request, "Сдача проверена.")

            else:
                if feedback:
                    feedback.teacher = request.user
                    feedback.grade = grade
                    feedback.comment = comment
                    feedback.save()
                else:
                    Feedback.objects.create(
                        submission=submission,
                        teacher=request.user,
                        grade=grade,
                        comment=comment,
                    )

                submission.status = Submission.Status.NEEDS_REVISION
                submission.save(update_fields=["status", "updated_at"])

                messages.success(request, "Сдача отправлена на доработку.")

            return redirect("teacher_submissions")
    else:
        form = ReviewForm(
            assignment=submission.assignment,
            feedback=feedback,
            submission=submission,
        )

    return render(
        request,
        "lms/teacher_submission_review.html",
        {
            "submission": submission,
            "feedback": feedback,
            "form": form,
        },
    )