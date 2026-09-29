"""Vendor bills raised from purchase orders, and what follows a goods receipt.

`on_goods_received` is the single hook every receiving path calls (the
Procurement screen and the AI receiving tools). It auto-posts the receipt to
the ledger and — when the company's Accounting setup has "create vendor bill
on receipt" on — raises the vendor bill from the PO, which is auto-posted too
(Dr GRNI / Cr Accounts Payable). It never raises into the receiving flow.
"""
import logging
import re
from datetime import timedelta

from django.utils import timezone

from core.utils import log_activity

from .models import Bill, BillLine

logger = logging.getLogger(__name__)


def payment_terms_days(terms):
    """Days until payment for vendor terms such as "net30", "Net 45" or
    "due on receipt"; None when the terms say nothing usable."""
    text = (terms or "").strip().lower()
    if not text:
        return None
    if "receipt" in text or text in ("cod", "cash"):
        return 0
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def existing_bill(po):
    return po.bills.exclude(status="cancelled").first()


def create_bill_from_po(po, bill_date=None, due_date=None, bill_number=""):
    """Create a vendor bill copying the purchase order's lines and total.
    The due date defaults from the vendor's payment terms."""
    bill_date = bill_date or timezone.localdate()
    if due_date is None:
        days = payment_terms_days(po.vendor.payment_terms)
        due_date = bill_date + timedelta(days=days) if days is not None else None

    bill = Bill.objects.create(
        company=po.vendor.company,
        purchase_order=po,
        vendor=po.vendor,
        bill_number=bill_number,
        bill_date=bill_date,
        due_date=due_date,
        total_amount=po.total_amount,
    )
    for po_item in po.items.select_related("item"):
        BillLine.objects.create(
            bill=bill,
            item=po_item.item,
            description=po_item.item.name,
            quantity=po_item.quantity,
            unit_price=po_item.unit_price,
            amount=po_item.total_price,
        )
    return bill


def push_bill_to_quickbooks(bill):
    """Mirror to QuickBooks (Level 1 sync); failures land in sync errors."""
    from quickbooks.push import get_active_connection, safe_push

    connection = get_active_connection(bill.company)
    if connection:
        safe_push(connection, "bill", bill)


def auto_bill_on_receipt_enabled(company):
    from accounting.models import AccountingSettings

    settings_obj = AccountingSettings.objects.filter(company=company).only("auto_bill_on_receipt").first()
    return bool(settings_obj and settings_obj.auto_bill_on_receipt)


def on_goods_received(po, receipt, user):
    """Ledger and billing follow-up for a goods receipt. Returns the bill it
    created, if any. Never raises."""
    from accounting.auto_posting import queue_auto_post

    company = po.vendor.company
    queue_auto_post("goods_receipt", company, receipt.id, user)

    try:
        if not auto_bill_on_receipt_enabled(company) or existing_bill(po) or po.total_amount <= 0:
            return None
        bill = create_bill_from_po(po)
    except Exception:
        logger.exception("Could not auto-create vendor bill for PO-%s", po.id)
        return None

    push_bill_to_quickbooks(bill)
    queue_auto_post("vendor_bill", bill.company, bill.id, user)
    try:
        log_activity(user, "Procurement", "Auto Bill on Receipt",
                     f"Created bill {bill.id} for PO #{po.id} on goods receipt (total: {bill.total_amount})")
    except Exception:
        logger.warning("Could not log auto bill for PO-%s", po.id, exc_info=True)
    return bill
