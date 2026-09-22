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
from django.db.models import Q
from django.utils import timezone

from .decorators import get_user_role
from .models import (
    AnswerDraft,
    Assignment,
    CardReview,
    Feedback,
    Flashcard,
    FlashcardDeck,
    Profile,
    Question,
    QuizAttempt,
    Submission,
    SubmissionEvent,
)
from .scoring import answers_summary, score_quiz

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


def submit_quiz(*, student, assignment_id, expected_version, answers):
    """Автопроверяемая попытка: Submission + QuizAttempt + Feedback в одной транзакции."""
    attempt = None
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
            if not assignment.is_visible or not assignment.is_quiz:
                raise PermissionDenied
            latest = (
                Submission.objects.filter(student=student, assignment=assignment)
                .order_by("-version")
                .first()
            )
            actual_version = latest.version if latest else 0
            if expected_version != actual_version:
                raise ConflictError(
                    "Тест уже отправлен или страница устарела. Обновите её перед новой попыткой."
                )
            recent = Submission.objects.filter(
                student=student, submitted_at__gte=timezone.now() - timedelta(hours=1)
            ).count()
            if recent >= settings.LMS_SUBMISSIONS_PER_HOUR:
                raise RateLimitError("Слишком много отправок. Повторите позже.")
            questions = list(
                Question.objects.filter(assignment=assignment)
                .prefetch_related("choices")
                .order_by("order", "pk")
            )
            if not questions:
                raise ValidationError("В этом тесте пока нет вопросов. Обратитесь к преподавателю.")
            result = score_quiz(questions, answers)
            if assignment.max_points != result["max_score"]:
                # Инвариант: максимум задания равен сумме баллов вопросов.
                assignment.max_points = result["max_score"]
                assignment.save(update_fields=["max_points", "updated_at"])
            attempt = Submission(
                student=student,
                assignment=assignment,
                version=actual_version + 1,
                text_answer=answers_summary(questions, result),
                status=Submission.Status.CHECKED,
                max_points_snapshot=assignment.max_points,
                deadline_snapshot=assignment.deadline,
            )
            attempt.save(force_insert=True)
            QuizAttempt.objects.create(
                submission=attempt,
                score=result["score"],
                max_score=result["max_score"],
                correct_count=result["correct_count"],
                total_count=result["total_count"],
                answers=result["details"],
            )
            Feedback.objects.create(
                submission=attempt,
                teacher=None,
                decision="checked",
                grade=result["score"],
                comment=(
                    "Автоматическая проверка: "
                    f"{result['correct_count']} из {result['total_count']} верных ответов."
                ),
            )
            SubmissionEvent.objects.create(
                submission=attempt, actor=student, action=SubmissionEvent.Action.SUBMITTED
            )
            SubmissionEvent.objects.create(
                submission=attempt,
                actor=None,
                action=SubmissionEvent.Action.REVIEWED,
                decision="checked",
                grade=result["score"],
                comment="Автопроверка теста",
            )
            AnswerDraft.objects.filter(student=student, assignment=assignment).delete()
            transaction.on_commit(
                lambda: logger.info(
                    "quiz.submitted id=%s version=%s score=%s/%s",
                    attempt.pk,
                    attempt.version,
                    result["score"],
                    result["max_score"],
                )
            )
        return attempt
    except Exception as exc:
        if isinstance(exc, IntegrityError) and (
            "unique_submission_attempt" in str(exc) or "lms_submission.student_id" in str(exc)
        ):
            raise ConflictError("Тест уже отправлен. Обновите страницу.") from exc
        raise


@transaction.atomic
def save_answer_draft(*, student, assignment_id, text):
    """Черновик ответа. Существует отдельно от попыток и не нарушает их неизменяемость."""
    if not student.is_active or get_user_role(student) != Profile.Role.STUDENT:
        raise PermissionDenied
    assignment = Assignment.objects.filter(pk=assignment_id).first()
    if not assignment or not assignment.is_visible or assignment.is_quiz:
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
    card = Flashcard.objects.select_related("deck__topic__block").filter(pk=card_id).first()
    if not card:
        raise PermissionDenied
    deck = card.deck
    if not deck_available(deck, student):
        raise PermissionDenied
    review, created = CardReview.objects.get_or_create(card=card, student=student)
    if not created:
        review = CardReview.objects.select_for_update().get(pk=review.pk)
    apply_sm2(review, rating)
    review.save()
    return review


def practice_queue(*, student, deck, limit=20):
    """Карточки к повтору: сначала просроченные, затем новые. Один запрос на состояние."""
    cards = list(deck.cards.all())
    reviews = {
        review.card_id: review
        for review in CardReview.objects.filter(student=student, card__deck=deck)
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


def deck_available(deck, student=None):
    """Доступен ли набор ученику: учебный — по активности курса, личный — по владельцу."""
    if not deck.is_active:
        return False
    if deck.is_personal:
        return student is not None and deck.owner_id == getattr(student, "pk", student)
    return bool(deck.topic and deck.topic.is_active and deck.topic.block.is_active)


def visible_decks(student):
    """Учебные наборы активного курса плюс личный словарь ученика (первым)."""
    return FlashcardDeck.objects.filter(
        Q(is_active=True, topic__is_active=True, topic__block__is_active=True)
        | Q(is_active=True, owner=student, topic__isnull=True)
    ).select_related("topic__block")


def get_or_create_personal_deck(student):
    """Личный словарь ученика: один набор без темы курса на владельца."""
    deck, _ = FlashcardDeck.objects.get_or_create(
        owner=student,
        topic=None,
        defaults={"title": "Мой словарь", "is_active": True},
    )
    return deck


def add_dictionary_word(*, student, term, translation, example="", source_assignment=None):
    """Слово в личный словарь. Дубликат слова внутри словаря обновляется, а не плодится."""
    term = (term or "").strip()[:300]
    translation = (translation or "").strip()[:300]
    if not term or not translation:
        raise ValidationError({"term": "Нужны слово и перевод."})
    deck = get_or_create_personal_deck(student)
    card = Flashcard.objects.filter(deck=deck, front__iexact=term).first()
    if card:
        card.back = translation
        if example:
            card.example = example[:500]
        card.save(update_fields=["back", "example"])
        return card, False
    card = Flashcard.objects.create(
        deck=deck,
        front=term,
        back=translation,
        example=(example or "")[:500],
        order=Flashcard.objects.filter(deck=deck).count(),
    )
    if source_assignment is not None:
        logger.info(
            "dictionary.add student=%s assignment=%s term=%r",
            student.pk,
            source_assignment,
            term,
        )
    return card, True


def deck_stats(student):
    """Сводка по наборам карточек: учебные активного курса + личный словарь ученика.

    Бюджет: 3 запроса (наборы, число карточек, состояния повторений).
    Личный словарь — первым, как в ProgressMe «My Words» на виду.
    """
    now = timezone.now()
    decks = list(
        visible_decks(student)
        .annotate(
            personal_order=models.Case(
                models.When(topic__isnull=True, then=1), default=0, output_field=models.IntegerField()
            )
        )
        .order_by("-personal_order", "topic__block__order", "topic__order", "order", "pk")
    )
    totals = {deck.pk: 0 for deck in decks}
    for deck_id in Flashcard.objects.filter(deck__in=decks).values_list("deck_id", flat=True):
        totals[deck_id] = totals.get(deck_id, 0) + 1
    reviewed = {deck.pk: 0 for deck in decks}
    due = {deck.pk: 0 for deck in decks}
    for row in CardReview.objects.filter(student=student, card__deck__in=decks).values_list(
        "card__deck_id", "due_at"
    ):
        deck_id, due_at = row
        reviewed[deck_id] = reviewed.get(deck_id, 0) + 1
        if due_at <= now:
            due[deck_id] = due.get(deck_id, 0) + 1
    for deck in decks:
        deck.card_total = totals.get(deck.pk, 0)
        deck.reviewed_count = reviewed.get(deck.pk, 0)
        deck.due_count = due.get(deck.pk, 0) + max(
            0, totals.get(deck.pk, 0) - reviewed.get(deck.pk, 0)
        )
    return decks
