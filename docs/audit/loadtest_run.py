"""Полный нагрузочный прогон на одном узле: PostgreSQL + gunicorn + HTTP-нагрузка.

Шаги:

1. поднять временный PostgreSQL (пакет ``pgserver``, только для стенда);
2. ``migrate`` и синтетические данные (``loadtest_seed.py``);
3. запустить gunicorn ровно так, как в ``docker-entrypoint.sh``
   (``core.wsgi``, sync-воркеры, ``WEB_CONCURRENCY``);
4. дождаться ``/health/ready/`` и прогнать ``loadtest.py``;
5. остановить сервер и базу, сохранить JSON-отчёт и логи.

Всё приватное (БД, media, отчёты) — в ``var/loadtest`` (каталог в .gitignore).

Примеры:

    .venv/bin/python docs/audit/loadtest_run.py --profile class --vus 30,60
    .venv/bin/python docs/audit/loadtest_run.py --profile big --vus 30 --duration 60 \\
        --workers 3 --threads 1

Если окружение заранее задано (``LMS_BASE_URL``), скрипт не поднимает сервер,
а только прогоняет нагрузку по существующему стенду.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VAR = REPO / "var" / "loadtest"
REPORTS = REPO / "docs" / "audit" / "loadtest_reports"
PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.exists():  # fallback: запускать из активированного venv
    PYTHON = Path(sys.executable)

SECRET = "load-test-" + "x" * 60


class Postgres:
    """Временная PostgreSQL: живёт всё время прогона, чистится в finally."""

    def __init__(self):
        import pgserver

        self.server = pgserver.get_server(str(VAR / "pgdata"))

    def uri(self):
        return self.server.get_uri()

    def cleanup(self):
        self.server.cleanup()


class Gunicorn:
    """Отдельный процесс gunicorn; PID — для замера RSS при нагрузке."""

    def __init__(self, command, env, log_path):
        self.log_path = log_path
        self.process = subprocess.Popen(command, cwd=REPO, env=env)

    def stop(self):
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def rss_mb(self):
        """Суммарный RSS master + всех воркеров."""
        if self.process.poll() is not None:
            return 0.0
        total = 0
        try:
            pids = [self.process.pid]
            children = Path(f"/proc/{self.process.pid}/task/{self.process.pid}/children")
            if children.exists():
                pids += [int(item) for item in children.read_text().split()]
            for pid in pids:
                status = Path(f"/proc/{pid}/status")
                if status.exists():
                    for line in status.read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1])
        except OSError:
            return 0.0
        return round(total / 1024, 1)


def run(command, *, env, check=True):
    print("+", " ".join(str(item) for item in command), flush=True)
    return subprocess.run(command, cwd=REPO, env=env, check=check)


def wait_ready(base, timeout=90):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/health/ready/", timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(1)
    print(f"Сервер не поднялся: {last}", file=sys.stderr)
    return False


def start_postgres():
    data = VAR / "pgdata"
    data.mkdir(parents=True, exist_ok=True)
    postgres = Postgres()
    print("PostgreSQL:", postgres.uri())
    # pgserver отдаёт URI unix-сокета; psycopg2 понимает host как каталог.
    return postgres


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="class", choices=["small", "class", "big"])
    parser.add_argument("--vus", default="10,30,60", help="ступени одновременных пользователей")
    parser.add_argument("--duration", type=float, default=30.0, help="секунд на ступень")
    parser.add_argument("--workers", type=int, default=int(os.getenv("WEB_CONCURRENCY", "3")))
    parser.add_argument("--threads", type=int, default=int(os.getenv("WEB_THREADS", "1")))
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="не останавливать PostgreSQL после прогона (для loadtest_probe.py и тестов)",
    )
    parser.add_argument("--seed-only", action="store_true", help="только наполнить стенд и выйти")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="после наполнения снять серверный профиль экранов (loadtest_probe.py)",
    )
    parser.add_argument("--existing", default="", help="URL уже запущенного стенда")
    args = parser.parse_args()

    VAR.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    media = VAR / "media"
    media.mkdir(exist_ok=True)

    if args.existing:
        os.environ["LMS_BASE_URL"] = args.existing
        return run([str(PYTHON), "docs/audit/loadtest.py"]).returncode

    env = os.environ.copy()
    env.update(
        DJANGO_SETTINGS_MODULE="core.settings",
        DJANGO_DEBUG="False",
        DJANGO_SECRET_KEY=SECRET,
        DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost",
        DJANGO_MEDIA_ROOT=str(media),
        DJANGO_STATIC_ROOT=str(VAR / "static"),
        DJANGO_SECURE_SSL_REDIRECT="False",
        DJANGO_SESSION_COOKIE_SECURE="False",
        DJANGO_CSRF_COOKIE_SECURE="False",
        DJANGO_SECURE_HSTS_SECONDS="0",
        DJANGO_LOGIN_FAILURE_LIMIT="100000",
        # Без этого axes пишет каждую неудачную попытку и держит БД-локи;
        # для нагрузочного профиля важно не изменить поведение входа, а не
        # ослабить защиту: лимит остаётся высоким только на стенде.
        LMS_AI_ENABLED="False",
        WEB_CONCURRENCY=str(args.workers),
        WEB_THREADS=str(args.threads),
        LMS_LOADTEST_PROFILE=args.profile,
    )

    postgres = None
    gunicorn = None
    log_path = VAR / "gunicorn.log"
    try:
        if os.getenv("LMS_LOADTEST_DATABASE_URL"):
            env["DATABASE_URL"] = os.environ["LMS_LOADTEST_DATABASE_URL"]
            print("DB:", env["DATABASE_URL"])
        else:
            postgres = start_postgres()
            env["DATABASE_URL"] = postgres.uri()
        env["LMS_LOADTEST_DATABASE_URL"] = env["DATABASE_URL"]

        run([str(PYTHON), "manage.py", "collectstatic", "--noinput", "-v", "0"], env=env)
        if not args.skip_seed:
            run([str(PYTHON), "docs/audit/loadtest_seed.py", args.profile], env=env)
        if args.probe:
            probe_env = env.copy()
            probe_env["LMS_PROFILE_NAME"] = args.profile
            result = run([str(PYTHON), "docs/audit/loadtest_probe.py"], env=probe_env, check=False)
            run([str(PYTHON), "docs/audit/transaction_probe.py"], env=probe_env, check=False)
            if args.seed_only:
                return result.returncode
        if args.seed_only:
            print("DATABASE_URL=" + env["DATABASE_URL"])
            return 0

        print(f"gunicorn: workers={args.workers} threads={args.threads}")
        worker_class = "gthread" if args.threads > 1 else "sync"
        command = [
            str(REPO / ".venv" / "bin" / "gunicorn"),
            "core.wsgi:application",
            "--bind",
            "127.0.0.1:8000",
            "--workers",
            str(args.workers),
            "--threads",
            str(args.threads),
            "--worker-class",
            worker_class,
            "--timeout",
            "180",
            "--access-logfile",
            str(VAR / "access.log"),
            "--error-logfile",
            str(log_path),
            "--log-level",
            "warning",
        ]
        gunicorn = Gunicorn(command, env, log_path)
        base = "http://127.0.0.1:8000"
        if not wait_ready(base):
            print(log_path.read_text()[-4000:] if log_path.exists() else "", file=sys.stderr)
            return 3

        load_env = env.copy()
        load_env.update(
            LMS_BASE_URL=base,
            LMS_VUS=args.vus.split(",")[-1],
            LMS_DURATION=str(args.duration),
            LMS_STEADY=args.vus,
            LMS_RSS_PID=str(gunicorn.process.pid),
            LMS_REPORT=str(
                REPORTS
                / f"{time.strftime('%Y%m%d-%H%M')}-{args.profile}-w{args.workers}t{args.threads}.json"
            ),
        )
        result = run([str(PYTHON), "docs/audit/loadtest.py"], env=load_env, check=False)
        return result.returncode
    finally:
        if gunicorn is not None:
            gunicorn.stop()
        if postgres is not None and not args.keep_db:
            try:
                postgres.cleanup()
            except Exception as exc:  # noqa: BLE001
                print("cleanup:", exc, file=sys.stderr)
        elif postgres is not None:
            print("PostgreSQL оставлен запущенным:", postgres.uri())
        if log_path.exists():
            tail = log_path.read_text(errors="replace")[-3000:]
            print("\n--- gunicorn.log (tail) ---\n" + tail)


if __name__ == "__main__":
    sys.exit(main())
