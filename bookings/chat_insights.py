"""
Chatbot conversations as a sales tool: what each chat is about, how ready the
guest is to buy, turning a chat into a Lead, and the analytics page's numbers.
Keyword rules on purpose: predictable, free, and staff can read them here.
"""

import re
from collections import Counter
from datetime import timedelta

from django.utils import timezone

from .assistant import _EMAIL, _PHONE
from .models import ChatThread, Lead

TOPICS = {
    "Prices": r"price|cost|how much|\$|usd|fee|expensive|cheap|discount|afford",
    "Booking & payment": r"\bbook|reserve|\bpay|deposit|instal+ment|balance|checkout|card|momo|mobile money",
    "Dates": r"\bdates?\b|\bwhen\b|january|\bjan\b|december|\bdec\b|how long|schedule|itinerary",
    "Accommodation": r"airbnb|hotel|four points|\brooms?\b|\bstay|accommodation|bedroom",
    "Experiences": r"enzo|massage|boti|cape coast|crown forest|michael|blackson|academy|tour\b|experience",
    "Transfers & travel": r"airport|pickup|pick up|transfer|flight|visa|passport|vaccin",
    "Groups": r"\bgroup|friends|family|couple|\bwife\b|\bhusband\b|\bkids?\b|children",
    "Refunds & changes": r"refund|cancel|change my|reschedule",
}
_TOPICS = {name: re.compile(pattern, re.I) for name, pattern in TOPICS.items()}
# Ready to buy, not just curious.
_HOT = re.compile(r"\b(i want to|i'd like to|i would like to|ready to|how (do|can) i|sign me up|can i)\b.*"
                  r"\b(book|pay|reserve|join|come|deposit)\b|\bbook(ing)? (now|today|for)\b", re.I)


def _guest_lines(thread) -> list:
    return [m["content"] for m in thread.messages if m["role"] == "user"]


def _find_phone(text: str) -> str:
    for match in _PHONE.findall(text):
        if 10 <= sum(c.isdigit() for c in match) <= 15:  # not a date or an amount
            return re.sub(r"[^\d+]", "", match)
    return ""


def analyse(thread) -> None:
    """Refresh a thread's topics, intent and contact details from what the guest wrote."""
    text = "\n".join(_guest_lines(thread))
    thread.topics = [name for name, pattern in _TOPICS.items() if pattern.search(text)]
    email = _EMAIL.search(text)
    thread.contact_email = thread.contact_email or (email.group(0)[:254] if email else "")
    thread.contact_phone = thread.contact_phone or _find_phone(text)[:30]
    if _HOT.search(text) or thread.contact_email or thread.contact_phone:
        thread.intent = ChatThread.Intent.HOT
    elif {"Prices", "Booking & payment", "Dates"} & set(thread.topics):
        thread.intent = ChatThread.Intent.WARM
    else:
        thread.intent = ChatThread.Intent.BROWSING


def transcript(thread, last=20) -> str:
    return "\n".join(f"{'Guest' if m['role'] == 'user' else 'Azura'}: {m['content']}" for m in thread.messages[-last:])


def capture_lead(thread):
    """The thread's Lead: existing, an open one with the same contact, or a new
    one. None when there's no way to reach the guest. Never marketing consent:
    a lead from a chat gets a reply to the enquiry, not sales reminders.
    The caller saves the thread."""
    from payments.models import OpsConfig

    if thread.lead_id:
        return thread.lead
    email = thread.contact_email
    phone = thread.contact_phone or (f"+{thread.wa_id}" if thread.channel == ChatThread.Channel.WHATSAPP else "")
    if not email and not phone:
        return None
    open_leads = Lead.objects.exclude(stage__in=[Lead.Stage.WON, Lead.Stage.LOST])
    lead = (open_leads.filter(email__iexact=email).first() if email else None) \
        or (open_leads.filter(phone=phone).first() if phone else None)
    if lead is None:
        lead = Lead.objects.create(
            name=thread.name or email or phone, email=email, phone=phone, message=transcript(thread),
            referral_code=thread.referral_code,
            source=Lead.Source.WHATSAPP if thread.channel == ChatThread.Channel.WHATSAPP else Lead.Source.CHAT,
            reply_due_at=timezone.now() + timedelta(hours=OpsConfig.get().lead_reply_hours),
        )
    thread.lead = lead
    return lead


def note_guest_message(thread) -> None:
    """After a guest message: reopen a closed chat, refresh the insights, and
    capture a lead once the guest shared contact details (or, on WhatsApp,
    where the number is known, once they're ready to buy)."""
    if thread.status == ChatThread.Status.CLOSED:
        thread.status = ChatThread.Status.OPEN
    analyse(thread)
    shared = thread.contact_email or thread.contact_phone
    if shared or (thread.channel == ChatThread.Channel.WHATSAPP and thread.intent == ChatThread.Intent.HOT):
        capture_lead(thread)


def build_chat_report(start, end) -> dict:
    # ponytail: counted in Python, fine for thousands of chats; aggregate in SQL if it ever slows.
    threads = list(ChatThread.objects.filter(created_at__date__range=(start, end)).select_related("lead"))
    n = len(threads)
    pct = lambda a, b: round(a / b * 100) if b else 0
    leads = [t for t in threads if t.lead_id]
    booked = [t for t in leads if t.lead.booking_id]
    person = [t for t in threads if t.needs_person]
    waiting = [t for t in person if t.status == ChatThread.Status.OPEN]
    hot = [t for t in threads if t.intent == ChatThread.Intent.HOT]
    questions = sum(len(_guest_lines(t)) for t in threads)

    topics = Counter(name for t in threads for name in t.topics)
    topic_leads = Counter(name for t in leads for name in t.topics)
    first_questions = Counter(_guest_lines(t)[0].strip().lower().rstrip("?!. ")[:120] for t in threads if _guest_lines(t))
    days = Counter(timezone.localtime(t.created_at).date() for t in threads)
    hours = Counter(timezone.localtime(t.created_at).hour for t in threads)
    span = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    busiest = max(days.values(), default=1)

    return {
        "start": start, "end": end,
        "kpis": [
            {"label": "Conversations", "value": n, "note": f"{questions} guest message{'s' if questions != 1 else ''}"},
            {"label": "Ready to buy", "value": len(hot), "note": f"{pct(len(hot), n)}% of conversations"},
            {"label": "Leads captured", "value": len(leads), "note": f"{pct(len(leads), n)}% of conversations"},
            {"label": "Booked", "value": len(booked), "note": f"{pct(len(booked), len(leads))}% of captured leads"},
            {"label": "Bot needed a person", "value": len(person), "note": f"{len(waiting)} still waiting"},
        ],
        "funnel": [{"label": "Conversations", "n": n}, {"label": "Ready to buy", "n": len(hot)},
                   {"label": "Leads captured", "n": len(leads)}, {"label": "Booked", "n": len(booked)}],
        "funnel_max": max(n, 1),
        "channels": [{"label": label, "n": c, "pct": pct(c, n)} for value, label in ChatThread.Channel.choices
                     if (c := sum(t.channel == value for t in threads))],
        "topics": [{"label": k, "n": v, "pct": pct(v, n), "leads": topic_leads[k]} for k, v in topics.most_common()],
        "questions": [{"label": k, "n": v} for k, v in first_questions.most_common(10)],
        "unanswered": [{"thread": t, "question": _guest_lines(t)[-1]}
                       for t in sorted(waiting, key=lambda t: t.created_at, reverse=True)[:10] if _guest_lines(t)],
        "days": [{"date": d, "n": days[d], "height": max(round(days[d] / busiest * 100), 2 if days[d] else 0)} for d in span],
        "hours": [{"label": f"{h:02d}:00", "n": c} for h, c in hours.most_common(5)],
    }
