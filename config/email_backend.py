"""Django email backend that sends through Resend via payments.email.deliver,
so framework emails (admin password reset) use the same verified sender and
appear in the email log like every other message."""

from django.core.mail.backends.base import BaseEmailBackend


class ResendEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        from payments.email import deliver

        sent = 0
        for message in email_messages:
            html = next((body for body, mime in getattr(message, "alternatives", []) if mime == "text/html"), None)
            body = html or "<pre style='font-family:inherit;white-space:pre-wrap'>{}</pre>".format(
                message.body.replace("&", "&amp;").replace("<", "&lt;"))
            for to in message.to:
                sent += deliver("django_mail", to, message.subject, body)
        return sent
