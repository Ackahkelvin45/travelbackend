"""
payments/autotasks.py
---------------------
Self-scheduling background tasks — no host crontab required.

A lightweight middleware checks (at most once per AUTOTASK_CHECK_SECONDS per
process, via a cache fast-path) whether any task is due, CLAIMS it under a
row lock (so multiple gunicorn workers never double-run it), then executes it
in a daemon thread so no customer request ever waits on it.

Tasks are visible and pausable in admin (Payments → Scheduled tasks).
Running the management commands manually or via a real cron remains possible
and safe — every task is idempotent.
"""

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300  # cache fast-path: look at the DB every 5 min max


def _run_refresh_fx_rates():
    from django.core.management import call_command
    call_command("refresh_fx_rates")
    return "ok"


def _run_send_payment_reminders():
    from io import StringIO

    from django.core.management import call_command
    out = StringIO()
    call_command("send_payment_reminders", stdout=out)
    return out.getvalue().strip()


# name → (interval, runner). Intervals: FX hourly (matches the cache window),
# reminders daily (the command itself is idempotent per marker).
TASKS = {
    "refresh_fx_rates": (lambda: timedelta(seconds=getattr(settings, "FX_CACHE_SECONDS", 3600)), _run_refresh_fx_rates),
    "send_payment_reminders": (lambda: timedelta(hours=24), _run_send_payment_reminders),
}


def _execute(task_name):
    from .models import ScheduledTask

    close_old_connections()
    _, runner = TASKS[task_name]
    try:
        result = runner()
        ScheduledTask.objects.filter(name=task_name).update(last_result=(result or "ok")[:2000])
        logger.info("Autotask %s completed: %s", task_name, result)
    except Exception as exc:
        ScheduledTask.objects.filter(name=task_name).update(last_result=f"ERROR: {exc}"[:2000])
        logger.exception("Autotask %s failed", task_name)
    finally:
        close_old_connections()


def run_due_tasks():
    """Claim-then-run every due task. Safe to call from any worker at any time."""
    from .models import ScheduledTask

    now = timezone.now()
    for name, (interval_fn, _) in TASKS.items():
        try:
            with transaction.atomic():
                task, _ = ScheduledTask.objects.get_or_create(name=name)
                task = ScheduledTask.objects.select_for_update().get(pk=task.pk)
                if not task.is_enabled:
                    continue
                if task.last_run_at and now - task.last_run_at < interval_fn():
                    continue
                # Claim inside the lock BEFORE running — other workers see the
                # fresh timestamp and skip.
                task.last_run_at = now
                task.save(update_fields=["last_run_at"])
        except Exception:
            logger.exception("Autotask claim failed for %s", name)
            continue

        threading.Thread(target=_execute, args=(name,), daemon=True).start()


class AutoTaskMiddleware:
    """Piggybacks scheduling on normal traffic. Fast path is one cache read."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "AUTOTASKS_ENABLED", True) and cache.get("autotasks:checked") is None:
            cache.set("autotasks:checked", True, CHECK_INTERVAL_SECONDS)
            try:
                run_due_tasks()
            except Exception:
                logger.exception("Autotask scheduling failed")
        return self.get_response(request)
