"""Нагрузочный тест English LMS по HTTP (как production: gunicorn + PostgreSQL).

Сценарий одного виртуального пользователя близок к реальному уроку:

* ученик: главная → карта курса → карточка задания → сохранение черновика;
* преподаватель (20 % нагрузки): консоль → очередь → карточка проверки →
  открытие фрагмента «ответ и разбор».

Мерим: количество запросов/с, задержки p50/p95/p99 по классам страниц,
долю ошибок (5xx/4xx по вине сервера), а также поведение при ступенчатом росте
нагрузки до появления ошибок или роста p95 выше порога.

Запуск (нужен живой сервер — обычно ``docs/audit/loadtest_run.py``):

    LMS_BASE_URL=http://127.0.0.1:8000 LMS_VUS=20 LMS_DURATION=60 \\
        python docs/audit/loadtest.py

Переменные окружения:

    LMS_BASE_URL       адрес стенда                       (обязательно)
    LMS_STUDENT        логин ученика                      (по умолчанию load-student-001)
    LMS_TEACHER        логин преподавателя                (по умолчанию load-test-teacher)
    LMS_PASSWORD       пароль стенда                      (LoadTest-pass-2026)
    LMS_VUS            число одновременных пользователей  (10)
    LMS_DURATION       длительность одной ступени, сек     (30)
    LMS_STEADY         ступени для steady-state            (30,60,120)
    LMS_ITEM_ID        id тестового задания (для item check, необязательно)
    LMS_REPORT         путь для JSON-отчёта                (не пишется, если пусто)
    LMS_RSS_PID        PID gunicorn master: RSS и число воркеров в каждый пик
    LMS_SKIP_LOGIN     при повторных прогонах сессии можно переиспользовать? нет:
                       вход всегда выполняется заново (честная проверка login)
    LMS_PAUSE          пауза между ступенями, сек        (5)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

BASE = os.getenv("LMS_BASE_URL", "").rstrip("/")
STUDENT = os.getenv("LMS_STUDENT", "load-student-001")
TEACHER = os.getenv("LMS_TEACHER", "load-test-teacher")
PASSWORD = os.getenv("LMS_PASSWORD", "LoadTest-pass-2026")
VUS = int(os.getenv("LMS_VUS", "10"))
DURATION = float(os.getenv("LMS_DURATION", "30"))
STEADY = [int(item) for item in os.getenv("LMS_STEADY", "30,60,120").split(",") if item.strip()]
ITEM_ID = int(os.getenv("LMS_ITEM_ID", "0") or 0)
REPORT = os.getenv("LMS_REPORT", "")

# Задержка «человека» между кликами: без неё виртуальный пользователь даёт
# нереалистично высокий RPS и упирается в синтетику, а не в сервер.
THINK_RANGE = (0.6, 2.4)

STUDENT_PAGES = ["/", "/assignments/", "/my/", "/grades/"]
TEACHER_PAGES = ["/teacher/", "/teacher/review/", "/teacher/students/", "/teacher/groups/"]


def pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


class Sampler:
    def __init__(self):
        self.latency = defaultdict(list)
        self.errors = defaultdict(int)
        self.total = defaultdict(int)
        self.statuses = defaultdict(int)
        self.bytes = 0
        self.login_ok = 0
        self.login_fail = 0
        self.server = {}
        self.lock = asyncio.Lock()

    def record_sync(self, kind, elapsed_ms, status, size=0):
        self.total[kind] += 1
        self.statuses[status] += 1
        self.bytes += size
        if status >= 400:
            self.errors[kind] += 1
        self.latency[kind].append(elapsed_ms)

    async def record(self, kind, elapsed_ms, status, size=0):
        async with self.lock:
            self.record_sync(kind, elapsed_ms, status, size)

    def summary(self):
        rows = []
        for kind in sorted(self.latency):
            values = self.latency[kind]
            rows.append(
                {
                    "kind": kind,
                    "count": len(values),
                    "errors": self.errors[kind],
                    "p50": round(pct(values, 0.50), 1),
                    "p95": round(pct(values, 0.95), 1),
                    "p99": round(pct(values, 0.99), 1),
                    "max": round(max(values), 1),
                    "mean": round(statistics.fmean(values), 1),
                }
            )
        return rows


async def timed(sampler, kind, client, method, url, **kwargs):
    started = time.perf_counter()
    try:
        response = await client.request(method, BASE + url, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        await sampler.record(kind, elapsed, response.status_code, len(response.content))
        return response
    except Exception:  # noqa: BLE001 — сетевые ошибки тоже результат нагрузки
        elapsed = (time.perf_counter() - started) * 1000
        await sampler.record(kind, elapsed, 599)
        return None


async def one_session(sampler, role, index, until):
    """Один виртуальный пользователь: свой httpx-клиент и своя сессия.

    Сетевой ввод/вывод httpx асинхронный, поэтому ``await`` здесь не блокирует
    event loop. Пароли (расшифровка хеша) считаются в процессе сервера, а не тут.
    """
    headers = {"User-Agent": f"lms-loadtest/1.0 {role} vu={index}"}
    if role == "teacher":
        headers["HX-Request"] = "true"
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        login_page = await timed(sampler, "login page", client, "GET", "/accounts/login/")
        if login_page is None or login_page.status_code != 200:
            sampler.login_fail += 1
            return
        token = client.cookies.get("csrftoken", "")
        response = await timed(
            sampler,
            "login POST",
            client,
            "POST",
            "/accounts/login/",
            data={
                "username": STUDENT if role == "student" else TEACHER,
                "password": PASSWORD,
                "csrfmiddlewaretoken": token,
            },
            headers={"Referer": BASE + "/accounts/login/"},
        )
        if response is None or response.status_code != 302 or "sessionid" not in client.cookies:
            sampler.login_fail += 1
            return
        sampler.login_ok += 1
        pages = list(STUDENT_PAGES if role == "student" else TEACHER_PAGES)
        # Настоящие id из HTML: у ученика доступны только назначенные задания,
        # случайные id дают 404 и портят статистику ошибок.
        visible_assignments = []
        if role == "student":
            course_page = await timed(sampler, "GET /assignments/", client, "GET", "/assignments/")
            if course_page is not None and course_page.status_code == 200:
                visible_assignments = sorted(link_ids(course_page.text, "/assignments/"))
                if visible_assignments:
                    pages = STUDENT_PAGES + [
                        f"/assignments/{visible_assignments[len(visible_assignments) // 2]}/"
                    ]
        while time.monotonic() < until:
            page = random.choice(pages)
            await timed(sampler, f"GET {page}", client, "GET", page)
            await asyncio.sleep(random.uniform(*THINK_RANGE))
            if role == "student" and visible_assignments and random.random() < 0.45:
                assignment_id = random.choice(visible_assignments[:40])
                detail = await timed(
                    sampler,
                    "GET /assignments/<pk>/",
                    client,
                    "GET",
                    f"/assignments/{assignment_id}/",
                )
                if detail is not None and detail.status_code == 200 and random.random() < 0.5:
                    token = client.cookies.get("csrftoken", "")
                    await timed(
                        sampler,
                        "POST draft (HTMX)",
                        client,
                        "POST",
                        f"/assignments/{assignment_id}/draft/",
                        data={"text": "Load test draft answer " * 6, "csrfmiddlewaretoken": token},
                        headers={
                            "Referer": BASE + f"/assignments/{assignment_id}/",
                            "HX-Request": "true",
                        },
                    )
            elif role == "teacher" and random.random() < 0.6:
                listing = await timed(
                    sampler, "GET /teacher/review/", client, "GET", "/teacher/review/"
                )
                if listing is not None and listing.status_code == 200:
                    target = random.choice(sorted(review_ids(listing.text))[:10] or [0])
                    if target:
                        await timed(
                            sampler,
                            "GET /teacher/review/<pk>/",
                            client,
                            "GET",
                            f"/teacher/review/{target}/",
                        )


def link_ids(html_text, prefix):
    """Числовые id из ссылок вида ``prefix<id>/`` — без парсера, достаточно."""
    ids = set()
    marker = prefix
    position = html_text.find(marker)
    while position != -1:
        tail = html_text[position + len(marker) :]
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        if digits:
            ids.add(int(digits))
        position = html_text.find(marker, position + len(marker))
    return ids


def review_ids(html_text):
    return link_ids(html_text, "/teacher/review/")


def server_stats(pid):
    """RSS master+workers и число воркеров по /proc."""
    if not pid:
        return {}
    total = 0
    workers = 0
    try:
        pids = [pid]
        children = Path(f"/proc/{pid}/task/{pid}/children")
        if children.exists():
            extra = [int(item) for item in children.read_text().split()]
            pids += extra
            workers = len(extra)
        for item in pids:
            status = Path(f"/proc/{item}/status")
            if status.exists():
                for line in status.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
    except OSError:
        return {}
    return {"rss_mb": round(total / 1024, 1), "workers": workers}


async def steady_phase(vus, duration):
    sampler = Sampler()
    rss_pid = int(os.getenv("LMS_RSS_PID", "0") or 0)
    peak = {"rss_mb": 0.0, "workers": 0}
    samples = []

    async def watch():
        while time.monotonic() < until:
            stats = server_stats(rss_pid)
            if stats:
                samples.append(stats)
                peak["rss_mb"] = max(peak["rss_mb"], stats["rss_mb"])
                peak["workers"] = max(peak["workers"], stats["workers"])
            await asyncio.sleep(2)

    until = time.monotonic() + duration
    teacher_vus = max(1, vus // 5)
    tasks = [one_session(sampler, "student", index, until) for index in range(vus - teacher_vus)]
    tasks += [one_session(sampler, "teacher", index, until) for index in range(teacher_vus)]
    started = time.monotonic()
    await asyncio.gather(*tasks, *([watch()] if rss_pid else []), return_exceptions=True)
    elapsed = time.monotonic() - started
    rows = sampler.summary()
    total = sum(row["count"] for row in rows)
    errors = sum(row["errors"] for row in rows)
    for row in rows:
        row["rps"] = round(row["count"] / elapsed, 2)
    return {
        "vus": vus,
        "duration_s": round(elapsed, 1),
        "requests": total,
        "errors": errors,
        "error_rate": round(100 * errors / total, 2) if total else 0,
        "rps": round(total / elapsed, 2),
        "mb": round(sampler.bytes / (1024 * 1024), 1),
        "login_ok": sampler.login_ok,
        "login_fail": sampler.login_fail,
        "statuses": dict(sorted(sampler.statuses.items())),
        "server": peak,
        "rows": rows,
    }


def print_phase(result):
    print(
        f"\n=== {result['vus']} VU, {result['duration_s']} c: "
        f"{result['rps']} req/s, {result['requests']} запросов, "
        f"ошибок {result['error_rate']}%, логинов ok/fail "
        f"{result['login_ok']}/{result['login_fail']} ==="
    )
    server = result.get("server") or {}
    if server.get("rss_mb"):
        print(f"  RSS gunicorn: пик {server['rss_mb']} МБ, воркеров {server['workers']}")
    print(
        f"{'страница':38} {'n':>6} {'err':>4} {'rps':>7} "
        f"{'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}"
    )
    for row in result["rows"]:
        print(
            f"{row['kind'][:38]:38} {row['count']:6d} {row['errors']:4d} {row['rps']:7.2f} "
            f"{row['p50']:7.1f} {row['p95']:7.1f} {row['p99']:7.1f} {row['max']:7.1f}"
        )


def main() -> int:
    if not BASE:
        print("Задайте LMS_BASE_URL — адрес запущенного стенда.")
        return 2
    if not Path("manage.py").is_file():
        print("Запускайте из корня репозитория english_lms.")
        return 2
    print(f"Стенд: {BASE}, ученик: {STUDENT}, преподаватель: {TEACHER}")
    phases = []
    # Разминка: один виртуальный пользователь, чтобы не мерить импорт/шаблоны.
    warmup = asyncio.run(steady_phase(2, min(8.0, DURATION)))
    print_phase(warmup)
    pause = float(os.getenv("LMS_PAUSE", "5"))
    for index, vus in enumerate(STEADY):
        if index and pause:
            time.sleep(pause)
        phase = asyncio.run(steady_phase(vus, DURATION))
        print_phase(phase)
        print(f"  статусы HTTP: {phase['statuses']}")
        phases.append(phase)
    if REPORT:
        Path(REPORT).write_text(
            json.dumps(
                {"base": BASE, "warmup": warmup, "phases": phases}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        print(f"\nОтчёт: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
