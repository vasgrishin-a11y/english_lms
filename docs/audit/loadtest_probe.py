"""Серверный профиль экранов LMS на PostgreSQL: время, SQL, вес ответа.

Отличие от HTTP-нагрузки (``loadtest.py``): здесь нет сети и очереди на
gunicorn, поэтому видно чистую стоимость приложения — сколько запросов в БД,
сколько времени в SQL и Python, сколько байт HTML. Так можно отделить
«страница тяжёлая сама по себе» от «всем не хватает воркеров».

Данные берутся из стенда ``loadtest_seed.py`` (профиль ``class``/``big``),
сервер PostgreSQL должен быть запущен; строка подключения — в переменной
``DATABASE_URL`` (формат pgserver: ``postgresql://postgres:@/postgres?host=/путь``).

    DATABASE_URL=... LMS_LOADTEST_PROFILE=class \\
        .venv/bin/python docs/audit/loadtest_probe.py
"""

from __future__ import annotations

import gzip
import os
import statistics
import sys
import time
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

REPS = int(os.getenv("LMS_PROBE_REPS", "5"))
# Django test client представляется как testserver; профиль — только локальный стенд.
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "testserver"]
PASSWORD = "LoadTest-pass-2026"


def login_client(username):
    client = Client()
    response = client.post(
        "/accounts/login/", {"username": username, "password": PASSWORD}, follow=True
    )
    if response.status_code != 200:
        raise SystemExit(f"Не удалось войти как {username}: {response.status_code}")
    return client


def measure(client, url, reps=REPS, method="GET", data=None):
    samples = []
    queries = []
    sizes = []
    gz = []
    for _ in range(reps):
        started = time.perf_counter()
        with CaptureQueriesContext(connection) as captured:
            if method == "GET":
                response = client.get(url)
            else:
                response = client.post(url, data or {})
        elapsed = (time.perf_counter() - started) * 1000
        samples.append(elapsed)
        queries.append(len(captured.captured_queries))
        sizes.append(len(response.content))
        if response.status_code == 200 and len(response.content) > 512:
            gz.append(len(gzip.compress(response.content)))
        else:
            gz.append(len(response.content))
    return {
        "url": url,
        "status": response.status_code,
        "p50_ms": round(statistics.median(samples), 1),
        "max_ms": round(max(samples), 1),
        "sql": int(statistics.median(queries)),
        "html_kb": round(statistics.median(sizes) / 1024, 1),
        "gzip_kb": round(statistics.median(gz) / 1024, 1),
    }


def sql_weight(client, url):
    with CaptureQueriesContext(connection) as captured:
        client.get(url)
    lengths = sorted((len(item["sql"]) for item in captured.captured_queries), reverse=True)
    return {
        "sql": len(captured.captured_queries),
        "sql_chars": sum(lengths),
        "longest": lengths[:3],
    }


def main() -> int:
    User = get_user_model()
    student = User.objects.get(username="load-student-001")

    teacher_client = login_client("load-test-teacher")
    student_client = login_client(student.username)

    # Берём задания, реально видимые ученику: иначе 404 и нулевые метрики.
    visible = m.Assignment.objects.visible(user=student).order_by("pk")
    assignment = visible.filter(assignment_type="text").first()
    quiz = visible.filter(assignment_type="quiz").first()
    pending = m.Submission.objects.filter(status="submitted").order_by("pk").first()
    detail_student = (
        User.objects.filter(username__startswith="load-student-").order_by("pk").first()
    )

    screens = [
        ("student / (главная)", student_client, "/"),
        ("student /assignments/ (карта курса)", student_client, "/assignments/"),
        ("student /assignments/<pk>/ (текст)", student_client, f"/assignments/{assignment.pk}/"),
        ("student /assignments/<pk>/ (тест)", student_client, f"/assignments/{quiz.pk}/"),
        ("student /my/", student_client, "/my/"),
        ("student /grades/", student_client, "/grades/"),
        ("student /upcoming/", student_client, "/upcoming/"),
        ("student /trainer/", student_client, "/trainer/"),
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
        ("teacher /teacher/groups/", teacher_client, "/teacher/groups/"),
        ("teacher /teacher/analytics/", teacher_client, "/teacher/analytics/"),
        ("teacher /teacher/library/", teacher_client, "/teacher/library/"),
        ("teacher /teacher/settings/", teacher_client, "/teacher/settings/"),
    ]

    print(f"\nПрофиль: {os.getenv('LMS_PROFILE_NAME', '?')}, повторов: {REPS}")
    print(
        f"{'экран':42} {'статус':>6} {'p50, мс':>8} {'max, мс':>8} "
        f"{'SQL':>5} {'HTML, КБ':>9} {'gzip, КБ':>9}"
    )
    for title, client, url in screens:
        try:
            row = measure(client, url)
        except Exception as exc:  # noqa: BLE001
            print(f"{title:42} ОШИБКА: {exc}")
            continue
        print(
            f"{title[:42]:42} {row['status']:6d} {row['p50_ms']:8.1f} {row['max_ms']:8.1f} "
            f"{row['sql']:5d} {row['html_kb']:9.1f} {row['gzip_kb']:9.1f}"
        )

    print("\nN+1: SQL на '/my/' до/после добавления голосовых комментариев")
    # Убрать возможные пустые аудио-записи прошлых прогонов: шаблон зовёт
    # reverse('media_preview') и падает на пустом имени файла.
    m.FeedbackAudioComment.objects.filter(audio="", feedback__submission__student=student).delete()
    base_sql = measure(student_client, "/my/", reps=3)["sql"]
    comments = list(
        m.Feedback.objects.filter(submission__student=student)
        .exclude(comment="")
        .order_by("-pk")[:8]
    )
    for index, item in enumerate(comments):
        for order in range(2):
            m.FeedbackAudioComment.objects.get_or_create(
                feedback=item,
                order=order,
                defaults={"audio": f"loadtest/voice-{item.pk}-{order}.wav"},
            )
    after_sql = measure(student_client, "/my/", reps=3)["sql"]
    print(
        f"  проверок с комментариями: {len(comments)} (по 2 файла), "
        f"SQL было {base_sql}, стало {after_sql} (прирост {after_sql - base_sql})"
    )

    print("\nВес SQL (информативно):")
    for title, client, url in screens[:4] + screens[8:11]:
        try:
            info = sql_weight(client, url)
        except Exception:  # noqa: BLE001
            continue
        print(
            f"  {title[:40]:40} запросов {info['sql']:3d}, "
            f"{info['sql_chars']:7d} симв., топ: {info['longest']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
