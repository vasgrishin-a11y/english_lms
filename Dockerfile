# Multi-platform digest recorded by docker-library/repo-info on 2026-09-19.
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_MEDIA_ROOT=/app/var/media
WORKDIR /app

COPY requirements.txt ./
RUN pip install --require-hashes -r requirements.txt
COPY . .
RUN groupadd --gid 10001 lms \
    && useradd --uid 10001 --gid lms --no-create-home lms \
    && mkdir -p /app/var/media \
    && chown -R lms:lms /app/var \
    && sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh \
    && DJANGO_DEBUG=True DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" python manage.py collectstatic --noinput

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health/ready/', timeout=3)"
CMD ["./docker-entrypoint.sh"]
