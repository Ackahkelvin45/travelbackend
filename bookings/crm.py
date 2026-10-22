"""
CRM follow-ups (Change Request v4 §8), run hourly by payments.autotasks and
safe to run any time (every step is idempotent):

  1. checkout_leads   — an unpaid checkout older than an hour becomes a lead
                        carrying its server quote.
  2. quote_reminders  — consent-based reminders at OpsConfig.reminder_schedule_hours
                        after the lead arrived; stop once the guest pays, the
                        lead is closed, or they unsubscribe.
  3. staff_alerts     — one email per overdue lead reply and per guest not
                        contacted within the coordinator SLA.
"""

import html
import logging
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

from .models import Booking, Lead

logger = logging.getLogger(__name__)

ABANDON_AFTER = timedelta(hours=1)
UNSUBSCRIBE_SALT = "lead-unsubscribe"


def unsubscribe_url(lead) -> str:
    token = signing.dumps(str(lead.pk), salt=UNSUBSCRIBE_SALT)
    return f"{settings.FRONTEND_URL.rstrip('/')}/unsubscribe?token={token}"


def unsubscribe(token: str):
    """Turn off marketing for the lead in a signed link. Returns the lead or None."""
    try:
        pk = signing.loads(token, salt=UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None
    lead = Lead.objects.filter(pk=pk).first()
    if lead and lead.marketing_consent:
        lead.marketing_consent = False
        lead.notes = (lead.notes + "\n" if lead.notes else "") + f"Unsubscribed {timezone.now():%d %b %Y %H:%M} GMT."
        lead.save(update_fields=["marketing_consent", "notes", "updated_at"])
    return lead


def _has_paid_booking(email: str) -> bool:
    return Booking.objects.filter(
        email__iexact=email, status__in=[Booking.Status.CONFIRMED, Booking.Status.COMPLETED],
    ).exists()


def checkout_leads(now=None) -> int:
    from payments.models import OpsConfig

    now = now or timezone.now()
    made = 0
    unpaid = Booking.objects.filter(
        status=Booking.Status.PENDING, amount_paid=0, created_at__lt=now - ABANDON_AFTER,
    ).exclude(mode="").select_related("package")
    for b in unpaid:
        if Lead.objects.filter(quote__abandoned_booking=b.reference).exists() or _has_paid_booking(b.email):
            continue
        if Lead.objects.filter(email__iexact=b.email).exclude(stage__in=[Lead.Stage.WON, Lead.Stage.LOST]).exists():
            continue  # already being followed up
        Lead.objects.create(
            name=b.full_name, email=b.email, phone=b.phone or "", source=Lead.Source.CHECKOUT,
            marketing_consent=b.marketing_consent, stage=Lead.Stage.QUOTED, package=b.package,
            quote={**(b.quote_snapshot or {}), "abandoned_booking": b.reference},
            message=f"Started checkout ({b.reference}) but didn't pay.",
            reply_due_at=now + timedelta(hours=OpsConfig.get().lead_reply_hours),
        )
        made += 1
    return made


def quote_reminders(now=None) -> int:
    from payments.email import send_saved_quote
    from payments.models import OpsConfig

    now = now or timezone.now()
    schedule = sorted(int(h) for h in (OpsConfig.get().reminder_schedule_hours or []) if str(h).isdigit())
    if not schedule:
        return 0
    sent = 0
    leads = Lead.objects.filter(marketing_consent=True, booking__isnull=True).exclude(
        stage__in=[Lead.Stage.WON, Lead.Stage.LOST]).exclude(email="").exclude(quote={})
    for lead in leads:
        age = (now - lead.created_at).total_seconds() / 3600
        due = [h for h in schedule if h <= age and str(h) not in (lead.reminders_sent or {})]
        if not due:
            continue
        if _has_paid_booking(lead.email):
            lead.stage = Lead.Stage.WON
            lead.save(update_fields=["stage", "updated_at"])
            continue
        # Only the latest due reminder: a missed run never sends a burst.
        if send_saved_quote(lead, reminder=True):
            lead.reminders_sent = {**(lead.reminders_sent or {}), **{str(h): now.isoformat() for h in due}}
            lead.save(update_fields=["reminders_sent", "updated_at"])
            sent += 1
    return sent


def _alert(to: list, subject: str, lines: list) -> bool:
    from payments.email import _shell, deliver

    body = (f'<p style="margin:0 0 12px;font-size:16px;font-weight:800;color:#1a1a2e;">{html.escape(subject)}</p>'
            + "".join(f'<p style="margin:0 0 8px;font-size:13px;">{html.escape(l)}</p>' for l in lines if l)
            + '<p style="margin:16px 0 0;font-size:12px;color:#888;">Follow up in the Azura admin.</p>')
    ok = False
    for email in dict.fromkeys(e for e in to if e):
        ok = deliver("staff_alert", email, f"[Azura] {subject}", _shell(subject, body)) or ok
    return ok


def staff_alerts(now=None) -> int:
    from config.dashboard import contact_overdue_ids
    from payments.models import OpsConfig

    now = now or timezone.now()
    ops = OpsConfig.get().alert_email or getattr(settings, "ADMIN_ALERT_EMAIL", "")
    alerted = 0
    late_leads = Lead.objects.filter(
        reply_due_at__lt=now, replied_at__isnull=True, reply_alerted_at__isnull=True,
    ).exclude(stage__in=[Lead.Stage.WON, Lead.Stage.LOST]).select_related("assigned_to")
    for lead in late_leads:
        who = lead.email or lead.phone or "no contact details"
        if _alert([lead.assigned_to.email if lead.assigned_to else "", ops], f"Lead reply overdue: {lead.name}",
                  [f"{lead.get_source_display()} lead ({who}) was due a reply by {lead.reply_due_at:%d %b %H:%M} GMT.",
                   lead.message[:300]]):
            lead.reply_alerted_at = now
            lead.save(update_fields=["reply_alerted_at", "updated_at"])
            alerted += 1
    late_contacts = Booking.objects.filter(pk__in=contact_overdue_ids(), contact_alerted_at__isnull=True) \
        .select_related("coordinator")
    for b in late_contacts:
        if _alert([b.coordinator.email if b.coordinator else "", ops], f"Guest not contacted: {b.reference}",
                  [f"{b.full_name} paid and hasn't been contacted within {OpsConfig.get().coordinator_contact_hours} hours.",
                   "Coordinator: " + (b.coordinator.full_name if b.coordinator else "not assigned yet")]):
            b.contact_alerted_at = now
            b.save(update_fields=["contact_alerted_at", "updated_at"])
            alerted += 1
    return alerted


def run_followups() -> str:
    return (f"{checkout_leads()} checkout lead(s), {quote_reminders()} reminder(s), "
            f"{staff_alerts()} staff alert(s)")
