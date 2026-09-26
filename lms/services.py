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
from django.db import IntegrityError, models, transaction
from django.urls import reverse
from django.utils import timezone

from .decorators import get_user_role
from .models import (
    TEACHER_FEEDBACK_RECORDING_LIMIT_SECONDS,
    AnswerDraft,
    Assignment,
    CardReview,
    Feedback,
    Flashcard,
    Profile,
    Question,
    QuestionResponse,
    QuizAttempt,
    Submission,
    SubmissionEvent,
)
from .scoring import (
    answer_parts,
    answers_summary,
    describe_answer,
    is_blank_answer,
    score_question,
)
from .validators import validate_recording_limit, validate_upload

logger = logging.getLogger("lms.activity")


class ConflictError(Exception):
    """The page is stale; never silently overwrite newer data."""


class RateLimitError(Exception):
    pass


def _lock_student(student_id):
    return get_user_model().objects.select_for_update().get(pk=student_id)


def _used_bytes(student):
    # Count shared files once: a new attempt can reuse its predecessor's file.
    names = set(
        Submission.objects.filter(student=student)
        .exclude(file_answer="")
        .order_by()
        .values_list("file_answer", flat=True)
        .distinct()
    )
    names.update(
        QuestionResponse.objects.filter(student=student)
        .exclude(file_answer="")
        .values_list("file_answer", flat=True)
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
                .select_related("topic__block", "topic__chapter")
                .get(pk=assignment_id)
            )
            if not (assignment.is_active and assignment.topic.is_reachable):
                raise PermissionDenied
            if assignment.is_no_submission:
                # Карточки и материалы для занятий: отвечать не нужно,
                # попытки не создаются даже прямым вызовом сервиса.
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
                try:
                    validate_recording_limit(file_answer, assignment.recording_limit_seconds)
                except ValidationError as exc:
                    raise ValidationError({"file_answer": exc.messages}) from exc
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
            # Ответ отправлен — черновик больше не нужен.
            AnswerDraft.objects.filter(student=student, assignment=assignment).delete()
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
    *,
    teacher,
    submission_id,
    expected_version,
    expected_review_revision,
    grade,
    comment,
    decision,
    item_points=None,
    audio_comment=None,
    remove_audio=False,
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
    if item_points:
        grade = _apply_item_points(submission, item_points, grade)
    if audio_comment:
        validate_recording_limit(audio_comment, TEACHER_FEEDBACK_RECORDING_LIMIT_SECONDS)
    feedback, _ = Feedback.objects.select_for_update().get_or_create(
        submission=submission,
        defaults={
            "teacher": teacher,
            "grade": grade,
            "comment": comment,
            "decision": decision,
        },
    )
    feedback.teacher = teacher
    feedback.grade = grade
    feedback.comment = comment
    feedback.decision = decision
    # Empty upload means «оставить текущую запись». Explicit removal is separate
    # so a teacher can edit the text without accidentally deleting useful audio.
    if audio_comment:
        feedback.audio_comment = audio_comment
    elif remove_audio:
        feedback.audio_comment = ""
    feedback.save()
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


def _apply_item_points(submission, item_points, grade):
    """Баллы преподавателя за свободные/голосовые пункты. Возвращает итоговый балл.

    Если общий балл не указан, он складывается из автопроверки и оценок пунктов.
    """
    responses = {
        item.pk: item
        for item in QuestionResponse.objects.select_for_update(of=("self",))
        .filter(submission=submission)
        .select_related("question")
    }
    for pk, value in item_points.items():
        response = responses.get(int(pk))
        if response is None or response.question is None or not response.question.is_manual:
            raise ValidationError({"grade": "Оценка относится к пункту другой работы."})
        if value is None:
            continue
        if value < 0 or value > response.question.points:
            raise ValidationError(
                {"grade": f"За пункт можно поставить от 0 до {response.question.points} баллов."}
            )
        response.teacher_points = value
        response.save(update_fields=["teacher_points", "updated_at"])
    if grade is None:
        auto = sum(
            item.points
            for item in responses.values()
            if item.question is None or not item.question.is_manual
        )
        manual = [
            item.teacher_points
            for item in responses.values()
            if item.question is not None and item.question.is_manual
        ]
        if all(value is not None for value in manual):
            grade = min(auto + sum(manual), submission.max_points_snapshot)
    return grade


# ── Тесты с автопроверкой, черновики и карточки ────────────────────────────
#
# Тот же контракт целостности, что и у ручной сдачи: блокировка строки ученика,
# проверка версии, лимит частоты, одна транзакция на попытку и её результат.

RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4
RATING_CHOICES = {
    "again": RATING_AGAIN,
    "hard": RATING_HARD,
    "good": RATING_GOOD,
    "easy": RATING_EASY,
}


def submit_quiz(*, student, assignment_id, expected_version, answers, by_student=False):
    """Сдать все пункты теста разом: одна окончательная попытка на каждый пункт.

    Используется импортом, демо-данными и сценариями без пошаговой проверки.
    Результат — та же неизменяемая сдача, что и при пошаговом прохождении.
    Пункты со свободным ответом так сдать нельзя: для них нужен ``save_item_answer``.
    """
    try:
        with transaction.atomic():
            student, assignment = _student_and_quiz(student, assignment_id)
            current, latest, accepting = round_state(student, assignment)
            if expected_version != latest:
                raise ConflictError(
                    "Тест уже отправлен или страница устарела. Обновите её перед новой попыткой."
                )
            if by_student and not accepting and not assignment.allow_retake:
                raise ConflictError("Задание уже завершено, повторное прохождение выключено.")
            questions = list(
                Question.objects.filter(assignment=assignment)
                .prefetch_related("choices")
                .order_by("order", "pk")
            )
            if not questions:
                raise ValidationError("В этом тесте пока нет вопросов. Обратитесь к преподавателю.")
            recent = Submission.objects.filter(
                student=student, submitted_at__gte=timezone.now() - timedelta(hours=1)
            ).count()
            if recent >= settings.LMS_SUBMISSIONS_PER_HOUR:
                raise RateLimitError("Слишком много отправок. Повторите позже.")
            for question in questions:
                if question.is_manual:
                    continue
                response = _response_for(student, assignment, question, current)
                if response.is_closed:
                    continue
                _apply_try(
                    response,
                    question,
                    (answers or {}).get(str(question.pk)),
                    max_tries=assignment.tries_per_item,
                    final=True,
                )
            return _finish_if_complete(student, assignment, current)
    except IntegrityError as exc:
        raise ConflictError("Тест уже отправлен. Обновите страницу.") from exc


# ── Пошаговая проверка пунктов: «Принять» → ✓ / ✗, до N попыток ─────────────
#
# Ученик отвечает на каждый пункт отдельно. Попытки пишутся в QuestionResponse
# текущего прохода; когда закрыты все пункты, проход превращается в обычную
# неизменяемую сдачу (Submission + QuizAttempt). Пункты без правильного ответа
# (свободный текст, голос) принимаются без проверки и ждут преподавателя.


def _student_and_quiz(student, assignment_id):
    """Заблокировать ученика и задание; проверить роль и видимость теста."""
    student = _lock_student(student.pk)
    if not student.is_active or get_user_role(student) != Profile.Role.STUDENT:
        raise PermissionDenied
    assignment = (
        Assignment.objects.select_for_update(of=("self",))
        .select_related("topic__block", "topic__chapter")
        .get(pk=assignment_id)
    )
    if not assignment.is_visible or not assignment.is_quiz:
        raise PermissionDenied
    return student, assignment


def round_state(student, assignment):
    """Текущий проход ученика: (номер, последняя версия сдачи, можно ли отвечать)."""
    latest = (
        Submission.objects.filter(student=student, assignment=assignment)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
        or 0
    )
    current = latest + 1
    started = QuestionResponse.objects.filter(
        student=student, assignment=assignment, round=current
    ).exists()
    return current, latest, latest == 0 or started


def _open_round(student, assignment, expected_round):
    current, _, accepting = round_state(student, assignment)
    if expected_round is not None and expected_round != current:
        raise ConflictError("Страница устарела: задание уже завершено. Обновите страницу.")
    if not accepting:
        raise ConflictError("Задание уже завершено. Результат — на странице задания.")
    return current


def _question_for(assignment, question_id):
    question = (
        Question.objects.filter(assignment=assignment, pk=question_id)
        .prefetch_related("choices")
        .first()
    )
    if question is None:
        raise PermissionDenied
    return question


def _response_for(student, assignment, question, current):
    response, _ = QuestionResponse.objects.select_for_update(of=("self",)).get_or_create(
        student=student,
        assignment=assignment,
        question=question,
        round=current,
    )
    return response


def check_item(*, student, assignment_id, question_id, answer, expected_round=None):
    """Проверить ответ на пункт с автопроверкой и вернуть обновлённый QuestionResponse.

    Пустой ответ попытку не расходует. Верный ответ даёт полный балл пункта
    независимо от номера попытки; после последней неудачной попытки пункт
    закрывается с частичным баллом последней попытки (для составных вопросов).
    """
    with transaction.atomic():
        student, assignment = _student_and_quiz(student, assignment_id)
        current = _open_round(student, assignment, expected_round)
        question = _question_for(assignment, question_id)
        if question.is_manual:
            raise ValidationError("Этот пункт проверяет преподаватель — ответ проверять не нужно.")
        response = _response_for(student, assignment, question, current)
        if response.is_closed:
            raise ConflictError("Этот пункт уже закрыт. Обновите страницу.")
        if is_blank_answer(question, answer):
            raise ValidationError("Сначала ответьте на пункт, затем нажмите «Принять».")
        _apply_try(response, question, answer, max_tries=assignment.tries_per_item)
        _finish_if_complete(student, assignment, current)
    return response


def _apply_try(response, question, answer, *, max_tries, final=False):
    """Записать попытку ответа на пункт. ``final`` закрывает пункт сразу."""
    result = score_question(question, answer)
    tries = list(response.tries or [])
    tries.append(
        {
            "given": result["given"],
            "display": describe_answer(question, result["given"]),
            "correct": result["correct"],
            "ratio": result["ratio"],
            "parts": answer_parts(question, result["given"]),
            "at": timezone.now().isoformat(timespec="seconds"),
        }
    )
    response.tries = tries
    if result["correct"]:
        response.state = QuestionResponse.State.CORRECT
        response.points = int(question.points or 0)
    elif final or len(tries) >= max_tries:
        response.state = QuestionResponse.State.FAILED
        response.points = result["points"]
    response.save(update_fields=["tries", "state", "points", "updated_at"])
    return result


def save_item_answer(
    *, student, assignment_id, question_id, text="", file=None, expected_round=None
):
    """Принять свободный или голосовой ответ на пункт. До завершения его можно заменить."""
    new_upload = isinstance(file, UploadedFile)
    previous_file = ""
    response = None
    try:
        with transaction.atomic():
            student, assignment = _student_and_quiz(student, assignment_id)
            current = _open_round(student, assignment, expected_round)
            question = _question_for(assignment, question_id)
            if not question.is_manual:
                raise ValidationError("Для этого пункта нажмите «Принять» — ответ проверится.")
            response = _response_for(student, assignment, question, current)
            text = (text or "").strip()[:20000]
            if question.kind == Question.Kind.TEXT:
                if not text:
                    raise ValidationError("Напишите ответ, затем нажмите «Принять».")
                response.text_answer = text
            else:
                if not new_upload:
                    raise ValidationError("Запишите ответ на микрофон или прикрепите аудиофайл.")
                validate_upload(file)
                extension = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
                if extension not in {"mp3", "wav", "m4a", "ogg", "aac"}:
                    raise ValidationError("Загрузите аудио: MP3, WAV, M4A, OGG или AAC.")
                validate_recording_limit(file, question.voice_limit)
                if file.size > settings.LMS_MAX_FILE_BYTES:
                    raise ValidationError("Файл превышает допустимый размер.")
                if _used_bytes(student) + file.size > settings.LMS_STUDENT_QUOTA_BYTES:
                    raise ValidationError("Квота хранения исчерпана. Обратитесь к администратору.")
                previous_file = response.file_answer.name if response.file_answer else ""
                response.file_answer = file
                response.text_answer = text
            response.state = QuestionResponse.State.ANSWERED
            response.tries = list(response.tries or []) + [
                {
                    "display": "аудиозапись" if question.kind == Question.Kind.VOICE else text,
                    "correct": None,
                    "at": timezone.now().isoformat(timespec="seconds"),
                }
            ]
            response.save()
            if previous_file:
                transaction.on_commit(lambda: _delete_response_file(previous_file))
            _finish_if_complete(student, assignment, current)
        return response
    except Exception:
        # Файл пишется в хранилище до коммита: откатываем только новый UUID-файл.
        if new_upload and response is not None and response.file_answer:
            name = response.file_answer.name
            if (
                name
                and name != previous_file
                and getattr(response.file_answer, "_committed", False)
            ):
                _delete_response_file(name)
        raise


def reset_item_answer(*, student, assignment_id, question_id, expected_round=None):
    """Удалить принятый свободный/голосовой ответ, чтобы записать новый (до завершения)."""
    with transaction.atomic():
        student, assignment = _student_and_quiz(student, assignment_id)
        current = _open_round(student, assignment, expected_round)
        question = _question_for(assignment, question_id)
        if not question.is_manual:
            raise PermissionDenied
        response = _response_for(student, assignment, question, current)
        old = response.file_answer.name if response.file_answer else ""
        response.file_answer = ""
        response.text_answer = ""
        response.state = QuestionResponse.State.OPEN
        response.save(update_fields=["file_answer", "text_answer", "state", "updated_at"])
        if old:
            transaction.on_commit(lambda: _delete_response_file(old))
    return response


def start_retake(*, student, assignment_id):
    """Начать задание заново, если преподаватель это разрешил."""
    with transaction.atomic():
        student, assignment = _student_and_quiz(student, assignment_id)
        current, latest, accepting = round_state(student, assignment)
        if accepting:
            return current
        if not assignment.allow_retake:
            raise PermissionDenied
        for question in assignment.questions.all():
            QuestionResponse.objects.get_or_create(
                student=student, assignment=assignment, question=question, round=current
            )
    return current


def finish_round(*, student, assignment_id, expected_round=None):
    """Отправить работу преподавателю, когда все пункты закрыты или приняты."""
    with transaction.atomic():
        student, assignment = _student_and_quiz(student, assignment_id)
        current = _open_round(student, assignment, expected_round)
        attempt = _finish_if_complete(student, assignment, current, explicit=True)
        if attempt is None:
            raise ValidationError("Ответьте на все пункты, чтобы отправить работу.")
    return attempt


def _delete_response_file(name):
    from .file_cleanup import delete_unreferenced_file

    delete_unreferenced_file(name)


def response_details(question, response):
    """Разбор пункта для QuizAttempt.answers — совместим с прежним форматом теста."""
    tries = list(response.tries or []) if response else []
    last = tries[-1] if tries else {}
    state = response.state if response else QuestionResponse.State.OPEN
    points = response.points if response else 0
    manual = question.is_manual
    expected = ""
    if not manual:
        expected = score_question(question, None)["expected"]
    ratio = 1.0 if state == QuestionResponse.State.CORRECT else float(last.get("ratio") or 0)
    return {
        "ratio": ratio,
        "points": points,
        "correct": state == QuestionResponse.State.CORRECT,
        "partially": not manual and state == QuestionResponse.State.FAILED and 0 < ratio < 1,
        "expected": expected,
        "given": last.get("given"),
        "state": state,
        "manual": manual,
        "tries": tries,
        "wrong_tries": sum(1 for item in tries if item.get("correct") is False),
    }


def _finish_if_complete(student, assignment, current, *, explicit=False):
    """Когда закрыты все пункты — создать неизменяемую сдачу с проверенным результатом.

    Задание только с автопроверкой завершается само, как только закрыт последний
    пункт. Если есть свободные или голосовые пункты, ученик отправляет работу
    сам (``explicit``): до этого запись можно удалить и перезаписать.
    """
    questions = list(
        Question.objects.filter(assignment=assignment)
        .prefetch_related("choices")
        .order_by("order", "pk")
    )
    if not questions:
        return None
    if not explicit and any(question.is_manual for question in questions):
        return None
    responses = {
        item.question_id: item
        for item in QuestionResponse.objects.select_for_update(of=("self",)).filter(
            student=student, assignment=assignment, round=current, submission__isnull=True
        )
    }
    if any(
        question.pk not in responses or not responses[question.pk].is_closed
        for question in questions
    ):
        return None
    details = {}
    score = max_score = correct_count = 0
    has_manual = False
    for question in questions:
        response = responses[question.pk]
        detail = response_details(question, response)
        details[str(question.pk)] = detail
        max_score += int(question.points or 0)
        if question.is_manual:
            has_manual = True
            continue
        score += detail["points"]
        correct_count += 1 if detail["correct"] else 0
    auto_total = sum(1 for question in questions if not question.is_manual)
    if assignment.max_points != max_score:
        assignment.max_points = max_score
        assignment.save(update_fields=["max_points", "updated_at"])
    result = {
        "score": score,
        "max_score": max_score,
        "correct_count": correct_count,
        "total_count": auto_total,
        "details": details,
    }
    recent = Submission.objects.filter(
        student=student, submitted_at__gte=timezone.now() - timedelta(hours=1)
    ).count()
    if recent >= settings.LMS_SUBMISSIONS_PER_HOUR:
        raise RateLimitError("Слишком много отправок. Повторите позже.")
    attempt = Submission(
        student=student,
        assignment=assignment,
        version=current,
        text_answer=answers_summary(questions, result),
        status=Submission.Status.SUBMITTED if has_manual else Submission.Status.CHECKED,
        max_points_snapshot=assignment.max_points,
        deadline_snapshot=assignment.deadline,
    )
    attempt.save(force_insert=True)
    QuizAttempt.objects.create(
        submission=attempt,
        score=score,
        max_score=max_score,
        correct_count=correct_count,
        total_count=auto_total,
        answers=details,
    )
    QuestionResponse.objects.filter(pk__in=[item.pk for item in responses.values()]).update(
        submission=attempt
    )
    SubmissionEvent.objects.create(
        submission=attempt, actor=student, action=SubmissionEvent.Action.SUBMITTED
    )
    first_try = sum(1 for item in details.values() if item["correct"] and len(item["tries"]) == 1)
    summary = (
        f"Автоматическая проверка: {correct_count} из {auto_total} пунктов верно"
        f" (с первой попытки — {first_try})."
    )
    if has_manual:
        SubmissionEvent.objects.create(
            submission=attempt,
            actor=None,
            action=SubmissionEvent.Action.REVIEWED,
            decision="",
            grade=score,
            comment=summary + " Пункты со свободным ответом ждут проверки преподавателя.",
        )
    else:
        Feedback.objects.create(
            submission=attempt, teacher=None, decision="checked", grade=score, comment=summary
        )
        SubmissionEvent.objects.create(
            submission=attempt,
            actor=None,
            action=SubmissionEvent.Action.REVIEWED,
            decision="checked",
            grade=score,
            comment="Автопроверка по пунктам",
        )
    transaction.on_commit(
        lambda: logger.info(
            "quiz.completed id=%s version=%s score=%s/%s manual=%s",
            attempt.pk,
            attempt.version,
            score,
            max_score,
            has_manual,
        )
    )
    return attempt


def save_answer_draft(*, student, assignment_id, text):
    """Черновик ответа. Существует отдельно от попыток и не нарушает их неизменяемость."""
    if not student.is_active or get_user_role(student) != Profile.Role.STUDENT:
        raise PermissionDenied
    assignment = Assignment.objects.filter(pk=assignment_id).first()
    if (
        not assignment
        or not assignment.is_visible
        or assignment.is_quiz
        or assignment.is_no_submission
    ):
        raise PermissionDenied
    draft, _ = AnswerDraft.objects.update_or_create(
        student=student,
        assignment=assignment,
        defaults={"text": (text or "")[:20000]},
    )
    return draft


def apply_sm2(review, rating):
    """Упрощённый SM-2: оценка < 3 возвращает карточку в очередь через 10 минут."""
    rating = max(1, min(5, int(rating)))
    now = timezone.now()
    if rating < 3:
        review.repetitions = 0
        review.interval_days = 0
        review.lapses += 1
        review.ease = max(1.3, round(review.ease - 0.2, 3))
        review.due_at = now + timedelta(minutes=10)
        return review
    review.repetitions += 1
    if review.repetitions == 1:
        review.interval_days = 1
    elif review.repetitions == 2:
        review.interval_days = 6
    else:
        review.interval_days = max(1, round(review.interval_days * review.ease))
    review.ease = min(
        3.5,
        max(1.3, round(review.ease + (0.1 - (5 - rating) * (0.08 + (5 - rating) * 0.02)), 3)),
    )
    review.due_at = now + timedelta(days=review.interval_days)
    return review


@transaction.atomic
def review_flashcard(*, student, card_id, rating):
    if rating not in RATING_CHOICES.values():
        raise ValidationError({"rating": "Недопустимая оценка повторения."})
    if not student.is_active or get_user_role(student) != Profile.Role.STUDENT:
        raise PermissionDenied
    card = (
        Flashcard.objects.select_related("assignment__topic__block", "assignment__topic__chapter")
        .filter(pk=card_id)
        .first()
    )
    if not card:
        raise PermissionDenied
    if not card_available(card, student):
        raise PermissionDenied
    review, created = CardReview.objects.get_or_create(card=card, student=student)
    if not created:
        review = CardReview.objects.select_for_update().get(pk=review.pk)
    apply_sm2(review, rating)
    review.save()
    return review


def practice_queue(*, student, cards, limit=20):
    """Карточки к повтору: сначала просроченные, затем новые. Один запрос на состояние."""
    cards = list(cards)
    card_ids = [card.pk for card in cards]
    reviews = {
        review.card_id: review
        for review in CardReview.objects.filter(student=student, card_id__in=card_ids)
    }
    now = timezone.now()
    due = []
    fresh = []
    for card in cards:
        review = reviews.get(card.pk)
        if review is None:
            fresh.append(card)
        elif review.due_at <= now:
            due.append((review.due_at, card))
    due = [card for _, card in sorted(due, key=lambda item: item[0])]
    queue = (due + fresh)[: max(1, int(limit))]
    return {
        "queue": queue,
        "due": len(due),
        "fresh": len(fresh),
        "total": len(cards),
        "learned": sum(1 for review in reviews.values() if review.interval_days >= 21),
    }


def card_available(card, student=None):
    """Доступна ли карточка ученику: курсовая — по активности курса, личная — владельцу."""
    assignment = card.assignment
    if assignment is not None:
        return bool(
            assignment.is_visible and assignment.is_active and assignment.topic.is_reachable
        )
    if card.owner_id is not None:
        return student is not None and card.owner_id == getattr(student, "pk", student)
    return False


def personal_cards(student):
    """Личный словарь ученика: карточки без задания курса."""
    return Flashcard.objects.filter(owner=student, assignment__isnull=True)


def visible_card_sets(student):
    """Задания-тренажёры активного курса, доступные ученику прямо сейчас."""
    return (
        Assignment.objects.visible(user=student)
        .filter(
            assignment_type=Assignment.Type.FLASHCARDS,
            is_active=True,
            topic__is_active=True,
            topic__chapter__is_active=True,
            topic__block__is_active=True,
        )
        .select_related("topic__block", "topic__chapter")
        .order_by("topic__block__order", "topic__chapter__order", "topic__order", "order", "pk")
    )


def add_dictionary_word(*, student, term, translation, example="", source_assignment=None):
    """Слово в личный словарь. Дубликат слова внутри словаря обновляется, а не плодится."""
    term = (term or "").strip()[:300]
    translation = (translation or "").strip()[:300]
    if not term or not translation:
        raise ValidationError({"term": "Нужны слово и перевод."})
    card = personal_cards(student).filter(front__iexact=term).first()
    if card:
        card.back = translation
        if example:
            card.example = example[:500]
        card.save(update_fields=["back", "example"])
        return card, False
    card = Flashcard.objects.create(
        owner=student,
        front=term,
        back=translation,
        example=(example or "")[:500],
        order=personal_cards(student).count(),
    )
    if source_assignment is not None:
        logger.info(
            "dictionary.add student=%s assignment=%s term=%r",
            student.pk,
            source_assignment,
            term,
        )
    return card, True


def card_set_stats(student):
    """Сводка по наборам карточек: задания-тренажёры курса и личный словарь ученика.

    Бюджет: фиксированное число запросов независимо от размера курса. Личный
    словарь — первым: собственные слова ученика всегда на виду.
    """
    now = timezone.now()
    sets = []
    personal = list(personal_cards(student))
    sets.append(
        _card_set(("personal", 0), "Мой словарь", "Личный словарь ученика", personal, student, now)
    )
    course = list(
        visible_card_sets(student).prefetch_related(
            models.Prefetch("cards", queryset=Flashcard.objects.order_by("order", "pk"))
        )
    )
    for assignment in course:
        sets.append(
            _card_set(
                ("assignment", assignment.pk),
                assignment.title,
                f"{assignment.topic.block.name} · {assignment.topic.title}",
                list(assignment.cards.all()),
                student,
                now,
                assignment=assignment,
            )
        )
    return sets


def _card_set(key, title, subtitle, cards, student, now, assignment=None):
    """Одна карточка статистики набора: сколько всего, изучено и к повтору."""
    card_ids = [card.pk for card in cards]
    reviewed = 0
    due = 0
    for due_at in CardReview.objects.filter(student=student, card_id__in=card_ids).values_list(
        "due_at", flat=True
    ):
        reviewed += 1
        if due_at <= now:
            due += 1
    return {
        "key": f"{key[0]}:{key[1]}",
        "title": title,
        "subtitle": subtitle,
        "cards": cards,
        "assignment": assignment,
        "is_personal": assignment is None,
        "card_total": len(cards),
        "reviewed_count": reviewed,
        "due_count": due + max(0, len(cards) - reviewed),
        "session_url": (
            reverse("student_dictionary_session")
            if assignment is None
            else reverse("trainer_session", args=[assignment.pk])
        ),
    }


@transaction.atomic
def regrade_assignment(assignment):
    """Recalculate automatic marks without rewriting submitted answers/manual grades."""
    questions = {q.pk: q for q in assignment.questions.prefetch_related("choices")}
    for response in QuestionResponse.objects.select_for_update(of=("self",)).filter(
        assignment=assignment
    ):
        question = questions.get(response.question_id)
        if question is None or question.is_manual or not response.tries:
            continue
        tries = []
        for entry in response.tries:
            result = score_question(question, entry.get("given"))
            tries.append({**entry, **result})
        last = tries[-1]
        response.tries = tries
        response.points = last["points"]
        if last["correct"]:
            response.state = QuestionResponse.State.CORRECT
        elif response.submission_id or len(tries) >= assignment.max_tries:
            response.state = QuestionResponse.State.FAILED
        else:
            response.state = QuestionResponse.State.OPEN
        response.save(update_fields=["tries", "points", "state", "updated_at"])

    for attempt in QuizAttempt.objects.select_for_update(of=("self",)).filter(
        submission__assignment=assignment
    ):
        details = dict(attempt.answers)
        responses = {r.question_id: r for r in attempt.submission.question_responses.all()}
        for key, detail in details.items():
            question = questions.get(int(key))
            if question is None or question.is_manual:
                continue
            if question.pk in responses:
                details[key] = response_details(question, responses[question.pk])
            else:
                details[key] = {**detail, **score_question(question, detail.get("given"))}
        attempt.answers = details
        attempt.score = sum(d.get("points", 0) for d in details.values() if not d.get("manual"))
        attempt.correct_count = sum(
            bool(d.get("correct")) for d in details.values() if not d.get("manual")
        )
        # Historical maxima and manually reviewed totals remain snapshots.
        attempt.score = min(attempt.score, attempt.max_score)
        attempt.save(update_fields=["answers", "score", "correct_count"])
        Feedback.objects.filter(submission_id=attempt.submission_id, teacher__isnull=True).update(
            grade=attempt.score,
            comment=f"Автопроверка после изменения ключа: {attempt.correct_count} из {attempt.total_count} верно.",
        )
