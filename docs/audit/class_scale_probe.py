"""Стенд «большой класс»: кнопки разбора в карточке ученика и счётчик очереди.

Общий стенд (`perf_probe.py`) берёт 40 учеников по 2 группе — для проверки
доступа этого мало: цена `visibility_q()` растёт вместе с числом групп ученика,
а цена `queue_counts()` — с числом сдач. Здесь 150 учеников, 12 групп и ~4800
сдач, и меряются ровно те запросы, на которые жалуются пользователи:

* страница карточки ученика;
* HTMX «Показать ответ и разбор»;
* HTMX «Показать содержимое как увидит ученик»;
* консоль после них (прогрев/кинетика кэша счётчика).

Запуск из клона репозитория (нужен venv с requirements.txt):

    python docs/audit/class_scale_probe.py

Размер класса меняется переменными окружения, чтобы прогонять и A/B на двух
ревизиях (worktree до и после) в одной сессии:

    TOGGLE_GROUPS=12 TOGGLE_GROUPS_PER_STUDENT=6 TOGGLE_STUDENTS=150 \\
        python docs/audit/class_scale_probe.py

Важно: `max` здесь интереснее медианы. В версии «до» первый клик по фрагменту
пересчитывал счётчик очереди и писал строку сессии, поэтому отдельные клики
вместо 3–4 мс шли за 40+ мс — тот самый «иногда грузит очень долго».
"""

from __future__ import annotations

import os
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
from django.urls import reverse  # noqa: E402

from lms import models as m  # noqa: E402

BLOCKS = int(os.getenv("TOGGLE_BLOCKS", "2"))
TOPICS_PER_BLOCK = int(os.getenv("TOGGLE_TOPICS", "4"))
TASKS_PER_TOPIC = int(os.getenv("TOGGLE_TASKS", "6"))
STUDENTS = int(os.getenv("TOGGLE_STUDENTS", "150"))
GROUPS = int(os.getenv("TOGGLE_GROUPS", "12"))
GROUPS_PER_STUDENT = int(os.getenv("TOGGLE_GROUPS_PER_STUDENT", "6"))
REPS = int(os.getenv("TOGGLE_REPS", "7"))


def build():
    """Курс, сдачи и группы: всё через bulk_create, иначе фикстура дороже замеров."""
    User = get_user_model()
    teacher = User.objects.create_user("teacher")
    teacher.profile.role = m.Profile.Role.TEACHER
    teacher.profile.save()
    groups = [
        m.Group.objects.create(name=f"Группа {index}", slug=f"group-{index}")
        for index in range(GROUPS)
    ]
    students = []
    for index in range(STUDENTS):
        student = User.objects.create_user(f"stu-{index}", first_name=f"Ivan{index}")
        students.append(student)
        for offset in range(GROUPS_PER_STUDENT):
            groups[(index + offset * 3) % GROUPS].students.add(student)
    assignments = []
    for block_index in range(BLOCKS):
        block = m.Block.objects.create(name=f"Класс {block_index}", slug=f"block-{block_index}")
        for topic_index in range(TOPICS_PER_BLOCK):
            topic = m.Topic.objects.create(
                block=block,
                title=f"Тема {block_index}.{topic_index}",
                slug=f"t-{block_index}-{topic_index}",
            )
            for task_index in range(TASKS_PER_TOPIC):
                assignments.append(
                    m.Assignment.objects.create(
                        topic=topic,
                        title=f"Задание {block_index}.{topic_index}.{task_index}",
                        description="Напишите ответ.",
                        max_points=100,
                    )
                )
    submissions = [
        m.Submission(
            student=student,
            assignment=assignment,
            version=1,
            text_answer="Some reasonably long answer " * 6,
            max_points_snapshot=100,
            status=m.Submission.Status.SUBMITTED,
        )
        for student in students
        for assignment in assignments
        if (student.pk + assignment.pk) % 3
    ]
    m.Submission.objects.bulk_create(submissions)
    graded = [item for item in submissions if (item.student_id + item.assignment_id) % 4 == 0]
    for submission in graded:
        submission.status = m.Submission.Status.CHECKED
    m.Submission.objects.bulk_update(graded, ["status"])
    m.Feedback.objects.bulk_create(
        [
            m.Feedback(submission=submission, grade=80, comment="Good", teacher=teacher)
            for submission in graded
        ]
    )
    return teacher, students, assignments, len(submissions)


def measure(client, url, reps=REPS):
    """Медиана и разброс серверного времени, SQL и его «вес»."""
    times, counts, bytes_, sql_size = [], [], [], []
    for _ in range(reps):
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as captured:
            response = client.get(url)
        times.append((time.perf_counter() - started) * 1000)
        counts.append(len(captured.captured_queries))
        sql_size.append(sum(len(item["sql"]) for item in captured.captured_queries))
        bytes_.append(len(response.content))
        assert response.status_code == 200, (url, response.status_code)
    return {
        "ms": statistics.median(times),
        "ms_max": max(times),
        "sql": max(counts),
        "sql_kbytes": round(max(sql_size) / 1024, 1),
        "html_kbytes": round(max(bytes_) / 1024, 1),
    }


def main():
    runner = DiscoverRunner(verbosity=0, interactive=False)
    runner.setup_test_environment()
    config = runner.setup_databases()
    started = time.perf_counter()
    teacher, students, assignments, attempts = build()
    print(
        f"# фикстура: {len(assignments)} заданий, {STUDENTS} учеников, {GROUPS} групп "
        f"по {GROUPS_PER_STUDENT} на ученика, {attempts} сдач "
        f"(сборка {time.perf_counter() - started:.0f} с)"
    )
    student = students[7]
    target = next(
        item
        for item in assignments
        if m.Submission.objects.filter(student=student, assignment=item).exists()
    )
    htmx = Client(headers={"HX-Request": "true"})
    htmx.force_login(teacher)
    plain = Client()
    plain.force_login(teacher)
    progress = reverse("teacher_student_assignment_progress", args=[student.pk, target.pk])
    preview = reverse("teacher_student_assignment_preview", args=[student.pk, target.pk])
    card = reverse("teacher_student_detail", args=[student.pk])
    console = reverse("teacher_home")
    measure(plain, card)  # прогрев: кэш счётчика очереди и связи курса
    rows = [
        ("Карточка ученика (страница)", measure(plain, card)),
        ("HTMX: ответ и разбор", measure(htmx, progress)),
        ("HTMX: как видит ученик", measure(htmx, preview)),
        ("Консоль после фрагментов", measure(plain, console)),
    ]
    print()
    print("| экран | медиана мс | max мс | SQL | SQL КБ | HTML КБ |")
    print("|---|---:|---:|---:|---:|---:|")
    for label, values in rows:
        print(
            f"| {label} | {values['ms']:.1f} | {values['ms_max']:.1f} | {values['sql']} "
            f"| {values['sql_kbytes']} | {values['html_kbytes']} |"
        )
    runner.teardown_databases(config)


main()
