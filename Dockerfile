# Multi-platform digest recorded by docker-library/repo-info on 2026-10-22.
FROM python:3.15.2-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_MEDIA_ROOT=/data
WORKDIR /app

COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt
COPY . .
RUN groupadd --gid 10001 lms \
    && useradd --uid 10001 --gid lms --no-create-home lms \
    && mkdir -p /app/var/media /data \
    && chown -R lms:lms /app/var /data \
    && sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh \
    && DJANGO_DEBUG=True DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" python manage.py collectstatic --noinput

# /data — монтирование постоянного хранилища Amvera (run.persistenceMount,
# значение по умолчанию платформы); каталог создан выше под UID 10001, чтобы
# контейнер не падал на mkdir при любом порядке шагов платформы.
# /app/var/media остаётся для compose (named volume) и как fallback-чтение
# старых загрузок (LMS_MEDIA_FALLBACK_ROOTS).
VOLUME ["/data", "/app/var/media"]
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health/ready/', timeout=3)"
CMD ["./docker-entrypoint.sh"]
