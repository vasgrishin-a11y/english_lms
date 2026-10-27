"""Стенд для замеров производительности всех вкладок LMS.

Синтетический курс строится в памяти (SQLite), реальные данные не читаются.
Скрипт меряет серверную часть: wall time, число и «вес» SQL, размер HTML,
gzip-размер и число DOM-узлов — по каждой вкладке преподавателя и ученика.

Запуск из корня репозитория (нужен venv с requirements.txt):

    python docs/audit/perf_probe.py                 # полный прогон
    python docs/audit/perf_probe.py --reps 7        # больше повторов
    python docs/audit/perf_probe.py --profile       # + cProfile по горячим путям
    python docs/audit/perf_probe.py --scale 8x3x5x6 # курс побольше

Результаты пишутся в docs/audit/perf_results.json (если каталог доступен на
запись) и печатаются таблицей в stdout.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import secrets
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if not (REPO / "manage.py").is_file():
    raise SystemExit("Запускайте скрипт из клона репозитория english_lms.")
sys.path.insert(0, str(REPO))

os.environ["DJANGO_SETTINGS_MODULE"] = "core.test_settings"
os.environ.setdefault("DJANGO_SECRET_KEY", secrets.token_urlsafe(64))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.runner import DiscoverRunner  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import NoReverseMatch, reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

from lms.models import (  # noqa: E402
    Assignment,
    Block,
    CardReview,
    Chapter,
    Choice,
    Feedback,
    Flashcard,
    Group,
    Profile,
    Question,
    QuestionResponse,
    Skill,
    Submission,
    Topic,
)

TAG_RE = re.compile(r"<[a-zA-Z]")
DOM_NODES_RE = re.compile(r"<[a-zA-Z][^>]*>")


# ──────────────────────────────────────────────────────────────────────────
# Синтетический курс
# ──────────────────────────────────────────────────────────────────────────
def build_course(blocks: int, chapters: int, topics: int, assignments: int, students: int):
    User = get_user_model()
    teacher = User.objects.create_user("perf-teacher")
    teacher.profile.role = Profile.Role.TEACHER
    teacher.profile.save()

    group = Group.objects.create(name="Perf группа", slug="perf-group")
    pupil = None
    for index in range(students):
        pupil = User.objects.create_user(f"perf-student-{index}")
        group.students.add(pupil)

    skills = [
        Skill.objects.create(name=name, slug=slug, kind=kind, order=order)
        for order, (slug, name, kind) in enumerate(
            [
                ("grammar", "Grammar", "grammar"),
                ("vocabulary", "Vocabulary", "vocabulary"),
                ("reading", "Reading", "reading"),
                ("listening", "listening-audio", "listening"),
                ("speaking", "Speaking", "speaking"),
                ("writing", "Writing", "writing"),
            ],
            start=1,
        )
    ]

    text = "Lorem ipsum dolor sit amet " * 4
    now = timezone.now()
    topics_by_block = []
    first_topic = None
    assignments_created = []
    questions_created = []
    for block_no in range(1, blocks + 1):
        block = Block.objects.create(
            name=f"Класс {block_no}",
            slug=f"block-{block_no}",
            order=block_no,
            cefr_level="B1",
            description=text,
        )
        block.groups.add(group)
        for chapter_no in range(1, chapters + 1):
            chapter = Chapter.objects.create(
                block=block,
                title=f"Глава {block_no}.{chapter_no}",
                slug=f"chapter-{block_no}-{chapter_no}",
                order=chapter_no,
                description=text,
            )
            for topic_no in range(1, topics + 1):
                topic = Topic.objects.create(
                    block=block,
                    chapter=chapter,
                    title=f"Тема {block_no}.{chapter_no}.{topic_no}",
                    slug=f"topic-{block_no}-{chapter_no}-{topic_no}",
                    order=topic_no,
                    description=text,
                )
                if first_topic is None:
                    first_topic = topic
                topics_by_block.append(topic)
                for task_no in range(1, assignments + 1):
                    kind = (
                        Assignment.Type.QUIZ
                        if task_no % 3
                        else Assignment.Type.TEXT
                        if task_no % 4
                        else Assignment.Type.FLASHCARDS
                    )
                    assignment = Assignment.objects.create(
                        topic=topic,
                        title=f"Задание {block_no}.{chapter_no}.{topic_no}.{task_no}",
                        description=text,
                        assignment_type=kind,
                        status=Assignment.Publication.PUBLISHED,
                        max_points=100,
                        order=task_no,
                        deadline=now + timezone.timedelta(days=3),
                    )
                    assignment.skills.add(skills[task_no % len(skills)])
                    assignments_created.append(assignment)
                    if kind is Assignment.Type.QUIZ:
                        for q_no in range(1, 5):
                            question = Question.objects.create(
                                assignment=assignment,
                                kind="mcq",
                                text=f"Вопрос {q_no}? " + text[:120],
                                explanation=text[:200],
                                points=5,
                                order=q_no,
                            )
                            Choice.objects.bulk_create(
                                [
                                    Choice(
                                        question=question,
                                        text=f"Вариант {letter} " + text[:60],
                                        is_correct=index == 0,
                                        order=index,
                                    )
                                    for index, letter in enumerate("ABCD")
                                ]
                            )
                            questions_created.append(question)
                    if kind is Assignment.Type.FLASHCARDS:
                        Flashcard.objects.bulk_create(
                            [
                                Flashcard(
                                    assignment=assignment,
                                    front=f"Слово {i}",
                                    back=f"Перевод {i} " + text[:40],
                                    example=text[:80],
                                    order=i,
                                )
                                for i in range(12)
                            ]
                        )

    # Сдачи: каждый ученик сдаёт часть заданий, каждое третье — с проверкой.
    submissions = []
    feedbacks = []
    responses = []
    step = max(1, len(assignments_created) // 8)
    for pupil_index, student in enumerate(
        User.objects.filter(profile__role=Profile.Role.STUDENT).order_by("pk")
    ):
        for position, assignment in enumerate(assignments_created):
            if (pupil_index + position) % step:
                continue
            submission = Submission(
                student=student,
                assignment=assignment,
                status=Submission.Status.CHECKED
                if position % 3 == 0
                else Submission.Status.SUBMITTED,
                version=1 + (pupil_index % 2),
                text_answer=text[:400],
                max_points_snapshot=assignment.max_points,
                deadline_snapshot=assignment.deadline,
                submitted_at=now - timezone.timedelta(days=1),
            )
            submissions.append(submission)
    Submission.objects.bulk_create(submissions)

    for submission in Submission.objects.select_related("assignment"):
        if submission.status == Submission.Status.CHECKED:
            feedbacks.append(
                Feedback(
                    submission=submission,
                    teacher=teacher,
                    decision="checked",
                    grade=85,
                    comment=text[:300],
                )
            )
        if submission.assignment.is_quiz:
            for question in submission.assignment.questions.all():
                responses.append(
                    QuestionResponse(
                        student=submission.student,
                        assignment=submission.assignment,
                        question=question,
                        submission=submission,
                        state="correct",
                        tries=[{"given": "1", "correct": True, "display": "A", "parts": {}}],
                        points=question.points,
                    )
                )
    Feedback.objects.bulk_create(feedbacks)
    QuestionResponse.objects.bulk_create(responses)

    # Карточки к повтору у первого ученика — блок «Карточек к повтору».
    if pupil is not None:
        CardReview.objects.bulk_create(
            [
                CardReview(
                    card_id=card_id,
                    student=pupil,
                    due_at=now - timezone.timedelta(days=1),
                )
                for card_id in range(1, min(20, Flashcard.objects.count() + 1))
            ]
        )

    return {
        "teacher": teacher,
        "students": list(User.objects.filter(profile__role=Profile.Role.STUDENT).order_by("pk")),
        "first_topic": first_topic,
        "assignments": assignments_created,
        "questions": questions_created,
        "counts": {
            "blocks": blocks,
            "chapters_per_block": chapters,
            "topics_per_chapter": topics,
            "assignments_per_topic": assignments,
            "assignments": len(assignments_created),
            "quiz_questions": len(questions_created),
            "students": students,
            "submissions": Submission.objects.count(),
            "question_responses": QuestionResponse.objects.count(),
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Замер
# ──────────────────────────────────────────────────────────────────────────
def measure(client, url, *, reps=5, headers=None, warmup=True):
    """Медиана wall time + число/вес SQL + размер HTML + gzip + DOM-узлы."""
    kwargs = dict(headers or {})
    samples, sql_counts, sql_bytes, sizes, gz_sizes, nodes = [], [], [], [], [], []
    for index in range(reps + (1 if warmup else 0)):
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        if index == 0 and warmup:
            continue
        content = response.content
        samples.append(elapsed)
        sql_counts.append(len(queries))
        sql_bytes.append(sum(len(q["sql"]) for q in queries))
        sizes.append(len(content))
        gz_sizes.append(len(gzip.compress(content, 6)))
        nodes.append(len(DOM_NODES_RE.findall(content.decode("utf-8", "ignore"))))
    return {
        "status": response.status_code,
        "ms": round(statistics.median(samples), 2),
        "ms_max": round(max(samples), 2),
        "sql": max(sql_counts),
        "sql_kbytes": round(statistics.median(sql_bytes) / 1024, 1),
        "html_kbytes": round(statistics.median(sizes) / 1024, 1),
        "gzip_kbytes": round(statistics.median(gz_sizes) / 1024, 1),
        "dom_nodes": int(statistics.median(nodes)),
    }


def cprofile_request(client, url, *, loops=5, headers=None):
    import cProfile
    import pstats
    from io import StringIO

    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(loops):
        client.get(url, **(headers or {}))
    profiler.disable()
    stream = StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("cumulative")
    stats.print_stats(28)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--scale", default="6x2x3x4", help="blocks x chapters x topics x tasks")
    parser.add_argument("--students", type=int, default=40)
    parser.add_argument("--json", default="docs/audit/perf_results.json")
    parser.add_argument(
        "--only",
        default="",
        help="список подстрок через запятую: мерить только совпавшие разделы (для A/B)",
    )
    args = parser.parse_args()
    blocks, chapters, topics, tasks = (int(part) for part in args.scale.lower().split("x"))

    runner = DiscoverRunner(verbosity=0, interactive=False)
    runner.setup_test_environment()
    runner.setup_databases()

    data = build_course(blocks, chapters, topics, tasks, args.students)
    teacher_client = Client()
    teacher_client.force_login(data["teacher"])
    # HTMX-клиент: заголовок HX-Request задаётся на клиенте, как это делает браузер.
    htmx_client = Client(headers={"HX-Request": "true"})
    htmx_client.force_login(data["teacher"])
    student = data["students"][0]
    student_client = Client()
    student_client.force_login(student)

    assignment = data["assignments"][0]
    quiz_assignment = next((item for item in data["assignments"] if item.is_quiz), assignment)
    flashcard_assignment = next(
        (item for item in data["assignments"] if item.is_flashcards), assignment
    )
    submission = (
        Submission.objects.filter(student=student, assignment=quiz_assignment).first()
        or Submission.objects.filter(student=student).first()
    )

    targets = [
        ("teacher", "/teacher/", "Консоль (home)"),
        ("teacher", reverse("teacher_curriculum"), "Классы: карта курса"),
        ("teacher", f"{reverse('teacher_curriculum')}?q=Тема", "Классы: поиск"),
        (
            "teacher",
            reverse("teacher_topic_board", args=[data["first_topic"].pk]),
            "Классы: уровень заданий (доска темы)",
        ),
        (
            "teacher",
            f"{reverse('teacher_topic_board', args=[data['first_topic'].pk])}?skill=grammar",
            "Классы: доска темы + фильтр навыка",
        ),
        (
            "teacher",
            reverse("teacher_assignment_form", args=[quiz_assignment.pk]),
            "Задание: полный редактор",
        ),
        (
            "teacher",
            f"{reverse('teacher_topic_board', args=[data['first_topic'].pk])}?edit={quiz_assignment.pk}",
            "Классы: доска темы с открытым редактором",
        ),
        (
            "teacher",
            reverse("teacher_assignment_new") + f"?topic={data['first_topic'].pk}",
            "Задание: создание",
        ),
        (
            "teacher",
            reverse("teacher_assignment_preview", args=[quiz_assignment.pk]),
            "Задание: предпросмотр глазами ученика",
        ),
        (
            "teacher",
            reverse("teacher_assignment_preview", args=[quiz_assignment.pk]) + "?answers=1",
            "Задание: предпросмотр с ответами",
        ),
        (
            "teacher",
            reverse("teacher_questions", args=[quiz_assignment.pk]),
            "Задание: редактор вопросов",
        ),
        (
            "teacher",
            reverse("teacher_item_results", args=[quiz_assignment.pk]),
            "Задание: результаты по пунктам",
        ),
        (
            "teacher",
            reverse("teacher_assignment_cards", args=[flashcard_assignment.pk]),
            "Задание: карточки",
        ),
        ("teacher", reverse("teacher_review_queue"), "Проверка: очередь"),
        (
            "teacher",
            reverse("teacher_submission_review", args=[submission.pk])
            if submission
            else "/teacher/review/1/",
            "Проверка: разбор сдачи",
        ),
        ("teacher", reverse("teacher_students"), "Ученики: список"),
        ("teacher", reverse("teacher_student_detail", args=[student.pk]), "Ученики: карточка"),
        (
            "htmx",
            reverse("teacher_student_assignment_progress", args=[student.pk, quiz_assignment.pk]),
            "HTMX: Показать ответ и разбор",
        ),
        (
            "htmx",
            reverse("teacher_student_assignment_preview", args=[student.pk, quiz_assignment.pk]),
            "HTMX: Показать содержимое как видит ученик",
        ),
        (
            "teacher",
            reverse("teacher_student_assignment_preview", args=[student.pk, quiz_assignment.pk]),
            "Страница: предпросмотр ученика (без HTMX)",
        ),
        ("teacher", reverse("teacher_groups"), "Группы"),
        ("teacher", reverse("teacher_analytics"), "Аналитика (журнал)"),
        ("teacher", reverse("teacher_settings"), "Настройки"),
        ("teacher", reverse("teacher_archive"), "Архив"),
        ("teacher", reverse("teacher_library"), "Библиотека"),
        ("teacher", reverse("teacher_ai"), "ИИ-помощник"),
        ("student", reverse("student_home"), "Ученик: Главная"),
        ("student", reverse("student_assignments"), "Ученик: Курс"),
        (
            "student",
            reverse("assignment_detail", args=[quiz_assignment.pk]),
            "Ученик: страница задания (тест)",
        ),
        (
            "student",
            reverse("assignment_detail", args=[flashcard_assignment.pk]),
            "Ученик: страница задания (карточки)",
        ),
        ("student", reverse("student_grades"), "Ученик: Оценки"),
        ("student", reverse("student_upcoming"), "Ученик: Сроки"),
        ("student", reverse("student_trainer"), "Ученик: Тренажёр"),
        ("student", reverse("student_dictionary"), "Ученик: Словарь"),
    ]

    try:
        targets.insert(
            6,
            (
                "htmx",
                reverse("teacher_assignment_quick_editor", args=[quiz_assignment.pk]),
                "HTMX: быстрый редактор задания",
            ),
        )
    except NoReverseMatch:
        pass

    results = {}
    print(f"\n=== Синтетический курс: {json.dumps(data['counts'], ensure_ascii=False)} ===\n")
    print(
        f"{'раздел':52} {'мс':>8} {'max':>8} {'SQL':>5} {'SQL КБ':>8} "
        f"{'HTML КБ':>9} {'gzip КБ':>9} {'DOM':>7} {'код':>4}"
    )
    needles = [part.strip().lower() for part in args.only.split(",") if part.strip()]
    selected = [t for t in targets if not needles or any(n in t[2].lower() for n in needles)]
    if args.only:
        # --only нужен для коротких A/B-прогонов: счётчик очереди и кэш доски
        # прогреваются первым запросом, поэтому греем его до замеров.
        for who, url, label in targets[:1]:
            measure(
                {"teacher": teacher_client, "student": student_client, "htmx": htmx_client}[who],
                url,
                reps=1,
            )
    for item in selected:
        who, url, label = item[0], item[1], item[2]
        client = {"teacher": teacher_client, "student": student_client, "htmx": htmx_client}[who]
        started = time.perf_counter()
        metrics = measure(client, url, reps=args.reps)
        metrics["seconds_total"] = round(time.perf_counter() - started, 2)
        results[label] = metrics
        print(
            f"{label:52} {metrics['ms']:8.2f} {metrics['ms_max']:8.2f} {metrics['sql']:5d} "
            f"{metrics['sql_kbytes']:8.1f} {metrics['html_kbytes']:9.1f} "
            f"{metrics['gzip_kbytes']:9.1f} {metrics['dom_nodes']:7d} {metrics['status']:4d}"
        )

    if args.profile:
        profiles = {}
        for url, label in [
            (reverse("teacher_curriculum"), "Классы: карта курса"),
            (
                reverse("teacher_topic_board", args=[data["first_topic"].pk]),
                "Классы: уровень заданий",
            ),
            (
                reverse(
                    "teacher_student_assignment_preview", args=[student.pk, quiz_assignment.pk]
                ),
                "HTMX: содержимое как видит ученик",
            ),
            (
                reverse(
                    "teacher_student_assignment_progress", args=[student.pk, quiz_assignment.pk]
                ),
                "HTMX: ответ и разбор",
            ),
            (
                reverse("teacher_student_detail", args=[student.pk]),
                "Ученики: карточка",
            ),
        ]:
            text = cprofile_request(teacher_client, url, loops=max(3, args.reps))
            profiles[label] = text
            print(f"\n### cProfile: {label}\n{text}")
        results["_profiles"] = profiles

    results["_course"] = data["counts"]
    out = REPO / args.json
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nЗаписано: {out}")
    except OSError as error:  # pragma: no cover - только для локального запуска
        print(f"\nJSON не записан: {error}")

    # Отдельно: сколько «мёртвого» DOM остаётся на странице после ленивой загрузки.
    detail = teacher_client.get(reverse("teacher_student_detail", args=[student.pk]))
    html = detail.content.decode("utf-8", "ignore")
    extras = {
        "assignment_progress_nodes": html.count("data-progress-details-toggle"),
        "assignment_preview_nodes": html.count("data-preview-toggle"),
        "details_tags": len(re.findall(r"<details", html)),
        "scripts": len(re.findall(r"<script", html)),
        "audio_tags": len(re.findall(r"<audio", html)),
        "forms": len(re.findall(r"<form", html)),
        "buttons": len(re.findall(r"<button", html)),
        "bytes_per_assignment": round(
            len(html) / max(1, len(re.findall(r"data-assignment-preview=", html))), 1
        ),
    }
    print("\nКарточка ученика — разметка на задание:", json.dumps(extras, ensure_ascii=False))
    results["_student_detail_markup"] = extras

    board = teacher_client.get(reverse("teacher_topic_board", args=[data["first_topic"].pk]))
    board_html = board.content.decode("utf-8", "ignore")
    board_extras = {
        "bytes": len(board.content),
        "dom_nodes": len(DOM_NODES_RE.findall(board_html)),
        "forms": len(re.findall(r"<form", board_html)),
        "inputs": len(re.findall(r"<input", board_html)),
        "selects": len(re.findall(r"<select", board_html)),
        "textareas": len(re.findall(r"<textarea", board_html)),
        "details": len(re.findall(r"<details", board_html)),
        "bytes_per_assignment": round(len(board.content) / max(1, tasks * topics), 1),
        "gzip_kbytes": round(len(gzip.compress(board.content, 6)) / 1024, 1),
    }
    print("\nДоска темы — разметка:", json.dumps(board_extras, ensure_ascii=False))
    results["_topic_board_markup"] = board_extras
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nИтог записан: {out}")
    print(f"Время прогона: {time.strftime('%H:%M:%S', time.gmtime(time.monotonic()))}")
    print("URL для справки: ", reverse("teacher_curriculum"))
    assert TAG_RE  # статический анализ не должен считать регулярку мусором


if __name__ == "__main__":
    main()
