"""Printable business documents (Track B, TB-12 / TB-13).

Each builder turns one transaction into a document description that the
shared template renders. Documents are always built from live transaction
data and print the upstream references (customer order, production order,
lot, dispatch, invoice) so a paper copy can be traced back.
"""
from django.db.models import Sum
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from fulfillment.models import Dispatch
from inventory.models import Batch, StockTransfer
from production.models import ProductionOrder, ProductionPlan
from procurement.models import Bill, GoodsReceipt, PurchaseOrder
from quality.models import QualityCheck
from sales.models import Invoice, Quotation, SalesOrder, SummaryInvoice


def _n(value, places=4):
    if value is None:
        return ""
    text = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _money(value):
    return f"{value:,.2f}"


def _c(value, num=False):
    return {"value": value, "num": num}


def _table(title, columns, rows):
    return {
        "title": title,
        "columns": [{"label": c[0], "num": c[1]} if isinstance(c, tuple) else {"label": c, "num": False} for c in columns],
        "rows": rows,
    }


def _dt(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else None


def _base(company, title, number, status=None, subtitle=None):
    return {
        "company": company.name if company else "",
        "title": title, "number": number, "status": status, "subtitle": subtitle,
        "meta": [], "tables": [], "totals": [], "notes": [], "signatures": [], "references": "",
    }


def _order_refs(order):
    so = order.sales_order
    return " · ".join(filter(None, [
        order.order_number,
        f"Customer order SO-{so.id}" if so else None,
        f"Plan {order.production_plan_ref}" if order.production_plan_ref else None,
        f"Rework of {order.rework_of.order_number}" if order.rework_of_id else None,
    ]))


# ---------------------------------------------------------------------------
# Production documents
# ---------------------------------------------------------------------------

def _production_meta(order):
    so = order.sales_order
    return [
        ("Product", order.recipe.product.name),
        ("Planned quantity", f"{_n(order.quantity)} {order.recipe.product.unit}"),
        ("Produced", _n(order.produced_quantity)),
        ("Remaining", _n(order.remaining_quantity)),
        ("Customer order", f"SO-{so.id}" if so else "Make to stock"),
        ("Customer", so.customer.name if so else None),
        ("Production plan", order.production_plan_ref or None),
        ("BOM / recipe", f"Recipe #{order.recipe_id} (batch size {_n(order.recipe.batch_size)})"),
        ("Line", order.line.name if order.line_id else None),
        ("Warehouse", order.warehouse.name),
        ("Planned start", _dt(order.planned_start)),
        ("Planned completion", _dt(order.planned_end)),
        ("Actual start", _dt(order.start_time)),
        ("Actual completion", _dt(order.end_time)),
        ("QA status", order.get_qa_status_display()),
    ]


def _materials_table(order, title="Required Materials"):
    from production.execution import build_operations  # noqa: F401  (keeps import cycle lazy)
    ledgers = {l.item_id: l for l in order.material_requirements.select_related("item")}
    rows = []
    for ing, required in order.recipe.material_requirements(order.quantity):
        led = ledgers.get(ing.item_id)
        rows.append([
            _c(ing.item.name), _c(ing.item.unit), _c(_n(required), True),
            _c(_n(led.consumed_quantity if led else 0), True),
            _c(_n(max(required - (led.consumed_quantity if led else 0), 0)), True),
            _c(_n(led.shortage_quantity if led else None), True),
        ])
    return _table(title, ["Material", "Unit", ("Required", True), ("Issued", True), ("To issue", True), ("Shortage", True)], rows)


def _operations_table(order, with_signoff=False):
    from production.execution import build_operations
    rows = []
    for op in build_operations(order):
        row = [
            _c(op.sequence, True), _c(op.name), _c(op.location),
            _c(op.machine.name if op.machine_id else ""), _c(op.manpower.name if op.manpower_id else ""),
            _c(_n(op.planned_quantity), True), _c(_n(op.completed_quantity), True),
            _c(op.get_status_display()),
        ]
        if with_signoff:
            row += [_c(""), _c("")]
        rows.append(row)
    cols = ["Op", "Operation", "Location", "Machine", "Manpower", ("Planned", True), ("Done", True), "Status"]
    if with_signoff:
        cols += ["Operator / date", "Sign"]
    return _table("Operations", cols, rows)


def production_order_doc(order):
    doc = _base(order.recipe.product.company, "Production Order", order.order_number, order.get_status_display())
    doc["meta"] = _production_meta(order)
    doc["tables"] = [_materials_table(order), _operations_table(order)]
    if order.notes:
        doc["notes"].append(order.notes)
    doc["signatures"] = ["Prepared by", "Approved by (Production Manager)"]
    doc["references"] = _order_refs(order)
    return doc


def traveler_doc(order):
    """Production Traveler / Work Ticket: travels with the batch on the floor."""
    doc = _base(order.recipe.product.company, "Production Traveler / Work Ticket", order.order_number,
                order.get_status_display(), "Keep with the batch. Record quantities and sign each operation.")
    doc["meta"] = _production_meta(order)[:10]
    doc["tables"] = [_operations_table(order, with_signoff=True)]
    lots = Batch.objects.filter(production_order=order, source="produced")
    doc["tables"].append(_table(
        "Output lots", ["Lot", ("Quantity", True), "QA status"],
        [[_c(l.batch_number), _c(_n(l.quantity), True), _c(l.get_qa_status_display())] for l in lots],
    ))
    doc["notes"].append("Rejected / scrapped / rework quantities must be recorded at the operation where they occur.")
    doc["signatures"] = ["Shift supervisor", "Production manager", "QA released by"]
    doc["references"] = _order_refs(order)
    return doc


def pick_list_doc(order):
    """Material pick list: what the store must issue and from which lots."""
    from inventory.lots import _available_lots
    doc = _base(order.recipe.product.company, "Material Pick List", f"PICK-{order.order_number}", order.get_status_display())
    doc["meta"] = _production_meta(order)[:6]
    rows = []
    ledgers = {l.item_id: l for l in order.material_requirements.all()}
    for ing, required in order.recipe.material_requirements(order.quantity):
        led = ledgers.get(ing.item_id)
        to_issue = max(required - (led.consumed_quantity if led else 0), 0)
        lots = list(_available_lots(ing.item, ing.item.company)[:3])
        lot_text = ", ".join(f"{l.batch_number} ({_n(l.remaining_quantity)})" for l in lots) or "No lot on hand"
        rows.append([_c(ing.item.name), _c(ing.item.unit), _c(_n(to_issue), True), _c(lot_text), _c("")])
    doc["tables"] = [_table("Pick", ["Material", "Unit", ("Quantity to pick", True), "Suggested lots (FIFO)", "Picked"], rows)]
    doc["signatures"] = ["Picked by (Store)", "Received by (Production)"]
    doc["references"] = _order_refs(order)
    return doc


def dispatch_pick_list_doc(dispatch):
    doc = _base(dispatch.company, "Finished Goods Picking List", dispatch.dispatch_number, dispatch.get_status_display())
    doc["meta"] = [
        ("Customer order", f"SO-{dispatch.sales_order_id}"),
        ("Customer", dispatch.sales_order.customer.name),
        ("Warehouse", dispatch.warehouse.name if dispatch.warehouse_id else None),
        ("Ship to", dispatch.ship_to_name or None),
        ("Address", dispatch.ship_to_address or None),
    ]
    doc["tables"] = [_table(
        "Pick and stage",
        ["Item", "Unit", ("Requested", True), ("Picked", True), "Picked / verified by"],
        [[_c(line.item.name), _c(line.item.unit), _c(_n(line.requested_quantity), True),
          _c(_n(line.quantity), True), _c("")] for line in dispatch.lines.select_related("item")],
    )]
    doc["signatures"] = ["Picked by (Warehouse)", "Packed by", "Dispatch verification"]
    doc["references"] = f"{dispatch.dispatch_number} · SO-{dispatch.sales_order_id}"
    return doc


def material_requisition_doc(order):
    """Material requisition raised by production for this order (TB-13)."""
    doc = _base(order.recipe.product.company, "Material Requisition", f"MR-{order.order_number}", order.get_status_display())
    doc["meta"] = _production_meta(order)[:6]
    doc["tables"] = [_materials_table(order, "Requested materials")]
    requests = order.inventoryrequest_set.select_related("item", "purchase_order")
    doc["tables"].append(_table(
        "Shortage requests to stores / purchasing",
        ["Request", "Material", ("Quantity", True), "Status", "Purchase order"],
        [[_c(f"#{r.id}"), _c(r.item.name), _c(_n(r.quantity), True), _c(r.get_status_display()),
          _c(f"PO #{r.purchase_order_id}" if r.purchase_order_id else "")] for r in requests],
    ))
    doc["signatures"] = ["Requested by (Production)", "Issued by (Store)"]
    doc["references"] = _order_refs(order)
    return doc


# ---------------------------------------------------------------------------
# Quality documents
# ---------------------------------------------------------------------------

def _qc_meta(qc):
    meta = [("Inspection type", qc.get_inspection_type_display())]
    if qc.production_order_id:
        o = qc.production_order
        meta += [("Production order", o.order_number), ("Product", o.recipe.product.name),
                 ("Customer order", f"SO-{o.sales_order_id}" if o.sales_order_id else None)]
    if qc.goods_receipt_id:
        meta += [("Goods receipt", f"GRN #{qc.goods_receipt_id}"),
                 ("Purchase order", f"PO #{qc.goods_receipt.purchase_order_id}"),
                 ("Supplier", qc.vendor.name if qc.vendor_id else None),
                 ("Material", qc.item.name if qc.item_id else None)]
    meta += [
        ("Lot", qc.lot.batch_number if qc.lot_id else None),
        ("Inspected quantity", _n(qc.inspected_quantity)),
        ("Sample quantity", _n(qc.sample_quantity)),
        ("Inspector", (qc.inspector.get_full_name() or qc.inspector.username) if qc.inspector_id else None),
        ("Inspection date", _dt(qc.decided_at or qc.inspected_at)),
        ("Result", qc.get_result_decision_display() if qc.result_decision else qc.get_status_display()),
    ]
    return meta


def _qc_company(qc):
    if qc.production_order_id:
        return qc.production_order.recipe.product.company
    return qc.vendor.company if qc.vendor_id else None


def _qc_tables(qc):
    params = [[_c(qc.parameter or qc.test_type), _c(qc.target), _c(qc.result), _c("")]] if (qc.parameter or qc.test_type) else []
    for p in qc.parameters or []:
        params.append([_c(p.get("parameter")), _c(p.get("target")), _c(p.get("result")),
                       _c("Pass" if p.get("pass") else "Fail" if p.get("pass") is False else "")])
    qty = [
        [_c("Accepted"), _c(_n(qc.accepted_quantity), True)],
        [_c("Rejected"), _c(_n(qc.rejected_quantity), True)],
        [_c("Quarantined"), _c(_n(qc.quarantined_quantity), True)],
        [_c("Rework"), _c(_n(qc.rework_quantity), True)],
    ]
    return [
        _table("Inspection parameters", ["Parameter", "Target", "Result", "Pass / Fail"], params),
        _table("Disposition", ["Outcome", ("Quantity", True)], qty),
    ]


def qa_inspection_doc(qc):
    doc = _base(_qc_company(qc), "QA Inspection Record", f"QC-{qc.id:05d}", qc.get_status_display())
    doc["meta"] = _qc_meta(qc)
    doc["tables"] = _qc_tables(qc)
    if qc.remarks:
        doc["notes"].append(f"Remarks: {qc.remarks}")
    doc["signatures"] = ["Inspector", "QA Manager"]
    doc["references"] = " · ".join(filter(None, [
        f"QC #{qc.id}", qc.production_order.order_number if qc.production_order_id else None,
        f"Lot {qc.lot.batch_number}" if qc.lot_id else None,
    ]))
    return doc


def qa_certificate_doc(qc):
    if not qc.accepted_quantity:
        raise ValueError("A QA clearance certificate is only issued for accepted quantity.")
    doc = _base(_qc_company(qc), "QA Clearance Certificate", qc.certificate_number or f"QAC-{qc.id:05d}", "Cleared")
    doc["meta"] = _qc_meta(qc)
    doc["notes"].append(
        f"This certifies that {_n(qc.accepted_quantity)} unit(s)"
        + (f" of lot {qc.lot.batch_number}" if qc.lot_id else "")
        + " were inspected and released for use / sale."
    )
    doc["tables"] = _qc_tables(qc)[:1]
    doc["signatures"] = ["Inspector", "Authorised QA signatory"]
    doc["references"] = f"QC #{qc.id}" + (f" · {qc.production_order.order_number}" if qc.production_order_id else "")
    return doc


# ---------------------------------------------------------------------------
# Dispatch and finance documents
# ---------------------------------------------------------------------------

def _dispatch_meta(d):
    so = d.sales_order
    return [
        ("Customer", so.customer.name),
        ("Customer order", f"SO-{so.id}"),
        ("Ship to", d.ship_to_name),
        ("Address", d.ship_to_address),
        ("Dispatch date", _dt(d.dispatched_at)),
        ("Expected delivery", d.expected_delivery.isoformat() if d.expected_delivery else None),
        ("Shipment mode", d.get_shipment_mode_display() if d.shipment_mode else None),
        ("Carrier", d.carrier),
        ("Shipment reference", d.shipment_reference),
        ("Vehicle", d.vehicle_number),
        ("Driver", d.driver_name),
        ("Packages", d.packages or None),
        ("Gross weight", d.gross_weight),
        ("Warehouse", d.warehouse.name if d.warehouse_id else None),
    ]


def _dispatch_lines(d, with_lots=True):
    rows = []
    for line in d.lines.select_related("item", "sales_order_item"):
        lots = ""
        if with_lots and d.shipment_id:
            lots = ", ".join(
                f"{sl.lot.batch_number} ({_n(sl.quantity)})"
                for sl in d.shipment.shipment_lots.filter(lot__item=line.item).select_related("lot")
            )
        rows.append([
            _c(line.item.name), _c(line.item.unit), _c(_n(line.sales_order_item.quantity), True),
            _c(_n(line.quantity), True), _c(lots),
        ])
    return _table("Goods", ["Item", "Unit", ("Ordered", True), ("Delivered", True), "Lots"], rows)


def delivery_note_doc(d):
    doc = _base(d.company, "Delivery Note", d.dispatch_number, d.get_status_display())
    doc["meta"] = _dispatch_meta(d)[:8]
    doc["tables"] = [_dispatch_lines(d)]
    doc["notes"].append("Please check the goods on receipt and sign below. Note any damage or shortage on this copy.")
    doc["signatures"] = ["Dispatched by", "Received by (name, date, stamp)"]
    doc["references"] = f"{d.dispatch_number} · SO-{d.sales_order_id}" + (f" · INV-{d.invoice_id}" if d.invoice_id else "")
    return doc


def dispatch_note_doc(d):
    doc = _base(d.company, "Dispatch Note", d.dispatch_number, d.get_status_display(),
                "Internal / carrier copy with transport details.")
    doc["meta"] = _dispatch_meta(d)
    doc["tables"] = [_dispatch_lines(d)]
    if d.transport_details:
        doc["notes"].append(f"Transport details: {d.transport_details}")
    stages = [("Picked", d.picked_at), ("Packed", d.packed_at), ("Staged", d.staged_at),
              ("Shipment prepared", d.prepared_at), ("Verified", d.verified_at), ("Dispatched", d.dispatched_at)]
    doc["tables"].append(_table("Dispatch workflow", ["Stage", "Completed"], [[_c(s), _c(_dt(t))] for s, t in stages]))
    doc["signatures"] = ["Store", "Verified by", "Carrier / driver"]
    doc["references"] = f"{d.dispatch_number} · SO-{d.sales_order_id}"
    return doc


def _tax_label(order):
    return (order.tax_label if order is not None and order.tax_label else "Tax")


def invoice_doc(inv):
    customer = inv.customer
    doc = _base(inv.company or customer.company, "Commercial Sales Invoice", f"INV-{inv.id}", inv.get_status_display())
    dispatch = inv.dispatches.first()
    doc["meta"] = [
        ("Bill to", customer.name),
        ("Address", customer.address),
        ("Invoice date", inv.invoice_date.isoformat() if inv.invoice_date else None),
        ("Due date", inv.due_date.isoformat() if inv.due_date else None),
        ("Payment terms", customer.payment_terms or None),
        ("Customer order", f"SO-{inv.sales_order_id}" if inv.sales_order_id else None),
        ("Dispatch", dispatch.dispatch_number if dispatch else None),
        ("Shipment mode", dispatch.get_shipment_mode_display() if dispatch and dispatch.shipment_mode else None),
    ]
    rows = [[_c(l.description or l.item.name), _c(_n(l.quantity), True), _c(_money(l.unit_price), True),
             _c(_money(l.amount), True)] for l in inv.lines.select_related("item")]
    doc["tables"] = [_table("Lines", ["Description", ("Quantity", True), ("Unit price", True), ("Amount", True)], rows)]
    totals = []
    if inv.discount_amount or inv.tax_amount:
        totals.append(("Subtotal", _money(inv.subtotal)))
        if inv.discount_amount:
            totals.append(("Discount", "-" + _money(inv.discount_amount)))
        if inv.tax_amount:
            totals.append((_tax_label(inv.sales_order), _money(inv.tax_amount)))
    totals += [("Total", _money(inv.total_amount)), ("Paid", _money(inv.amount_paid)),
               ("Balance due", _money(inv.balance_due))]
    doc["totals"] = totals
    if inv.summary_invoice_id:
        doc["notes"].append(f"Included in summary invoice {inv.summary_invoice.number}.")
    doc["notes"].append("Prices are those agreed on the customer order.")
    doc["signatures"] = ["Authorised signatory"]
    doc["references"] = f"INV-{inv.id}" + (f" · SO-{inv.sales_order_id}" if inv.sales_order_id else "") + \
        (f" · {dispatch.dispatch_number}" if dispatch else "")
    return doc


def customer_order_confirmation_doc(order):
    customer = order.customer
    doc = _base(
        customer.company,
        "Customer Order Confirmation",
        f"SO-{order.id}",
        order.get_status_display(),
    )
    doc["subtitle"] = "Confirmation of the customer order and requested delivery."
    doc["meta"] = [
        ("Customer", customer.name),
        ("Customer email", customer.email or None),
        ("Customer phone", customer.phone or None),
        ("Order date", order.created_at.strftime("%Y-%m-%d") if order.created_at else None),
        ("Requested delivery", str(order.required_delivery_date) if order.required_delivery_date else None),
        ("Customer reference", order.customer_order_reference or None),
        ("Order source", order.get_source_display()),
        ("Priority", order.get_priority_display()),
        ("Delivery requirements", order.delivery_requirements or None),
    ]
    rows = [
        [
            _c(line.item.sku or ""),
            _c(line.item.name),
            _c(line.item.unit),
            _c(_n(line.quantity), True),
            _c(_money(line.unit_price), True),
            _c(_money(Decimal(str(line.quantity)) * line.unit_price), True),
        ]
        for line in order.salesorderitem_set.select_related("item").all()
    ]
    doc["tables"] = [_table(
        "Confirmed items",
        ["Item code", "Description", "Unit", ("Quantity", True), ("Unit price", True), ("Amount", True)],
        rows,
    )]
    totals = []
    discounts = list(order.discounts.all())
    if discounts or order.tax_amount:
        totals.append(("Subtotal", _money(order.subtotal)))
        for d in discounts:
            totals.append((f"{d.label} ({_n(d.percent, 2)}%)",
                           "-" + _money(order.subtotal * d.percent / Decimal("100"))))
        if order.tax_amount:
            totals.append((_tax_label(order), _money(order.tax_amount)))
    totals.append(("Order total", _money(order.total_amount)))
    doc["totals"] = totals
    deliveries = [d for d in order.dispatches.exclude(status="cancelled")]
    if deliveries:
        doc["tables"].append(_table(
            "Deliveries so far", ["Dispatch", "Status", ("Quantity", True), "Invoice"],
            [[_c(d.dispatch_number), _c(d.get_status_display()), _c(_n(d.total_quantity), True),
              _c(f"INV-{d.invoice_id}" if d.invoice_id else "Not invoiced")] for d in deliveries],
        ))
    if order.custom_specifications:
        doc["notes"].append(f"Custom specifications: {order.custom_specifications}")
    doc["references"] = f"SO-{order.id}"
    return doc


def summary_invoice_doc(summary):
    customer = summary.customer
    doc = _base(summary.company or customer.company, "Summary Invoice", summary.number)
    doc["subtitle"] = "Statement combining the invoices listed below."
    invoices = list(summary.invoices.select_related("sales_order").order_by("invoice_date", "id"))
    doc["meta"] = [
        ("Bill to", customer.name), ("Address", customer.address),
        ("Statement date", summary.created_at.strftime("%Y-%m-%d")),
        ("Payment terms", customer.payment_terms or None),
        ("Invoices combined", len(invoices)),
    ]
    doc["tables"] = [_table(
        "Invoices", ["Invoice", "Date", "Customer order", "Due date", ("Amount", True), ("Paid", True), ("Balance", True)],
        [[_c(f"INV-{i.id}"), _c(i.invoice_date.isoformat()),
          _c(f"SO-{i.sales_order_id}" if i.sales_order_id else ""),
          _c(i.due_date.isoformat() if i.due_date else ""),
          _c(_money(i.total_amount), True), _c(_money(i.amount_paid), True), _c(_money(i.balance_due), True)]
         for i in invoices],
    )]
    total = sum((i.total_amount for i in invoices), Decimal("0"))
    paid = sum((i.amount_paid for i in invoices), Decimal("0"))
    doc["totals"] = [("Total invoiced", _money(total)), ("Paid", _money(paid)), ("Balance due", _money(total - paid))]
    if summary.notes:
        doc["notes"].append(summary.notes)
    doc["signatures"] = ["Authorised signatory"]
    doc["references"] = summary.number + " · " + ", ".join(f"INV-{i.id}" for i in invoices)
    return doc


def vendor_bill_doc(bill):
    doc = _base(bill.company or bill.vendor.company, "Vendor Bill", bill.bill_number or f"BILL-{bill.id}",
                bill.get_status_display())
    doc["meta"] = [
        ("Supplier", bill.vendor.name), ("Supplier address", bill.vendor.address or None),
        ("Bill date", bill.bill_date.isoformat()),
        ("Due date", bill.due_date.isoformat() if bill.due_date else None),
        ("Purchase order", f"PO-{bill.purchase_order_id}" if bill.purchase_order_id else None),
    ]
    doc["tables"] = [_table(
        "Lines", ["Description", ("Quantity", True), ("Unit price", True), ("Amount", True)],
        [[_c(l.description or l.item.name), _c(_n(l.quantity), True), _c(_money(l.unit_price), True),
          _c(_money(l.amount), True)] for l in bill.lines.select_related("item")],
    )]
    doc["totals"] = [("Total", _money(bill.total_amount)), ("Paid", _money(bill.amount_paid)),
                     ("Balance due", _money(bill.balance_due))]
    doc["references"] = f"BILL-{bill.id}" + (f" · PO-{bill.purchase_order_id}" if bill.purchase_order_id else "")
    return doc


def _order_lines_table(order, title):
    rows = [
        [_c(line.item.sku or ""), _c(line.item.name), _c(line.item.unit), _c(_n(line.quantity), True),
         _c(_money(line.unit_price), True), _c(_money(Decimal(str(line.quantity)) * line.unit_price), True)]
        for line in order.salesorderitem_set.select_related("item").all()
    ]
    return _table(title, ["Item code", "Description", "Unit", ("Quantity", True), ("Unit price", True), ("Amount", True)], rows)


def _order_totals(order):
    totals = []
    discounts = list(order.discounts.all())
    if discounts or order.tax_amount:
        totals.append(("Subtotal", _money(order.subtotal)))
        for d in discounts:
            totals.append((f"{d.label} ({_n(d.percent, 2)}%)", "-" + _money(order.subtotal * d.percent / Decimal("100"))))
        if order.tax_amount:
            totals.append((_tax_label(order), _money(order.tax_amount)))
    totals.append(("Total", _money(order.total_amount)))
    return totals


def pro_forma_invoice_doc(order):
    customer = order.customer
    doc = _base(customer.company, "Pro Forma Invoice", f"PF-{order.id}", order.get_status_display())
    doc["subtitle"] = "Advance invoice for payment or customs. Not a tax invoice."
    doc["meta"] = [
        ("Bill to", customer.name), ("Address", customer.address),
        ("Date", timezone.localdate().isoformat()),
        ("Payment terms", customer.payment_terms or None),
        ("Customer order", f"SO-{order.id}"),
        ("Customer reference", order.customer_order_reference or None),
    ]
    doc["tables"] = [_order_lines_table(order, "Lines")]
    doc["totals"] = _order_totals(order)
    doc["signatures"] = ["Authorised signatory"]
    doc["references"] = f"PF-{order.id} · SO-{order.id}"
    return doc


def sales_quotation_doc(order):
    customer = order.customer
    doc = _base(customer.company, "Sales Quotation", f"QT-{order.id}")
    doc["subtitle"] = "Offer of sale. Becomes a customer order on acceptance."
    created = order.created_at.date() if order.created_at else timezone.localdate()
    doc["meta"] = [
        ("Customer", customer.name), ("Address", customer.address),
        ("Contact", customer.email or customer.phone or None),
        ("Quotation date", created.isoformat()),
        ("Valid until", (created + timedelta(days=30)).isoformat()),
        ("Requested delivery", str(order.required_delivery_date) if order.required_delivery_date else None),
        ("Payment terms", customer.payment_terms or None),
    ]
    doc["tables"] = [_order_lines_table(order, "Quoted items")]
    doc["totals"] = _order_totals(order)
    doc["notes"].append("Prices are net unless a tax line is shown. Subject to stock availability at order.")
    doc["signatures"] = ["Issued by", "Accepted by (name, date, stamp)"]
    doc["references"] = f"QT-{order.id}"
    return doc


def quotation_doc(q):
    customer = q.customer
    doc = _base(q.company or customer.company, "Quotation", q.quote_number, q.get_status_display())
    doc["subtitle"] = "Price quotation. It becomes a customer order on acceptance."
    doc["meta"] = [
        ("Customer", customer.name), ("Address", customer.address),
        ("Contact", customer.email or customer.phone or None),
        ("Quotation date", q.created_at.date().isoformat() if q.created_at else None),
        ("Valid until", q.valid_until.isoformat() if q.valid_until else None),
        ("Payment terms", customer.payment_terms or None),
    ]
    rows = [[_c(l.item.name), _c(l.item.unit), _c(_n(l.quantity), True), _c(_money(l.unit_price), True),
             _c(_money(Decimal(str(l.quantity)) * l.unit_price), True)]
            for l in q.lines.select_related("item")]
    doc["tables"] = [_table("Quoted items", ["Item", "Unit", ("Quantity", True), ("Unit price", True), ("Amount", True)], rows)]
    totals = []
    if q.discount_amount or q.tax_amount:
        totals.append(("Subtotal", _money(q.subtotal)))
        if q.discount_amount:
            totals.append((f"Discount ({q.discount_percent:g}%)", "-" + _money(q.discount_amount)))
        if q.tax_amount:
            totals.append((q.tax_label or "Tax", _money(q.tax_amount)))
    totals.append(("Total", _money(q.total_amount)))
    doc["totals"] = totals
    if q.notes:
        doc["notes"].append(q.notes)
    if q.terms:
        doc["notes"].append(q.terms)
    doc["notes"].append("Prices are valid until the date shown and are subject to stock availability at order.")
    doc["signatures"] = ["Issued by", "Accepted by (name, date, stamp)"]
    doc["references"] = q.quote_number
    return doc


def packing_note_doc(d):
    doc = _base(d.company, "Packing Note", d.dispatch_number, d.get_status_display())
    meta = _dispatch_meta(d)
    doc["meta"] = [m for m in meta if m[0] in ("Customer", "Customer order", "Ship to", "Address", "Warehouse",
                                                "Packages", "Gross weight")]
    rows = [[_c(l.item.name), _c(l.item.unit), _c(_n(l.sales_order_item.quantity), True),
             _c(_n(l.quantity), True), _c(_n(max(l.sales_order_item.quantity - l.sales_order_item.shipped_quantity, 0)), True)]
            for l in d.lines.select_related("item", "sales_order_item")]
    doc["tables"] = [_table("Packed goods", ["Item", "Unit", ("Ordered", True), ("Packed", True), ("Remaining on order", True)], rows)]
    doc["signatures"] = ["Packed by", "Checked by"]
    doc["references"] = f"{d.dispatch_number} · SO-{d.sales_order_id}"
    return doc


def production_plan_doc(plan):
    doc = _base(plan.company, "Production Plan", plan.plan_number, plan.get_status_display())
    doc["meta"] = [
        ("Customer order", f"SO-{plan.sales_order_id}"),
        ("Customer", plan.customer.name if plan.customer_id else None),
        ("Product", plan.item.name),
        ("Order quantity", _n(plan.order_quantity)),
        ("Planned quantity", _n(plan.planned_quantity)),
        ("Target delivery", str(plan.target_date) if plan.target_date else None),
        ("Remaining quantity", _n(plan.remaining_quantity)),
    ]
    from production.mrp import calculate_mrp_for_plan
    mrp = calculate_mrp_for_plan(plan)
    doc["tables"] = [_table(
        "Material plan",
        ["Material", "Category", ("Required", True), ("Available", True), ("On order", True),
         ("Shortage", True), "Unit"],
        [[_c(row["item_name"]), _c(row["category"]), _c(_n(row["required_quantity"]), True),
          _c(_n(row["available_quantity"]), True), _c(_n(row["on_order_quantity"]), True),
          _c(_n(row["shortage_quantity"]), True), _c(row["unit"])]
         for row in mrp["consolidated"]],
    )]
    if plan.notes:
        doc["notes"].append(plan.notes)
    doc["references"] = f"{plan.plan_number} · SO-{plan.sales_order_id}"
    return doc


def purchase_order_doc(po):
    doc = _base(po.vendor.company, "Purchase Order", f"PO-{po.id}", po.get_status_display())
    doc["meta"] = [
        ("Supplier", po.vendor.name),
        ("Supplier address", po.vendor.address or None),
        ("Expected delivery", str(po.expected_delivery) if po.expected_delivery else None),
        ("Customer order", f"SO-{po.requisition.sales_order_id}" if po.requisition and po.requisition.sales_order_id else None),
        ("Production plan", po.requisition.production_plan.plan_number if po.requisition and po.requisition.production_plan_id else None),
    ]
    rows = [[_c(line.item.sku or ""), _c(line.item.name),
             _c(line.unit_of_measure.code if line.unit_of_measure_id else line.item.unit),
             _c(_n(line.quantity), True), _c(_money(line.unit_price), True), _c(_money(line.total_price), True)]
            for line in po.items.select_related("item", "unit_of_measure")]
    doc["tables"] = [_table("Order lines", ["Item code", "Description", "Unit", ("Quantity", True),
                                               ("Unit price", True), ("Amount", True)], rows)]
    doc["totals"] = [("Total", _money(po.total_amount))]
    if po.notes:
        doc["notes"].append(po.notes)
    doc["signatures"] = ["Prepared by", "Approved by", "Supplier acknowledgement"]
    doc["references"] = f"PO-{po.id}"
    return doc


def goods_receipt_doc(receipt):
    po = receipt.purchase_order
    doc = _base(po.vendor.company, "Goods Receipt / Material Handover", f"GRN-{receipt.id}", po.get_status_display())
    doc["meta"] = [
        ("Supplier", po.vendor.name),
        ("Purchase order", f"PO-{po.id}"),
        ("Received at", _dt(receipt.received_at)),
        ("Warehouse", receipt.warehouse.name),
        ("Customer order", f"SO-{po.requisition.sales_order_id}" if po.requisition and po.requisition.sales_order_id else None),
    ]
    received = {l.po_item_id: l.quantity for l in receipt.lines.all()}
    doc["tables"] = [_table(
        "Received materials",
        ["Material", "Unit", ("Ordered quantity", True), ("Received now", True), "Incoming QC"],
        [[_c(line.item.name), _c(line.unit_of_measure.code if line.unit_of_measure_id else line.item.unit),
          _c(_n(line.quantity), True),
          _c(_n(received.get(line.id, line.quantity)), True),
          _c(", ".join(q.get_status_display() for q in receipt.quality_checks.filter(item=line.item)) or "Not required")]
         for line in po.items.select_related("item", "unit_of_measure")
         if not received or line.id in received],
    )]
    doc["signatures"] = ["Received by (Store)", "Inspected by (Quality)", "Issued to Production"]
    doc["references"] = f"GRN-{receipt.id} · PO-{po.id}"
    return doc


def stock_transfer_doc(t):
    doc = _base(t.company, "Stock Transfer Voucher", f"STV-{t.id:05d}", t.get_status_display())
    doc["meta"] = [
        ("From warehouse", t.source_warehouse.name), ("To warehouse", t.dest_warehouse.name),
        ("Created", _dt(t.created_at)), ("Completed", _dt(t.completed_at)),
        ("Reference", t.reference or None),
        ("Created by", t.created_by.username if t.created_by_id else None),
    ]
    doc["tables"] = [_table("Items", ["Item", "Unit", ("Quantity", True)],
                            [[_c(t.item.name), _c(t.item.unit), _c(_n(t.quantity), True)]])]
    doc["signatures"] = ["Issued by (source)", "Received by (destination)"]
    doc["references"] = f"Transfer #{t.id}"
    return doc


DOCUMENTS = {
    # type: (label, model, company lookup, builder)
    "customer_order_confirmation": (
        "Customer Order Confirmation", SalesOrder, "customer__company", customer_order_confirmation_doc
    ),
    "production_plan": ("Production Plan", ProductionPlan, "company", production_plan_doc),
    "purchase_order": ("Purchase Order", PurchaseOrder, "vendor__company", purchase_order_doc),
    "goods_receipt": ("Goods Receipt / Material Handover", GoodsReceipt, "purchase_order__vendor__company", goods_receipt_doc),
    "production_order": ("Production Order", ProductionOrder, "recipe__product__company", production_order_doc),
    "traveler": ("Production Traveler / Work Ticket", ProductionOrder, "recipe__product__company", traveler_doc),
    "pick_list": ("Material Pick List", ProductionOrder, "recipe__product__company", pick_list_doc),
    "dispatch_pick_list": ("Finished Goods Picking List", Dispatch, "company", dispatch_pick_list_doc),
    "material_requisition": ("Material Requisition", ProductionOrder, "recipe__product__company", material_requisition_doc),
    "qa_inspection": ("QA Inspection Record", QualityCheck, None, qa_inspection_doc),
    "qa_certificate": ("QA Clearance Certificate", QualityCheck, None, qa_certificate_doc),
    "delivery_note": ("Delivery Note", Dispatch, "company", delivery_note_doc),
    "dispatch_note": ("Dispatch Note", Dispatch, "company", dispatch_note_doc),
    "invoice": ("Commercial Sales Invoice", Invoice, "company", invoice_doc),
    "stock_transfer": ("Stock Transfer Voucher", StockTransfer, "company", stock_transfer_doc),
    "pro_forma_invoice": ("Pro Forma Invoice", SalesOrder, "customer__company", pro_forma_invoice_doc),
    "quotation": ("Quotation", Quotation, "company", quotation_doc),
    "sales_quotation": ("Sales Quotation", SalesOrder, "customer__company", sales_quotation_doc),
    "packing_note": ("Packing Note", Dispatch, "company", packing_note_doc),
    "summary_invoice": ("Summary Invoice", SummaryInvoice, "company", summary_invoice_doc),
    "vendor_bill": ("Vendor Bill", Bill, "company", vendor_bill_doc),
}


def get_object(doc_type, pk, company):
    from django.db.models import Q
    _label, model, lookup, _builder = DOCUMENTS[doc_type]
    qs = model.objects.filter(pk=pk)
    if model is QualityCheck:
        qs = qs.filter(Q(production_order__recipe__product__company=company) | Q(vendor__company=company))
    else:
        qs = qs.filter(**{lookup: company})
    return qs.first()



# ---------------------------------------------------------------------------
# Document register (AI Reports page)
# ---------------------------------------------------------------------------

def _d(value):
    return value.isoformat()[:10] if value else None


# type -> (section, queryset factory(company), describe(obj) -> (number, date, party, status))
def _register_sources(company):
    from django.db.models import Q
    return [
        ("Sales", "sales_quotation", SalesOrder.objects.filter(customer__company=company, status__in=("draft", "pending", "pending_approval")).select_related("customer"),
         lambda o: (f"QT-{o.id}", _d(o.created_at), o.customer.name, o.get_status_display())),
        ("Sales", "customer_order_confirmation", SalesOrder.objects.filter(customer__company=company).exclude(status="draft").select_related("customer"),
         lambda o: (f"SO-{o.id}", _d(o.created_at), o.customer.name, o.get_status_display())),
        ("Sales", "pro_forma_invoice", SalesOrder.objects.filter(customer__company=company).exclude(status__in=("draft", "cancelled")).select_related("customer"),
         lambda o: (f"PF-{o.id}", _d(o.created_at), o.customer.name, o.get_status_display())),
        ("Sales", "invoice", Invoice.objects.filter(company=company).select_related("customer"),
         lambda o: (f"INV-{o.id}", _d(o.invoice_date), o.customer.name, o.get_status_display())),
        ("Sales", "summary_invoice", SummaryInvoice.objects.filter(company=company).select_related("customer"),
         lambda o: (o.number, _d(o.created_at), o.customer.name, "")),
        ("Delivery", "dispatch_note", Dispatch.objects.filter(company=company).exclude(status="cancelled").select_related("sales_order__customer"),
         lambda o: (o.dispatch_number, _d(o.dispatched_at or o.created_at), o.sales_order.customer.name, o.get_status_display())),
        ("Delivery", "delivery_note", Dispatch.objects.filter(company=company, status__in=("dispatched", "delivered")).select_related("sales_order__customer"),
         lambda o: (o.dispatch_number, _d(o.dispatched_at), o.sales_order.customer.name, o.get_status_display())),
        ("Delivery", "packing_note", Dispatch.objects.filter(company=company, status__in=("packed", "staged", "prepared", "verified", "dispatched", "delivered")).select_related("sales_order__customer"),
         lambda o: (o.dispatch_number, _d(o.packed_at), o.sales_order.customer.name, o.get_status_display())),
        ("Delivery", "dispatch_pick_list", Dispatch.objects.filter(company=company).exclude(status="cancelled").select_related("sales_order__customer"),
         lambda o: (o.dispatch_number, _d(o.created_at), o.sales_order.customer.name, o.get_status_display())),
        ("Quality", "qa_inspection", QualityCheck.objects.filter(Q(production_order__recipe__product__company=company) | Q(vendor__company=company)),
         lambda o: (f"QC-{o.id}", _d(getattr(o, "created_at", None)), "", o.get_status_display() if hasattr(o, "get_status_display") else "")),
        ("Quality", "qa_certificate", QualityCheck.objects.filter(Q(production_order__recipe__product__company=company) | Q(vendor__company=company), status="approved"),
         lambda o: (f"QC-{o.id}", _d(getattr(o, "created_at", None)), "", "Approved")),
        ("Procurement", "purchase_order", PurchaseOrder.objects.filter(vendor__company=company).select_related("vendor"),
         lambda o: (f"PO-{o.id}", _d(o.created_at), o.vendor.name, o.get_status_display())),
        ("Procurement", "goods_receipt", GoodsReceipt.objects.filter(purchase_order__vendor__company=company).select_related("purchase_order__vendor"),
         lambda o: (f"GRN-{o.id}", _d(o.received_at), o.purchase_order.vendor.name, "Received")),
        ("Procurement", "vendor_bill", Bill.objects.filter(company=company).select_related("vendor"),
         lambda o: (o.bill_number or f"BILL-{o.id}", _d(o.bill_date), o.vendor.name, o.get_status_display())),
        ("Production", "production_plan", ProductionPlan.objects.filter(company=company).select_related("customer"),
         lambda o: (o.plan_number, _d(o.created_at), o.customer.name if o.customer_id else "", o.get_status_display())),
        ("Production", "production_order", ProductionOrder.objects.filter(recipe__product__company=company).select_related("recipe__product"),
         lambda o: (o.order_number, _d(getattr(o, "created_at", None)), o.recipe.product.name, o.get_status_display())),
        ("Inventory", "stock_transfer", StockTransfer.objects.filter(company=company),
         lambda o: (f"STV-{o.id:05d}", _d(o.created_at), "", o.get_status_display())),
    ]


def document_register(company, limit=150):
    """Every printable document the system can issue, grouped by section,
    newest first. Cheap to build: no document is rendered here."""
    sections = {}
    for section, doc_type, queryset, describe in _register_sources(company):
        label = DOCUMENTS[doc_type][0]
        rows = []
        for obj in queryset.order_by("-pk")[:limit]:
            number, date, party, status = describe(obj)
            rows.append({"type": doc_type, "label": label, "id": obj.pk, "number": number,
                         "date": date, "party": party, "status": status})
        sections.setdefault(section, []).extend(rows)
    return [{"section": name, "documents": sorted(rows, key=lambda r: r["date"] or "", reverse=True)}
            for name, rows in sections.items()]
