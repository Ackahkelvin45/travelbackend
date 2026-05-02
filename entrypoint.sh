#!/bin/sh
# ============================================================
# entrypoint.sh — Container startup script
# ============================================================
# Runs database migrations and (in production) collects static
# files before handing off to the CMD process.
# ============================================================
set -e

echo "⏳  Running database migrations..."
python manage.py migrate --noinput

# Collect static files only in production (DJANGO_ENV=production)
if [ "$DJANGO_ENV" = "production" ]; then
    echo "📦  Collecting static files..."
    python manage.py collectstatic --noinput --clear
fi

echo "✅  Startup complete. Launching application..."
exec "$@"
