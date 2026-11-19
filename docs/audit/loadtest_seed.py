"""Синтетический стенд для нагрузочного теста English LMS.

Наполняет БД (PostgreSQL или SQLite) курсом, группами, учениками, сдачами и
тестами. Используется нагрузочным прогоном (``loadtest.py``) и не читает
реальные данные. Запуск — из клона репозитория с ``DJANGO_SETTINGS_MODULE``.

Профили (масштаб задаётся переменными окружения):

* ``small``  — smoke: 1 класс, 1 глава, 2 темы × 5 заданий, 10 учеников;
* ``class``  — обычный класс: 40 учеников, 4 группы, 8 тем × 8 заданий;
* ``big``    — крупная школа: 150 учеников, 12 групп, 12 тем × 12 заданий.

Ученики получают группу с вероятностью 60–80 %, чтобы проверить и «общие»,
и назначенные задания. Каждая сдача — отдельная версия (модель immutable).
"""

from __future__ import annotations

import os
import random
import sys
import time

import django

PROFILES = {
    "small": {
        "blocks": 1,
        "chapters": 1,
        "topics": 2,
        "assignments": 5,
        "students": 10,
        "groups": 2,
        "groups_per_student": 1,
    },
    "class": {
        "blocks": 2,
        "chapters": 2,
        "topics": 4,
        "assignments": 8,
        "students": 40,
        "groups": 4,
        "groups_per_student": 1,
    },
    "big": {
        "blocks": 3,
        "chapters": 2,
        "topics": 6,
        "assignments": 8,
        "students": 150,
        "groups": 12,
        "groups_per_student": 2,
    },
}

PASSWORD = "LoadTest-pass-2026"


def main(profile_name: str = None) -> None:
    profile_name = profile_name or os.getenv("LMS_LOADTEST_PROFILE", "class")
    if profile_name not in PROFILES:
        raise SystemExit(f"Неизвестный профиль {profile_name!r}; есть {sorted(PROFILES)}")
    limits = PROFILES[profile_name]
    random.seed(20261119)

    django.setup()

    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test.utils import setup_test_environment

    from lms import models as m

    setup_test_environment()
    started = time.monotonic()
    call_command("migrate", "--noinput", verbosity=0)

    User = get_user_model()
    if User.objects.filter(username="load-test-teacher").exists():
        print("Данные нагрузочного стенда уже есть — пропускаю наполнение.")
        count_summary()
        return

    teacher = User.objects.create_user(
        "load-test-teacher", password=PASSWORD, first_name="Load", last_name="Teacher"
    )
    teacher.profile.role = m.Profile.Role.TEACHER
    teacher.profile.save()

    groups = []
    for index in range(limits["groups"]):
        group = m.Group.objects.create(
            name=f"Load Group {index + 1:02d}",
            slug=f"load-group-{index + 1:02d}",
            teacher=teacher,
            cefr_level="B1",
        )
        groups.append(group)

    students = []
    for index in range(limits["students"]):
        student = User.objects.create_user(
            f"load-student-{index + 1:03d}",
            password=PASSWORD,
            first_name=f"Student{index + 1:03d}",
            last_name="Load",
        )
        students.append(student)

    membership = []
    for student in students:
        primary = groups[students.index(student) % len(groups)]
        membership.append((primary, student))
        for _ in range(limits["groups_per_student"] - 1):
            other = random.choice(groups)
            if other != primary:
                membership.append((other, student))
    for group in groups:
        group.students.set([student for chosen, student in membership if chosen == group])

    # 30 % учеников не состоит в группах: видят только «общие» задания.
    for student in students[-max(1, len(students) // 5) :]:
        student.student_groups.set([])

    assignments = []
    for block_index in range(limits["blocks"]):
        block = m.Block.objects.create(
            name=f"Load Class {block_index + 1}", slug=f"load-block-{block_index + 1}"
        )
        if block_index % 2:
            block.groups.add(groups[0])
        for chapter_index in range(limits["chapters"]):
            chapter = m.Chapter.objects.create(
                block=block,
                title=f"Chapter {chapter_index + 1}",
                slug=f"chapter-{chapter_index + 1}",
                order=chapter_index,
            )
            if chapter_index == 0:
                chapter.groups.add(groups[-1])
            for topic_index in range(limits["topics"]):
                topic = m.Topic.objects.create(
                    block=block,
                    chapter=chapter,
                    title=f"Topic {block_index}.{chapter_index}.{topic_index}",
                    slug=f"topic-{block_index}-{chapter_index}-{topic_index}",
                    order=topic_index,
                )
                for task_index in range(limits["assignments"]):
                    kind = [m.Assignment.Type.TEXT, m.Assignment.Type.QUIZ][task_index % 2]
                    assignment = m.Assignment.objects.create(
                        topic=topic,
                        title=f"Task {block_index}.{chapter_index}.{topic_index}.{task_index}",
                        description=("Write a short essay about your weekend. " * 8).strip(),
                        assignment_type=kind,
                        max_points=100,
                        order=task_index,
                    )
                    assignments.append(assignment)

    # Тестовые задания: 5 вопросов с вариантами ответа.
    quiz_assignments = [item for item in assignments if item.assignment_type == "quiz"]
    for index, assignment in enumerate(quiz_assignments):
        m.Assignment.objects.filter(pk=assignment.pk).update(assignment_type="quiz")
        for q_index in range(5):
            question = m.Question.objects.create(
                assignment=assignment,
                kind=m.Question.Kind.MCQ if q_index % 2 == 0 else m.Question.Kind.GAP,
                text=f"Question {q_index + 1}: choose the right answer.",
                points=1,
                order=q_index,
            )
            for c_index in range(4):
                m.Choice.objects.create(
                    question=question,
                    text=f"Option {c_index + 1}",
                    is_correct=(c_index == 0),
                    order=c_index,
                )
    skills = [
        m.Skill.objects.get_or_create(
            slug=f"load-skill-{index}", defaults={"name": f"Load Skill {index}"}
        )[0]
        for index in range(2)
    ]
    for assignment in assignments:
        if assignment.assignment_type != "quiz":
            assignment.skills.set(skills)

    # Сдачи: 70 % заданий у каждого ученика, четверть — уже проверена.
    submissions = []
    for student in students:
        for assignment in assignments:
            if random.random() > 0.7:
                continue
            submissions.append(
                m.Submission(
                    student=student,
                    assignment=assignment,
                    version=1,
                    text_answer=("Some reasonably long student answer sentence. " * 8).strip(),
                    status=m.Submission.Status.SUBMITTED,
                    max_points_snapshot=100,
                )
            )
    m.Submission.objects.bulk_create(submissions, batch_size=500)

    graded = [item for item in submissions if (item.student_id + item.assignment_id) % 4 == 0]
    m.Submission.objects.filter(pk__in=[item.pk for item in graded]).update(
        status=m.Submission.Status.CHECKED
    )
    m.Feedback.objects.bulk_create(
        [
            m.Feedback(
                submission=item,
                teacher=teacher,
                decision="checked",
                grade=random.randint(60, 100),
                comment="Хорошая работа. " * 4,
            )
            for item in graded
        ],
        batch_size=500,
    )

    # Черновики и карточки: небольшой хвост реалистичных данных.
    m.AnswerDraft.objects.bulk_create(
        [
            m.AnswerDraft(
                student=student,
                assignment=assignments[index % len(assignments)],
                text="draft " * 20,
            )
            for index, student in enumerate(students)
            if index % 3 == 0
        ],
        ignore_conflicts=True,
    )

    print(f"Стенд «{profile_name}» готов за {time.monotonic() - started:.1f} c.")
    count_summary()


def count_summary() -> None:
    from django.contrib.auth import get_user_model

    from lms import models as m

    User = get_user_model()
    teacher = User.objects.filter(username="load-test-teacher").first()
    print(
        "  студентов: {students}, групп: {groups}, классов: {blocks}, "
        "заданий: {assignments}, сдач: {submissions}, проверок: {feedback}".format(
            students=len(
                User.objects.filter(profile__role=m.Profile.Role.STUDENT).filter(
                    username__startswith="load-student-"
                )
            ),
            groups=m.Group.objects.filter(slug__startswith="load-group-").count(),
            blocks=m.Block.objects.filter(slug__startswith="load-block-").count(),
            assignments=m.Assignment.objects.filter(
                topic__block__slug__startswith="load-block-"
            ).count(),
            submissions=m.Submission.objects.filter(
                student__username__startswith="load-student-"
            ).count(),
            feedback=m.Feedback.objects.filter(
                submission__student__username__startswith="load-student-"
            ).count(),
        )
    )
    # Демонстрационные id: нагрузчику нужны конкретные страницы.
    print("  teacher_id:", teacher.pk if teacher else None)
    if teacher:
        pending = (
            m.Submission.objects.filter(status=m.Submission.Status.SUBMITTED)
            .order_by("pk")
            .values_list("pk", flat=True)[:5]
        )
        print("  pending_submission_ids:", list(pending))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    main(sys.argv[1] if len(sys.argv) > 1 else None)
