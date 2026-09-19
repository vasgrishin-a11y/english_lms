"""The only application write path for attempts and reviews.

All operations lock the student's row first. This also serializes the *first*
attempt (there is no submission row to lock yet), quotas and review/resubmit races.
PostgreSQL is required in production; SQLite cannot provide these row locks.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from .decorators import get_user_role
from .models import Assignment, Feedback, Profile, Submission, SubmissionEvent

logger = logging.getLogger("lms.activity")


class ConflictError(Exception):
    """The page is stale; never silently overwrite newer data."""


class RateLimitError(Exception):
    pass


def _lock_student(student_id):
    return get_user_model().objects.select_for_update().get(pk=student_id)


def _used_bytes(student):
    # Count shared files once: a new attempt can reuse its predecessor's file.
    names = (
        Submission.objects.filter(student=student)
        .exclude(file_answer="")
        .order_by()
        .values_list("file_answer", flat=True)
        .distinct()
    )
    total = 0
    for name in names:
        try:
            total += default_storage.size(name)
        except FileNotFoundError:
            continue
    return total


def submit_assignment(*, student, assignment_id, expected_version, text_answer, file_answer=None):
    attempt = None
    new_upload = isinstance(file_answer, UploadedFile)
    try:
        with transaction.atomic():
            student = _lock_student(student.pk)
            if not student.is_active or get_user_role(student) != Profile.Role.STUDENT:
                raise PermissionDenied
            assignment = (
                Assignment.objects.select_for_update(of=("self",))
                .select_related("topic__block")
                .get(pk=assignment_id)
            )
            if not (
                assignment.is_active
                and assignment.topic.is_active
                and assignment.topic.block.is_active
            ):
                raise PermissionDenied
            latest = (
                Submission.objects.filter(student=student, assignment=assignment)
                .order_by("-version")
                .first()
            )
            actual_version = latest.version if latest else 0
            if expected_version != actual_version:
                raise ConflictError(
                    "Ответ уже отправлен или изменился. Обновите страницу перед новой попыткой."
                )
            recent = Submission.objects.filter(
                student=student, submitted_at__gte=timezone.now() - timedelta(hours=1)
            ).count()
            if recent >= settings.LMS_SUBMISSIONS_PER_HOUR:
                raise RateLimitError("Слишком много отправок. Повторите позже.")
            if new_upload:
                if file_answer.size > settings.LMS_MAX_FILE_BYTES:
                    raise ValidationError({"file_answer": "Файл превышает допустимый размер."})
                if _used_bytes(student) + file_answer.size > settings.LMS_STUDENT_QUOTA_BYTES:
                    raise ValidationError(
                        {"file_answer": "Квота хранения исчерпана. Обратитесь к администратору."}
                    )
            elif file_answer:
                if not latest or str(file_answer) != latest.file_answer.name:
                    raise ValidationError({"file_answer": "Нельзя использовать файл другой сдачи."})
                file_answer = latest.file_answer.name
            attempt = Submission(
                student=student,
                assignment=assignment,
                version=actual_version + 1,
                text_answer=text_answer,
                file_answer=file_answer or "",
                status=Submission.Status.SUBMITTED,
                max_points_snapshot=assignment.max_points,
                deadline_snapshot=assignment.deadline,
            )
            attempt.save(force_insert=True)
            SubmissionEvent.objects.create(
                submission=attempt, actor=student, action=SubmissionEvent.Action.SUBMITTED
            )
            transaction.on_commit(
                lambda: logger.info(
                    "submission.created id=%s version=%s actor=%s",
                    attempt.pk,
                    attempt.version,
                    student.pk,
                )
            )
        return attempt
    except Exception as exc:
        # Storage writes are not transactional. Undo only a newly written UUID file,
        # never a reused file belonging to an older immutable attempt.
        if new_upload and attempt and attempt.file_answer and attempt.file_answer._committed:
            from .file_cleanup import delete_unreferenced_file

            try:
                delete_unreferenced_file(attempt.file_answer.name)
            except Exception:
                logger.exception("file.rollback_cleanup_failed")
        if isinstance(exc, IntegrityError) and (
            "unique_submission_attempt" in str(exc) or "lms_submission.student_id" in str(exc)
        ):
            raise ConflictError("Работа уже отправлена. Обновите страницу.") from exc
        raise


@transaction.atomic
def review_submission(
    *, teacher, submission_id, expected_version, expected_review_revision, grade, comment, decision
):
    if not teacher.is_active or get_user_role(teacher) != Profile.Role.TEACHER:
        raise PermissionDenied
    student_id = Submission.objects.values_list("student_id", flat=True).get(pk=submission_id)
    _lock_student(student_id)
    submission = (
        Submission.objects.select_for_update(of=("self",))
        .select_related("assignment")
        .get(pk=submission_id)
    )
    has_newer = Submission.objects.filter(
        student_id=student_id,
        assignment_id=submission.assignment_id,
        version__gt=submission.version,
    ).exists()
    if (
        has_newer
        or submission.version != expected_version
        or submission.review_revision != expected_review_revision
    ):
        raise ConflictError(
            "Сдача или её проверка изменились. Обновите страницу; старое решение не сохранено."
        )
    feedback, _ = Feedback.objects.update_or_create(
        submission=submission,
        defaults={"teacher": teacher, "grade": grade, "comment": comment, "decision": decision},
    )
    submission.status = feedback.decision
    submission.review_revision += 1
    submission.save(update_fields=["status", "review_revision", "updated_at"])
    SubmissionEvent.objects.create(
        submission=submission,
        actor=teacher,
        action=SubmissionEvent.Action.REVIEWED,
        decision=decision,
        grade=grade,
        comment=comment,
    )
    transaction.on_commit(
        lambda: logger.info(
            "submission.reviewed id=%s revision=%s actor=%s decision=%s",
            submission.pk,
            submission.review_revision,
            teacher.pk,
            decision,
        )
    )
    return feedback
