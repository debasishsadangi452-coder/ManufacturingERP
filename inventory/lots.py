"""Lot lifecycle + genealogy (P1 SQF traceability).

The chain:
    receive  → create_raw_lot()       (raw lot ← goods receipt)
    produce  → consume_lots_fifo()    (raw lots → LotConsumption)
             → create_finished_lot()  (finished lot ← production order)
    ship     → ship_lots_fifo()       (finished lots → ShipmentLot)

Genealogy queries walk these links both directions so a shipped case traces
back to its production run and the raw lots (and vendor deliveries) behind it.
"""

from django.db import transaction
from django.utils import timezone

from .models import Batch, LotConsumption


def _make_lot_code(prefix, item, when=None):
    when = when or timezone.now()
    return f"{prefix}-{item.id}-{when:%Y%m%d}-{when:%H%M%S}"


def create_raw_lot(item, warehouse, quantity, goods_receipt, company=None, lot_code=None, expiry_date=None):
    """Create a raw-material lot at goods receipt."""
    return Batch.objects.create(
        item=item,
        batch_number=lot_code or _make_lot_code("RM", item),
        quantity=quantity,
        remaining_quantity=quantity,
        expiry_date=expiry_date,
        company=company or getattr(item, "company", None),
        warehouse=warehouse,
        source="received",
        goods_receipt=goods_receipt,
    )


def create_finished_lot(item, warehouse, quantity, production_order, company=None, lot_code=None, expiry_date=None):
    """Create a finished-goods lot when a production order completes."""
    return Batch.objects.create(
        item=item,
        batch_number=lot_code or _make_lot_code("FG", item),
        quantity=quantity,
        remaining_quantity=quantity,
        expiry_date=expiry_date,
        company=company or getattr(item, "company", None),
        warehouse=warehouse,
        source="produced",
        production_order=production_order,
    )


def _available_lots(item, company=None):
    """Lots of `item` with stock left, oldest first (FIFO)."""
    qs = Batch.objects.filter(item=item, remaining_quantity__gt=0)
    if company is not None:
        qs = qs.filter(company=company)
    return qs.order_by("created_at", "id")


@transaction.atomic
def consume_lots_fifo(production_order, item, quantity, company=None):
    """Draw `quantity` of `item` from its lots FIFO, recording LotConsumption.

    Records genealogy for whatever lots exist; if lots don't cover the whole
    quantity (legacy stock created before lot tracking), the shortfall is left
    unlinked rather than blocking production — traceability is best-effort over
    historical data but complete for anything received as a lot.
    Returns the list of LotConsumption rows created.
    """
    remaining = quantity
    consumptions = []
    for lot in _available_lots(item, company).select_for_update():
        if remaining <= 0:
            break
        take = min(lot.remaining_quantity, remaining)
        lot.remaining_quantity -= take
        lot.save(update_fields=["remaining_quantity"])
        consumptions.append(
            LotConsumption.objects.create(
                production_order=production_order, lot=lot, quantity=take
            )
        )
        remaining -= take
    return consumptions


@transaction.atomic
def ship_lots_fifo(shipment, item, quantity, company=None):
    """Draw `quantity` of finished `item` from its lots FIFO onto a shipment,
    recording ShipmentLot links. Same best-effort semantics as consumption."""
    from sales.models import ShipmentLot

    remaining = quantity
    shipped = []
    for lot in _available_lots(item, company).filter(source="produced").select_for_update():
        if remaining <= 0:
            break
        take = min(lot.remaining_quantity, remaining)
        lot.remaining_quantity -= take
        lot.save(update_fields=["remaining_quantity"])
        shipped.append(
            ShipmentLot.objects.create(shipment=shipment, lot=lot, quantity=take)
        )
        remaining -= take
    return shipped


# --- Genealogy queries -------------------------------------------------------

def trace_backward(lot):
    """From a finished lot, return the raw lots (and their goods receipts) that
    went into it, via its production order's LotConsumption rows."""
    if not lot.production_order_id:
        return []
    result = []
    for c in LotConsumption.objects.filter(
        production_order=lot.production_order
    ).select_related("lot", "lot__goods_receipt", "lot__item"):
        result.append({
            "lot_code": c.lot.batch_number,
            "item": c.lot.item.name,
            "quantity_consumed": c.quantity,
            "source": c.lot.source,
            "goods_receipt_id": c.lot.goods_receipt_id,
        })
    return result


def trace_forward(lot):
    """From a raw lot, return the finished lots it fed and the shipments those
    finished lots left on — the full downstream reach for a recall."""
    from sales.models import ShipmentLot

    finished = []
    for c in LotConsumption.objects.filter(lot=lot).select_related("production_order"):
        po = c.production_order
        finished_lots = Batch.objects.filter(production_order=po, source="produced")
        for fl in finished_lots:
            shipments = [
                {"shipment_id": sl.shipment_id, "quantity": sl.quantity,
                 "customer": sl.shipment.sales_order.customer.name}
                for sl in ShipmentLot.objects.filter(lot=fl).select_related(
                    "shipment", "shipment__sales_order", "shipment__sales_order__customer"
                )
            ]
            finished.append({
                "production_order_id": po.id,
                "finished_lot": fl.batch_number,
                "item": fl.item.name,
                "shipments": shipments,
            })
    return finished
