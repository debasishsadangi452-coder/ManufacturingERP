"""Printable business documents (Track B, TB-12 / TB-13).

Each builder turns one transaction into a document description that the
shared template renders. Documents are always built from live transaction
data and print the upstream references (customer order, production order,
lot, dispatch, invoice) so a paper copy can be traced back.
"""
from django.db.models import Sum

from fulfillment.models import Dispatch
from inventory.models import Batch, StockTransfer
from production.models import ProductionOrder
from quality.models import QualityCheck
from sales.models import Invoice


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
    doc["totals"] = [("Total", _money(inv.total_amount)), ("Paid", _money(inv.amount_paid)),
                     ("Balance due", _money(inv.balance_due))]
    doc["notes"].append("Prices are those agreed on the customer order.")
    doc["signatures"] = ["Authorised signatory"]
    doc["references"] = f"INV-{inv.id}" + (f" · SO-{inv.sales_order_id}" if inv.sales_order_id else "") + \
        (f" · {dispatch.dispatch_number}" if dispatch else "")
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
    "production_order": ("Production Order", ProductionOrder, "recipe__product__company", production_order_doc),
    "traveler": ("Production Traveler / Work Ticket", ProductionOrder, "recipe__product__company", traveler_doc),
    "pick_list": ("Material Pick List", ProductionOrder, "recipe__product__company", pick_list_doc),
    "material_requisition": ("Material Requisition", ProductionOrder, "recipe__product__company", material_requisition_doc),
    "qa_inspection": ("QA Inspection Record", QualityCheck, None, qa_inspection_doc),
    "qa_certificate": ("QA Clearance Certificate", QualityCheck, None, qa_certificate_doc),
    "delivery_note": ("Delivery Note", Dispatch, "company", delivery_note_doc),
    "dispatch_note": ("Dispatch Note", Dispatch, "company", dispatch_note_doc),
    "invoice": ("Commercial Sales Invoice", Invoice, "company", invoice_doc),
    "stock_transfer": ("Stock Transfer Voucher", StockTransfer, "company", stock_transfer_doc),
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
