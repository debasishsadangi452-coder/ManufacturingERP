"""Finished-goods fulfilment services (Track B, TB-06 / TB-10 / TB-11).

Availability (TB-20 "FG Availability"):
    sellable FG = finished-goods stock in the company's warehouses
                  - quantity allocated to customer orders and not yet dispatched
Finished-goods stock only grows when QA accepts output (quality.services), so
QA-pending, rejected and quarantined units are never part of it.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventory.models import Stock, Warehouse
from inventory.services import decrease_stock

from .models import Dispatch, DispatchLine, FGAllocation

EPS = 1e-6
OPEN_DISPATCH = ("picking", "picked", "packed", "staged", "prepared", "verified")


def _fmt(v):
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def _company(order):
    return order.customer.company


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def physical_fg(item, company):
    return Stock.objects.filter(item=item, warehouse__company=company).aggregate(
        t=Sum("quantity"))["t"] or 0


def allocated_open(item, company, exclude_order=None, only_order=None):
    qs = FGAllocation.objects.filter(item=item, company=company, status="active")
    if exclude_order is not None:
        qs = qs.exclude(sales_order=exclude_order)
    if only_order is not None:
        qs = qs.filter(sales_order=only_order)
    return sum(a.open_quantity for a in qs)


def free_fg(item, company):
    """QA-approved FG not allocated to any order."""
    return max(physical_fg(item, company) - allocated_open(item, company), 0)


def available_for_order(item, company, order=None):
    """FG this order may ship: free stock plus what is allocated to it."""
    if order is None:
        return free_fg(item, company)
    return max(physical_fg(item, company) - allocated_open(item, company, exclude_order=order), 0)


def fg_inventory(company):
    """TB-05 visibility: produced / QA buckets / allocated / available / dispatched per FG item."""
    from inventory.models import Batch, Item
    from quality.models import QualityCheck

    rows = []
    items = Item.objects.filter(company=company, category="finished_good").order_by("name")
    for item in items:
        lots = Batch.objects.filter(item=item, source="produced")
        checks = QualityCheck.objects.filter(production_order__recipe__product=item, inspection_type="production")
        produced = sum(o.produced_quantity for o in item.recipes.first().productionorder_set.all()) \
            if item.recipes.exists() else 0
        pending = sum(c.pending_quantity for c in checks.filter(status="pending"))
        agg = checks.aggregate(a=Sum("accepted_quantity"), r=Sum("rejected_quantity"),
                               q=Sum("quarantined_quantity"), w=Sum("rework_quantity"))
        quarantine_now = lots.filter(qa_status="quarantine").aggregate(t=Sum("remaining_quantity"))["t"] or 0
        rework_now = lots.filter(qa_status="rework").aggregate(t=Sum("remaining_quantity"))["t"] or 0
        dispatched = DispatchLine.objects.filter(
            item=item, dispatch__status__in=("dispatched", "delivered")).aggregate(t=Sum("quantity"))["t"] or 0
        physical = physical_fg(item, company)
        allocated = allocated_open(item, company)
        rows.append({
            "item_id": item.id,
            "item": item.name,
            "unit": item.unit,
            "produced": produced,
            "qa_pending": pending,
            "qa_approved": agg["a"] or 0,
            "qa_rejected": agg["r"] or 0,
            "qa_quarantine": quarantine_now,
            "qa_rework": rework_now,
            "on_hand": physical,
            "allocated": allocated,
            "available": max(physical - allocated, 0),
            "dispatched": dispatched,
            "warehouses": [
                {"warehouse": s.warehouse.name, "quantity": s.quantity}
                for s in Stock.objects.filter(item=item, warehouse__company=company).select_related("warehouse")
            ],
        })
    return rows


# ---------------------------------------------------------------------------
# Allocation (TB-06)
# ---------------------------------------------------------------------------

def _line_open_need(order_item):
    allocated = sum(a.open_quantity for a in order_item.fg_allocations.filter(status="active"))
    return max(order_item.quantity - order_item.shipped_quantity - allocated, 0)


@transaction.atomic
def allocate(order, user=None, lines=None):
    """Allocate free, QA-approved FG to the order's open lines.

    `lines`: optional [{"order_item_id", "quantity"}]; default allocates as much
    of every open line as stock allows. Returns (allocations, messages).
    """
    if order.status in ("cancelled", "draft", "delivered"):
        raise ValidationError(f"SO-{order.id} is {order.status}; nothing can be allocated.")
    company = _company(order)
    wanted = {int(l["order_item_id"]): float(l["quantity"]) for l in (lines or []) if l.get("quantity") not in (None, "")}
    created, messages = [], []
    for order_item in order.salesorderitem_set.select_related("item").select_for_update():
        need = _line_open_need(order_item)
        if wanted:
            if order_item.id not in wanted:
                continue
            if wanted[order_item.id] > need + EPS:
                raise ValidationError(
                    f"Cannot allocate {_fmt(wanted[order_item.id])} of {order_item.item.name}: "
                    f"only {_fmt(need)} still needs allocation."
                )
            need = wanted[order_item.id]
        if need <= EPS:
            continue
        free = free_fg(order_item.item, company)
        take = min(need, free)
        if take <= EPS:
            messages.append(f"{order_item.item.name}: no QA-approved stock free to allocate (needs {_fmt(need)}).")
            continue
        if wanted and take + EPS < need:
            raise ValidationError(
                f"Only {_fmt(free)} QA-approved {order_item.item.name} is free; cannot allocate {_fmt(need)}."
            )
        created.append(FGAllocation.objects.create(
            company=company, sales_order=order, sales_order_item=order_item, item=order_item.item,
            quantity=take, created_by=user if getattr(user, "is_authenticated", False) else None,
        ))
        if take + EPS < need:
            messages.append(f"{order_item.item.name}: allocated {_fmt(take)} of {_fmt(need)}; "
                            f"{_fmt(need - take)} awaits production/QA.")
    return created, messages


@transaction.atomic
def release_allocation(allocation, user=None):
    if allocation.status != "active":
        raise ValidationError("Allocation is not active.")
    on_dispatch = DispatchLine.objects.filter(
        sales_order_item=allocation.sales_order_item, dispatch__status__in=OPEN_DISPATCH,
    ).aggregate(t=Sum("requested_quantity"))["t"] or 0
    remaining_after = sum(
        a.open_quantity for a in allocation.sales_order_item.fg_allocations.filter(status="active")
        if a.id != allocation.id
    )
    if on_dispatch > remaining_after + EPS:
        raise ValidationError("This allocation is on an open pick list; cancel the dispatch first.")
    allocation.status = "released"
    allocation.released_at = timezone.now()
    allocation.save(update_fields=["status", "released_at"])
    return allocation


def consume_allocations(order_item, quantity):
    """Mark `quantity` of the line's allocations as dispatched (FIFO)."""
    remaining = quantity
    for alloc in order_item.fg_allocations.filter(status="active").order_by("created_at", "id"):
        if remaining <= EPS:
            break
        take = min(alloc.open_quantity, remaining)
        alloc.dispatched_quantity += take
        if alloc.dispatched_quantity >= alloc.quantity - EPS:
            alloc.status = "dispatched"
        alloc.save(update_fields=["dispatched_quantity", "status"])
        remaining -= take
    return quantity - remaining


# ---------------------------------------------------------------------------
# Dispatch workflow (TB-11)
# ---------------------------------------------------------------------------

def _on_open_dispatches(order_item, exclude=None):
    qs = DispatchLine.objects.filter(sales_order_item=order_item, dispatch__status__in=OPEN_DISPATCH)
    if exclude is not None:
        qs = qs.exclude(dispatch=exclude)
    return qs.aggregate(t=Sum("requested_quantity"))["t"] or 0


def pickable(order_item):
    allocated = sum(a.open_quantity for a in order_item.fg_allocations.filter(status="active"))
    return max(allocated - _on_open_dispatches(order_item), 0)


@transaction.atomic
def create_dispatch(order, user=None, lines=None, warehouse=None):
    """Create a pick list from allocated FG. `lines`: [{"order_item_id", "quantity"}]."""
    if order.status in ("cancelled", "draft"):
        raise ValidationError(f"SO-{order.id} is {order.status}.")
    company = _company(order)
    wanted = {int(l["order_item_id"]): float(l["quantity"]) for l in (lines or []) if l.get("quantity") not in (None, "")}
    plan = []
    for order_item in order.salesorderitem_set.select_related("item"):
        can = pickable(order_item)
        qty = wanted.get(order_item.id, can) if wanted else can
        if wanted and order_item.id not in wanted:
            continue
        if qty > can + EPS:
            raise ValidationError(
                f"Cannot pick {_fmt(qty)} of {order_item.item.name}: only {_fmt(can)} allocated and not yet on a pick list."
            )
        if qty > EPS:
            plan.append((order_item, qty))
    if not plan:
        raise ValidationError("Nothing allocated to pick. Allocate QA-approved finished goods first.")
    if warehouse is None:
        warehouse = (
            Stock.objects.filter(item=plan[0][0].item, warehouse__company=company, quantity__gt=0)
            .order_by("-quantity").values_list("warehouse", flat=True).first()
        )
        warehouse = Warehouse.objects.filter(pk=warehouse).first() if warehouse else \
            Warehouse.objects.filter(company=company).first()
    customer = order.customer
    dispatch = Dispatch.objects.create(
        company=company, sales_order=order, warehouse=warehouse, status="picking",
        ship_to_name=customer.name, ship_to_address=customer.address or "",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    for order_item, qty in plan:
        DispatchLine.objects.create(
            dispatch=dispatch, sales_order_item=order_item, item=order_item.item,
            requested_quantity=qty, quantity=0, unit_price=order_item.unit_price,
        )
    return dispatch


def _advance(dispatch, to_status):
    if dispatch.status in ("cancelled", "dispatched", "delivered"):
        raise ValidationError(f"{dispatch} is {dispatch.get_status_display()}.")
    flow = Dispatch.FLOW
    if flow.index(to_status) != flow.index(dispatch.status) + 1:
        raise ValidationError(
            f"{dispatch} is {dispatch.get_status_display()}; the next step is "
            f"{dict(Dispatch.STATUS_CHOICES)[flow[flow.index(dispatch.status) + 1]]}."
        )


@transaction.atomic
def pick(dispatch, lines=None, user=None):
    _advance(dispatch, "picked")
    picked = {int(l["line_id"]): float(l["quantity"]) for l in (lines or []) if l.get("quantity") not in (None, "")}
    any_picked = False
    for line in dispatch.lines.all():
        qty = picked.get(line.id, line.requested_quantity)
        if qty < 0 or qty > line.requested_quantity + EPS:
            raise ValidationError(f"Picked quantity for {line.item.name} must be between 0 and {_fmt(line.requested_quantity)}.")
        line.quantity = qty
        line.save(update_fields=["quantity"])
        any_picked = any_picked or qty > EPS
    if not any_picked:
        raise ValidationError("Nothing was picked.")
    dispatch.status, dispatch.picked_at = "picked", timezone.now()
    dispatch.save(update_fields=["status", "picked_at"])
    return dispatch


@transaction.atomic
def pack(dispatch, packages=None, gross_weight=None, user=None):
    _advance(dispatch, "packed")
    dispatch.lines.filter(quantity__gt=0).update(packed=True)
    if packages not in (None, ""):
        dispatch.packages = int(packages)
    if gross_weight not in (None, ""):
        dispatch.gross_weight = str(gross_weight)
    dispatch.status, dispatch.packed_at = "packed", timezone.now()
    dispatch.save()
    return dispatch


@transaction.atomic
def stage(dispatch, user=None):
    _advance(dispatch, "staged")
    dispatch.status, dispatch.staged_at = "staged", timezone.now()
    dispatch.save(update_fields=["status", "staged_at"])
    return dispatch


LOGISTICS_FIELDS = (
    "shipment_mode", "carrier", "shipment_reference", "vehicle_number", "driver_name",
    "transport_details", "freight_amount", "ship_to_name", "ship_to_address", "expected_delivery",
    "packages", "gross_weight",
)


@transaction.atomic
def prepare_shipment(dispatch, data, user=None):
    _advance(dispatch, "prepared")
    for field in LOGISTICS_FIELDS:
        if field in data and data[field] not in (None,):
            setattr(dispatch, field, data[field])
    modes = dict(Dispatch.MODE_CHOICES)
    if dispatch.shipment_mode not in modes:
        raise ValidationError({"shipment_mode": f"Choose one of: {', '.join(modes)}."})
    if dispatch.shipment_mode != "customer_pickup" and not dispatch.carrier:
        raise ValidationError({"carrier": "Carrier is required for this shipment mode."})
    dispatch.status, dispatch.prepared_at = "prepared", timezone.now()
    dispatch.full_clean(exclude=["company", "sales_order", "warehouse", "shipment", "invoice", "created_by", "verified_by"])
    dispatch.save()
    return dispatch


@transaction.atomic
def verify(dispatch, user=None):
    _advance(dispatch, "verified")
    company = dispatch.company
    for line in dispatch.lines.select_related("item", "sales_order_item"):
        if line.quantity <= EPS:
            continue
        own = allocated_open(line.item, company, only_order=dispatch.sales_order)
        if own + EPS < line.quantity:
            raise ValidationError(f"{line.item.name}: only {_fmt(own)} allocated to SO-{dispatch.sales_order_id}.")
        if physical_fg(line.item, company) + EPS < line.quantity:
            raise ValidationError(f"{line.item.name}: only {_fmt(physical_fg(line.item, company))} in stock.")
    dispatch.status, dispatch.verified_at = "verified", timezone.now()
    dispatch.verified_by = user if getattr(user, "is_authenticated", False) else None
    dispatch.save(update_fields=["status", "verified_at", "verified_by"])
    return dispatch


@transaction.atomic
def confirm_dispatch(dispatch, user=None):
    """Goods leave: stock, allocations, order line shipped quantities, lot
    genealogy, COGS posting and (per settings) the invoice."""
    from inventory.lots import ship_lots_fifo
    from production.models import ManufacturingSettings
    from sales.models import Shipment

    dispatch = Dispatch.objects.select_for_update().get(pk=dispatch.pk)
    _advance(dispatch, "dispatched")
    order = dispatch.sales_order
    company = dispatch.company
    reference = f"Dispatch #{dispatch.id} ({dispatch.dispatch_number}) SO#{order.id}"
    warehouse = dispatch.warehouse or Warehouse.objects.filter(company=company).first()
    shipment = Shipment.objects.create(
        sales_order=order, warehouse=warehouse, status="in-transit", progress=10,
        driver=dispatch.driver_name or dispatch.carrier, vehicle=dispatch.vehicle_number,
        departure_time=timezone.localtime().strftime("%Y-%m-%d %H:%M"),
        estimated_arrival=str(dispatch.expected_delivery or ""),
    )
    for line in dispatch.lines.select_related("item", "sales_order_item"):
        qty = line.quantity
        if qty <= EPS:
            continue
        remaining = qty
        stocks = list(Stock.objects.filter(item=line.item, warehouse__company=company, quantity__gt=0)
                      .order_by("-quantity"))
        stocks.sort(key=lambda s: s.warehouse_id != (warehouse.id if warehouse else None))
        for stock in stocks:
            if remaining <= EPS:
                break
            take = min(stock.quantity, remaining)
            decrease_stock(line.item, stock.warehouse, take, user=user, reference=reference)
            remaining -= take
        if remaining > EPS:
            raise ValidationError(f"{line.item.name}: stock changed; {_fmt(remaining)} short. Re-verify the dispatch.")
        consume_allocations(line.sales_order_item, qty)
        order_item = line.sales_order_item
        order_item.shipped_quantity += qty
        order_item.save(update_fields=["shipped_quantity"])
        ship_lots_fifo(shipment, line.item, qty, company=company)

    open_lines = [oi for oi in order.salesorderitem_set.all() if oi.shipped_quantity + EPS < oi.quantity]
    order.status = "shipped" if open_lines else "delivered"
    order.save(update_fields=["status"])

    dispatch.status, dispatch.dispatched_at, dispatch.shipment = "dispatched", timezone.now(), shipment
    dispatch.save(update_fields=["status", "dispatched_at", "shipment"])

    from accounting.auto_posting import queue_auto_post
    queue_auto_post("dispatch_shipment", company, dispatch.id, user)

    if ManufacturingSettings.for_company(company).auto_invoice_on_dispatch:
        try:
            with transaction.atomic():
                invoice_dispatch(dispatch, user)
        except ValidationError as e:
            dispatch.invoice_error = " ".join(str(x) for x in (e.detail if isinstance(e.detail, list) else [e.detail]))
            dispatch.save(update_fields=["invoice_error"])

    from core.utils import log_activity, send_notification
    send_notification(
        "sales",
        f"{dispatch.dispatch_number} dispatched for SO-{order.id} via {dispatch.get_shipment_mode_display()}"
        + (f" ({dispatch.carrier})" if dispatch.carrier else "") + ".",
        related_id=order.id, related_type="sales_order", company=company, module="sales",
    )
    log_activity(user, "Fulfilment", "Dispatch", f"{dispatch.dispatch_number} dispatched for SO-{order.id}")
    return dispatch


@transaction.atomic
def mark_delivered(dispatch, received_by="", notes="", user=None):
    _advance(dispatch, "delivered")
    dispatch.status, dispatch.delivered_at = "delivered", timezone.now()
    dispatch.received_by = received_by or dispatch.received_by
    dispatch.delivery_notes = notes or dispatch.delivery_notes
    dispatch.save()
    if dispatch.shipment_id:
        dispatch.shipment.status, dispatch.shipment.progress = "delivered", 100
        dispatch.shipment.save(update_fields=["status", "progress"])
    return dispatch


@transaction.atomic
def cancel_dispatch(dispatch, user=None):
    if dispatch.status not in OPEN_DISPATCH:
        raise ValidationError(f"{dispatch} is {dispatch.get_status_display()} and cannot be cancelled.")
    dispatch.status = "cancelled"
    dispatch.save(update_fields=["status"])
    return dispatch


# ---------------------------------------------------------------------------
# Invoice from dispatch (TB-10, agreed price)
# ---------------------------------------------------------------------------

def invoiced_quantity(order_item):
    from sales.models import InvoiceLine
    return InvoiceLine.objects.filter(
        sales_order_item=order_item
    ).exclude(invoice__status="cancelled").aggregate(t=Sum("quantity"))["t"] or 0


@transaction.atomic
def invoice_dispatch(dispatch, user=None):
    """Invoice exactly what left on this dispatch at the order's agreed price."""
    from sales.models import Invoice, InvoiceLine

    if dispatch.status not in ("dispatched", "delivered"):
        raise ValidationError("Only dispatched deliveries can be invoiced.")
    if dispatch.invoice_id:
        return dispatch.invoice
    order = dispatch.sales_order
    legacy = order.invoices.exclude(status="cancelled").filter(dispatches__isnull=True).first()
    if legacy and not legacy.lines.filter(sales_order_item__isnull=False).exists():
        raise ValidationError(f"SO-{order.id} was already invoiced in full as INV-{legacy.id}.")
    company = dispatch.company
    invoice = Invoice.objects.create(
        company=company, sales_order=order, customer=order.customer,
        due_date=timezone.localdate() + timedelta(days=30),
    )
    total = Decimal("0")
    for line in dispatch.lines.select_related("item", "sales_order_item"):
        if line.quantity <= EPS:
            continue
        order_item = line.sales_order_item
        billable = order_item.shipped_quantity - invoiced_quantity(order_item)
        if line.quantity > billable + EPS:
            raise ValidationError(f"{line.item.name}: only {_fmt(billable)} dispatched and not yet invoiced.")
        price = order_item.unit_price  # agreed order price, never the item master price
        amount = (price * Decimal(str(line.quantity))).quantize(Decimal("0.01"))
        InvoiceLine.objects.create(
            invoice=invoice, sales_order_item=order_item, item=line.item,
            description=f"{line.item.name} ({dispatch.dispatch_number})",
            quantity=line.quantity, unit_price=price, amount=amount,
        )
        total += amount
    invoice.total_amount = total
    invoice.save(update_fields=["total_amount"])
    dispatch.invoice = invoice
    dispatch.invoice_error = ""
    dispatch.save(update_fields=["invoice", "invoice_error"])

    from quickbooks.push import get_active_connection, safe_push
    connection = get_active_connection(company)
    if connection:
        safe_push(connection, "invoice", invoice)
    from accounting.auto_posting import queue_auto_post
    queue_auto_post("sales_invoice", company, invoice.id, user)
    return invoice


def order_fulfilment(order):
    """Per-line quantities for the order book / traceability."""
    rows = []
    for oi in order.salesorderitem_set.select_related("item"):
        allocated = sum(a.quantity for a in oi.fg_allocations.exclude(status="released"))
        picked = DispatchLine.objects.filter(sales_order_item=oi).exclude(
            dispatch__status="cancelled").aggregate(t=Sum("quantity"))["t"] or 0
        rows.append({
            "order_item_id": oi.id,
            "item_id": oi.item_id,
            "item": oi.item.name,
            "ordered": oi.quantity,
            "unit_price": float(oi.unit_price),
            "allocated": allocated,
            "open_allocation": sum(a.open_quantity for a in oi.fg_allocations.filter(status="active")),
            "picked": picked,
            "dispatched": oi.shipped_quantity,
            "invoiced": invoiced_quantity(oi),
            "remaining": max(oi.quantity - oi.shipped_quantity, 0),
            "free_fg": free_fg(oi.item, order.customer.company),
        })
    return rows
