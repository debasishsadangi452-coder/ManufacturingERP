"""Quotation logic: pricing a quote and converting it into a sales order.

A quotation is a priced offer. Converting it creates a normal SalesOrder (status
"pending") carrying the quoted lines, discount and tax. The quoted price is kept
unless the caller passes an explicit price for a line — so by default the order
is billed at exactly what was quoted.
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from inventory.models import Item
from .models import (
    Customer, Quotation, QuotationItem, SalesOrder, SalesOrderDiscount, SalesOrderItem,
)
from .pricing import money, order_adjustments

DEFAULT_VALIDITY_DAYS = 30


def _decimal(value, field, *, allow_blank=False):
    if value in (None, ""):
        if allow_blank:
            return None
        raise ValidationError({field: "A number is required."})
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError({field: "Must be a number."})


def _clean_lines(company, lines):
    """Validate raw line dicts -> [(item, quantity, unit_price, list_price)]."""
    if not lines:
        raise ValidationError({"items": "Add at least one product to the quotation."})
    cleaned = []
    for raw in lines:
        item = Item.objects.filter(pk=raw.get("item"), company=company).first()
        if item is None:
            raise ValidationError({"items": f"Item {raw.get('item')} does not belong to this company."})
        if item.category != "finished_good":
            raise ValidationError({"items": f"Only finished goods can be quoted. '{item.name}' is not one."})
        try:
            quantity = float(raw.get("quantity"))
        except (TypeError, ValueError):
            raise ValidationError({"items": f"Invalid quantity for '{item.name}'."})
        if quantity <= 0:
            raise ValidationError({"items": f"Quantity for '{item.name}' must be greater than zero."})
        list_price = item.selling_price or Decimal("0")
        price = _decimal(raw.get("unit_price"), "items", allow_blank=True)
        price = list_price if price is None else price
        if price < 0:
            raise ValidationError({"items": f"Price for '{item.name}' cannot be negative."})
        cleaned.append((item, quantity, price, list_price))
    return cleaned


def _totals(quotation, cleaned, discount_percent, tax):
    subtotal = sum((price * Decimal(str(qty)) for _i, qty, price, _l in cleaned), Decimal("0"))
    quotation.subtotal = money(subtotal)
    quotation.discount_percent = discount_percent
    quotation.discount_amount, quotation.tax_amount, quotation.total_amount = order_adjustments(
        subtotal, [discount_percent], tax
    )


@transaction.atomic
def save_quotation(company, user, data, quotation=None):
    """Create a quotation, or replace the lines and terms of an editable one."""
    if quotation is not None and quotation.status not in Quotation.EDITABLE_STATUSES:
        raise ValidationError({"status": f"A {quotation.get_status_display().lower()} quotation can no longer be edited."})
    customer = Customer.objects.filter(pk=data.get("customer"), company=company).first()
    if customer is None:
        raise ValidationError({"customer": "Pick a customer of this company."})
    discount_percent = _decimal(data.get("discount_percent") or 0, "discount_percent")
    if not Decimal("0") <= discount_percent <= Decimal("100"):
        raise ValidationError({"discount_percent": "Discount must be between 0 and 100."})
    tax = _decimal(data.get("tax_amount") or 0, "tax_amount")
    if tax < 0:
        raise ValidationError({"tax_amount": "Tax cannot be negative."})
    cleaned = _clean_lines(company, data.get("items"))

    # The form sends an ISO date string; store a real date so it can be compared and serialised.
    raw_valid = data.get("valid_until")
    if raw_valid in (None, ""):
        valid_until = timezone.localdate() + timedelta(days=DEFAULT_VALIDITY_DAYS) if quotation is None else None
    else:
        valid_until = parse_date(str(raw_valid)) if not isinstance(raw_valid, date) else raw_valid
        if valid_until is None:
            raise ValidationError({"valid_until": "Use a date in YYYY-MM-DD format."})
    if quotation is None:
        quotation = Quotation(company=company, created_by=user)
    quotation.valid_until = valid_until
    quotation.customer = customer
    quotation.notes = data.get("notes") or ""
    quotation.terms = data.get("terms") or ""
    quotation.tax_label = (data.get("tax_label") or "")[:60]
    _totals(quotation, cleaned, discount_percent, tax)
    quotation.save()
    if not quotation.quote_number:
        quotation.quote_number = f"QUO-{quotation.pk:05d}"
        quotation.save(update_fields=["quote_number"])

    quotation.lines.all().delete()
    QuotationItem.objects.bulk_create([
        QuotationItem(quotation=quotation, item=item, quantity=qty, unit_price=price, list_price=list_price)
        for item, qty, price, list_price in cleaned
    ])
    return quotation


@transaction.atomic
def convert_to_order(quotation, prices=None, user=None):
    """Turn an open quotation into a sales order.

    `prices` optionally maps quotation line id -> new unit price; lines not
    listed keep their quoted price (the default). Returns the SalesOrder.
    """
    if quotation.converted_order_id or quotation.status == "converted":
        raise ValidationError({"status": "This quotation has already been converted to an order."})
    if quotation.status == "rejected":
        raise ValidationError({"status": "A rejected quotation cannot be converted. Reopen it as a draft first."})
    prices = {str(k): v for k, v in (prices or {}).items()}

    order = SalesOrder.objects.create(
        customer=quotation.customer,
        status="pending",
        source="other",
        customer_order_reference=quotation.quote_number,
        custom_specifications=quotation.notes,
    )
    total = Decimal("0")
    for line in quotation.lines.select_related("item"):
        price = line.unit_price
        if str(line.id) in prices and prices[str(line.id)] not in (None, ""):
            price = _decimal(prices[str(line.id)], "prices")
            if price < 0:
                raise ValidationError({"prices": f"Price for '{line.item.name}' cannot be negative."})
        so_item = SalesOrderItem.objects.create(
            sales_order=order, item=line.item, quantity=line.quantity, unit_price=price,
        )
        total += so_item.unit_price * Decimal(str(so_item.quantity))

    percents = []
    if quotation.discount_percent > 0:
        SalesOrderDiscount.objects.create(
            sales_order=order, label="Quotation discount", percent=quotation.discount_percent,
        )
        percents.append(quotation.discount_percent)
    order.subtotal = money(total)
    order.discount_amount, order.tax_amount, order.total_amount = order_adjustments(total, percents, quotation.tax_amount)
    order.tax_label = quotation.tax_label
    order.save(update_fields=["subtotal", "discount_amount", "tax_amount", "total_amount", "tax_label"])

    quotation.status = "converted"
    quotation.converted_order = order
    quotation.save(update_fields=["status", "converted_order", "updated_at"])
    return order
