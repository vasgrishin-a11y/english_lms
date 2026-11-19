"""Профиль тяжёлых экранов: N+1, повторяющиеся запросы, cProfile.

Запуск на наполненном стенде (PostgreSQL из ``loadtest_run.py --seed-only``):

    DATABASE_URL=... LMS_PROFILE_NAME=class \\
        .venv/bin/python docs/audit/sql_profile.py
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from lms import models as m  # noqa: E402

if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]

PASSWORD = "LoadTest-pass-2026"
LITERAL = re.compile(r"'[^']*'|\b\d+\b")


#: cost of PBKDF2 (1 000 000 итераций в Django 5.2) — десятки процентов CPU
#: процесса при массовом входе; здесь логинимся force_login, чтобы профиль
#: показывал стоимость экранов, а не хеширования пароля.
REAL_LOGIN = os.getenv("LMS_REAL_LOGIN", "0") == "1"


def login_client(username):
    client = Client()
    if REAL_LOGIN:
        client.post("/accounts/login/", {"username": username, "password": PASSWORD})
    else:
        client.force_login(get_user_model().objects.get(username=username))
    return client


def fingerprint(sql):
    return LITERAL.sub("?", sql)[:160]


def analyse(client, url):
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    shapes = Counter(fingerprint(item["sql"]) for item in captured.captured_queries)
    duplicated = [(count, shape) for shape, count in shapes.items() if count > 1]
    duplicated.sort(reverse=True)
    return response, duplicated, len(captured.captured_queries)


def profile_view(client, url, label):
    buffer = io.StringIO()
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(3):
        client.get(url)
    profiler.disable()
    stats = pstats.Stats(profiler, stream=buffer).sort_stats("tottime")
    stats.print_stats(18)
    print(f"\n--- cProfile {label} (3 запроса) ---")
    lines = buffer.getvalue().splitlines()
    for line in lines[4:26]:
        print(line)


def main() -> int:
    User = get_user_model()
    student = User.objects.get(username="load-student-001")
    student_client = login_client(student.username)
    teacher_client = login_client("load-test-teacher")
    pending = m.Submission.objects.filter(status="submitted").order_by("pk").first()
    visible = m.Assignment.objects.visible(user=student).order_by("pk")
    assignment = visible.first()
    detail_student = (
        User.objects.filter(username__startswith="load-student-").order_by("pk").first()
    )
    screens = [
        ("student /assignments/", student_client, "/assignments/"),
        ("student /assignments/<pk>/", student_client, f"/assignments/{assignment.pk}/"),
        ("student /my/", student_client, "/my/"),
        ("teacher /teacher/", teacher_client, "/teacher/"),
        (
            "teacher /teacher/students/<pk>/",
            teacher_client,
            f"/teacher/students/{detail_student.pk}/",
        ),
        ("teacher /teacher/curriculum/", teacher_client, "/teacher/curriculum/"),
        ("teacher /teacher/analytics/", teacher_client, "/teacher/analytics/"),
        ("teacher /teacher/review/<pk>/", teacher_client, f"/teacher/review/{pending.pk}/"),
    ]
    print(f"\nПрофиль {os.getenv('LMS_PROFILE_NAME', '?')}: повторяющиеся SQL-запросы")
    for title, client, url in screens:
        response, duplicated, total = analyse(client, url)
        print(f"\n{title} — {response.status_code}, {total} запросов, повторов: {len(duplicated)}")
        for count, shape in duplicated[:4]:
            print(f"  x{count:3d}  {shape}")
    for title, client, url in screens[:5] + screens[6:]:
        profile_view(client, url, title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
