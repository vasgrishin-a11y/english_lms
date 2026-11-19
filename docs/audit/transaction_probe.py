"""Сравнение A/B: сколько раз каждая страница открывает read-only транзакцию.

Django под PostgreSQL ставит ``BEGIN ... SET TRANSACTION ISOLATION LEVEL READ
COMMITTED`` на каждый неатомарный view (ATOMIC_REQUESTS=False), а внутри
``transaction.atomic()`` — ещё один ``SAVEPOINT``. Это дешёвые, но сетевые
round-trip'ы к БД: они масштабируются числом запросов, а не их полезностью.

Скрипт печатает число BEGIN/SAVEPOINT по экранам. A/B — прогон на двух
ревизиях (или до/после патча) с одинаковым профилем данных.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from lms import models as m  # noqa: E402

if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]

PASSWORD = "LoadTest-pass-2026"
MARKERS = ("BEGIN", "SAVEPOINT", "COMMIT", "RELEASE SAVEPOINT")


def login_client(username):
    client = Client()
    client.post("/accounts/login/", {"username": username, "password": PASSWORD})
    return client


def count(client, url):
    with CaptureQueriesContext(connection_ref()) as captured:
        response = client.get(url)
    text = [item["sql"] for item in captured.captured_queries]
    stats = {marker: 0 for marker in MARKERS}
    for sql in text:
        for marker in MARKERS:
            if sql.strip().upper().startswith(marker):
                stats[marker] += 1
    return response.status_code, len(text), stats


def connection_ref():
    from django.db import connection

    return connection


def main() -> int:
    User = get_user_model()
    student = User.objects.get(username="load-student-001")
    pending = m.Submission.objects.filter(status="submitted").order_by("pk").first()
    student_client = login_client(student.username)
    teacher_client = login_client("load-test-teacher")
    visible = m.Assignment.objects.visible(user=student).order_by("pk")
    assignment = visible.first()
    detail_student = (
        User.objects.filter(username__startswith="load-student-").order_by("pk").first()
    )
    screens = [
        ("student /", student_client, "/"),
        ("student /assignments/", student_client, "/assignments/"),
        ("student /assignments/<pk>/", student_client, f"/assignments/{assignment.pk}/"),
        ("student /my/", student_client, "/my/"),
        ("teacher /teacher/", teacher_client, "/teacher/"),
        ("teacher /teacher/review/", teacher_client, "/teacher/review/"),
        ("teacher /teacher/review/<pk>/", teacher_client, f"/teacher/review/{pending.pk}/"),
        ("teacher /teacher/students/", teacher_client, "/teacher/students/"),
        (
            "teacher /teacher/students/<pk>/",
            teacher_client,
            f"/teacher/students/{detail_student.pk}/",
        ),
        ("teacher /teacher/curriculum/", teacher_client, "/teacher/curriculum/"),
        ("teacher /teacher/analytics/", teacher_client, "/teacher/analytics/"),
    ]
    print(f"\n{'экран':34} {'статус':>6} {'всего':>5} " + " ".join(f"{m[:7]:>7}" for m in MARKERS))
    totals = {marker: 0 for marker in MARKERS}
    for title, client, url in screens:
        status, total, stats = count(client, url)
        for marker in MARKERS:
            totals[marker] += stats[marker]
        print(
            f"{title:34} {status:6d} {total:5d} "
            + " ".join(f"{stats[marker]:7d}" for marker in MARKERS)
        )
    print(
        f"{'ИТОГО (11 экранов)':34} {'':6} {'':5} " + " ".join(f"{totals[x]:7d}" for x in MARKERS)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
