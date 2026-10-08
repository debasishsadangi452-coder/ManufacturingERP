"""End-to-end supply chain traceability (Track B, TB-17).

Search by customer order, production plan reference, production order,
product, lot, dispatch or invoice. Every hit resolves to the customer orders
(and stand-alone production orders) involved, and each is traced through:

  Customer Order → Production Plan → MRP → Purchasing → Material Receipt →
  Production Order → Material Issue → WIP Operations → QA → FG →
  Allocation → Picking → Dispatch → Invoice → AR → Payment → GL

Every record carries a `link` ({"type", "id"}) so the screen can drill into it
(re-trace from it, or open its document).
"""
from django.db.models import Q

from accounting.models import JournalEntry
from fulfillment.models import Dispatch, FGAllocation
from inventory.models import Batch, InventoryRequest, LotConsumption
from procurement.models import GoodsReceipt, PurchaseOrder
from production.models import ProductionMaterialRequirement, ProductionOrder, ProductionPlan
from quality.models import QualityCheck
from sales.models import CustomerPayment, Invoice, SalesOrder

SEARCH_TYPES = (
    "sales_order", "production_plan", "production_order", "product", "lot", "dispatch", "invoice",
    "purchase_order", "goods_receipt",
)


def _num(value):
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def resolve(company, search_type, value):
    """(sales orders, production orders without a customer order) for a search."""
    sos, standalone = set(), set()
    pos = ProductionOrder.objects.filter(recipe__product__company=company)
    if search_type == "sales_order":
        sos |= set(SalesOrder.objects.filter(customer__company=company, pk=_num(value)))
    elif search_type == "production_plan":
        raw_value = str(value).strip()
        plan_obj = ProductionPlan.objects.filter(company=company).filter(
            Q(plan_number__iexact=raw_value) | Q(pk=_num(raw_value) or 0)
        ).first()
        matches = pos.filter(
            Q(production_plan=plan_obj) | Q(production_plan_ref__iexact=raw_value)
        ) if plan_obj else pos.filter(production_plan_ref__iexact=raw_value)
        sos |= {p.sales_order for p in matches if p.sales_order_id}
        standalone |= {p for p in matches if not p.sales_order_id}
        if plan_obj and plan_obj.sales_order_id:
            sos.add(plan_obj.sales_order)
        elif plan_obj:
            standalone |= set(matches)
    elif search_type == "production_order":
        number = str(value).strip()
        matches = pos.filter(Q(order_number__iexact=number) | Q(pk=_num(number) or 0))
        for p in matches:
            root = p.rework_of or p
            (sos.add(root.sales_order) if root.sales_order_id else standalone.add(root))
    elif search_type == "product":
        sos |= set(SalesOrder.objects.filter(
            customer__company=company, salesorderitem__item__name__icontains=str(value).strip()
        ).distinct()[:25])
    elif search_type == "lot":
        lot = Batch.objects.filter(company=company, batch_number__iexact=str(value).strip()).first() or \
            Batch.objects.filter(company=company, pk=_num(value) or 0).first()
        if lot:
            orders = set()
            if lot.production_order_id:
                orders.add(lot.production_order)
            for c in LotConsumption.objects.filter(lot=lot).select_related("production_order"):
                orders.add(c.production_order)
            for p in orders:
                root = p.rework_of or p
                (sos.add(root.sales_order) if root.sales_order_id else standalone.add(root))
            for sl in lot.shipped_on.select_related("shipment__sales_order"):
                sos.add(sl.shipment.sales_order)
    elif search_type == "dispatch":
        number = str(value).strip()
        sos |= {d.sales_order for d in Dispatch.objects.filter(company=company).filter(
            Q(dispatch_number__iexact=number) | Q(pk=_num(number) or 0))}
    elif search_type == "invoice":
        sos |= {i.sales_order for i in Invoice.objects.filter(company=company, pk=_num(value) or 0)
                if i.sales_order_id}
    elif search_type == "purchase_order":
        po = PurchaseOrder.objects.filter(vendor__company=company, pk=_num(value) or 0).select_related(
            "requisition__sales_order"
        ).first()
        if po and po.requisition_id and po.requisition.sales_order_id:
            sos.add(po.requisition.sales_order)
    elif search_type == "goods_receipt":
        receipt = GoodsReceipt.objects.filter(
            purchase_order__vendor__company=company, pk=_num(value) or 0
        ).select_related("purchase_order__requisition__sales_order").first()
        if receipt and receipt.purchase_order.requisition_id and receipt.purchase_order.requisition.sales_order_id:
            sos.add(receipt.purchase_order.requisition.sales_order)
    return [s for s in sos if s is not None], list(standalone)


def _rec(type_, id_, label, status=None, **detail):
    return {"link": {"type": type_, "id": id_}, "label": label, "status": status, **detail}


def _stage(key, label, records):
    return {"key": key, "label": label, "count": len(records), "complete": bool(records), "records": records}


def _journal_entries(company, refs):
    q = Q()
    for module, ids in refs.items():
        ids = [i for i in ids if i]
        if ids:
            q |= Q(source_module=module, source_id__in=ids)
    if not q:
        return []
    return [
        _rec("journal_entry", je.id, je.entry_number or f"JE-{je.id}", je.status,
             module=je.source_module, date=je.transaction_date, reference=je.reference,
             amount=float(je.total_debit))
        for je in JournalEntry.objects.filter(company=company).filter(q).order_by("transaction_date", "id")
    ]


def trace_production_orders(company, orders):
    """The manufacturing part of the chain for a set of production orders."""
    orders = list(orders)
    rework = list(ProductionOrder.objects.filter(rework_of__in=orders))
    all_orders = orders + [r for r in rework if r not in orders]
    order_ids = [o.id for o in all_orders]

    plan_refs = {o.production_plan_ref for o in all_orders if o.production_plan_ref}
    plan = [
        _rec("production_plan", p.id, p.plan_number, p.get_status_display(),
             planned_quantity=p.planned_quantity, remaining_quantity=p.remaining_quantity,
             target_date=p.target_date)
        for p in ProductionPlan.objects.filter(company=company, plan_number__in=plan_refs)
    ]
    mrp = []
    for o in all_orders:
        for r in o.material_requirements.select_related("item"):
            mrp.append(_rec("item", r.item_id, f"{r.item.name} for {o.order_number}",
                            "short" if r.shortage_quantity > 0 else "covered",
                            required=r.required_quantity, reserved=r.reserved_quantity,
                            issued=r.issued_quantity, consumed=r.consumed_quantity,
                            shortage=r.shortage_quantity, unit=r.item.unit))
    requests = list(InventoryRequest.objects.filter(production_order_id__in=order_ids).select_related("item", "purchase_order__vendor"))
    po_ids = {r.purchase_order_id for r in requests if r.purchase_order_id}
    purchasing = [
        _rec("inventory_request", r.id, f"Request #{r.id}: {r.quantity:g} {r.item.name}", r.status,
             purchase_order=r.purchase_order_id) for r in requests
    ] + [
        _rec("purchase_order", po.id, f"PO #{po.id} · {po.vendor.name}", po.status, amount=float(po.total_amount))
        for po in PurchaseOrder.objects.filter(pk__in=po_ids).select_related("vendor")
    ]
    consumed_lot_ids = LotConsumption.objects.filter(production_order_id__in=order_ids).values_list("lot_id", flat=True)
    receipt_ids = set(GoodsReceipt.objects.filter(purchase_order_id__in=po_ids).values_list("id", flat=True))
    receipt_ids |= set(Batch.objects.filter(pk__in=consumed_lot_ids, goods_receipt__isnull=False)
                       .values_list("goods_receipt_id", flat=True))
    receipts = []
    for gr in GoodsReceipt.objects.filter(pk__in=receipt_ids).select_related("purchase_order__vendor", "warehouse"):
        incoming = QualityCheck.objects.filter(goods_receipt=gr)
        receipts.append(_rec("goods_receipt", gr.id, f"GRN #{gr.id} · PO #{gr.purchase_order_id} · {gr.purchase_order.vendor.name}",
                             "received", date=gr.received_at, warehouse=gr.warehouse.name,
                             incoming_qc=[{"id": q.id, "status": q.status, "decision": q.result_decision} for q in incoming]))

    production = [
        _rec("production_order", o.id, f"{o.order_number} · {o.recipe.product.name}", o.status,
             planned=o.quantity, produced=o.produced_quantity, remaining=o.remaining_quantity,
             qa_status=o.qa_status, rework_of=o.rework_of.order_number if o.rework_of_id else None,
             start=o.start_time, end=o.end_time)
        for o in all_orders
    ]
    issue = [
        _rec("production_order", r.production_order_id,
             f"Issued {r.consumed_quantity:g} {r.item.unit} {r.item.name} → {r.production_order.order_number}",
             "issued", item=r.item.name, quantity=r.consumed_quantity)
        for r in ProductionMaterialRequirement.objects.filter(
            production_order_id__in=order_ids, consumed_quantity__gt=0).select_related("item", "production_order")
    ] + [
        _rec("lot", c.lot_id, f"{c.quantity:g} of lot {c.lot.batch_number} ({c.lot.item.name}) → {c.production_order.order_number}",
             "consumed", quantity=c.quantity, goods_receipt=c.lot.goods_receipt_id)
        for c in LotConsumption.objects.filter(production_order_id__in=order_ids).select_related("lot__item", "production_order")
    ]
    operations = []
    for o in all_orders:
        for op in o.operations.select_related("machine", "manpower"):
            operations.append(_rec(
                "operation", op.id, f"{o.order_number} · Op {op.sequence} {op.name}", op.status,
                planned=op.planned_quantity, completed=op.completed_quantity, rejected=op.rejected_quantity,
                rework=op.rework_quantity, scrap=op.scrap_quantity,
                machine=op.machine.name if op.machine_id else None,
                manpower=op.manpower.name if op.manpower_id else None, operator=op.operator,
                start=op.actual_start, end=op.actual_end,
            ))
    qa = [
        _rec("quality_check", q.id, f"QC #{q.id} · {q.production_order.order_number}", q.result_decision or q.status,
             inspected=q.inspected_quantity, accepted=q.accepted_quantity, rejected=q.rejected_quantity,
             quarantined=q.quarantined_quantity, rework=q.rework_quantity,
             lot=q.lot.batch_number if q.lot_id else None, certificate=q.certificate_number or None,
             decided_at=q.decided_at)
        for q in QualityCheck.objects.filter(production_order_id__in=order_ids).select_related("production_order", "lot")
    ]
    fg = [
        _rec("lot", b.id, f"Lot {b.batch_number} · {b.item.name}", b.qa_status,
             quantity=b.quantity, remaining=b.remaining_quantity, parent=b.parent_id,
             production_order=b.production_order.order_number if b.production_order_id else None)
        for b in Batch.objects.filter(production_order_id__in=order_ids, source="produced").select_related("item", "production_order")
    ]
    return {
        "plan": plan, "mrp": mrp, "purchasing": purchasing, "receipts": receipts,
        "production": production, "issue": issue, "operations": operations, "qa": qa, "fg": fg,
        "order_ids": order_ids, "receipt_ids": list(receipt_ids),
    }


def trace_sales_order(company, order):
    from fulfillment.services import order_fulfilment

    pos = ProductionOrder.objects.filter(sales_order=order, rework_of__isnull=True).select_related("recipe__product")
    m = trace_production_orders(company, pos)
    planned_only = order.production_plans.exclude(status="cancelled").exclude(
        plan_number__in=[r["label"] for r in m["plan"]]
    )
    from django.core.exceptions import ValidationError
    from production.mrp import calculate_mrp_for_plan
    for plan in planned_only:
        m["plan"].append(_rec(
            "production_plan", plan.id, plan.plan_number, plan.get_status_display(),
            planned_quantity=plan.planned_quantity, remaining_quantity=plan.remaining_quantity,
            target_date=plan.target_date,
        ))
        try:
            requirements = calculate_mrp_for_plan(plan)["consolidated"]
        except ValidationError as exc:
            m["mrp"].append(_rec("production_plan", plan.id, f"MRP for {plan.plan_number}", "unavailable",
                                 error=str(exc)))
        else:
            m["mrp"].extend(
                _rec("item", row["item_id"], f"{row['item_name']} for {plan.plan_number}", row["status"],
                     required=row["required_quantity"], available=row["available_quantity"],
                     on_order=row["on_order_quantity"], shortage=row["shortage_quantity"], unit=row["unit"])
                for row in requirements
            )
    allocations = [
        _rec("allocation", a.id, f"ALLOC-{a.id}: {a.quantity:g} {a.item.name}", a.get_status_display(),
             quantity=a.quantity, dispatched=a.dispatched_quantity, date=a.created_at)
        for a in FGAllocation.objects.filter(sales_order=order).select_related("item")
    ]
    dispatches = list(Dispatch.objects.filter(sales_order=order).prefetch_related("lines__item"))
    picking = [
        _rec("dispatch_pick", d.id, f"{d.dispatch_number} pick list", d.get_status_display(),
             lines=[{"item": l.item.name, "requested": l.requested_quantity, "picked": l.quantity} for l in d.lines.all()],
             picked_at=d.picked_at, packed_at=d.packed_at, staged_at=d.staged_at)
        for d in dispatches
    ]
    dispatch_recs = [
        _rec("dispatch", d.id, f"{d.dispatch_number} · {d.get_shipment_mode_display() or 'mode not set'}"
             + (f" · {d.carrier}" if d.carrier else ""), d.get_status_display(),
             quantity=d.total_quantity, reference=d.shipment_reference, dispatched_at=d.dispatched_at,
             delivered_at=d.delivered_at, invoice=d.invoice_id)
        for d in dispatches if d.status in ("dispatched", "delivered")
    ]
    for sh in order.shipment_set.filter(dispatches__isnull=True):
        dispatch_recs.append(_rec("shipment", sh.id, f"Shipment #{sh.id} (direct)", sh.status,
                                  lots=[f"{x.quantity:g} of {x.lot.batch_number}" for x in sh.shipment_lots.select_related("lot")]))
    invoices = list(order.invoices.all())
    invoice_recs = [
        _rec("invoice", i.id, f"INV-{i.id}", i.status, amount=float(i.total_amount),
             date=i.invoice_date, due_date=i.due_date) for i in invoices
    ]
    ar = [
        _rec("invoice", i.id, f"INV-{i.id} receivable", i.status, amount=float(i.total_amount),
             paid=float(i.amount_paid), balance=float(i.balance_due), due_date=i.due_date)
        for i in invoices
    ]
    payments = list(CustomerPayment.objects.filter(invoice__in=invoices))
    payment_recs = [
        _rec("payment", p.id, f"Payment #{p.id} · {p.method}", "received", amount=float(p.amount), date=p.payment_date)
        for p in payments
    ]
    gl = _journal_entries(company, {
        "procurement.receipt": m["receipt_ids"],
        "manufacturing": m["order_ids"],
        "fulfillment.dispatch": [d.id for d in dispatches],
        "sales.shipment": [order.id],
        "sales.invoice": [i.id for i in invoices],
        "sales.payment": [p.id for p in payments],
    })
    lines = order_fulfilment(order)
    ordered = sum(l["ordered"] for l in lines)
    dispatched = sum(l["dispatched"] for l in lines)
    return {
        "customer_order": {
            "id": order.id, "number": f"SO-{order.id}", "customer": order.customer.name,
            "status": order.status, "source": order.source, "created_at": order.created_at,
            "priority": order.get_priority_display(),
            "customer_reference": order.customer_order_reference,
            "required_delivery_date": order.required_delivery_date,
            "custom_specifications": order.custom_specifications,
            "delivery_requirements": order.delivery_requirements,
            "total_amount": float(order.total_amount), "lines": lines,
            "fulfilment_percent": round(dispatched / ordered * 100, 1) if ordered else 0,
        },
        "stages": [
            _stage("customer_order", "Customer Order", [_rec("sales_order", order.id, f"SO-{order.id} · {order.customer.name}", order.status)]),
            _stage("production_plan", "Production Plan", [_rec("production_plan", ref, ref, "planned") for ref in m["plan"]]),
            _stage("mrp", "MRP", m["mrp"]),
            _stage("purchasing", "Purchasing", m["purchasing"]),
            _stage("material_receipt", "Material Receipt", m["receipts"]),
            _stage("production_order", "Production Order", m["production"]),
            _stage("material_issue", "Material Issue", m["issue"]),
            _stage("wip", "WIP Operations", m["operations"]),
            _stage("qa", "QA", m["qa"]),
            _stage("fg", "Finished Goods", m["fg"]),
            _stage("allocation", "Allocation", allocations),
            _stage("picking", "Picking", picking),
            _stage("dispatch", "Dispatch", dispatch_recs),
            _stage("invoice", "Invoice", invoice_recs),
            _stage("ar", "Accounts Receivable", ar),
            _stage("payment", "Payment", payment_recs),
            _stage("gl", "General Ledger", gl),
        ],
    }


def trace_standalone(company, order):
    m = trace_production_orders(company, [order])
    gl = _journal_entries(company, {"procurement.receipt": m["receipt_ids"], "manufacturing": m["order_ids"]})
    return {
        "customer_order": None,
        "stages": [
            _stage("production_plan", "Production Plan", [_rec("production_plan", r, r, "planned") for r in m["plan"]]),
            _stage("mrp", "MRP", m["mrp"]),
            _stage("purchasing", "Purchasing", m["purchasing"]),
            _stage("material_receipt", "Material Receipt", m["receipts"]),
            _stage("production_order", "Production Order", m["production"]),
            _stage("material_issue", "Material Issue", m["issue"]),
            _stage("wip", "WIP Operations", m["operations"]),
            _stage("qa", "QA", m["qa"]),
            _stage("fg", "Finished Goods", m["fg"]),
            _stage("gl", "General Ledger", gl),
        ],
    }


def trace(company, search_type, value):
    if search_type not in SEARCH_TYPES:
        raise ValueError(f"Search by one of: {', '.join(SEARCH_TYPES)}.")
    sos, standalone = resolve(company, search_type, value)
    chains = [trace_sales_order(company, so) for so in sorted(sos, key=lambda s: s.id)]
    chains += [trace_standalone(company, po) for po in standalone]
    return {"search": {"type": search_type, "value": value}, "results": chains}
