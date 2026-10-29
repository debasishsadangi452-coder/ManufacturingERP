"""
Routes operational events (sales invoice, vendor bill, customer payment...) into
the Indian ledger for companies that chose the Indian system.

``intercept`` is called from the legacy ``queue_auto_post``; when it returns True
the legacy posting is skipped so nothing is booked twice or in the wrong books.
GST is never guessed: missing state / unknown rate => the event is logged as
``failed`` and can be replayed after the master data is fixed.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import engine, geo, gst, valuation
from .models import AccountingMode, BillTaxDetail, IndianCompanyProfile, Ledger, PostingEvent, TaxCode

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")


class Skip(Exception):
    pass


def is_indian(company):
    return bool(company) and AccountingMode.objects.filter(company=company, mode="indian").exists()


def intercept(event, company, source_id, user=None, payload=None):
    """True if the company is on the Indian system (event handled/scheduled here)."""
    try:
        if not is_indian(company):
            return False
        transaction.on_commit(lambda: execute(event, company, source_id, user, payload))
        return True
    except Exception:
        logger.exception("Indian accounting intercept failed for %s #%s", event, source_id)
        return False


def _party_ledger(company, kind, obj):
    from .models import AccountGroup
    if kind == "customer":
        l = Ledger.objects.filter(company=company, customer=obj).first()
        texts = (obj.tax_status, obj.address)
        if l is None:
            l = Ledger.objects.create(company=company, code=f"C{obj.id:06d}", name=obj.name, customer=obj,
                                      group=AccountGroup.objects.get(company=company, name="Sundry Debtors"),
                                      control_type="receivable")
    else:
        l = Ledger.objects.filter(company=company, vendor=obj).first()
        texts = (obj.tax_id, obj.address)
        if l is None:
            l = Ledger.objects.create(company=company, code=f"S{obj.id:06d}", name=obj.name, vendor=obj,
                                      group=AccountGroup.objects.get(company=company, name="Sundry Creditors"),
                                      control_type="payable")
    if not l.state_code:   # never overwrite a state someone set by hand
        state, gstin, source = geo.derive(*texts)
        if state:
            l.state_code, l.state_source = state, source
            if gstin and not l.gstin:
                l.gstin = gstin
            l.save()
    return l


def _post_sales_invoice(company, source_id, user, payload):
    from sales.models import Invoice
    inv = Invoice.objects.select_related("customer").filter(pk=source_id, company=company).first()
    if not inv:
        raise ValidationError(f"Invoice #{source_id} not found.")
    if inv.total_amount <= 0:
        raise Skip(f"INV-{inv.id} has no amount.")
    profile = IndianCompanyProfile.objects.get(company=company)
    party = _party_ledger(company, "customer", inv.customer)
    if not party.state_code:
        raise ValidationError(f"Set the GST state code on ledger '{party.name}' (place of supply), then retry. "
                              "It could not be worked out from the customer's GSTIN or address.")
    tax = inv.tax_amount or ZERO
    taxable = (inv.subtotal - inv.discount_amount) if inv.subtotal else (inv.total_amount - tax)
    if taxable <= 0:
        raise ValidationError(f"INV-{inv.id}: taxable value is not positive.")
    rate, code = ZERO, None
    if tax > 0:
        rate = (tax * 100 / taxable).quantize(Decimal("0.01"))
        code = TaxCode.objects.filter(company=company, gst_rate=rate, treatment="taxable", is_active=True,
                                      effective_from__lte=inv.invoice_date).order_by("-effective_from").first()
        if code is None:
            raise ValidationError(f"INV-{inv.id}: tax {tax} on {taxable} implies {rate}%, which is not a configured GST rate.")
    doc = gst.calculate_document([dict(quantity=1, unit_price=taxable, rate=rate)], seller_state=profile.state_code,
                                 place_of_supply=party.state_code, round_to_rupee=False,
                                 treatment="taxable" if code else "nil")
    if abs(doc["tax_total"] - tax) > Decimal("0.05"):
        raise ValidationError(f"INV-{inv.id}: recomputed GST {doc['tax_total']} differs from invoice tax {tax}.")
    diff = inv.total_amount - (taxable + doc["tax_total"])
    if abs(diff) > Decimal("1.00"):
        raise ValidationError(f"INV-{inv.id}: total does not reconcile with subtotal, discount and tax.")
    doc["round_off"], doc["total"] = diff, inv.total_amount
    voucher = engine.post_tax_invoice(
        company, user, kind="sales", voucher_date=inv.invoice_date, party_ledger=party, place_of_supply=party.state_code,
        doc=doc, seller_state=profile.state_code, tax_code=code,
        source={"module": "sales", "document_type": "invoice", "document_id": inv.id, "event": "sales"},
        narration=f"Sales invoice INV-{inv.id} to {inv.customer.name}", post=True, due_date=inv.due_date)
    _cogs_for_invoice(company, user, inv)
    return voucher


def _grni_available(company, po_id):
    """Uncleared GRNI for a PO: receipts credited less bills/rejections debited."""
    from django.db.models import Sum
    from .models import VoucherLine
    agg = VoucherLine.objects.filter(
        company=company, is_posted=True, ledger=engine.mapped_ledger(company, "grni"),
        voucher__reference=f"PO-{po_id}").aggregate(d=Sum("debit"), c=Sum("credit"))
    return max((agg["c"] or ZERO) - (agg["d"] or ZERO), ZERO)


def _post_bill_with_gst(company, user, bill, party, detail):
    profile = IndianCompanyProfile.objects.get(company=company)
    tc = detail.tax_code
    doc = gst.calculate_document([dict(quantity=1, unit_price=detail.taxable_value, rate=tc.gst_rate)],
                                 seller_state=detail.supplier_state, place_of_supply=profile.state_code,
                                 round_to_rupee=False, treatment=tc.treatment)
    diff = bill.total_amount - (detail.taxable_value + doc["tax_total"])
    if abs(diff) > Decimal("1.00"):
        raise ValidationError(
            f"Bill total {bill.total_amount} does not match taxable value {detail.taxable_value} + GST {doc['tax_total']}.")
    doc["round_off"], doc["total"] = diff, bill.total_amount
    purchase_cost = detail.taxable_value + (ZERO if detail.itc_eligible else doc["tax_total"])
    clear = min(purchase_cost, _grni_available(company, bill.purchase_order_id)) if bill.purchase_order_id else ZERO
    return engine.post_tax_invoice(
        company, user, kind="purchase", voucher_date=bill.bill_date, party_ledger=party,
        place_of_supply=profile.state_code, doc=doc, seller_state=detail.supplier_state, tax_code=tc,
        itc_eligible=detail.itc_eligible, grni_clear=clear, supplier_invoice_no=bill.bill_number or "",
        reference=f"PO-{bill.purchase_order_id}" if bill.purchase_order_id else "",
        source={"module": "procurement", "document_type": "bill", "document_id": bill.id, "event": "purchase"},
        narration=f"Vendor bill {bill.bill_number or bill.id} from {bill.vendor.name}", post=True,
        due_date=bill.due_date)


def _derive_bill_detail(company, bill, party):
    """GST breakup for a PO-backed bill: bill total less the value of goods received/ordered is the tax, and it
    must equal a configured GST rate. No tax => None (booked without GST). Anything else must be entered by hand."""
    if not bill.purchase_order_id:
        return None
    base = _grni_available(company, bill.purchase_order_id)
    if base <= 0:
        base = sum(((i.unit_price or ZERO) * Decimal(str(i.quantity)) for i in bill.purchase_order.items.all()), ZERO).quantize(ZERO)
    if base <= 0 or bill.total_amount <= base:
        return None
    tax = bill.total_amount - base
    rate = (tax * 100 / base).quantize(Decimal("0.01"))
    label = f"bill {bill.bill_number or bill.id}"
    code = None
    for tc in TaxCode.objects.filter(company=company, treatment="taxable", is_active=True, gst_rate__gt=0,
                                     effective_from__lte=bill.bill_date).order_by("-effective_from"):
        if abs((base * tc.gst_rate / 100).quantize(ZERO) - tax) <= Decimal("1.00"):
            code = tc
            break
    if code is None:
        raise ValidationError(
            f"{label}: total {bill.total_amount} less goods value {base} leaves {tax} ({rate}%), which is not a configured "
            "GST rate. Enter the GST details manually.")
    if not party.state_code:
        raise ValidationError(f"Set the GST state code on supplier ledger '{party.name}' (from its GSTIN), then retry.")
    d, _ = BillTaxDetail.objects.update_or_create(
        bill=bill, defaults=dict(company=company, tax_code=code, taxable_value=base, supplier_state=party.state_code,
                                 itc_eligible=code.itc_eligible, derived=True))
    return d


def _post_vendor_bill(company, source_id, user, payload):
    from procurement.models import Bill
    bill = Bill.objects.select_related("vendor").filter(pk=source_id, company=company).first()
    if not bill:
        raise ValidationError(f"Bill #{source_id} not found.")
    if bill.total_amount <= 0:
        raise Skip(f"Bill {bill.bill_number or bill.id} has no amount.")
    party = _party_ledger(company, "vendor", bill.vendor)
    total = bill.total_amount
    detail = BillTaxDetail.objects.select_related("tax_code").filter(bill=bill).first()
    if detail is None:
        detail = _derive_bill_detail(company, bill, party)
    if detail is None and IndianCompanyProfile.objects.get(company=company).require_bill_gst:
        raise ValidationError(f"Enter the GST details for bill {bill.bill_number or bill.id}, then retry.")
    if detail is not None:
        return _post_bill_with_gst(company, user, bill, party, detail)
    # Goods already received sit in stock against GRNI: clear that; only the rest is a fresh purchase.
    clear = min(total, _grni_available(company, bill.purchase_order_id)) if bill.purchase_order_id else ZERO
    lines = []
    if clear > 0:
        lines.append({"ledger": engine.mapped_ledger(company, "grni"), "debit": clear})
    if total - clear > 0:
        lines.append({"ledger": engine.mapped_ledger(company, "purchases"), "debit": total - clear})
    lines.append({"ledger": party, "credit": total})
    # The ERP bill carries no GST breakup, so no input-credit split is booked yet.
    return engine.create_voucher(
        company, user, voucher_type="purchase", voucher_date=bill.bill_date, party_ledger=party,
        supplier_invoice_no=bill.bill_number or "", post=True, lines=lines, due_date=bill.due_date,
        reference=f"PO-{bill.purchase_order_id}" if bill.purchase_order_id else "",
        source={"module": "procurement", "document_type": "bill", "document_id": bill.id, "event": "purchase"},
        narration=f"Vendor bill {bill.bill_number or bill.id} from {bill.vendor.name} (GST breakup not captured)")


def _post_customer_payment(company, source_id, user, payload):
    from sales.models import CustomerPayment
    pay = CustomerPayment.objects.select_related("customer").filter(pk=source_id, company=company).first()
    if not pay:
        raise ValidationError(f"Payment #{source_id} not found.")
    party = _party_ledger(company, "customer", pay.customer)
    cash = engine.mapped_ledger(company, "cash" if pay.method == "cash" else "bank")
    receipt = engine.create_voucher(
        company, user, voucher_type="receipt", voucher_date=pay.payment_date, party_ledger=party, post=True,
        reference=pay.reference, lines=[{"ledger": cash, "debit": pay.amount}, {"ledger": party, "credit": pay.amount}],
        source={"module": "sales", "document_type": "payment", "document_id": pay.id, "event": "receipt"},
        narration=f"Receipt from {pay.customer.name}")
    _settle_linked_invoice(company, user, receipt, pay)
    return receipt


def _settle_linked_invoice(company, user, receipt, pay):
    if pay.invoice_id:
        _settle_linked(company, user, receipt, "sales", "invoice", pay.invoice_id, "sales", pay.payment_date)


def _settle_linked(company, user, payment_voucher, module, doc_type, doc_id, invoice_type, on_date):
    """A payment recorded against a specific ERP document settles that document (explicit link, no guessing).
    If the document is not posted yet or the amount exceeds it, the remainder stays an unallocated advance."""
    from . import allocations
    from .models import Voucher
    if allocations.allocated_from(payment_voucher) > 0:
        return
    inv = Voucher.objects.filter(company=company, source_module=module, source_document_type=doc_type,
                                 source_document_id=str(doc_id), voucher_type=invoice_type, status="posted").first()
    if inv is None:
        return
    amount = min(allocations.unallocated(payment_voucher), allocations.outstanding(inv))
    if amount > 0:
        allocations.allocate(company, user, payment_id=payment_voucher.id, invoice_id=inv.id, amount=amount, on_date=on_date)


_INV_KEY = {"1210": "raw_material", "1225": "finished_goods", "1230": "finished_goods"}


class NoChange(Exception):
    """A repeatable event with nothing new to book; leaves the recorded outcome untouched."""


def _method(company):
    return IndianCompanyProfile.objects.get(company=company).costing_method


def _cost(item):
    """(standard unit cost, stock ledger key) from the ERP's costing (read-only)."""
    from accounting.auto_posting import standard_unit_cost
    unit, code = standard_unit_cost(item)
    return (unit or ZERO), _INV_KEY.get(code, "finished_goods")


def _issue_value(company, item, qty, method=None):
    """What issuing ``qty`` of ``item`` is worth under the company's costing method."""
    qty = Decimal(str(qty or 0))
    std, _ = _cost(item)
    if (method or _method(company)) == "weighted_average":
        return valuation.issue_value(company, item, qty, std)
    return (std * qty).quantize(ZERO)


def _cogs_voucher(company, user, *, date, items, source, narration, reference=""):
    """Dr COGS / Cr stock for [(item, qty)]; raises Skip when nothing has a cost."""
    method = _method(company)
    credits, issues = {}, []
    for item, qty in items:
        qty = Decimal(str(qty or 0))
        if qty <= 0:
            continue
        amt = _issue_value(company, item, qty, method)
        if amt > 0:
            credits[_cost(item)[1]] = credits.get(_cost(item)[1], ZERO) + amt
            issues.append((item, qty, amt))
    total = sum(credits.values(), ZERO)
    if total <= 0:
        raise Skip("Items have no cost; no cost of goods sold booked.")
    lines = [{"ledger": engine.mapped_ledger(company, "cogs"), "debit": total}]
    lines += [{"ledger": engine.mapped_ledger(company, k), "credit": a} for k, a in sorted(credits.items())]
    v = engine.create_voucher(company, user, voucher_type="journal", voucher_date=date, lines=lines, post=True,
                              source=source, narration=narration, reference=reference)
    for item, qty, amt in issues:
        valuation.record(company, item, -qty, -amt, "cogs", v, date)
    return v


def _cogs_policy(company, wanted):
    if IndianCompanyProfile.objects.get(company=company).cogs_on != wanted:
        other = "invoice" if wanted == "dispatch" else "dispatch"
        raise Skip(f"Cost of goods sold is recognised on {other} under this company's policy.")


def _post_dispatch(company, source_id, user, payload):
    from fulfillment.models import Dispatch
    _cogs_policy(company, "dispatch")
    d = Dispatch.objects.select_related("sales_order").filter(pk=source_id, company=company).first()
    if not d:
        raise ValidationError(f"Dispatch #{source_id} not found.")
    return _cogs_voucher(
        company, user, date=timezone.localdate(), items=[(l.item, l.quantity) for l in d.lines.select_related("item")],
        source={"module": "fulfillment", "document_type": "dispatch", "document_id": d.id, "event": "cogs"},
        narration=f"Cost of goods sold - {d.dispatch_number} (SO-{d.sales_order_id})", reference=d.dispatch_number)


def _post_sales_shipment(company, source_id, user, payload):
    from inventory.models import Item
    from sales.models import SalesOrder
    _cogs_policy(company, "dispatch")
    so = SalesOrder.objects.filter(pk=source_id, customer__company=company).first()
    if not so:
        raise ValidationError(f"Sales order #{source_id} not found.")
    items = [(Item.objects.get(pk=l["item_id"]), l["quantity"]) for l in payload.get("lines", [])
             if Item.objects.filter(pk=l.get("item_id")).exists()]
    return _cogs_voucher(
        company, user, date=timezone.localdate(), items=items,
        source={"module": "sales", "document_type": "shipment", "document_id": so.id, "event": "cogs"},
        narration=f"Cost of goods shipped - SO-{so.id}", reference=f"SHIP-SO-{so.id}")


def _cogs_for_invoice(company, user, inv):
    """COGS booked with the invoice when the policy says so (separate voucher, own source key)."""
    if IndianCompanyProfile.objects.get(company=company).cogs_on != "invoice":
        return None
    try:
        return _cogs_voucher(
            company, user, date=inv.invoice_date, items=[(l.item, l.quantity) for l in inv.lines.select_related("item")],
            source={"module": "sales", "document_type": "invoice", "document_id": inv.id, "event": "cogs"},
            narration=f"Cost of goods sold - INV-{inv.id}", reference=f"INV-{inv.id}")
    except Skip:
        return None


def _post_goods_receipt(company, source_id, user, payload):
    from procurement.models import GoodsReceipt
    r = GoodsReceipt.objects.select_related("purchase_order__vendor").filter(
        pk=source_id, purchase_order__vendor__company=company).first()
    if not r:
        raise ValidationError(f"Goods receipt #{source_id} not found.")
    po = r.purchase_order
    rl = list(r.lines.select_related("po_item__item"))
    if rl:
        received = [(l.po_item.item, Decimal(str(l.quantity)), (l.po_item.unit_price or ZERO)) for l in rl]
    else:
        received = [(i.item, Decimal(str(i.quantity)), (i.unit_price or ZERO)) for i in po.items.select_related("item")]
    amount = sum((q * p for _, q, p in received), ZERO).quantize(ZERO)
    if amount <= 0:
        raise Skip(f"PO-{po.id} has no priced lines; nothing to capitalise.")
    day = timezone.localdate(r.received_at)
    v = engine.create_voucher(
        company, user, voucher_type="journal", voucher_date=day, post=True,
        reference=f"PO-{po.id}", narration=f"Goods received for PO-{po.id} from {po.vendor.name} (GRN-{r.id})",
        lines=[{"ledger": engine.mapped_ledger(company, "raw_material"), "debit": amount},
               {"ledger": engine.mapped_ledger(company, "grni"), "credit": amount}],
        source={"module": "procurement", "document_type": "receipt", "document_id": r.id, "event": "grn"})
    for item, qty, price in received:
        if qty > 0 and item is not None:
            valuation.record(company, item, qty, qty * price, "grn", v, day)
    return v


def _post_incoming_rejection(company, source_id, user, payload):
    from procurement.models import GoodsReceipt, PurchaseOrderItem
    from .models import Voucher
    r = GoodsReceipt.objects.select_related("purchase_order__vendor").filter(
        pk=payload.get("receipt_id"), purchase_order__vendor__company=company).first()
    if not r:
        raise ValidationError("Goods receipt not found.")
    if not Voucher.objects.filter(company=company, source_module="procurement", source_document_type="receipt",
                                  source_document_id=str(r.id), status="posted").exists():
        raise Skip("The goods receipt was not posted, so there is no inventory value to reverse.")
    line = PurchaseOrderItem.objects.select_related("item").filter(purchase_order=r.purchase_order, item_id=payload.get("item_id")).first()
    qty = Decimal(str(payload.get("quantity") or 0))
    amount = ((line.unit_price if line else 0) * qty).quantize(ZERO)
    if amount <= 0:
        raise Skip("Rejected material has no PO price.")
    po = r.purchase_order
    v = engine.create_voucher(
        company, user, voucher_type="journal", voucher_date=timezone.localdate(), post=True, reference=f"PO-{po.id}",
        narration=f"Incoming material rejected by QA - PO-{po.id} ({po.vendor.name}); claim against vendor",
        lines=[{"ledger": engine.mapped_ledger(company, "grni"), "debit": amount},
               {"ledger": engine.mapped_ledger(company, "raw_material"), "credit": amount}],
        source={"module": payload.get("source_module", "quality"), "document_type": "rejection",
                "document_id": source_id, "event": "reject"})
    valuation.record(company, line.item, -qty, -amount, "reject", v, timezone.localdate())
    return v


def _consumed_so_far(company, order_id):
    """{item_id: qty already issued to this order} from its non-voided production movements."""
    from django.db.models import Sum
    from .models import InventoryMovement
    rows = (InventoryMovement.objects.filter(company=company, kind="production_out", voided=False,
                                             voucher__source_module="production", voucher__source_document_type="order",
                                             voucher__source_document_id=str(order_id))
            .values("item_id").annotate(q=Sum("qty")))
    return {r["item_id"]: -r["q"] for r in rows}


def _post_production(company, source_id, user, payload):
    """Work-in-progress accounting for one production order, posted incrementally.

    Each run books everything incurred since the last run (materials issued, labour and overhead from the ERP's
    costing) into WIP, then moves the quality-approved units out of WIP at the order's cost per unit - good units to
    finished goods, scrap to the scrap expense. Cost of units not yet approved stays in WIP."""
    from inventory.models import Item
    from production.costing import order_cost
    from production.models import ProductionOrder
    from .models import InventoryMovement, ProductionPosting
    order = ProductionOrder.objects.select_related("recipe__product").filter(
        pk=source_id, recipe__product__company=company).first()
    if not order:
        raise ValidationError(f"Production order #{source_id} not found.")
    product = order.recipe.product
    method = _method(company)
    good = Decimal(str(payload.get("completed_qty") or 0))
    scrap = Decimal(str(payload.get("scrap_qty") or 0))
    pp, _ = ProductionPosting.objects.select_for_update().get_or_create(company=company, order_id=order.id)
    d_good, d_scrap = max(good - pp.good_qty, ZERO), max(scrap - pp.scrap_qty, ZERO)
    if d_good <= 0 and d_scrap <= 0:
        raise NoChange()

    actual = order_cost(order)["actual"]
    std_unit, fg_key = _cost(product)
    if fg_key == "raw_material":
        raise Skip("Product has no recipe cost; nothing to transfer.")
    units_done = good + scrap
    # --- material: issue what has been consumed since the last run
    issues, material_cum = [], ZERO
    consumed = {m["item_id"]: Decimal(str(m["quantity"])) for m in actual["materials"] if m["quantity"]}
    if consumed:
        already = _consumed_so_far(company, order.id)
        for item_id, qty in consumed.items():
            delta = qty - already.get(item_id, ZERO)
            if delta > 0:
                item = Item.objects.get(pk=item_id)
                if method == "weighted_average":
                    value = valuation.issue_value(company, item, delta, _cost(item)[0])
                else:
                    unit = Decimal(str(next(m["unit_cost"] for m in actual["materials"] if m["item_id"] == item_id)))
                    value = (unit * delta).quantize(ZERO)
                issues.append((item, delta, value))
        material_delta = sum((v for _, _, v in issues), ZERO)
        if method != "weighted_average":   # standard: ERP's own actual material cost is the figure of record
            material_delta = max(Decimal(str(actual["raw_material_cost"])) - pp.material, ZERO)
    else:   # nothing recorded as consumed: cost the approved units at standard recipe cost
        material_delta = max((std_unit * units_done).quantize(ZERO) - pp.material, ZERO)
    labour_cum = Decimal(str(actual["labour_cost"])).quantize(ZERO)
    overhead_cum = (Decimal(str(actual["machine_cost"])) + Decimal(str(actual["operation_cost"]))
                    + Decimal(str(actual["overhead_cost"])) + Decimal(str(actual["extra_cost"]))).quantize(ZERO)
    labour_d, overhead_d = max(labour_cum - pp.labour, ZERO), max(overhead_cum - pp.overhead, ZERO)
    material_cum = pp.material + material_delta
    total_cum = material_cum + pp.labour + labour_d + pp.overhead + overhead_d
    produced = max(Decimal(str(order.produced_quantity or 0)), units_done)
    unit_cost = (total_cum / produced).quantize(Decimal("0.0001")) if produced > 0 else ZERO
    fg_amt, scrap_amt = (unit_cost * d_good).quantize(ZERO), (unit_cost * d_scrap).quantize(ZERO)
    incurred = material_delta + labour_d + overhead_d
    if incurred <= 0 and fg_amt + scrap_amt <= 0:
        raise Skip("No cost to book yet.")

    L = lambda key: engine.mapped_ledger(company, key)
    lines = []
    if incurred > 0:
        lines.append({"ledger": L("wip"), "debit": incurred, "narration": "Cost incurred"})
    if material_delta > 0:
        lines.append({"ledger": L("raw_material"), "credit": material_delta})
    if labour_d > 0:
        lines.append({"ledger": L("labour_absorbed"), "credit": labour_d})
    if overhead_d > 0:
        lines.append({"ledger": L("overhead_absorbed"), "credit": overhead_d})
    if fg_amt > 0:
        lines.append({"ledger": L(fg_key), "debit": fg_amt, "narration": "Approved good units to stock"})
    if scrap_amt > 0:
        lines.append({"ledger": L("scrap"), "debit": scrap_amt, "narration": "Scrap"})
    if fg_amt + scrap_amt > 0:
        lines.append({"ledger": L("wip"), "credit": fg_amt + scrap_amt, "narration": "Transferred out of WIP"})
    if len(lines) < 2:
        raise Skip("No cost to book yet.")
    day = timezone.localdate()
    v = engine.create_voucher(
        company, user, voucher_type="journal", voucher_date=day, post=True,
        reference=order.order_number or f"PO#{order.id}", lines=lines,
        narration=(f"Production {order.order_number or order.id}: material {material_delta}, labour {labour_d}, overhead {overhead_d}"
                   f"; {d_good:g} good / {d_scrap:g} scrap of {product.name} at {unit_cost}"),
        source={"module": "production", "document_type": "order", "document_id": order.id,
                "event": f"completion{pp.runs + 1}"})
    for item, qty, value in issues:
        valuation.record(company, item, -qty, -value, "production_out", v, day)
    if d_good > 0:
        valuation.record(company, product, d_good, fg_amt, "production_in", v, day)
    pp.good_qty, pp.scrap_qty = good, scrap
    pp.material, pp.labour, pp.overhead = material_cum, pp.labour + labour_d, pp.overhead + overhead_d
    pp.runs += 1
    pp.save()
    return v


def _post_scrap(company, source_id, user, payload):
    from production.models import ScrapRecord
    rec = ScrapRecord.objects.select_related("item", "production_order").filter(
        pk=source_id, production_order__recipe__product__company=company).first()
    if not rec:
        raise ValidationError(f"Scrap record #{source_id} not found.")
    if rec.gl_treatment == "covered":
        raise Skip("Included in the production order's completion entry.")
    amount = Decimal(str(rec.cost_impact or 0)).quantize(ZERO)
    if amount <= 0:
        raise Skip("Scrap has no cost.")
    key = _cost(rec.item)[1] if rec.gl_treatment == "raw_material" else "finished_goods"
    v = engine.create_voucher(
        company, user, voucher_type="journal", voucher_date=timezone.localdate(), post=True, reference=f"SCRAP-{rec.id}",
        narration=f"Scrap write-off: {rec.quantity:g} {rec.item.name}",
        lines=[{"ledger": engine.mapped_ledger(company, "scrap"), "debit": amount},
               {"ledger": engine.mapped_ledger(company, key), "credit": amount}],
        source={"module": "production", "document_type": "scrap", "document_id": rec.id, "event": "scrap"})
    valuation.record(company, rec.item, -Decimal(str(rec.quantity)), -amount, "production_out", v, timezone.localdate())
    return v


def _post_vendor_payment(company, source_id, user, payload):
    from procurement.models import VendorPayment
    pay = VendorPayment.objects.select_related("vendor", "bill").filter(pk=source_id).first()
    if not pay or (pay.company_id or pay.vendor.company_id) != company.id:
        raise ValidationError(f"Vendor payment #{source_id} not found.")
    party = _party_ledger(company, "vendor", pay.vendor)
    cash = engine.mapped_ledger(company, "cash" if pay.method == "cash" else "bank")
    voucher = engine.create_voucher(
        company, user, voucher_type="payment", voucher_date=pay.payment_date, party_ledger=party, post=True,
        reference=pay.reference, lines=[{"ledger": party, "debit": pay.amount}, {"ledger": cash, "credit": pay.amount}],
        source={"module": "procurement", "document_type": "payment", "document_id": pay.id, "event": "payment"},
        narration=f"Payment to {pay.vendor.name}")
    if pay.bill_id:
        _settle_linked(company, user, voucher, "procurement", "bill", pay.bill_id, "purchase", pay.payment_date)
    return voucher


HANDLERS = {
    "sales_invoice": _post_sales_invoice, "vendor_bill": _post_vendor_bill, "customer_payment": _post_customer_payment,
    "goods_receipt": _post_goods_receipt, "incoming_rejection": _post_incoming_rejection,
    "production_completed": _post_production, "scrap_write_off": _post_scrap,
    "dispatch_shipment": _post_dispatch, "sales_shipment": _post_sales_shipment,
    "vendor_payment": _post_vendor_payment,
}
REPEATABLE = {"production_completed"}   # later completions book only what is new


def _prepare(event, company, source_id):
    """Create the party ledger outside the posting transaction so it survives a failed
    posting and the user can fix its state/GSTIN before retrying."""
    if event in ("sales_invoice", "customer_payment"):
        from sales.models import CustomerPayment, Invoice
        model = Invoice if event == "sales_invoice" else CustomerPayment
        obj = model.objects.select_related("customer").filter(pk=source_id, company=company).first()
        if obj:
            _party_ledger(company, "customer", obj.customer)
    elif event == "vendor_bill":
        from procurement.models import Bill
        obj = Bill.objects.select_related("vendor").filter(pk=source_id, company=company).first()
        if obj:
            _party_ledger(company, "vendor", obj.vendor)
    elif event == "vendor_payment":
        from procurement.models import VendorPayment
        obj = VendorPayment.objects.select_related("vendor").filter(pk=source_id).first()
        if obj:
            _party_ledger(company, "vendor", obj.vendor)


def execute(event, company, source_id, user=None, payload=None):
    """Run a handler and record the outcome. Never raises."""
    user = user if getattr(user, "is_authenticated", False) else None
    rec, created = PostingEvent.objects.get_or_create(
        company=company, event=event, source_id=str(source_id),
        defaults={"status": "failed", "payload": payload or {}})
    from . import seeds
    seeds.ensure_ledgers(company)
    if not created:
        if rec.status == "posted" and event not in REPEATABLE:
            return rec
        rec.attempts += 1
    if payload:
        rec.payload = payload
    voucher = None
    try:
        handler = HANDLERS.get(event)
        if handler is None:
            raise Skip("Not yet supported by the Indian accounting system; no entry booked.")
        _prepare(event, company, source_id)
        with transaction.atomic():
            voucher = handler(company, source_id, user, payload or {})
        rec.status, rec.message, rec.voucher = "posted", f"Posted {voucher.voucher_number}", voucher
    except NoChange:
        if created:   # first sight of this event with nothing to book
            rec.status, rec.message = "skipped", "Nothing to book yet."
        else:
            rec.attempts -= 1
            return rec
    except Skip as e:
        rec.status, rec.message = "skipped", str(e)
    except ValidationError as e:
        rec.status, rec.message = "failed", "; ".join(e.messages)
    except Exception as e:  # never break the operational flow
        logger.exception("Indian posting failed for %s #%s", event, source_id)
        rec.status, rec.message = "failed", f"Unexpected error: {e}"
    rec.save()
    return rec
