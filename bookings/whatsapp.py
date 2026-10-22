"""
bookings/whatsapp.py
--------------------
WhatsApp Cloud API: the webhook Meta calls with guest messages, and sending
replies. Guest text goes to the shared assistant; a hand-off creates a Lead,
pauses the bot and leaves the thread to staff (Admin → Chat threads).

Meta needs a fast 200 and retries otherwise, so the webhook only verifies,
records and de-duplicates; the assistant runs in a daemon thread, like
payments/autotasks.py.
"""

import hashlib
import hmac
import json
import logging
import threading
from datetime import timedelta

import requests
from django.conf import settings
from django.db import close_old_connections, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .assistant import reply
from .models import ChatThread, Lead

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v25.0"
CONTEXT_MESSAGES = 20  # turns replayed to the assistant
KEEP_MESSAGES = 200  # transcript kept for staff
KEEP_SEEN_IDS = 50


def send_text(wa_id, body):
    """Free-form text. Meta only delivers it within 24h of the guest's last message."""
    r = requests.post(
        f"{GRAPH_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": wa_id, "type": "text", "text": {"body": body[:4096]}},
        timeout=15,
    )
    r.raise_for_status()


def verify_signature(body, header):
    secret = settings.WHATSAPP_APP_SECRET
    if not secret or not header.startswith("sha256="):
        return False  # fail closed when unconfigured
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@csrf_exempt
def webhook(request):
    if request.method == "GET":  # Meta's one-off subscription check
        if (request.GET.get("hub.mode") == "subscribe" and settings.WHATSAPP_VERIFY_TOKEN
                and hmac.compare_digest(request.GET.get("hub.verify_token", ""), settings.WHATSAPP_VERIFY_TOKEN)):
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    if not verify_signature(request.body, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("WhatsApp webhook with invalid signature.")
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=200)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):  # delivery "statuses" are ignored
                thread_id = record_inbound(msg, names.get(msg.get("from"), ""))
                if thread_id:
                    in_background(respond, thread_id)
    return HttpResponse(status=200)


def record_inbound(msg, name):
    """Store a guest message. Returns the thread id if the bot should answer,
    None for a duplicate delivery or a thread staff have taken over."""
    wa_id, msg_id = msg.get("from", ""), msg.get("id", "")
    if not wa_id or not msg_id:
        return None
    text = msg["text"]["body"] if msg.get("type") == "text" else f"[{msg.get('type', 'unknown')} message]"
    with transaction.atomic():
        thread, _ = ChatThread.objects.select_for_update().get_or_create(wa_id=wa_id)
        if msg_id in thread.seen_ids:
            return None
        thread.seen_ids = (thread.seen_ids + [msg_id])[-KEEP_SEEN_IDS:]
        thread.messages = (thread.messages + [{"role": "user", "content": text}])[-KEEP_MESSAGES:]
        thread.name = name or thread.name
        thread.last_inbound_at = timezone.now()
        thread.save()
    return None if thread.bot_paused else thread.id


def in_background(fn, *args):
    def run():
        try:
            fn(*args)
        finally:
            close_old_connections()
    threading.Thread(target=run, daemon=True).start()


def respond(thread_id):
    # ponytail: two messages sent seconds apart get two replies; add a per-thread lock if that bites.
    thread = ChatThread.objects.get(id=thread_id)
    history = [{"role": m["role"], "content": m["content"]} for m in thread.messages[-CONTEXT_MESSAGES:]]
    while history and history[0]["role"] != "user":
        history.pop(0)  # the API needs the conversation to open with the guest
    text, handoff = reply(history)
    try:
        send_text(thread.wa_id, text)
    except requests.RequestException:
        logger.exception("WhatsApp send failed for thread %s", thread_id)
        return
    with transaction.atomic():
        thread = ChatThread.objects.select_for_update().get(id=thread_id)
        thread.messages = (thread.messages + [{"role": "assistant", "content": text}])[-KEEP_MESSAGES:]
        if handoff is not None:
            thread.bot_paused = True
            thread.lead = hand_off(thread, handoff)
        thread.save()


def hand_off(thread, summary):
    from payments.models import OpsConfig

    transcript = "\n".join(f"{'Guest' if m['role'] == 'user' else 'Azura'}: {m['content']}"
                           for m in thread.messages[-CONTEXT_MESSAGES:])
    return Lead.objects.create(
        name=thread.name or f"+{thread.wa_id}", phone=f"+{thread.wa_id}", source=Lead.Source.WHATSAPP,
        message=f"{summary}\n\n{transcript}",
        reply_due_at=timezone.now() + timedelta(hours=OpsConfig.get().lead_reply_hours),
    )
