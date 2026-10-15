#!/bin/sh
set -eu
umask 077

# A private, persistent volume must be mounted here; see docs/DEPLOYMENT.md.
# Amvera mounts its Data volume at /data (run.persistenceMount); the Dockerfile
# pre-creates the directory for UID 10001, so this is a no-op there.
mkdir -p "${DJANGO_MEDIA_ROOT:?DJANGO_MEDIA_ROOT must be set}"
if [ ! -w "${DJANGO_MEDIA_ROOT}" ]; then
  echo "docker-entrypoint.sh: WARNING: ${DJANGO_MEDIA_ROOT} is not writable for uid $(id -u); private uploads will fail" >&2
fi

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
  --timeout "${WEB_TIMEOUT:-180}" \
  --access-logfile - \
  --error-logfile -
