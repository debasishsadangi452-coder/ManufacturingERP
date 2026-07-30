"""Email-to-draft-order pipeline (P0-B).

Given an order email, extract the customer + line items with an LLM, match them
to existing Customers/Items, and create a DRAFT SalesOrder for human review.
Nothing here confirms an order or syncs to QuickBooks — that only happens when a
human confirms the draft (which promotes it out of "draft" status).

extract_order() degrades gracefully: with no GROQ_API_KEY it returns a
low-confidence empty parse marked "needs attention" rather than raising, so the
inbox still records the email and the pipeline stays testable.
"""

import json
import logging

from django.conf import settings
from django.db import transaction

from inventory.models import Item
from .models import Customer, InboundOrderEmail, SalesOrder, SalesOrderItem

logger = logging.getLogger(__name__)


EXTRACTION_PROMPT = (
    "You extract purchase orders from emails for a food manufacturer. "
    "Return ONLY JSON with this shape:\n"
    '{"customer": "<name>", "pickup_date": "<YYYY-MM-DD or null>", '
    '"lines": [{"product": "<name>", "cases": <number>}], '
    '"confidence": <0..1>}\n'
    "confidence reflects how sure you are of the whole extraction. "
    "If quantities or products are ambiguous, lower it."
)


def extract_order(email_body, subject=""):
    """Run the LLM extraction. Returns a dict with customer/lines/confidence.

    Never raises for a missing key or a bad response — returns a zero-confidence
    result so the caller records the email for manual handling.
    """
    config = getattr(settings, "AI_CONFIG", {})
    api_key = config.get("GROQ_API_KEY")
    if not api_key:
        return {"customer": "", "pickup_date": None, "lines": [], "confidence": 0.0,
                "note": "No AI model configured; needs manual entry."}

    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        model = config.get("MODEL", "llama-3.3-70b-versatile")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": f"Subject: {subject}\n\n{email_body}"},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(response.choices[0].message.content)
        data.setdefault("confidence", 0.0)
        data.setdefault("lines", [])
        return data
    except Exception as exc:  # network, JSON, quota — all non-fatal here
        logger.warning("Order extraction failed: %s", exc)
        return {"customer": "", "pickup_date": None, "lines": [], "confidence": 0.0,
                "note": f"Extraction error: {exc}"}


def _match_customer(company, name):
    if not name:
        return None
    return (
        Customer.objects.filter(company=company, name__iexact=name).first()
        or Customer.objects.filter(company=company, name__icontains=name).first()
    )


def _match_item(company, name):
    if not name:
        return None
    return (
        Item.objects.filter(company=company, name__iexact=name).first()
        or Item.objects.filter(company=company, name__icontains=name).first()
    )


# Below this extraction confidence, or with any unmatched line, the email is
# flagged for human attention rather than presented as a clean draft.
CONFIDENCE_REVIEW_THRESHOLD = 0.85


@transaction.atomic
def create_draft_from_email(company, sender, subject, body, received_at=None):
    """Full pipeline for one email: extract → match → create draft SalesOrder.

    Returns the InboundOrderEmail record (with .sales_order set when a customer
    matched). The order is always created as status="draft" so it never syncs
    to QuickBooks or triggers production until confirmed.
    """
    inbound = InboundOrderEmail.objects.create(
        company=company,
        sender=sender or "",
        subject=subject or "",
        raw_body=body or "",
        **({"received_at": received_at} if received_at else {}),
    )

    parsed = extract_order(body or "", subject or "")
    inbound.parsed_data = parsed
    inbound.confidence = parsed.get("confidence", 0.0)

    customer = _match_customer(company, parsed.get("customer"))
    matched_lines = []
    unmatched = []
    for line in parsed.get("lines", []):
        item = _match_item(company, line.get("product"))
        if item and line.get("cases"):
            matched_lines.append((item, float(line["cases"])))
        else:
            unmatched.append(line.get("product"))

    # Decide status: need a customer, at least one matched line, no unmatched
    # lines, and confidence above the review threshold to be a clean draft.
    clean = (
        customer is not None
        and matched_lines
        and not unmatched
        and inbound.confidence >= CONFIDENCE_REVIEW_THRESHOLD
    )

    if customer and matched_lines:
        order = SalesOrder.objects.create(
            customer=customer, status="draft", source="email"
        )
        for item, cases in matched_lines:
            SalesOrderItem.objects.create(sales_order=order, item=item, quantity=cases)
        inbound.sales_order = order

    inbound.status = "parsed" if clean else "needs_attention"
    if unmatched:
        inbound.error_message = "Unmatched products: " + ", ".join(str(u) for u in unmatched)
    inbound.save()
    return inbound
