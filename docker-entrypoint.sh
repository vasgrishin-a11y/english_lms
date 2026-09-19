#!/bin/sh
set -eu
umask 077

# A private, persistent volume must be mounted here; see docs/DEPLOYMENT.md.
mkdir -p "${DJANGO_MEDIA_ROOT:?DJANGO_MEDIA_ROOT must be set}"

if [ "${DJANGO_RUN_MIGRATIONS:-false}" = "true" ]; then
  python manage.py migrate --noinput
else
  # Production migrations belong to a single release job, not every replica.
  python manage.py migrate --check
fi

if [ "${DJANGO_CREATE_SUPERUSER:-false}" = "true" ]; then
  python manage.py bootstrap_admin
fi

exec gunicorn core.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-3}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
