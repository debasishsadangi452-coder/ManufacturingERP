"""
Automatic General Ledger posting for operational events.

Sales and purchase orders are commitments and have no ledger impact. The books
move when goods or money move:

  Event (where it fires)                    Journal entry
  ----------------------------------------  ------------------------------------------------
  goods_receipt       (PO received)          Dr 1210 Raw Materials     / Cr 2050 GRNI
  vendor_bill         (bill from PO)         Dr 2050 GRNI (or 1210)    / Cr 2010 Accounts Payable
  production_completed                       Dr 1220 WIP / Cr 1210 RM, then Dr 1230 FG / Cr 1220 WIP
  sales_shipment      (SO fulfilled/shipped) Dr 5050 COGS              / Cr 1230 Finished Goods
                                             (bought-in items without a recipe: Cr 1210)
  sales_invoice       (invoice from SO)      Dr 1100 AR                / Cr 4010 Sales
  customer_payment                           Dr 1010 Bank              / Cr 1100 AR

Costing is standard material cost: an item's `purchase_cost`, and for
manufactured items the recipe's ingredient cost per finished unit.

Safety guarantees for the operational flows that call `queue_auto_post`:
  * Posting runs after the surrounding transaction commits, so it cannot roll
    back or block a sale, receipt or production run.
  * It never raises into the caller. Every attempt is recorded in
    AutoPostingLog (posted / skipped / failed) and failed rows can be retried.
  * It only runs for companies that have set up accounting (a chart of
    accounts exists) and have `auto_post_enabled` switched on.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounting.engine import post_journal_entry
from accounting.models import (
    Account, AccountingSettings, AutoPostingLog, JournalEntry, JournalEntryLine,
)

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")

# Accounts the auto-poster needs beyond the ones AR/AP already resolve. Created
# on demand under their standard header for companies seeded before they existed.
AUTO_ACCOUNTS = {
    "1210": ("Raw Materials Inventory", "Inventory", "1200"),
    "1220": ("Work-in-Progress (WIP)", "Inventory", "1200"),
    "1230": ("Finished Goods Inventory", "Inventory", "1200"),
    "2050": ("Goods Received Not Invoiced (GRNI)", "Accrued Expenses & Other Current Liabilities", "2000"),
    "5050": ("Cost of Goods Sold - Finished Goods", "Cost of Goods Sold (Raw Materials)", "5000"),
}


class SkipPosting(Exception):
    """Nothing to post for this event (already posted, zero value, ...)."""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def is_auto_posting_enabled(company):
    if company is None:
        return False
    settings_obj = AccountingSettings.objects.filter(company=company).only("auto_post_enabled").first()
    if not settings_obj or not settings_obj.auto_post_enabled:
        return False
    return Account.objects.filter(company=company).exists()


def queue_auto_post(event, company, source_id, user=None, payload=None):
    """Schedule an automatic posting for after the current transaction commits.
    Safe to call from any operational view: it never raises."""
    try:
        if not is_auto_posting_enabled(company):
            return
    except Exception:
        logger.exception("Auto-posting: could not check settings for %s #%s", event, source_id)
        return
    transaction.on_commit(
        lambda: execute_auto_post(event, company, source_id, user=user, payload=payload)
    )


def execute_auto_post(event, company, source_id, user=None, payload=None, log=None):
    """Run the handler for `event` and record the outcome. Never raises."""
    from quickbooks.push import suppress_auto_push

    payload = payload or {}
    if log is None:
        log = AutoPostingLog(company=company, event=event, source_id=source_id, payload=payload)
    else:
        log.attempts += 1
    user = user if getattr(user, "is_authenticated", False) else None

    try:
        handler = HANDLERS[event]
        # Balance bookkeeping saves (customer/vendor balances) are not
        # QuickBooks-relevant changes, so don't let them trigger pushes.
        with suppress_auto_push():
            entry = handler(company, source_id, user, payload)
        log.status, log.journal_entry, log.message = "posted", entry, f"Posted {entry.entry_number}"
    except SkipPosting as e:
        log.status, log.message = "skipped", str(e)
    except ValidationError as e:
        log.status, log.message = "failed", _error_text(e)
    except Exception as e:  # never let accounting break an operational flow
        logger.exception("Auto-posting failed for %s #%s", event, source_id)
        log.status, log.message = "failed", f"Unexpected error: {e}"

    try:
        log.save()
    except Exception:
        logger.exception("Auto-posting: could not save log for %s #%s", event, source_id)
    return log


def retry_auto_post(log, user=None):
    if log.status != "failed":
        raise ValidationError("Only failed auto-postings can be retried.")
    return execute_auto_post(log.event, log.company, log.source_id, user=user, payload=log.payload, log=log)


def grni_account_for_bill(bill):
    """The GRNI account to debit for `bill` when its goods were already
    capitalised to inventory by an auto-posted goods receipt, else None."""
    if not bill.purchase_order_id:
        return None
    from procurement.models import GoodsReceipt
    receipt_ids = GoodsReceipt.objects.filter(purchase_order_id=bill.purchase_order_id).values_list("id", flat=True)
    received = JournalEntry.objects.filter(
        company=bill.company,
        source_module="procurement.receipt",
        source_id__in=list(receipt_ids),
        status="posted",
    ).exists()
    return _account(bill.company, "2050") if received else None


# ---------------------------------------------------------------------------
# Handlers: (company, source_id, user, payload) -> posted JournalEntry
# ---------------------------------------------------------------------------

def _post_goods_receipt(company, receipt_id, user, payload):
    from procurement.models import GoodsReceipt

    receipt = GoodsReceipt.objects.select_related("purchase_order__vendor").filter(
        pk=receipt_id, purchase_order__vendor__company=company
    ).first()
    if not receipt:
        raise ValidationError(f"Goods receipt #{receipt_id} not found.")
    _skip_if_posted(company, "procurement.receipt", receipt.id)

    po = receipt.purchase_order
    amount = _money(sum(
        (poi.unit_price or Decimal("0")) * Decimal(str(poi.quantity)) for poi in po.items.all()
    ))
    if amount <= 0:
        raise SkipPosting(f"PO-{po.id} has no priced lines; nothing to capitalise.")

    return _create_and_post(
        company, user,
        date=timezone.localdate(receipt.received_at),
        reference=f"GRN-{receipt.id}",
        description=f"Goods received for PO-{po.id} from {po.vendor.name}",
        source_module="procurement.receipt",
        source_id=receipt.id,
        lines=[
            (_account(company, "1210"), amount, 0, f"Raw materials received - PO-{po.id}"),
            (_account(company, "2050"), 0, amount, f"GRNI accrual - PO-{po.id}"),
        ],
    )


def _post_vendor_bill(company, bill_id, user, payload):
    from procurement.models import Bill
    from accounting.payables import post_bill_to_ap

    bill = Bill.objects.filter(pk=bill_id, company=company).first()
    if not bill:
        raise ValidationError(f"Bill #{bill_id} not found.")
    _skip_if_posted(company, "procurement.bill", bill.id)
    if bill.total_amount <= 0:
        raise SkipPosting(f"Bill {bill.bill_number or bill.id} has no amount.")
    # post_bill_to_ap clears GRNI itself when the receipt was auto-posted.
    return post_bill_to_ap(bill.id, user, company)


def _post_production(company, order_id, user, payload):
    from production.models import ProductionOrder

    order = ProductionOrder.objects.select_related("recipe__product").filter(
        pk=order_id, recipe__product__company=company
    ).first()
    if not order:
        raise ValidationError(f"Production order #{order_id} not found.")
    _skip_if_posted(company, "production.order", order.id)

    cost = _money(sum(
        Decimal(str(required)) * (ing.item.purchase_cost or Decimal("0"))
        for ing, required in order.recipe.material_requirements(order.quantity)
    ))
    if cost <= 0:
        raise SkipPosting(
            f"Recipe ingredients for '{order.recipe.product.name}' have no purchase cost; nothing to post."
        )

    wip, product = _account(company, "1220"), order.recipe.product.name
    return _create_and_post(
        company, user,
        date=timezone.localdate(order.end_time) if order.end_time else timezone.localdate(),
        reference=f"PROD-{order.id}",
        description=f"Production #{order.id}: {order.quantity} x {product}",
        source_module="production.order",
        source_id=order.id,
        lines=[
            (wip, cost, 0, f"Materials issued to production #{order.id}"),
            (_account(company, "1210"), 0, cost, f"Raw materials consumed - production #{order.id}"),
            (_account(company, "1230"), cost, 0, f"Finished goods completed - {product}"),
            (wip, 0, cost, f"WIP relieved - production #{order.id}"),
        ],
    )


def _post_sales_shipment(company, order_id, user, payload):
    from inventory.models import Item
    from sales.models import SalesOrder

    order = SalesOrder.objects.select_related("customer").filter(pk=order_id, customer__company=company).first()
    if not order:
        raise ValidationError(f"Sales order #{order_id} not found.")

    credits = {}  # inventory account code -> amount
    for line in payload.get("lines", []):
        item = Item.objects.filter(pk=line.get("item_id")).first()
        qty = Decimal(str(line.get("quantity") or 0))
        if not item or qty <= 0:
            continue
        unit_cost, inventory_code = standard_unit_cost(item)
        amount = _money(unit_cost * qty)
        if amount > 0:
            credits[inventory_code] = credits.get(inventory_code, Decimal("0")) + amount

    total = sum(credits.values(), Decimal("0"))
    if total <= 0:
        raise SkipPosting(f"Shipped items on SO-{order.id} have no standard cost; nothing to post.")

    lines = [(_account(company, "5050"), total, 0, f"Cost of goods shipped - SO-{order.id}")]
    for code, amount in sorted(credits.items()):
        lines.append((_account(company, code), 0, amount, f"Inventory relieved - SO-{order.id}"))
    return _create_and_post(
        company, user,
        date=timezone.localdate(),
        reference=f"SHIP-SO-{order.id}",
        description=f"Cost of goods sold for SO-{order.id} ({order.customer.name})",
        source_module="sales.shipment",
        source_id=order.id,
        lines=lines,
    )


def _post_sales_invoice(company, invoice_id, user, payload):
    from sales.models import Invoice
    from accounting.receivables import post_invoice_to_ar

    invoice = Invoice.objects.filter(pk=invoice_id, company=company).first()
    if not invoice:
        raise ValidationError(f"Invoice #{invoice_id} not found.")
    _skip_if_posted(company, "sales.invoice", invoice.id)
    if invoice.total_amount <= 0:
        raise SkipPosting(f"INV-{invoice.id} has no amount.")
    return post_invoice_to_ar(invoice.id, user, company)


def _post_customer_payment(company, payment_id, user, payload):
    from sales.models import CustomerPayment
    from accounting.receivables import get_ar_account, get_bank_account, sync_customer_balance

    payment = CustomerPayment.objects.select_related("customer", "invoice").filter(
        pk=payment_id, company=company
    ).first()
    if not payment:
        raise ValidationError(f"Customer payment #{payment_id} not found.")
    _skip_if_posted(company, "sales.payment", payment.id)

    inv_ref = f"INV-{payment.invoice_id}" if payment.invoice_id else "on account"
    entry = _create_and_post(
        company, user,
        date=payment.payment_date,
        reference=payment.reference or f"PMT-{payment.id}",
        description=f"Customer payment from {payment.customer.name} applied to {inv_ref} ({payment.method})",
        source_module="sales.payment",
        source_id=payment.id,
        lines=[
            (get_bank_account(company), payment.amount, 0, f"Payment received - {payment.customer.name}"),
            (get_ar_account(company), 0, payment.amount, f"AR reduction - {inv_ref}"),
        ],
    )
    sync_customer_balance(payment.customer)
    return entry


HANDLERS = {
    "goods_receipt": _post_goods_receipt,
    "vendor_bill": _post_vendor_bill,
    "production_completed": _post_production,
    "sales_shipment": _post_sales_shipment,
    "sales_invoice": _post_sales_invoice,
    "customer_payment": _post_customer_payment,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def standard_unit_cost(item):
    """(unit cost, inventory account code) for a finished unit of `item`.
    Manufactured items cost their recipe's ingredients per finished unit and sit
    in Finished Goods; bought-in items use purchase cost from Raw Materials."""
    recipe = item.recipes.prefetch_related("recipeingredient_set__item").first()
    if recipe:
        batch_cost = sum(
            Decimal(str(ing.quantity)) * (ing.item.purchase_cost or Decimal("0"))
            for ing in recipe.recipeingredient_set.all()
        )
        batch_size = Decimal(str(recipe.batch_size or 1))
        return batch_cost / batch_size, "1230"
    return item.purchase_cost or Decimal("0"), "1210"


def _create_and_post(company, user, *, date, reference, description, source_module, source_id, lines):
    with transaction.atomic():
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=date,
            reference=reference,
            description=description,
            source_module=source_module,
            source_id=source_id,
            created_by=user,
            status="draft",
        )
        for number, (account, debit, credit, text) in enumerate(lines, start=1):
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=account,
                line_number=number,
                debit=_money(debit),
                credit=_money(credit),
                description=text,
            )
        return post_journal_entry(entry.id, user, company=company)


def _skip_if_posted(company, source_module, source_id):
    existing = JournalEntry.objects.filter(
        company=company, source_module=source_module, source_id=source_id, status="posted"
    ).first()
    if existing:
        raise SkipPosting(f"Already posted as {existing.entry_number}.")


def _account(company, code):
    """Active leaf account `code`, creating the standard auto-posting accounts
    under their header when a company's chart predates them."""
    acc = Account.objects.filter(company=company, code=code).first()
    if acc is None and code in AUTO_ACCOUNTS:
        from accounting.seeds import ensure_account_types
        name, type_name, parent_code = AUTO_ACCOUNTS[code]
        acc = Account.objects.create(
            company=company,
            code=code,
            name=name,
            account_type=ensure_account_types()[type_name],
            parent=Account.objects.filter(company=company, code=parent_code).first(),
            description="Created automatically for GL auto-posting",
            is_active=True,
        )
    if acc is None or not acc.is_active or acc.is_header:
        raise ValidationError(
            f"Account {code} is missing, inactive or a header account. "
            "Fix it in the Chart of Accounts, then retry."
        )
    return acc


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _error_text(error):
    if hasattr(error, "message_dict"):
        return "; ".join(f"{k}: {' '.join(v)}" for k, v in error.message_dict.items())
    return " ".join(str(m) for m in error.messages)
