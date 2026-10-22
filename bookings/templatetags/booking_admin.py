from django import template

register = template.Library()


@register.simple_tag
def booking_summary(booking):
    """Numbers for the booking change-form header (admin/bookings/booking_summary.html)."""
    from bookings.admin import CHECKLIST, contact_sla

    paid = booking.amount_paid - booking.amount_refunded
    done = sum(1 for k, _ in CHECKLIST if (booking.checklist or {}).get(k))
    state, text = contact_sla(booking)
    coordinator = booking.coordinator
    return {
        "paid_pct": min(100, max(0, int(paid / booking.total_amount * 100))) if booking.total_amount else 0,
        "deadline": booking.effective_payment_deadline,
        "coordinator": (coordinator.full_name or coordinator.email) if coordinator else None,
        "sla_state": state, "sla_text": text,
        "checks_done": done, "checks_total": len(CHECKLIST), "checks_pct": int(done / len(CHECKLIST) * 100),
    }
