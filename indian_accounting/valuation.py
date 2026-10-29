"""
Perpetual inventory valuation for the Indian ledger (weighted average, with the ERP's standard cost as fallback).

Every receipt/issue is an ``InventoryMovement`` tied to the voucher that booked it, so reversing the voucher voids
its movements and the average re-computes from what remains. Stock value here always reconciles to the stock
ledgers because both are written by the same handler in the same transaction.
"""
from decimal import Decimal

from django.db.models import Sum

from .models import InventoryMovement

ZERO = Decimal("0.00")
Q4 = Decimal("0.0001")


def on_hand(company, item):
    agg = InventoryMovement.objects.filter(company=company, item=item, voided=False).aggregate(q=Sum("qty"), v=Sum("value"))
    return (agg["q"] or Decimal("0")), (agg["v"] or ZERO)


def average_cost(company, item):
    """Weighted-average unit cost, or None when there is no positive stock/value to average."""
    q, v = on_hand(company, item)
    return (v / q).quantize(Q4) if q > 0 and v > 0 else None


def issue_value(company, item, qty, fallback_unit):
    """Value of issuing ``qty``: the on-hand part at the average, any shortfall at ``fallback_unit``."""
    qty = Decimal(str(qty))
    q, v = on_hand(company, item)
    if q > 0 and v > 0:
        avg = v / q
        covered = min(qty, q)
        return (covered * avg + (qty - covered) * Decimal(str(fallback_unit))).quantize(ZERO)
    return (qty * Decimal(str(fallback_unit))).quantize(ZERO)


def record(company, item, qty, value, kind, voucher, date):
    """qty/value are signed: receipts positive, issues negative."""
    return InventoryMovement.objects.create(
        company=company, item=item, qty=Decimal(str(qty)), value=Decimal(str(value)).quantize(ZERO),
        kind=kind, voucher=voucher, movement_date=date)


def void_for_voucher(voucher):
    InventoryMovement.objects.filter(voucher=voucher, voided=False).update(voided=True)


def stock_report(company):
    rows = []
    ids = InventoryMovement.objects.filter(company=company, voided=False).values_list("item_id", flat=True).distinct()
    from inventory.models import Item
    for item in Item.objects.filter(pk__in=ids):
        q, v = on_hand(company, item)
        rows.append({"item_id": item.id, "item": item.name, "qty": q, "value": v,
                     "average_cost": (v / q).quantize(Q4) if q > 0 else None})
    return rows
