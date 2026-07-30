"""Poll an IMAP mailbox for order emails and turn each new message into a
draft SalesOrder (P0-B ingest stage).

Intended to run on a schedule, like the QuickBooks sync command:

    python manage.py poll_order_inbox --company <slug>

Mailbox settings come from env (per the plan's "forwarding address" option):
    ORDER_INBOX_HOST, ORDER_INBOX_USER, ORDER_INBOX_PASSWORD, ORDER_INBOX_FOLDER

Each message becomes an InboundOrderEmail + draft order via the same
create_draft_from_email pipeline the tests exercise directly. Drafts never sync
to QuickBooks until a human confirms them.
"""

import email
import imaplib
import os
from email.header import decode_header

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Company
from sales.email_orders import create_draft_from_email


def _decode(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        out.append(text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text)
    return "".join(out)


def _plain_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""


class Command(BaseCommand):
    help = "Poll the order inbox and create draft sales orders from new emails."

    def add_arguments(self, parser):
        parser.add_argument("--company", dest="company_slug", required=True,
                            help="Company slug the orders belong to.")
        parser.add_argument("--limit", type=int, default=25, help="Max messages per run.")

    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options["company_slug"]).first()
        if not company:
            raise CommandError(f"No company with slug {options['company_slug']!r}.")

        host = os.getenv("ORDER_INBOX_HOST")
        user = os.getenv("ORDER_INBOX_USER")
        password = os.getenv("ORDER_INBOX_PASSWORD")
        folder = os.getenv("ORDER_INBOX_FOLDER", "INBOX")
        if not (host and user and password):
            raise CommandError(
                "Set ORDER_INBOX_HOST / ORDER_INBOX_USER / ORDER_INBOX_PASSWORD to poll the inbox."
            )

        created = 0
        with imaplib.IMAP4_SSL(host) as imap:
            imap.login(user, password)
            imap.select(folder)
            typ, data = imap.search(None, "UNSEEN")
            ids = data[0].split()[: options["limit"]]
            for msg_id in ids:
                typ, msg_data = imap.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                inbound = create_draft_from_email(
                    company=company,
                    sender=email.utils.parseaddr(msg.get("From"))[1],
                    subject=_decode(msg.get("Subject")),
                    body=_plain_body(msg),
                )
                created += 1
                self.stdout.write(
                    f"  {inbound.sender}: {inbound.status} "
                    f"(confidence {inbound.confidence or 0:.0%})"
                )

        self.stdout.write(self.style.SUCCESS(f"Processed {created} new email(s)."))
