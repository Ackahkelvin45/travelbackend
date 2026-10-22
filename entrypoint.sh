#!/bin/sh
# ============================================================
# entrypoint.sh — Container startup script
# ============================================================
# Runs database migrations and (in production) collects static
# files before handing off to the CMD process.
# ============================================================
set -e

echo "⏳  Running database migrations..."
# --skip-checks: on an empty database some admin checks query tables that
# migrate is about to create; every other command still runs the checks.
python manage.py migrate --noinput --skip-checks

# Collect static files only in production (DJANGO_ENV=production)
if [ "$DJANGO_ENV" = "production" ]; then
    echo "📦  Collecting static files..."
    python manage.py collectstatic --noinput --clear
fi

# Seed/refresh exchange rates so the FX fallback chain is warm from the very
# first request (best-effort — the in-app scheduler keeps it fresh afterwards).
echo "💱  Seeding exchange rates..."
python manage.py refresh_fx_rates || echo "⚠️  FX seed failed (will retry via in-app scheduler)"

echo "✅  Startup complete. Launching application..."
exec "$@"
