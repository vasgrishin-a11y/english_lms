#!/bin/sh
set -e

if [ -n "$DJANGO_MEDIA_ROOT" ]; then
  mkdir -p "$DJANGO_MEDIA_ROOT"
fi

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ -n "$DJANGO_SUPERUSER_USERNAME" ]; then
  python manage.py createsuperuser --noinput || echo "Superuser already exists or could not be created."
fi

exec gunicorn core.wsgi:application \
  --bind 0.0.0.0:${PORT:-8000} \
  --workers ${WEB_CONCURRENCY:-3} \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -