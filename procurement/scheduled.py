"""Scheduled purchase orders: place them automatically on their date.

`run_due_scheduled_orders()` is called by the in-process scheduler
(procurement/scheduler.py) and by `manage.py place_scheduled_orders`. Each due
schedule is claimed with an atomic UPDATE, so several web workers (or a cron
job running alongside them) can never place the same order twice.

Placing follows the normal ordering rules: the PO is raised at the vendor's
price and set to "ordered" (vendor email drafted) when the person who scheduled
it may approve that amount — admins, or anyone within their auto-approve
limit. Otherwise it is raised as "pending" for an admin to approve.
"""
import calendar
import logging
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import PurchaseOrder, PurchaseOrderItem, ScheduledPurchaseOrder, VendorPriceList

logger = logging.getLogger(__name__)

# A schedule left in "processing" this long was interrupted mid-run (its
# placement transaction rolled back), so it is safe to pick up again.
STALE_PROCESSING = timedelta(minutes=30)


def resolve_vendor_price(company, item, vendor=None):
    """(vendor, unit price) to buy `item` from. A chosen vendor uses its quoted
    price, falling back to the item's catalogue cost; otherwise the cheapest
    active vendor quote on file is used."""
    quotes = VendorPriceList.objects.filter(
        item=item, is_active=True, vendor__company=company
    ).select_related("vendor")
    if vendor is not None:
        quote = quotes.filter(vendor=vendor).first()
        return vendor, Decimal(str(quote.unit_price if quote else (item.purchase_cost or 0)))
    quote = quotes.order_by("unit_price").first()
    if quote is None:
        raise ValidationError(
            f"No vendor price is on file for {item.name}. Assign a vendor to it in "
            "Procurement → Catalog, or choose a vendor for this schedule."
        )
    return quote.vendor, Decimal(str(quote.unit_price))


def next_occurrence(current, repeat, anchor_day=None):
    if repeat == "weekly":
        return current + timedelta(days=7)
    if repeat == "monthly":
        year = current.year + current.month // 12
        month = current.month % 12 + 1
        day = min(anchor_day or current.day, calendar.monthrange(year, month)[1])
        return current.replace(year=year, month=month, day=day)
    return None


def run_due_scheduled_orders(today=None):
    """Place every schedule due on or before `today`. Returns the schedules handled."""
    today = today or timezone.localdate()
    ScheduledPurchaseOrder.objects.filter(
        status="processing", updated_at__lt=timezone.now() - STALE_PROCESSING
    ).update(status="scheduled")

    due_ids = list(
        ScheduledPurchaseOrder.objects.filter(status="scheduled", scheduled_date__lte=today)
        .values_list("id", flat=True)
    )
    handled = []
    for schedule_id in due_ids:
        if claim(schedule_id, from_statuses=("scheduled",)):
            handled.append(place_scheduled_order(ScheduledPurchaseOrder.objects.get(pk=schedule_id), today))
    return handled


def claim(schedule_id, from_statuses):
    """Atomically move a schedule to "processing". Only one caller can win."""
    return ScheduledPurchaseOrder.objects.filter(pk=schedule_id, status__in=from_statuses).update(
        status="processing", updated_at=timezone.now()
    ) == 1


def place_scheduled_order(schedule, today=None):
    """Raise and place the PO for a claimed schedule. Never raises; a failure is
    recorded on the schedule (status "failed") and the store is notified."""
    today = today or timezone.localdate()
    now = timezone.now()
    try:
        with transaction.atomic():
            po, ordered = _raise_purchase_order(schedule)
            schedule.purchase_order = po
            schedule.runs_count += 1
            schedule.last_run_at = now
            schedule.last_message = (
                f"PO-{po.id:04d} placed with {po.vendor.name} for {po.total_amount}."
                if ordered else
                f"PO-{po.id:04d} raised for {po.total_amount} and sent to an admin for approval "
                "(above the scheduler's approval limit)."
            )
            upcoming = next_occurrence(schedule.scheduled_date, schedule.repeat, schedule.anchor_day)
            while upcoming and upcoming <= today:
                upcoming = next_occurrence(upcoming, schedule.repeat, schedule.anchor_day)
            if upcoming:
                schedule.scheduled_date, schedule.status = upcoming, "scheduled"
            else:
                schedule.status = "placed"
            schedule.save()
    except Exception as error:
        if isinstance(error, ValidationError):
            message = " ".join(error.messages)
        else:
            logger.exception("Scheduled purchase order #%s failed", schedule.pk)
            message = f"Unexpected error: {error}"
        ScheduledPurchaseOrder.objects.filter(pk=schedule.pk).update(
            status="failed", last_message=message, last_run_at=now, updated_at=now
        )
        _notify(schedule, "store", f"Scheduled order for {schedule.quantity:g} {schedule.item.name} failed: {message}")
        schedule.refresh_from_db()
        return schedule

    _after_placement(schedule, po, ordered)
    return schedule


def _raise_purchase_order(schedule):
    item, company = schedule.item, schedule.company
    if item.company_id != company.id:
        raise ValidationError("Item does not belong to this company.")
    if schedule.quantity <= 0:
        raise ValidationError("Quantity must be greater than zero.")

    vendor, unit_price = resolve_vendor_price(company, item, schedule.vendor)
    po = PurchaseOrder.objects.create(
        vendor=vendor,
        status="draft",
        notes=f"Placed automatically from scheduled order #{schedule.id}. {schedule.notes}".strip(),
    )
    PurchaseOrderItem.objects.create(
        purchase_order=po, item=item, quantity=schedule.quantity, unit_price=unit_price
    )
    po.refresh_from_db()

    user = schedule.created_by
    limit = Decimal(str(getattr(user, "auto_approve_limit", 0) or 0))
    ordered = bool(user) and (user.role == "admin" or (limit > 0 and po.total_amount <= limit))
    po.status = "ordered" if ordered else "pending"
    po.save(update_fields=["status"])
    return po, ordered


def _after_placement(schedule, po, ordered):
    """Side effects after the placement committed. None of them are fatal."""
    from core.utils import log_activity

    if ordered:
        try:
            from .emails import draft_for_orders
            draft_for_orders([po])
        except Exception:
            logger.warning("Could not draft vendor email for scheduled PO-%s", po.id, exc_info=True)
        _notify(schedule, "store",
                f"Scheduled order placed: PO-{po.id:04d} for {schedule.quantity:g} {schedule.item.name} "
                f"from {po.vendor.name}.", po)
    else:
        _notify(schedule, "admin",
                f"Scheduled PO-{po.id:04d} ({schedule.quantity:g} {schedule.item.name}, {po.total_amount}) "
                "needs your approval.", po)
    if schedule.created_by:
        try:
            log_activity(schedule.created_by, "Procurement", "Scheduled Order Placed", schedule.last_message)
        except Exception:
            logger.warning("Could not log scheduled order #%s", schedule.pk, exc_info=True)


def _notify(schedule, role, message, po=None):
    try:
        from core.utils import send_notification
        send_notification(
            role, message,
            related_id=po.id if po else schedule.id,
            related_type="purchase_order" if po else "scheduled_purchase_order",
            company=schedule.company, module="procurement",
        )
    except Exception:
        logger.warning("Could not notify about scheduled order #%s", schedule.pk, exc_info=True)
