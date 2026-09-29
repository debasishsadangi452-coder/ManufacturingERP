"""Auto-mirror ERP writes into QuickBooks (Level 1 integration).

Whenever a master or transactional record is saved in the ERP, we queue a
push to QuickBooks after the surrounding transaction commits. Pushes never
raise into the ERP request — failures are recorded as QuickBooksSyncError
rows and can be retried via the push endpoints.
"""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from inventory.models import Item, Stock
from procurement.models import PurchaseOrder, Vendor
from sales.models import Customer, SalesOrder, SalesOrderItem

from .push import auto_push_suppressed, get_active_connection, safe_push


def _queue_push(entity_type, instance, company):
    if auto_push_suppressed():
        return
    connection = get_active_connection(company)
    if not connection:
        return
    transaction.on_commit(lambda: safe_push(connection, entity_type, instance))


@receiver(post_save, sender=Customer, dispatch_uid="qb_push_customer")
def _customer_saved(sender, instance, **kwargs):
    _queue_push("customer", instance, instance.company)


@receiver(post_save, sender=Vendor, dispatch_uid="qb_push_vendor")
def _vendor_saved(sender, instance, **kwargs):
    _queue_push("vendor", instance, instance.company)


@receiver(post_save, sender=Item, dispatch_uid="qb_push_item")
def _item_saved(sender, instance, **kwargs):
    _queue_push("item", instance, instance.company)


@receiver(post_save, sender=SalesOrder, dispatch_uid="qb_push_sales_order")
def _sales_order_saved(sender, instance, **kwargs):
    # Draft orders come from unconfirmed email parses — never sync them to
    # QuickBooks until a human confirms (promotes them out of "draft").
    if instance.status == "draft":
        return
    _queue_push("sales_order", instance, instance.customer.company)


# Sales order lines are created after the order row itself, so re-push the
# parent order as each line lands to keep the QuickBooks estimate complete.
@receiver(post_save, sender=SalesOrderItem, dispatch_uid="qb_push_sales_order_item")
def _sales_order_item_saved(sender, instance, **kwargs):
    if instance.sales_order.status == "draft":
        return
    _queue_push("sales_order", instance.sales_order, instance.sales_order.customer.company)


# PurchaseOrderItem.save() recalculates the PO total, which saves the PO and
# re-fires this receiver — so line changes are mirrored without a separate hook.
@receiver(post_save, sender=PurchaseOrder, dispatch_uid="qb_push_purchase_order")
def _purchase_order_saved(sender, instance, **kwargs):
    # The UI creates the PO header first and adds lines afterwards, so this
    # receiver fires once on a line-less order. QuickBooks rejects a
    # PurchaseOrder with no Line ("Required parameter Line is missing", code
    # 2020), which would leave the order permanently unlinked. Wait for the
    # first line — adding it re-saves the PO and fires this receiver again.
    if not instance.items.exists():
        return
    _queue_push("purchase_order", instance, instance.vendor.company)


@receiver(post_save, sender=Stock, dispatch_uid="qb_push_stock")
def _stock_saved(sender, instance, **kwargs):
    _queue_push("item_quantity", instance.item, instance.item.company)


# ---------------------------------------------------------------------------
# Main books: payments and ERP-only journal entries (see push.py)
# ---------------------------------------------------------------------------
# Every path that records money against an invoice or bill creates one of
# these per-document payment rows (Sales, Accounts Receivable/Payable, pay from
# Procurement, Payments & Allocations), so hooking the rows covers them all.

from django.db.models.signals import pre_delete  # noqa: E402

from accounting.models import JournalEntry  # noqa: E402
from procurement.models import VendorPayment  # noqa: E402
from sales.models import CustomerPayment  # noqa: E402

from .push import journal_entry_goes_to_quickbooks, safe_delete  # noqa: E402


@receiver(post_save, sender=CustomerPayment, dispatch_uid="qb_push_customer_payment")
def _customer_payment_saved(sender, instance, created, **kwargs):
    if created:
        _queue_push("payment", instance, instance.company)


@receiver(post_save, sender=VendorPayment, dispatch_uid="qb_push_vendor_payment")
def _vendor_payment_saved(sender, instance, created, **kwargs):
    if created and instance.bill_id:
        _queue_push("bill_payment", instance, instance.company)


@receiver(post_save, sender=JournalEntry, dispatch_uid="qb_push_journal_entry")
def _journal_entry_saved(sender, instance, **kwargs):
    if instance.status == "posted" and not instance.quickbooks_id and journal_entry_goes_to_quickbooks(instance):
        _queue_push("journal_entry", instance, instance.company)


def _queue_delete(entity_type, resource, instance):
    """An ERP payment that reached QuickBooks was removed (e.g. an allocation
    was undone): remove it from QuickBooks too, once the deletion commits."""
    if auto_push_suppressed() or not instance.quickbooks_id:
        return
    connection = get_active_connection(instance.company)
    if not connection:
        return
    qb_id, token, local_id = instance.quickbooks_id, instance.quickbooks_sync_token, instance.pk
    transaction.on_commit(lambda: safe_delete(connection, entity_type, resource, qb_id, token, local_id))


@receiver(pre_delete, sender=CustomerPayment, dispatch_uid="qb_delete_customer_payment")
def _customer_payment_deleted(sender, instance, **kwargs):
    _queue_delete("payment", "payment", instance)


@receiver(pre_delete, sender=VendorPayment, dispatch_uid="qb_delete_vendor_payment")
def _vendor_payment_deleted(sender, instance, **kwargs):
    _queue_delete("bill_payment", "billpayment", instance)
