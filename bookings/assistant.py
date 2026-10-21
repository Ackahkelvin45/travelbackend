"""
bookings/assistant.py
---------------------
The guest assistant behind the site chat and WhatsApp (Change Request §8).

Its facts are the live public catalogue: the same pricing matrices and FAQs
the website renders, rebuilt per request. Nothing is hard-coded here, so a
price change in admin reaches the bot immediately, and anything switched off
(on_sale=false, disabled add-ons) never appears. It never takes payments or
touches bookings; anything it can't answer goes to a person.
"""

import json
import logging

import anthropic
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

HANDOFF_TOOL = {
    "name": "hand_off_to_human",
    "description": "Pass the conversation to Azura's customer experience team.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One line for staff: who the guest is and what they need."},
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
}

INSTRUCTIONS = """You are the Azura Travels assistant. You answer guests on the website chat and on WhatsApp. Azura runs curated trips in Ghana.

Answer only from the catalogue and contact details below. They are live and approved: prices, dates, rooms, what is included, availability and FAQs. If something is not covered, or the text is a placeholder in square brackets such as [TO BE CONFIRMED], say it hasn't been confirmed yet and offer to pass the question to the team. Never estimate or invent prices, dates, policies, refunds, visa rules or availability.

Prices are in the currency listed (USD). Stays and experiences with on_sale false or units_available 0 can't be booked right now. Availability changes, so never promise or hold a room. Booking and payment happen only through the website checkout: share the package link. You cannot take payments, look up or change existing bookings, or issue refunds.

Call hand_off_to_human when the guest asks for a person, has a question about their own booking or payment (a failed payment, changes, cancellation, refund), is unhappy, or you can't answer from the facts below. Don't claim someone is available around the clock or will reply instantly; quote the support hours if they are listed.

Never ask for card numbers, passwords or passport scans in chat.

Keep replies short and warm, in plain text that reads well on WhatsApp: no markdown, headings or tables. Reply in the guest's language."""


def _catalogue():
    """Public facts for every package the site currently lists."""
    from packages.models import TravelPackage

    from .pricing import build_component_matrix, build_configurable_matrix, build_pricing_matrix

    builders = {
        TravelPackage.PricingModel.COMPONENT: build_component_matrix,
        TravelPackage.PricingModel.CORE_PLUS_ADDONS: build_configurable_matrix,
    }
    today = timezone.now().date()
    packages = (
        TravelPackage.objects.filter(is_active=True)
        .filter(Q(available_to__isnull=True) | Q(available_to__gte=today))
        .prefetch_related("options", "addon_groups", "addons", "discount_rules", "faqs", "itineraries")
        .order_by("-is_featured", "title")
    )
    site = settings.FRONTEND_URL.rstrip("/")
    out = []
    for p in packages:
        pricing = builders.get(p.pricing_model, build_pricing_matrix)(p)
        # Volatile or internal: the clock and GHS charge rate would also defeat prompt caching.
        for key in ("package_id", "server_now", "charge"):
            pricing.pop(key, None)
        out.append({
            "title": p.title,
            "link": f"{site}/tour/{p.id}",
            "description": p.description,
            "highlights": p.highlights,
            "included": p.whats_included,
            "excluded": p.whats_excluded,
            "duration_days": p.duration_days,
            "itinerary": [f"Day {i.day}: {i.title}" for i in p.itineraries.all()],
            "faqs": [{"q": f.question, "a": f.answer} for f in p.faqs.all()],
            "pricing": pricing,
        })
    return out


def _contact():
    from payments.models import OpsConfig

    c = OpsConfig.get()
    # Blank means "not confirmed yet": leave it out rather than let the bot guess.
    return {k: v for k, v in {
        "support_hours": c.staffed_hours, "whatsapp": c.whatsapp_number, "email": c.contact_email,
        "phone": c.contact_phone, "emergency_phone": c.emergency_phone,
    }.items() if v}


def system_prompt():
    facts = json.dumps({"contact": _contact(), "packages": _catalogue()}, default=str, sort_keys=True)
    return f"{INSTRUCTIONS}\n\n<facts>\n{facts}\n</facts>"


def fallback_text():
    hours = _contact().get("support_hours")
    return "Sorry, I can't help with that right now, so I'm passing you to our team" + (
        f". They're available {hours}." if hours else ".")


def reply(history):
    """``history`` is [{"role": "user"|"assistant", "content": str}, ...], ending
    with the guest's message. Returns (text, handoff_summary or None)."""
    try:
        response = anthropic.Anthropic().beta.messages.create(
            model=settings.ASSISTANT_MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
            messages=history,
            tools=[HANDOFF_TOOL],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception:  # API errors, missing key, bad catalogue data: a guest must never get silence
        logger.exception("Assistant call failed")
        return fallback_text(), "Assistant unavailable: guest needs a reply."

    if response.stop_reason == "refusal":
        return fallback_text(), "Assistant declined: guest needs a reply."
    text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
    handoff = next((b.input["summary"] for b in response.content
                    if b.type == "tool_use" and b.name == HANDOFF_TOOL["name"]), None)
    if handoff is not None:
        hours = _contact().get("support_hours")
        text = (text + "\n\n" if text else "") + "I'm passing this to our team" + (
            f". They're available {hours}." if hours else " and they'll be in touch.")
    return text or fallback_text(), handoff
