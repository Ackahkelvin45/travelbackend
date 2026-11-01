"""Chatbot conversations in the CRM: tagging, lead capture, staff tools, analytics."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from .chat_insights import analyse, build_chat_report, capture_lead
from .models import Booking, ChatThread, Lead
from .tests import make_package


def site_chat(*questions, **fields):
    messages = [m for q in questions for m in ({"role": "user", "content": q}, {"role": "assistant", "content": "Ok"})]
    return ChatThread.objects.create(channel="site", wa_id=f"site-{ChatThread.objects.count()}", messages=messages, **fields)


class InsightTests(TestCase):
    def tagged(self, text):
        thread = SimpleNamespace(messages=[{"role": "user", "content": text}, {"role": "assistant", "content": "Call us"}],
                                 contact_email="", contact_phone="", topics=[], intent="browsing")
        analyse(thread)
        return thread

    def test_topics_intent_and_contacts(self):
        t = self.tagged("How much is the Airbnb and when is ENZO?")
        self.assertEqual(t.topics, ["Prices", "Dates", "Accommodation", "Experiences"])
        self.assertEqual(t.intent, "warm")
        self.assertEqual(self.tagged("Hello there").intent, "browsing")
        self.assertEqual(self.tagged("I want to book for two").intent, "hot")
        t = self.tagged("Reach me at ama@example.com or +233 24 412 3456")
        self.assertEqual((t.contact_email, t.contact_phone, t.intent), ("ama@example.com", "+233244123456", "hot"))

    def test_only_the_guests_words_count_and_dates_are_not_phones(self):
        t = self.tagged("We land 2027-01-20 and paid 12500")
        self.assertEqual(t.contact_phone, "")  # the bot's own number isn't the guest's either


class LeadCaptureTests(TestCase):
    def ask(self, text, session=""):
        with mock.patch("bookings.assistant.reply", return_value=("Ok", None)):
            return self.client.post("/api/assistant/", {"session": session, "messages": [{"role": "user", "content": text}]},
                                    content_type="application/json").json()["session"]

    def setUp(self):
        cache.clear()

    def test_sharing_an_email_in_chat_captures_one_lead_without_marketing_consent(self):
        session = self.ask("How much is it?")
        self.assertFalse(Lead.objects.exists())
        self.ask("I'm ama@example.com", session)
        lead = Lead.objects.get()
        self.assertEqual((lead.email, lead.source, lead.marketing_consent), ("ama@example.com", "chat", False))
        self.assertIn("How much is it?", lead.message)
        self.assertIsNotNone(lead.reply_due_at)
        self.ask("Also my friend's question", session)
        self.assertEqual(Lead.objects.count(), 1)

        # The "message the team" form completes that lead, it doesn't duplicate it.
        self.client.post("/api/leads/", {"name": "Ama O", "email": "ama@example.com", "source": "chat", "message": "Call me",
                                         "marketing_consent": True, "chat_session": session}, content_type="application/json")
        lead = Lead.objects.get()
        self.assertEqual((lead.name, lead.message, lead.marketing_consent), ("Ama O", "Call me", True))
        self.assertTrue(ChatThread.objects.get().needs_person)

    def test_an_open_lead_with_the_same_email_is_reused(self):
        existing = Lead.objects.create(name="Ama", email="AMA@example.com", source="contact")
        self.ask("I'm ama@example.com")
        self.assertEqual(ChatThread.objects.get().lead, existing)

    def test_no_contact_means_no_lead(self):
        self.assertIsNone(capture_lead(site_chat("How much?")))

    def test_a_ready_to_buy_whatsapp_guest_is_captured_from_their_number(self):
        from .whatsapp import record_inbound
        msg = lambda i, text: {"from": "233244123456", "id": str(i), "type": "text", "text": {"body": text}}
        record_inbound(msg(1, "Hello"), "Kofi")
        self.assertFalse(Lead.objects.exists())
        record_inbound(msg(2, "I want to book for January"), "Kofi")
        lead = Lead.objects.get()
        self.assertEqual((lead.name, lead.phone, lead.source), ("Kofi", "+233244123456", "whatsapp"))

    def test_a_new_message_reopens_a_closed_chat(self):
        session = self.ask("Hi")
        ChatThread.objects.update(status="closed")
        self.ask("One more thing", session)
        self.assertEqual(ChatThread.objects.get().status, "open")

    def test_bot_handing_off_marks_the_chat_as_needing_a_person(self):
        with mock.patch("bookings.assistant.reply", return_value=("A person will help", "Wants a custom trip")):
            self.client.post("/api/assistant/", {"messages": [{"role": "user", "content": "Custom trip?"}]},
                             content_type="application/json")
        self.assertTrue(ChatThread.objects.get().needs_person)


class StaffToolsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="crm@test.com", password="x", is_staff=True)
        self.user.user_permissions.add(*Permission.objects.filter(
            codename__in=["view_lead", "change_lead", "view_chatthread", "change_chatthread"]))
        self.client.force_login(self.user)

    def act(self, action, *threads):
        return self.client.post("/admin/bookings/chatthread/", {
            "action": action, "_selected_action": [str(t.pk) for t in threads]}, follow=True)

    def test_queues_topics_and_page(self):
        hot = site_chat("I want to book", intent="hot")
        site_chat("Hello")
        page = self.client.get("/admin/bookings/chatthread/?queue=hot")
        self.assertContains(page, "Ready to buy")
        self.assertEqual(list(page.context["cl"].queryset), [hot])
        self.assertEqual(self.client.get("/admin/bookings/chatthread/?topic=Prices").context["cl"].queryset.count(), 0)

    def test_assign_close_and_create_leads_in_bulk(self):
        reachable, anonymous = site_chat("Hi", contact_email="k@example.com"), site_chat("Hi")
        self.act("assign_to_me", reachable)
        self.act("mark_closed", reachable)
        res = self.act("capture_leads", reachable, anonymous)
        self.assertContains(res, "1 skipped")
        reachable.refresh_from_db()
        anonymous.refresh_from_db()
        self.assertEqual((reachable.assigned_to, reachable.status), (self.user, "closed"))
        self.assertEqual(reachable.lead.email, "k@example.com")
        self.assertIsNone(anonymous.lead)

    def test_staff_can_note_a_website_chat_but_not_reply(self):
        thread = site_chat("Hi")
        url = f"/admin/bookings/chatthread/{thread.pk}/change/"
        self.assertNotContains(self.client.get(url), 'name="reply"')
        res = self.client.post(url, {"status": "following_up", "assigned_to": "", "staff_note": "Called"}, follow=True)
        self.assertEqual(res.status_code, 200)
        thread.refresh_from_db()
        self.assertEqual((thread.status, thread.staff_note), ("following_up", "Called"))
        self.assertEqual(len(thread.messages), 2)  # the transcript survives the save

    def test_create_lead_button_opens_the_lead(self):
        thread = site_chat("Hi", contact_phone="+233244123456")
        res = self.client.get(f"/admin/bookings/chatthread/{thread.pk}/capture-lead/")
        thread.refresh_from_db()
        self.assertRedirects(res, f"/admin/bookings/lead/{thread.lead_id}/change/", fetch_redirect_response=False)

    def test_analytics_page_needs_chat_access(self):
        self.assertEqual(self.client.get("/admin/chat-analytics/").status_code, 200)
        self.assertEqual(self.client.get("/admin/chat-analytics/?export=csv").status_code, 200)
        self.user.user_permissions.clear()
        self.assertEqual(self.client.get("/admin/chat-analytics/").status_code, 403)


class ReportTests(TestCase):
    def test_numbers_add_up_and_respect_the_range(self):
        today = timezone.localdate()
        pkg = make_package()
        booking = Booking.objects.create(package=pkg, first_name="A", last_name="B", email="a@example.com", num_guests=1,
                                         travel_date=today, unit_price=1, total_amount=1, currency="USD")
        won = Lead.objects.create(name="A", email="a@example.com", source="chat", booking=booking)
        lead = Lead.objects.create(name="B", email="b@example.com", source="chat")
        site_chat("How much?", topics=["Prices"], intent="warm", lead=won)
        site_chat("How much?", topics=["Prices"], intent="hot", lead=lead)
        site_chat("Is there a gym?", needs_person=True)
        ChatThread.objects.create(wa_id="233200000001", messages=[{"role": "user", "content": "Hi"}])
        old = site_chat("Old")
        ChatThread.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=30))

        r = build_chat_report(today - timedelta(days=6), today)
        self.assertEqual([(k["label"], k["value"]) for k in r["kpis"]], [
            ("Conversations", 4), ("Ready to buy", 1), ("Leads captured", 2), ("Booked", 1), ("Bot needed a person", 1)])
        self.assertEqual(r["topics"], [{"label": "Prices", "n": 2, "pct": 50, "leads": 2}])
        self.assertEqual(r["questions"][0], {"label": "how much", "n": 2})
        self.assertEqual({c["label"]: c["n"] for c in r["channels"]}, {"WhatsApp": 1, "Website chat": 3})
        self.assertEqual(r["unanswered"][0]["question"], "Is there a gym?")
        self.assertEqual(len(r["days"]), 7)
        self.assertEqual(sum(d["n"] for d in r["days"]), 4)
