"""Executive management dashboard (Track B, TB-16). Numbers follow the
definitions in insights.kpis.

Flow figures (orders, production, dispatches, invoices, collections) are
counted inside the selected period; position figures (stock, WIP, AR
outstanding, open allocations) are as of now.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

from fulfillment.models import Dispatch, FGAllocation
from fulfillment.services import fg_inventory
from inventory.models import Item, Stock
from production.models import ProductionOrder
from quality.models import QualityCheck
from sales.models import CustomerPayment, Invoice, InvoiceLine, SalesOrder, SalesOrderItem

from .kpis import percent

CONFIRMED = ("confirmed", "shipped", "delivered")


class Period:
    """Inclusive date range; `dt()` gives aware datetimes for DateTimeFields."""

    def __init__(self, start=None, end=None):
        today = timezone.localdate()
        self.end = end or today
        self.start = start or today.replace(day=1)
        if self.start > self.end:
            self.start, self.end = self.end, self.start

    def dt(self):
        tz = timezone.get_current_timezone()
        return (timezone.make_aware(datetime.combine(self.start, time.min), tz),
                timezone.make_aware(datetime.combine(self.end, time.max), tz))

    def as_dict(self):
        return {"start": self.start, "end": self.end}


def _orders(company, period):
    start, end = period.dt()
    orders = SalesOrder.objects.filter(customer__company=company, created_at__range=(start, end))
    delayed = list(
        ProductionOrder.objects.filter(
            recipe__product__company=company, sales_order__isnull=False, planned_end__lt=timezone.now(),
        ).exclude(status__in=("completed", "closed", "cancelled"))
        .select_related("sales_order__customer", "recipe__product")
    )
    lines = SalesOrderItem.objects.filter(sales_order__in=orders, sales_order__status__in=CONFIRMED)
    agg = lines.aggregate(ordered=Sum("quantity"), shipped=Sum("shipped_quantity"))
    return {
        "total": orders.count(),
        "pending": orders.filter(status__in=("draft", "pending")).count(),
        "confirmed": orders.filter(status="confirmed").count(),
        "active": orders.filter(status__in=("confirmed", "shipped")).count(),
        "completed": orders.filter(status="delivered").count(),
        "cancelled": orders.filter(status="cancelled").count(),
        "delayed": len({p.sales_order_id for p in delayed}),
        "partially_fulfilled": orders.filter(status="shipped").count(),
        "order_fulfilment_percent": percent(agg["shipped"] or 0, agg["ordered"] or 0),
        "order_book_value": float(SalesOrder.objects.filter(customer__company=company, status__in=("confirmed", "shipped"))
                                  .aggregate(t=Sum("total_amount"))["t"] or 0),
        "delayed_list": [
            {"sales_order": p.sales_order_id, "customer": p.sales_order.customer.name, "production_order": p.id,
             "order_number": p.order_number, "product": p.recipe.product.name, "planned_end": p.planned_end}
            for p in delayed[:20]
        ],
    }


def _production(company, period):
    start, end = period.dt()
    pos = ProductionOrder.objects.filter(recipe__product__company=company, rework_of__isnull=True).exclude(status="cancelled")
    in_period = pos.filter(created_at__range=(start, end))
    agg = in_period.aggregate(planned=Sum("quantity"), produced=Sum("produced_quantity"))
    active = pos.filter(status__in=ProductionOrder.ACTIVE_STATUSES)
    qa = QualityCheck.objects.filter(production_order__recipe__product__company=company, inspection_type="production")
    qa_period = qa.filter(decided_at__range=(start, end))
    qa_agg = qa_period.aggregate(a=Sum("accepted_quantity"), r=Sum("rejected_quantity"),
                                 q=Sum("quarantined_quantity"), w=Sum("rework_quantity"))
    decided = sum((qa_agg[k] or 0) for k in "arqw")
    return {
        "active": active.count(),
        "in_production": pos.filter(status="running").count(),
        "material_pending": pos.filter(status="material_pending").count(),
        "partially_completed": pos.filter(status="partially_completed").count(),
        "completed": in_period.filter(status__in=("completed", "closed")).count(),
        "delayed": active.filter(planned_end__lt=timezone.now()).count() + pos.filter(status="delayed").count(),
        "qa_pending": qa.filter(status="pending").count(),
        "rework_orders": ProductionOrder.objects.filter(recipe__product__company=company, rework_of__isnull=False)
        .exclude(status__in=("closed", "cancelled", "completed")).count(),
        "production_completion_percent": percent(agg["produced"] or 0, agg["planned"] or 0),
        "qa_pass_rate_percent": percent(qa_agg["a"] or 0, decided),
        "wip_units": round(sum(o.remaining_quantity for o in active.filter(status__in=("running", "partially_completed"))), 2),
    }


def _wip_value(company):
    from production.execution import unit_cost_of
    value = Decimal("0")
    open_orders = ProductionOrder.objects.filter(
        recipe__product__company=company, status__in=ProductionOrder.ACTIVE_STATUSES
    )
    for order in open_orders.prefetch_related("material_requirements__item"):
        for ledger in order.material_requirements.all():
            if ledger.consumed_quantity <= 0:
                continue
            share_done = min(order.produced_quantity / order.quantity, 1) if order.quantity else 0
            value += unit_cost_of(ledger.item, company) * Decimal(str(ledger.consumed_quantity * (1 - share_done)))
    return float(value.quantize(Decimal("0.01")))


def _inventory(company):
    from production.execution import unit_cost_of
    from production.models import ProductionMaterialRequirement
    raw = Item.objects.filter(company=company, category__in=("raw_material", "intermediate"))
    shortages = []
    for item in raw:
        on_hand = Stock.objects.filter(item=item).aggregate(t=Sum("quantity"))["t"] or 0
        if item.reorder_point is not None and on_hand <= item.reorder_point:
            shortages.append({"item": item.name, "on_hand": on_hand, "reorder_point": item.reorder_point})
    open_ledgers = ProductionMaterialRequirement.objects.filter(
        production_order__recipe__product__company=company,
        production_order__status__in=ProductionOrder.ACTIVE_STATUSES,
    ).select_related("item", "production_order")
    covered = total = 0
    for ledger in open_ledgers:
        total += 1
        on_hand = Stock.objects.filter(item=ledger.item).aggregate(t=Sum("quantity"))["t"] or 0
        if ledger.consumed_quantity + on_hand >= ledger.required_quantity - 1e-6:
            covered += 1
        else:
            shortages.append({"item": ledger.item.name,
                              "short": round(ledger.required_quantity - ledger.consumed_quantity - on_hand, 4),
                              "production_order": ledger.production_order.order_number,
                              "production_order_id": ledger.production_order_id})
    fg = fg_inventory(company)
    value = Decimal("0")
    for stock in Stock.objects.filter(item__company=company, quantity__gt=0).select_related("item"):
        value += unit_cost_of(stock.item, company) * Decimal(str(stock.quantity))
    return {
        "rm_items": raw.count(),
        "rm_shortages": shortages[:20],
        "rm_shortage_count": len(shortages),
        "material_availability_percent": percent(covered, total) if total else 100.0,
        "wip_value": _wip_value(company),
        "fg_on_hand": round(sum(r["on_hand"] for r in fg), 2),
        "fg_available": round(sum(r["available"] for r in fg), 2),
        "fg_allocated": round(sum(r["allocated"] for r in fg), 2),
        "fg_qa_pending": round(sum(r["qa_pending"] for r in fg), 2),
        "fg_quarantine": round(sum(r["qa_quarantine"] for r in fg), 2),
        "inventory_value": float(value.quantize(Decimal("0.01"))),
    }


def _fulfilment(company, period):
    start, end = period.dt()
    dispatches = Dispatch.objects.filter(company=company)
    done = dispatches.filter(status__in=("dispatched", "delivered"), dispatched_at__range=(start, end))
    with_date = done.filter(expected_delivery__isnull=False)
    on_time = sum(1 for d in with_date if d.dispatched_at and timezone.localtime(d.dispatched_at).date() <= d.expected_delivery)
    open_alloc = FGAllocation.objects.filter(company=company, status="active")
    open_dispatches = dispatches.exclude(status__in=("dispatched", "delivered", "cancelled")).select_related("sales_order__customer")
    return {
        "allocated_awaiting_pick": round(sum(a.open_quantity for a in open_alloc), 2),
        "ready_for_dispatch": dispatches.filter(status__in=("staged", "prepared", "verified")).count(),
        "in_picking": dispatches.filter(status__in=("picking", "picked", "packed")).count(),
        "dispatched": done.count(),
        "pending_fulfilment": SalesOrder.objects.filter(customer__company=company, status="confirmed").count(),
        "partial_fulfilment": SalesOrder.objects.filter(customer__company=company, status="shipped").count(),
        "on_time_delivery_percent": percent(on_time, with_date.count()),
        "open_dispatches": [
            {"dispatch": d.id, "number": d.dispatch_number, "sales_order": d.sales_order_id,
             "customer": d.sales_order.customer.name, "status": d.get_status_display()}
            for d in open_dispatches[:20]
        ],
    }


def _finance(company, period):
    today = timezone.localdate()
    invoices = Invoice.objects.filter(company=company).exclude(status="cancelled")
    in_period = invoices.filter(invoice_date__range=(period.start, period.end))
    open_inv = invoices.filter(status__in=("open", "partial"))
    outstanding = open_inv.aggregate(t=Sum(F("total_amount") - F("amount_paid")))["t"] or 0
    overdue = open_inv.filter(due_date__lt=today).aggregate(t=Sum(F("total_amount") - F("amount_paid")))["t"] or 0
    invoiced = float(in_period.aggregate(t=Sum("total_amount"))["t"] or 0)
    return {
        "invoice_value_period": invoiced,
        "invoice_value_total": float(invoices.aggregate(t=Sum("total_amount"))["t"] or 0),
        "ar_outstanding": float(outstanding),
        "ar_overdue": float(overdue),
        "collections_period": float(CustomerPayment.objects.filter(
            company=company, payment_date__range=(period.start, period.end)).aggregate(t=Sum("amount"))["t"] or 0),
        "revenue_period": invoiced,
    }


def _capacity(company):
    from production.capacity import capacity_plan
    plan = capacity_plan(company, days=7)
    s = plan["summary"]
    return {
        "machine_utilisation_percent": percent(s["machine_load_hours"], s["machine_available_hours"]),
        "manpower_utilisation_percent": percent(s["manpower_load_hours"], s["manpower_available_hours"]),
        "overloaded_resources": s["overloaded_resources"],
        "manpower_shortages": len(s["manpower_shortages"]),
    }


def executive_dashboard(company, start=None, end=None):
    period = Period(start, end)
    return {
        "generated_at": timezone.now(),
        "period": period.as_dict(),
        "orders": _orders(company, period),
        "production": _production(company, period),
        "inventory": _inventory(company),
        "fulfilment": _fulfilment(company, period),
        "finance": _finance(company, period),
        "capacity": _capacity(company),
        "pipeline": pipeline(company, period),
    }


def pipeline(company, period=None):
    """Units at each stage for orders created in the period."""
    period = period or Period(date(2000, 1, 1), timezone.localdate())
    start, end = period.dt()
    lines = SalesOrderItem.objects.filter(sales_order__customer__company=company, sales_order__status__in=CONFIRMED,
                                          sales_order__created_at__range=(start, end))
    ordered = lines.aggregate(t=Sum("quantity"))["t"] or 0
    dispatched = lines.aggregate(t=Sum("shipped_quantity"))["t"] or 0
    pos = ProductionOrder.objects.filter(recipe__product__company=company, sales_order__in=lines.values("sales_order"),
                                         rework_of__isnull=True).exclude(status="cancelled")
    planned = pos.aggregate(t=Sum("quantity"))["t"] or 0
    produced = pos.aggregate(t=Sum("produced_quantity"))["t"] or 0
    accepted = QualityCheck.objects.filter(production_order__in=pos).aggregate(t=Sum("accepted_quantity"))["t"] or 0
    allocated = FGAllocation.objects.filter(sales_order_item__in=lines).exclude(status="released") \
        .aggregate(t=Sum("quantity"))["t"] or 0
    invoiced = InvoiceLine.objects.filter(sales_order_item__in=lines).exclude(invoice__status="cancelled") \
        .aggregate(t=Sum("quantity"))["t"] or 0
    return [
        {"stage": "Ordered", "quantity": ordered},
        {"stage": "Planned for production", "quantity": planned},
        {"stage": "Produced", "quantity": produced},
        {"stage": "QA approved", "quantity": accepted},
        {"stage": "Allocated", "quantity": allocated},
        {"stage": "Dispatched", "quantity": dispatched},
        {"stage": "Invoiced", "quantity": invoiced},
    ]
