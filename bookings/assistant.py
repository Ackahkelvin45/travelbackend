"""
bookings/assistant.py
---------------------
The guest assistant behind the site chat and WhatsApp (Change Request §8).

Its facts are the live public catalogue: the same pricing matrices and FAQs
the website renders, rebuilt per request. Nothing is hard-coded here, so a
price change in admin reaches the bot immediately, and anything switched off
(on_sale=false, disabled add-ons) never appears. It never takes payments or
touches bookings; anything it can't answer goes to a person.

Grounding, in three layers:
  1. Knowledge: only the facts block (catalogue, day-by-day programme, FAQs,
     how booking works, published policies, confirmed contact details) built
     fresh per request. Staff extend it by adding FAQs in the admin: the FAQ
     page and the assistant read the same entries.
  2. Instructions: answer only from it, rank the sources, never compute totals.
  3. Fact check: before a reply leaves, every price, percentage, link, email
     and phone number in it must appear in the facts. Anything else is held
     back and the guest is handed to a person instead of shown a guess.
"""

import json
import logging
import re
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

HANDOFF_TOOL = {"type": "function", "function": {
    "name": "hand_off_to_human",
    "description": "Pass the conversation to Azura's customer experience team.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One line for staff: who the guest is and what they need."},
        },
        "required": ["summary"],
    },
}}

INSTRUCTIONS = """You are the Azura Travels assistant. You answer guests on the website chat and on WhatsApp. Azura runs curated trips in Ghana.

Answer only from the facts below. They are live and approved: prices, dates, rooms, the day-by-day programme, what is included, availability, FAQs, how booking works (payment methods, checkout, what guests get afterwards, changes), the published policies and the confirmed contact details. If something is not covered, or the text is a placeholder in square brackets such as [TO BE CONFIRMED], say it hasn't been confirmed yet and offer to pass the question to the team. Never estimate or invent prices, dates, policies, refunds, visa rules, availability, venues or who will attend. Don't use general knowledge about Ghana or travel to fill gaps.

Which source wins: for prices, dates, deadlines, rooms and the programme, the packages section is the truth. Policies explain the rules (how refunds, instalments, cancellations and changes work); if a policy's dates or contact details differ from the packages or contact sections, use those sections, and if you're unsure, hand off. Give contact details only from the contact section; if one isn't listed, say it hasn't been confirmed yet.

Quote prices exactly as listed. Don't add up totals, shares or discounts yourself: the trip builder on the package page gives the exact total for the guest's group.

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
            "programme": [
                {"day": i.day, "date": i.title, "what_happens": i.description, "notes": i.notes}
                for i in p.itineraries.all()
            ],
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


def _policies():
    """The published policy documents guests accept at checkout."""
    from .models import PolicyDocument

    return [{"title": d.title, "text": d.body}
            for d in PolicyDocument.objects.filter(is_current=True, published_at__isnull=False).order_by("type")]


def _how_booking_works():
    """How the site actually behaves, read from the settings and rules the code
    enforces (never restated by hand, so it can't drift from the checkout)."""
    from payments.donations import donations_open
    from payments.models import OpsConfig
    from payments.views import PAYMENT_CHANNELS

    from .services import ADDON_ADD_CUTOFF_DAYS, TRANSFER_CUTOFF_DAYS

    ops = OpsConfig.get()
    my_booking = f"{settings.FRONTEND_URL.rstrip('/')}/dashboard"
    # "MTN Mobile Money", ..., "Card (Visa / Mastercard)"
    methods = [p["label"] for c in PAYMENT_CHANNELS for p in c["providers"]] + \
              [c["label"] for c in PAYMENT_CHANNELS if not c["providers"]]
    rules = {
        "payment_methods": methods,
        "payment_currency": "Prices are set in USD. Card and Mobile Money payments go through Paystack and are "
                            "charged in Ghana cedis (GHS); the exact cedi amount is shown before paying.",
        "rooms_during_checkout": f"Chosen rooms are held for {ops.hold_minutes} minutes while the guest checks out. "
                                 "A room is only secured once payment is made.",
        "my_booking_link": my_booking,
        "after_booking": "A confirmation email with the receipt and ticket. My Booking (my_booking_link) shows tickets, the payment "
                         "ledger and balance, and lets the guest pay the balance, complete the travel information "
                         "form (flights, airport transfer, emergency contact, dietary and accessibility needs) and "
                         "add experiences. Guests booking without an account get an emailed link to attach the "
                         "booking to one.",
        "adding_experiences_later": f"Experiences can be added from My Booking ({my_booking}) until "
                                    f"{ADDON_ADD_CUTOFF_DAYS} days before departure.",
        "name_changes_and_replacements": "Name corrections and replacing a traveller are done by the team, with no "
                                         f"charge. Replacements less than {TRANSFER_CUTOFF_DAYS} days before "
                                         "departure need individual approval. Payments stay with the booking.",
    }
    if ops.whatsapp_confirmation_template:
        rules["whatsapp_updates"] = ("Guests can opt in at checkout, or later in My Booking, to receive payment "
                                     "confirmations on WhatsApp.")
    if donations_open():
        rules["academy_donations"] = ("Guests can make an optional donation to the Michael Blackson Academy, paid "
                                      "separately to the Academy's own account.")
    return rules


def facts_json():
    return json.dumps({"contact": _contact(), "packages": _catalogue(), "how_booking_works": _how_booking_works(),
                       "policies": _policies()}, default=str, sort_keys=True)


def system_prompt(facts=None):
    return f"{INSTRUCTIONS}\n\n<facts>\n{facts or facts_json()}\n</facts>"


# ── Fact check ────────────────────────────────────────────────────────────────
_AMOUNT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONEY = re.compile(rf"(?:\$|USD|GHS|GH₵|₵)\s?({_AMOUNT})|({_AMOUNT})\s?(?:USD|GHS|dollars|cedis)\b", re.I)
# Money in the facts: the catalogue's decimal strings ("899.00") plus amounts written with a currency.
_FACT_MONEY = re.compile(r"(?<![\d.])(\d+\.\d{2})(?![\d])")
# Percentages in the facts: "50%" in text and the catalogue's *percent* fields.
_FACT_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%|percent[a-z_]*\"?:\s*\"?(\d+(?:\.\d+)?)", re.I)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_URL = re.compile(r"https?://[^\s)>\]]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _num(raw):
    try:
        return Decimal(raw.replace(",", "")).normalize()
    except InvalidOperation:
        return None


def unsupported(text, facts):
    """Prices, percentages, links, emails and phone numbers in ``text`` that
    the facts don't back up. Empty list = nothing invented."""
    known_money = {_num(m) for m in _FACT_MONEY.findall(facts)} | {_num(a or b) for a, b in _MONEY.findall(facts)}
    known_percents = {_num(a or b) for a, b in _FACT_PERCENT.findall(facts)}
    # Contact details count only from the confirmed contact settings, never
    # from policy text (the v1.0 policies still carry sample numbers).
    contact = _contact()
    contact_text = " ".join(contact.values()).lower()
    known_digits = {re.sub(r"\D", "", m) for m in _PHONE.findall(contact_text)}
    site = settings.FRONTEND_URL.rstrip("/").lower()
    whatsapp = re.sub(r"\D", "", contact.get("whatsapp", ""))
    allowed_links = (site,) + ((f"https://wa.me/{whatsapp}",) if whatsapp else ())
    problems = []
    for url in _URL.findall(text):
        url = url.rstrip(".,;:!?").lower()
        if not url.startswith(allowed_links):
            problems.append(f"link {url}")
    for email in _EMAIL.findall(text):
        if email.lower() not in contact_text:
            problems.append(f"email {email}")
    # Links and emails are checked; drop them so IDs inside them aren't misread as prices or phone numbers.
    text = _EMAIL.sub(" ", _URL.sub(" ", text))
    for a, b in _MONEY.findall(text):
        if _num(a or b) not in known_money:
            problems.append(f"price {a or b}")
    for pct in _PERCENT.findall(text):
        if _num(pct) not in known_percents:
            problems.append(f"{pct}%")
    for phone in _PHONE.findall(text):
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 9 and digits not in known_digits:
            problems.append(f"phone {phone.strip()}")
    return problems


def fallback_text():
    hours = _contact().get("support_hours")
    return "Sorry, I can't help with that right now, so I'm passing you to our team" + (
        f". They're available {hours}." if hours else ".")


def reply(history):
    """``history`` is [{"role": "user"|"assistant", "content": str}, ...], ending
    with the guest's message. Returns (text, handoff_summary or None)."""
    try:
        if not settings.DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        # DeepSeek's OpenAI-compatible chat API. The system prompt goes first so
        # its automatic prefix cache covers the catalogue across requests.
        facts = facts_json()
        r = requests.post(
            f"{settings.DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"},
            json={
                "model": settings.ASSISTANT_MODEL,
                "messages": [{"role": "system", "content": system_prompt(facts)}, *history],
                "tools": [HANDOFF_TOOL],
                "max_tokens": 1024,
                # Most predictable output: factual answers, not creative ones.
                "temperature": 0,
            },
            timeout=60,
        )
        r.raise_for_status()
        choice = r.json()["choices"][0]
    except Exception:  # API errors, missing key, bad catalogue data: a guest must never get silence
        logger.exception("Assistant call failed")
        return fallback_text(), "Assistant unavailable: guest needs a reply."

    if choice.get("finish_reason") == "content_filter":
        return fallback_text(), "Assistant declined: guest needs a reply."
    message = choice["message"]
    text = (message.get("content") or "").strip()
    handoff = None
    for call in message.get("tool_calls") or []:
        if call["function"]["name"] == HANDOFF_TOOL["function"]["name"]:
            try:
                handoff = json.loads(call["function"]["arguments"]).get("summary") or ""
            except (ValueError, AttributeError):
                handoff = ""
            handoff = handoff or "Guest asked for a person."
    problems = unsupported(text, facts)
    if problems:
        # Something in the draft isn't in the facts: hold it back rather than risk a wrong answer.
        logger.warning("Assistant reply held back (not in facts: %s): %r", ", ".join(problems), text)
        question = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        text = "I don't want to give you a wrong answer on that."
        handoff = handoff or f"Assistant couldn't answer from approved facts: {question[:200]}"
    if handoff is not None:
        hours = _contact().get("support_hours")
        text = (text + "\n\n" if text else "") + "I'm passing this to our team" + (
            f". They're available {hours}." if hours else " and they'll be in touch.")
    return text or fallback_text(), handoff
